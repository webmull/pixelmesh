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
    sample_radius   = 6,     # px: radius around grid point to sample (12×12=144 px patch).
                             # Smaller patches make a distant phone (few px wide) a larger
                             # fraction: a 3-px phone = 12.5% of a 144-px patch.
                             # Total pixel work (25920 pts × 144) ≈ 3.7M — less than the
                             # old step=15/r=12 setup (7424 × 576 ≈ 4.3M).
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

    def add_sample(self, brightness, ts, history_seconds):
        self.history.append((ts, brightness))
        if len(self.history) % 60 == 0:
            cutoff = ts - history_seconds
            self.history = [(t, b) for t, b in self.history if t >= cutoff]

    def try_decode(self, ts, cfg):
        if ts - self.last_decode_attempt < cfg["decode_interval"]:
            return
        self.last_decode_attempt = ts

        vals = [b for _, b in self.history]
        n = cfg["recent_n"]

        if len(vals) < n:
            self.decode_fail_reason = f"hist={len(vals)}<{n}"
            return

        # Gate 1: must be actively blinking NOW
        if self.recent_std < cfg["min_recent_std"]:
            self.decode_fail_reason = f"std={self.recent_std:.3f}<{cfg['min_recent_std']}"
            # Phone has moved away — drop stale decoded ID so position doesn't linger
            if self.decoded_id is not None:
                self.decoded_id  = None
                self.confidence  = 0.0
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

        # Only maintain decode history for points showing non-trivial variance.
        # _std_buf handles all points for recent_std; history is only for try_decode.
        # IMPORTANT: history_gate must be much lower than min_recent_std so that guard
        # phase samples are recorded even when the phone's recent_std temporarily dips
        # (the dark guard pulls std down ~5× vs Manchester phase for distant phones).
        # Using 0.003 — below any realistic phone signal but above sensor noise floor.
        if computed_stds is not None:
            for pt, b, s in zip(self._points, brightnesses, computed_stds):
                if s >= 0.003:
                    pt.add_sample(float(b), ts, cfg["history_seconds"])
        else:
            for pt, b in zip(self._points, brightnesses):
                pt.add_sample(float(b), ts, cfg["history_seconds"])

        # 4. Adapt gate to current noise floor.
        #    Use the already-computed stds array to avoid rebuilding a Python list.
        if computed_stds is not None:
            active_mask = computed_stds > 0
            if active_mask.sum() >= 20:
                stds = computed_stds[active_mask]
            else:
                stds = []
        else:
            stds = []
        if len(stds) >= 20:
            # p75 is robust — phones would need to cover 25%+ of the frame to bias it.
            # Slow EMA (α=0.02, τ≈50 frames) prevents transient phone activity spiking the gate.
            p75 = float(np.percentile(stds, 75))
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * p75
            cfg["min_recent_std"] = max(min(self._noise_floor * 3.5, 0.15), 0.015)

        # 5. Attempt decode on high-variance points only.
        #    numpy where() finds active indices in one vectorised pass;
        #    try_decode is called only for the few points above the gate
        #    (typically 0-50) rather than all 25K.
        #    For already-decoded points that drop below gate (e.g. during the dark
        #    guard phase), try_decode is also called so its internal rate-limited
        #    gate can clear the ID at the right time — NOT via an immediate clear
        #    which would wipe IDs on every single frame of the guard.
        gate = cfg["min_recent_std"]
        if computed_stds is not None:
            active_idx = np.where(computed_stds >= gate)[0]
            for i in active_idx:
                self._points[i].try_decode(ts, cfg)
            for pt in self._points:
                if pt.decoded_id is not None and pt.recent_std < gate:
                    pt.try_decode(ts, cfg)   # lets internal gate handle cleanup
        else:
            for pt in self._points:
                pt.try_decode(ts, cfg)

        # 5. Collect best DetectedDevice per blink_id
        id_map: dict[int, DetectedDevice] = {}
        for pt in self._points:
            if pt.decoded_id is None:
                continue
            existing = id_map.get(pt.decoded_id)
            if existing is None or pt.confidence > existing.confidence:
                id_map[pt.decoded_id] = DetectedDevice(
                    blink_id=pt.decoded_id,
                    cx_px=float(pt.px),
                    cy_px=float(pt.py),
                    confidence=pt.confidence,
                )

        self.last_results = list(id_map.values())

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
        # Vectorised: compute all v values at once, then only call cv2.circle
        # for active points (v >= 10).  Avoids iterating 25K points per frame.
        vs = np.clip(self._last_stds * 3.0, 0.0, 1.0) * 255
        active = np.where(vs >= 10)[0]
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

        # Track which decoded IDs have already been drawn (one label per ID)
        drawn_ids: set[int] = set()

        for pt in self._points:
            px, py = to_canvas(pt.px, pt.py)

            if pt.decoded_id is not None:
                # Draw only the first (highest-std) instance of each ID
                if pt.decoded_id in drawn_ids:
                    continue
                drawn_ids.add(pt.decoded_id)

                # Green dot + bold ID label
                cv2.circle(frame, (px, py), 7, (0, 220, 80), -1)
                cv2.putText(frame, str(pt.decoded_id),
                            (px + 12, py + 7), font, 0.9, (0, 0, 0), 5, cv2.LINE_AA)
                cv2.putText(frame, str(pt.decoded_id),
                            (px + 12, py + 7), font, 0.9, (80, 255, 80), 2, cv2.LINE_AA)

        # Actively blinking but not yet decoded — show a scrolling binary stream.
        # Filter: must swing from near-zero (dark phase) to bright (white phase).
        # Deduplicate by proximity so one phone = one stream, not one per grid pt.
        STREAM_DISPLAY_N = 12
        # Check window must span past the guard (NUM_GUARD phases × ~3 frames/phase at 11fps ≈ 13 frames)
        # so the max-min check includes pre-guard Manchester frames and doesn't drop to zero.
        STREAM_CHECK_N = 22
        CLUSTER_R     = 120   # px — grid points within this distance = same phone
        candidates = sorted(
            (p for p in self._points
             if (p.recent_std >= min_std
                 and p.history
                 and (max(b for _, b in p.history[-STREAM_CHECK_N:])
                      - min(b for _, b in p.history[-STREAM_CHECK_N:])) > max(min_std * 1.2, 0.08))),
            key=lambda p: p.recent_std,
            reverse=True,
        )

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
        self.last_results.clear()
        self._std_buf = None
        self._std_buf_pos = 0
        self._std_buf_count = 0
