"""
PixelMesh V2 — Blink Detector (grid-sampler, variance-gated)

Samples raw brightness at every grid_step pixels.  The key discriminant is
*recent standard deviation* over ~1.5 seconds, not max-min over 15 seconds.

Why:
  - Blinking cell (0↔1 at 5 Hz):  recent_std ≈ 0.35
  - Scrolling chat / slowly changing UI: recent_std ≈ 0.01-0.05
  - Animated favicon (small amplitude): recent_std ≈ 0.05-0.12
  - Static text/background: recent_std ≈ 0.00-0.02

process_frame() returns (detections, DebugImages).
"""

import time
import math
import cv2
import numpy as np
from dataclasses import dataclass, field

from blink_encoder import decode_phases_verbose, CYCLE_LEN, PHASE_MS

from log import log

DEFAULTS = dict(
    grid_step       = 8,     # px: distance between grid sample points.
                             # Denser grid = smaller phones (further away) are always within
                             # radius of some grid point.  At step=8 the farthest any pixel
                             # can be from the nearest grid centre is sqrt(4²+4²) ≈ 5.7 px,
                             # so a phone that is just 3 px wide always lands inside a patch.
                             # At 1080p this covers phones up to ~25-30 m away.
    history_seconds = 30.0,  # seconds of brightness history to keep (≥ 2 full cycles)
    decode_interval = 0.2,   # seconds between decode attempts per point
    min_history     = 150,   # unused — decode gate is now time-based (_MIN_HISTORY_SECS)
    min_recent_std  = 0.10,  # initial gate — overridden adaptively after warmup
    recent_n        = 24,    # samples in the recent window (~1.6s at 15fps)
    brightness_pct  = 3,     # 3rd percentile: a phone covering ~3% of the patch (≥4 px wide
                             # in a 12×12 = 144 px patch) will shift this percentile.
                             # Lower than 5 to handle very small/distant phones.
    sample_radius   = 4,     # px: radius around grid point to sample (8×8=64 px patch).
                             # r=4 keeps the 25920×64 matrix at 1.66 MB — inside L2/L3
                             # cache on M1.  r=6 (3.7 MB) spills to RAM, making partition
                             # 10× slower purely due to cache pressure, not arithmetic.
                             # Coverage is unchanged: the worst-case phone pixel (5.66 px
                             # from its nearest grid centre) falls inside an adjacent
                             # centre's r=4 patch, so nothing is missed.
    roi_top_frac    = 0.20,  # fraction of frame height to skip from top
    roi_left_frac   = 0.00,  # fraction of frame width to skip from left
    log_interval    = 3.0,   # seconds between diagnostic log lines
)


@dataclass
class DebugImages:
    gray:     np.ndarray | None = None   # raw grayscale
    contrast: np.ndarray | None = None   # per-grid-point recent-std heatmap


@dataclass
class DetectedDevice:
    blink_id:   int
    cx_px:      float
    cy_px:      float
    confidence: float


@dataclass
class _GridPoint:
    px: int
    py: int
    history:             list  = field(default_factory=list)  # [(ts, brightness)]
    decoded_id:          int | None = None
    confidence:          float = 0.0
    last_decode_attempt: float = 0.0
    decode_fail_reason:  str  = ""
    recent_std:          float = 0.0  # updated each decode cycle
    last_active_ts:      float = 0.0  # last time this point was above the detection gate

    def add_sample(self, brightness, ts, history_seconds):
        self.history.append((ts, brightness))
        if len(self.history) % 60 == 0:
            cutoff = ts - history_seconds
            self.history = [(t, b) for t, b in self.history if t >= cutoff]

    def try_decode(self, ts, cfg):
        # Never re-decode a phone that's already been found — budget is reserved
        # for undiscovered phones.  IDs are only cleared on detector.reset().
        if self.decoded_id is not None:
            return
        if ts - self.last_decode_attempt < cfg["decode_interval"]:
            return
        self.last_decode_attempt = ts

        vals = [b for _, b in self.history]
        n = cfg["recent_n"]

        if len(vals) < n:
            self.decode_fail_reason = f"hist={len(vals)}<{n}"
            return

        # Gate 1b: history must span at least one full decode cycle.
        # At 60fps, 159 samples = 2.65s — far short of the 13.2s cycle duration.
        # The decoder finds a guard run but has no samples for the Manchester
        # bit windows that follow, producing empty_win / phase_ambig failures.
        # This check was present in an earlier version and removed inadvertently.
        _MIN_HIST_SECS = CYCLE_LEN * PHASE_MS / 1000   # 13.2s for default config
        if len(self.history) >= 2:
            span = self.history[-1][0] - self.history[0][0]
            if span < _MIN_HIST_SECS:
                self.decode_fail_reason = f"warmup {span:.1f}s/{_MIN_HIST_SECS:.1f}s"
                return

        # Gate 2: must be actively blinking NOW
        if self.recent_std < cfg["min_recent_std"]:
            self.decode_fail_reason = f"std={self.recent_std:.3f}<{cfg['min_recent_std']}"
            # Once decoded, keep the ID regardless — phone may have left or entered
            # a guard phase.  IDs are only cleared on detector.reset().
            return


        result, reason = decode_phases_verbose(self.history)
        if result is not None:
            self.decoded_id, self.confidence = result
            self.decode_fail_reason = ""
        else:
            lo, hi = min(vals), max(vals)
            self.decode_fail_reason = (
                f"hist={len(vals)} std={self.recent_std:.2f} "
                f"range={hi-lo:.2f} → {reason}"
            )


class BlinkDetector:
    def __init__(self):
        self._points:      list[_GridPoint] = []
        self.last_results: list[DetectedDevice] = []
        self.cfg           = dict(DEFAULTS)
        self._grid_shape   = (0, 0, 0, 0)
        self._last_log_ts  = 0.0
        self._noise_floor  = DEFAULTS["min_recent_std"]  # adaptive EMA estimate
        # Circular buffer for vectorised recent_std — shape (N_points, recent_n).
        # One np.std call on the full matrix is ~100× faster than N Python calls.
        self._std_buf:       np.ndarray | None = None
        self._std_buf_pos:   int = 0
        self._std_buf_count: int = 0
        # Precomputed flat indices into padded grayscale for each grid patch.
        # Built once per grid/radius combo; avoids 25K Python slice ops per frame.
        self._px_arr:        np.ndarray | None = None   # (N,) int32
        self._py_arr:        np.ndarray | None = None   # (N,) int32
        self._patch_idx:     np.ndarray | None = None   # (N, flat_size) int32
        self._patch_idx_r:   int = -1                   # radius used to build _patch_idx
        self._last_stds:     np.ndarray | None = None   # (N,) float32, latest computed_stds
        self._decoded_pts:   list = []                  # points with decoded_id != None (tiny list)

    # ---------------------------------------------------------------- #

    def _rebuild_grid(self, h, w):
        cfg     = self.cfg
        roi_top  = int(h * cfg["roi_top_frac"])
        roi_left = int(w * cfg["roi_left_frac"])
        step    = cfg["grid_step"]
        xs = list(range(roi_left + step // 2, w, step))
        ys = list(range(roi_top  + step // 2, h, step))
        shape = (roi_top, roi_left, len(ys), len(xs))
        if shape == self._grid_shape and self._points:
            return
        self._grid_shape = shape
        old_map = {(p.px, p.py): p for p in self._points}
        self._points = []
        for py in ys:
            for px in xs:
                self._points.append(old_map.get((px, py)) or _GridPoint(px=px, py=py))
        # Grid changed — reset all cached per-point arrays
        self._px_arr = np.array([pt.px for pt in self._points], dtype=np.int32)
        self._py_arr = np.array([pt.py for pt in self._points], dtype=np.int32)
        self._patch_idx = None   # force recompute (radius may differ)
        self._std_buf = None
        self._std_buf_pos = 0
        self._std_buf_count = 0

    # ---------------------------------------------------------------- #

    def process_frame(
        self,
        frame: np.ndarray,
        ts: float | None = None,
    ) -> tuple[list[DetectedDevice], DebugImages]:

        if ts is None:
            ts = time.time()

        cfg  = self.cfg
        h, w = frame.shape[:2]

        # 1. Raw grayscale — no CLAHE (it would compress the white/black contrast
        #    we rely on, making the blink amplitude appear smaller)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # 2. Rebuild grid if frame size or ROI changed
        self._rebuild_grid(h, w)

        r   = cfg["sample_radius"]
        pct = cfg["brightness_pct"]
        n   = cfg["recent_n"]
        N   = len(self._points)

        # 3. Sample brightness — fully vectorised via precomputed flat indices.
        #    _patch_idx (N, flat_size) is built once per grid/radius combo and
        #    reused every frame, replacing 25K Python slice ops with one gather.
        side      = 2 * r
        flat_size = side * side
        gray_pad  = np.pad(gray, r, mode="edge")

        if self._patch_idx is None or self._patch_idx_r != r:
            row_off = np.arange(side, dtype=np.int32)
            col_off = np.arange(side, dtype=np.int32)
            rows = self._py_arr[:, None, None] + row_off[None, :, None]
            cols = self._px_arr[:, None, None] + col_off[None, None, :]
            self._patch_idx   = (rows * gray_pad.shape[1] + cols).reshape(N, flat_size)
            self._patch_idx_r = r

        k            = max(0, min(int(flat_size * pct / 100), flat_size - 1))
        brightnesses = np.partition(
            gray_pad.ravel()[self._patch_idx], k, axis=1
        )[:, k] / 255.0  # (N,)

        # Update std circular buffer and compute recent_std for all points in one call.
        # Do this before add_sample so computed_stds can gate history tracking below.
        if self._std_buf is None or self._std_buf.shape[0] != N:
            self._std_buf = np.zeros((N, n), dtype=np.float32)
            self._std_buf_pos = 0
            self._std_buf_count = 0
        self._std_buf[:, self._std_buf_pos % n] = brightnesses
        self._std_buf_pos += 1
        if self._std_buf_count < n:
            self._std_buf_count += 1

        if self._std_buf_count >= n:
            computed_stds = np.std(self._std_buf, axis=1)   # one call, all points
            self._last_stds = computed_stds                  # cached for _std_heatmap
            for pt, s in zip(self._points, computed_stds):
                pt.recent_std = float(s)
        else:
            computed_stds = None

        # Only maintain decode history for points at or near an active phone.
        # Camera sensor noise typically produces std=0.003-0.009 across the whole frame,
        # causing all 25,920 grid points to record history with the old 0.003 gate.
        # At 25,920 active points with ~180 history entries each, the periodic trim
        # (every 60 appends per point) processes 4.67M Python iterations per frame,
        # degrading detection from 8fps to 2fps after ~20s.
        #
        # Fix: record history only for points currently above the detection gate (active
        # phone signal) OR points that were recently above it within history_seconds
        # (to capture the dark guard phase, which pulls std down ~5× for distant phones).
        # Background noise points never exceed the gate, so they never start recording.
        if computed_stds is not None:
            gate = cfg["min_recent_std"]
            hist_secs = cfg["history_seconds"]
            for pt, b, s in zip(self._points, brightnesses, computed_stds):
                if s >= gate:
                    pt.last_active_ts = ts
                    pt.add_sample(float(b), ts, hist_secs)
                elif pt.last_active_ts > 0 and (ts - pt.last_active_ts) < hist_secs:
                    pt.add_sample(float(b), ts, hist_secs)
        else:
            for pt, b in zip(self._points, brightnesses):
                pt.add_sample(float(b), ts, cfg["history_seconds"])

        # 4. Adapt gate to current noise floor.
        #    Use the already-computed stds array to avoid rebuilding a Python list.
        #    p90 (not p75) avoids the gate collapsing to its floor: with p75, the EMA
        #    converges to near-zero after ~60 frames because 99%+ of grid points are
        #    static background with std≈0.003-0.01.  p90 gives a 3-5× higher estimate
        #    for the same noise distribution, keeping the gate above 0.05.
        #    Floor raised from 0.015 → 0.05: at 0.015 a single spurious active frame
        #    floods above_gate from ~20 to ~800, which stalls detection for the rest
        #    of the session.  All real phone signals observed have std ≥ 0.08, so 0.05
        #    provides comfortable headroom.
        if computed_stds is not None:
            active_mask = computed_stds > 0
            if active_mask.sum() >= 20:
                stds = computed_stds[active_mask]
            else:
                stds = []
        else:
            stds = []
        if len(stds) >= 20:
            # p90 is robust — phones would need to cover 10%+ of the frame to bias it.
            # Slow EMA (α=0.05, τ≈20 frames) prevents transient phone activity spiking the gate.
            p90 = float(np.percentile(stds, 90))
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * p90
            cfg["min_recent_std"] = max(min(self._noise_floor * 3.5, 0.15), 0.05)

        # 5. Attempt decode on high-variance points only.
        #    Time-budget approach: decode highest-std points first, stop when the
        #    per-frame decode allocation is exhausted.  A fixed count cap assumes
        #    all calls cost the same; a time budget adapts — fast decodes (clean
        #    signal, early-exit at conf≥0.95) consume little budget and allow more
        #    phones to be processed in the same frame.  50ms keeps the detection
        #    thread above 15fps even if a handful of calls are expensive.
        #    Points whose interval hasn't elapsed fall through the loop cheaply.
        DECODE_BUDGET_SECS = 0.050
        gate = cfg["min_recent_std"]
        if computed_stds is not None:
            active_idx = np.where(computed_stds >= gate)[0]
            # Sort highest-std first so real phones (strong blink) get priority
            if len(active_idx) > 1:
                active_idx = active_idx[np.argsort(-computed_stds[active_idx])]
            deadline = time.time() + DECODE_BUDGET_SECS
            for i in active_idx:
                pt = self._points[i]
                will_decode = (ts - pt.last_decode_attempt) >= cfg["decode_interval"]
                if will_decode and time.time() >= deadline:
                    continue
                pt.try_decode(ts, cfg)
        else:
            for pt in self._points:
                pt.try_decode(ts, cfg)

        # 5. Collect best DetectedDevice per blink_id
        #    Also rebuild _decoded_pts here — same pass, no extra O(N) scan.
        id_map: dict[int, DetectedDevice] = {}
        decoded_pts_new: list = []
        for pt in self._points:
            if pt.decoded_id is None:
                continue
            decoded_pts_new.append(pt)
            existing = id_map.get(pt.decoded_id)
            if existing is None or pt.confidence > existing.confidence:
                id_map[pt.decoded_id] = DetectedDevice(
                    blink_id=pt.decoded_id,
                    cx_px=float(pt.px),
                    cy_px=float(pt.py),
                    confidence=pt.confidence,
                )

        self.last_results = list(id_map.values())
        self._decoded_pts = decoded_pts_new

        # 6. Diagnostic logging
        if ts - self._last_log_ts >= cfg["log_interval"]:
            self._last_log_ts = ts
            self._log_diagnostics()

        # 7. Build recent-std heatmap
        dbg_img = self._std_heatmap(h, w)
        return self.last_results, DebugImages(gray=gray, contrast=dbg_img)

    # ---------------------------------------------------------------- #

    def _log_diagnostics(self):
        cfg = self.cfg
        active = [p for p in self._points if len(p.history) >= cfg["min_history"]]
        if not active:
            log.info("[blink] no points with enough history yet")
            return

        # Sort by recent_std descending
        by_std = sorted(active, key=lambda p: p.recent_std, reverse=True)
        max_std = by_std[0].recent_std
        n_above = sum(1 for p in active if p.recent_std >= cfg["min_recent_std"])
        decoded = [p for p in active if p.decoded_id is not None]

        # Warn if the best candidate has low signal range — likely auto-exposure
        # compensating for the blink and compressing amplitude (range < 0.5 means
        # AE is fighting the signal; seen as range=0.28 vs expected 0.99).
        top = by_std[0]
        if top.history and len(top.history) >= 2:
            raw_vals = [b for _, b in top.history]
            top_range = max(raw_vals) - min(raw_vals)
            if top_range < 0.5 and n_above > 0:
                log.info(f"[blink] WARNING: low signal range={top_range:.2f} — "
                         f"auto-exposure may be compensating for blink. "
                         f"Use fixed exposure/ISO in camera app.")

        log.info(
            f"[blink] pts={len(active)} above_gate={n_above} "
            f"max_std={max_std:.3f} gate={cfg['min_recent_std']:.3f} decoded={len(decoded)}"
        )
        for p in by_std[:5]:
            status = (
                f"ID={p.decoded_id} conf={p.confidence:.2f}"
                if p.decoded_id is not None
                else p.decode_fail_reason or "pending"
            )
            log.info(
                f"  ({p.px:4d},{p.py:4d}) std={p.recent_std:.3f} "
                f"hist={len(p.history)} → {status}"
            )

    def _std_heatmap(self, h, w):
        img = np.zeros((h, w), dtype=np.uint8)
        if self._last_stds is None:
            return img
        # Draw circles only for points meaningfully above the detection gate.
        # Using a hard threshold (vs >= 10, i.e. std >= 0.013) caused thousands
        # of cv2.circle calls per frame once the gate drifted to its 0.015 floor,
        # stalling the detection thread.  Tying the threshold to the current gate
        # keeps the circle count proportional to the number of real candidates.
        gate = self.cfg["min_recent_std"]
        vs = np.clip(self._last_stds * 3.0, 0.0, 1.0) * 255
        active = np.where(self._last_stds >= gate * 0.5)[0]
        r = max(1, self.cfg["grid_step"] // 2 - 2)
        for i in active:
            pt = self._points[i]
            cv2.circle(img, (pt.px, pt.py), r, int(vs[i]), -1)
        return img

    # ---------------------------------------------------------------- #

    def draw_overlay(
        self,
        frame: np.ndarray,
        scale: float = 1.0,
        crop_x: int = 0,
        crop_y: int = 0,
    ) -> np.ndarray:
        font      = cv2.FONT_HERSHEY_SIMPLEX
        min_std   = self.cfg["min_recent_std"]

        def to_canvas(raw_x: int, raw_y: int) -> tuple[int, int]:
            return (int(raw_x * scale) - crop_x, int(raw_y * scale) - crop_y)

        # Decoded labels — iterate only the tiny _decoded_pts list (0-5 points),
        # not all 25K grid points.
        def _draw_id_box(img, cx, cy, label, border_color):
            """Small dark box with centred ID number — shared style for detection
            and device-overlay tags so they look identical."""
            (tw, th), _ = cv2.getTextSize(label, font, 0.55, 1)
            pad = 5
            x1, y1 = cx - tw // 2 - pad, cy - th // 2 - pad - 1
            x2, y2 = cx + tw // 2 + pad, cy + th // 2 + pad + 1
            cv2.rectangle(img, (x1, y1), (x2, y2), (20, 20, 20), -1)
            cv2.rectangle(img, (x1, y1), (x2, y2), border_color, 2)
            cv2.putText(img, label, (cx - tw // 2, cy + th // 2),
                        font, 0.55, (255, 255, 255), 1, cv2.LINE_AA)

        drawn_ids: set[int] = set()
        for pt in self._decoded_pts:
            if pt.decoded_id is None:   # may have been cleared since last frame
                continue
            if pt.decoded_id in drawn_ids:
                continue
            drawn_ids.add(pt.decoded_id)
            px, py = to_canvas(pt.px, pt.py)
            _draw_id_box(frame, px, py, str(pt.decoded_id), (0, 220, 80))

        # Actively blinking but not yet decoded — show a scrolling binary stream.
        # Filter: must swing from near-zero (dark phase) to bright (white phase).
        # Deduplicate by proximity so one phone = one stream, not one per grid pt.
        STREAM_DISPLAY_N = 12
        # Check window must span past the guard (NUM_GUARD phases × ~3 frames/phase at 11fps ≈ 13 frames)
        # so the max-min check includes pre-guard Manchester frames and doesn't drop to zero.
        STREAM_CHECK_N = 22
        CLUSTER_R     = 120   # px — grid points within this distance = same phone

        # numpy-gate: find above-threshold indices in one vectorised pass,
        # then range-check only those ~0-50 points instead of all 25K.
        if self._last_stds is not None:
            cand_idx = np.where(self._last_stds >= min_std)[0]
            candidates = sorted(
                (self._points[i] for i in cand_idx
                 if (self._points[i].history
                     and (max(b for _, b in self._points[i].history[-STREAM_CHECK_N:])
                          - min(b for _, b in self._points[i].history[-STREAM_CHECK_N:]))
                     > max(min_std * 1.2, 0.08))),
                key=lambda p: p.recent_std,
                reverse=True,
            )
        else:
            candidates = []

        seen_canvas: list[tuple[int, int]] = []
        for pt in candidates:
            cx, cy = to_canvas(pt.px, pt.py)
            if any(abs(cx - ex) < CLUSTER_R and abs(cy - ey) < CLUSTER_R
                   for ex, ey in seen_canvas):
                continue
            seen_canvas.append((cx, cy))
            # Skip stream text if a decoded label is already drawn here
            if pt.decoded_id is not None and pt.decoded_id in drawn_ids:
                continue

            vals = [b for _, b in pt.history[-STREAM_DISPLAY_N:]]
            lo, hi = min(vals), max(vals)
            rng  = hi - lo if hi - lo > 0.01 else 1.0
            bits = "".join("1" if (b - lo) / rng >= 0.5 else "0" for b in vals)
            cv2.putText(frame, bits, (cx + 12, cy + 4),
                        font, 0.28, (0, 0, 0), 2, cv2.LINE_AA)
            cv2.putText(frame, bits, (cx + 12, cy + 4),
                        font, 0.28, (255, 255, 255), 1, cv2.LINE_AA)

        return frame

    def get_blobs(self):
        return self._points

    def reset(self):
        for pt in self._points:
            pt.history.clear()
            pt.decoded_id = None
            pt.confidence = 0.0
            pt.last_decode_attempt = 0.0
            pt.decode_fail_reason = ""
            pt.recent_std = 0.0
            pt.last_active_ts = 0.0
        self.last_results.clear()
        self._std_buf = None
        self._std_buf_pos = 0
        self._std_buf_count = 0
        # Reset gate so each detection session starts from the default.
        # Without this, the noise_floor EMA inherited from a previous session
        # can immediately set gate=0.05 (the floor), flooding above_gate with
        # hundreds of background points before any real phone is detected.
        self._noise_floor = DEFAULTS["min_recent_std"]
        self.cfg["min_recent_std"] = DEFAULTS["min_recent_std"]

