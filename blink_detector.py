# (c) Adam Davis - adamdavis.co.uk
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
    roi_top_frac    = 0.00,  # fraction of frame height to skip from top (0 = full frame)
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
    decode_failures:     int   = 0    # consecutive failed decode attempts

    def add_sample(self, brightness, ts, history_seconds):
        self.history.append((ts, brightness))
        if len(self.history) % 60 == 0:
            cutoff = ts - history_seconds
            self.history = [(t, b) for t, b in self.history if t >= cutoff]

    def try_decode(self, ts, cfg, skip_std_gate: bool = False):
        # Never re-decode a phone that's already been found — budget is reserved
        # for undiscovered phones.  IDs are only cleared on detector.reset().
        if self.decoded_id is not None:
            return
        # Exponential backoff after consecutive failures: 0.2s → 0.4 → 0.8 → … → 5s cap.
        # Stops non-phone objects (LEDs, reflections) eating decode budget indefinitely.
        backoff = min(cfg["decode_interval"] * (2 ** self.decode_failures), 5.0)
        if ts - self.last_decode_attempt < backoff:
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

        # Gate 2: must be actively blinking NOW.
        # skip_std_gate is set for guard-phase candidates: points that were recently
        # above gate but have gone quiet during the dark guard phases.  Their history
        # is complete and valid — only their current std is temporarily suppressed.
        if not skip_std_gate and self.recent_std < cfg["min_recent_std"]:
            self.decode_fail_reason = f"std={self.recent_std:.3f}<{cfg['min_recent_std']}"
            return


        result, reason = decode_phases_verbose(self.history)
        if result is not None:
            self.decoded_id, self.confidence = result
            self.decode_fail_reason = ""
            self.decode_failures = 0
        else:
            lo, hi = min(vals), max(vals)
            self.decode_fail_reason = (
                f"hist={len(vals)} std={self.recent_std:.2f} "
                f"range={hi-lo:.2f} → {reason}"
            )
            self.decode_failures += 1


class BlinkDetector:
    def __init__(self):
        self._points:      list[_GridPoint] = []
        self.last_results: list[DetectedDevice] = []
        self.cfg           = dict(DEFAULTS)
        self._grid_shape   = (0, 0, 0, 0)
        self._last_log_ts  = 0.0
        self._noise_floor  = DEFAULTS["min_recent_std"]  # adaptive EMA estimate
        self.signal_range: float = 1.0   # best observed range across active points (0–1)
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
        self._ever_active:   set[int] = set()           # indices of points that have ever gone above gate
        self._last_evict_ts: float = 0.0               # wall-clock time of last eviction run
        self._locked_positions: dict[int, tuple[float, float]] = {}  # blink_id → (cx, cy) frozen at first decode
        # Pre-allocated padded grayscale buffer — reused every frame to avoid
        # the ~2MB allocation that np.pad issues on each call.
        self._gray_pad:         np.ndarray | None = None
        self._gray_pad_r:       int = -1   # radius used to size _gray_pad
        # Frame-diff state for diff-based phone finder
        self._diff_prev_gray:   np.ndarray | None = None
        self._diff_accum:       np.ndarray | None = None
        self._diff_accum_count: int = 0
        self._diff_discovered:  set[int] = set()           # grid indices found by diff → centroid sampling
        self._diff_centroids:   dict[int, tuple[int,int]] = {}  # idx → actual blob centroid (cx, cy)

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
        self._gray_pad  = None   # reallocate on next frame (frame size changed)
        self._std_buf = None
        self._std_buf_pos = 0
        self._std_buf_count = 0
        self._ever_active.clear()  # indices are position-dependent; invalidate on grid change
        self._diff_prev_gray   = None  # force diff reset on grid change
        self._diff_accum       = None
        self._diff_accum_count = 0
        self._diff_discovered.clear()
        self._diff_centroids.clear()

    # ---------------------------------------------------------------- #

    def process_frame(
        self,
        frame: np.ndarray,
        ts: float | None = None,
        need_debug: bool = False,
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
        # Pre-allocated padded buffer — allocated once per frame size / radius combo.
        # Fills interior + edges in-place; avoids the ~2MB allocation np.pad issues
        # every frame (measured saving: 0.13 ms/frame on M1 detection thread).
        if self._gray_pad is None or self._gray_pad_r != r or self._gray_pad.shape != (h + 2*r, w + 2*r):
            self._gray_pad   = np.empty((h + 2*r, w + 2*r), dtype=np.uint8)
            self._gray_pad_r = r
        gray_pad = self._gray_pad
        gray_pad[r:r+h, r:r+w] = gray          # interior
        gray_pad[:r,    r:r+w] = gray[0:1, :]  # top edge
        gray_pad[r+h:,  r:r+w] = gray[-1:, :]  # bottom edge
        gray_pad[:,  :r]        = gray_pad[:, r:r+1]   # left edge
        gray_pad[:, r+w:]       = gray_pad[:, r+w-1:r+w]  # right edge

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

        # Centroid override for diff-discovered points.
        # Patch-percentile sampling misses 1-pixel distant phones (they occupy <3%
        # of the 8×8 patch, below the k=1 threshold).  For points found by the diff
        # finder, replace the patch value with the single pixel at the grid centre —
        # the exact phone pixel — giving full blink amplitude regardless of phone size.
        if self._diff_discovered:
            for i in self._diff_discovered:
                cx_c, cy_c = self._diff_centroids.get(i, (self._points[i].px, self._points[i].py))
                if 0 <= cy_c < h and 0 <= cx_c < w:
                    brightnesses[i] = float(gray[cy_c, cx_c]) / 255.0

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
            # Update recent_std only for points we actually read it from — _ever_active
            # covers all decoded phones + guard-phase candidates.  Skips the 25K-entry
            # Python loop that otherwise runs every frame (saves ~3-5 ms/frame).
            # New above-gate points get updated in the active_idx loop below (they
            # aren't in _ever_active yet this frame).
            for i in self._ever_active:
                self._points[i].recent_std = float(computed_stds[i])
        else:
            computed_stds = None

        # ---- Diff-based phone finder (inspired by Seb Lee-Delisle's PixelPhones) ----
        # Frame-to-frame absDiff catches dim/distant phones whose per-point variance
        # (recent_std) is stuck just below the adaptive gate.  A blink transition
        # produces a clear instantaneous diff even when the 24-frame rolling std is low.
        # We accumulate N consecutive frame diffs, find blinking pixel clusters, then
        # inject the nearest grid point into _ever_active so history starts recording
        # immediately — without waiting for the variance gate to be crossed.
        # The existing decode pipeline handles decoding; this only improves discovery.
        _DIFF_ACCUM_N      = 5   # frames to accumulate before processing (~0.33s @ 15fps)
        _DIFF_THRESH       = 10  # minimum total pixel change across N frames (0–255×N)
        _DIFF_MAX_AREA     = 1500  # ignore blobs larger than this px² (people, large motion)
        _DIFF_MAX_INJECT   = 50  # max new ever_active injections per cycle — prevents diff
                                 # flooding the set with scene motion (outdoor, moving people)

        # Initialise or reinitialise persistent buffers when frame shape changes.
        # Pre-allocation avoids a ~0.5 MB numpy allocation every frame.
        if self._diff_prev_gray is None or self._diff_prev_gray.shape != gray.shape:
            self._diff_prev_gray   = np.empty_like(gray)
            self._diff_accum       = np.zeros(gray.shape, dtype=np.uint16)
            self._diff_accum_count = 0
            np.copyto(self._diff_prev_gray, gray)   # seed — skip diff this frame
        else:
            frame_diff = cv2.absdiff(gray, self._diff_prev_gray)  # uint8, fast C++
            np.copyto(self._diff_prev_gray, gray)                  # update — no allocation
            self._diff_accum += frame_diff                         # uint16 += uint8 (safe)
            self._diff_accum_count += 1

            if self._diff_accum_count >= _DIFF_ACCUM_N:
                blink_mask = (self._diff_accum >= _DIFF_THRESH).astype(np.uint8) * 255
                n_lbl, _, stats, centroids = cv2.connectedComponentsWithStats(
                    blink_mask, connectivity=8)
                if n_lbl > 1 and self._grid_shape[3] > 0:
                    roi_top, roi_left, n_ys, n_xs = self._grid_shape
                    step = cfg["grid_step"]
                    injected = 0
                    # Sort smallest-area-first: phone blobs (1–5px²) get
                    # injected before larger scene-motion blobs hit the cap.
                    blob_labels = sorted(range(1, n_lbl),
                                         key=lambda l: stats[l, cv2.CC_STAT_AREA])
                    for lbl in blob_labels:
                        if injected >= _DIFF_MAX_INJECT:
                            break
                        if stats[lbl, cv2.CC_STAT_AREA] > _DIFF_MAX_AREA:
                            continue
                        cx_b = int(round(float(centroids[lbl, 0])))
                        cy_b = int(round(float(centroids[lbl, 1])))
                        ix = max(0, min(n_xs - 1,
                                       round((cx_b - roi_left - step // 2) / step)))
                        iy = max(0, min(n_ys - 1,
                                       round((cy_b - roi_top  - step // 2) / step)))
                        idx = iy * n_xs + ix
                        if idx < len(self._points):
                            # Always register as diff_discovered so centroid-sampling
                            # overrides the 8×8 patch percentile.  Only set the centroid
                            # on first discovery — later blobs mapped to the same grid
                            # point may be scene-motion, not the phone.
                            self._diff_discovered.add(idx)
                            if idx not in self._diff_centroids:
                                self._diff_centroids[idx] = (cx_b, cy_b)
                            if idx not in self._ever_active:
                                self._ever_active.add(idx)
                                self._points[idx].last_active_ts = ts
                                injected += 1
                self._diff_accum.fill(0)     # reset in-place — no reallocation
                self._diff_accum_count = 0
        # ---- end diff finder ----

        # Evict stale non-decoded entries from _ever_active (and related caches).
        # A sudden phone brightness change (auto-exposure, screen auto-brightness)
        # spikes recent_std across many grid points simultaneously, flooding
        # _ever_active with hundreds of new entries in a single frame.  Without
        # eviction these points record history on every subsequent frame via the
        # loop below — O(|ever_active|) Python appends per frame — and FPS never
        # recovers because _ever_active only grows.
        #
        # Safe to evict: points that (a) have not been decoded, and (b) have been
        # below gate for longer than one full decode cycle (13.2 s).  A phone that
        # was genuinely blinking will come back above gate within the next cycle and
        # re-enter _ever_active naturally.  Decoded points are never evicted.
        #
        # Eviction runs on a wall-clock timer (not frame count) so that FPS
        # degradation after a brightness spike doesn't push the eviction window
        # out to 20+ seconds and prevent recovery.
        _EVICT_INTERVAL = 3.0   # seconds between eviction sweeps (vs. 6 s before)
        if (ts - self._last_evict_ts) >= _EVICT_INTERVAL and len(self._ever_active) > 0:
            self._last_evict_ts = ts
            _stale_full   = CYCLE_LEN * PHASE_MS / 1000   # 13.2 s — real phone in guard phase
            _stale_noise  = 5.0                            # repeated failures → noise, evict fast
            stale = {
                i for i in self._ever_active
                if self._points[i].decoded_id is None
                and self._points[i].last_active_ts > 0
                and (ts - self._points[i].last_active_ts) > (
                    _stale_noise if self._points[i].decode_failures > 3 else _stale_full
                )
            }
            if stale:
                self._ever_active   -= stale
                self._diff_discovered -= stale
                for i in stale:
                    self._diff_centroids.pop(i, None)
                    self._points[i].history.clear()
                    self._points[i].decode_failures = 0
                log.debug(f"[blink] evicted {len(stale)} stale pts from _ever_active "
                          f"(remaining={len(self._ever_active)})")

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
        # Record brightness history only for points near an active phone.
        # Use np.where to get the ~50 above-gate indices first, then iterate only
        # those — avoids 25K Python loop iterations per frame (measured: 18ms/frame).
        # Guard-phase preservation: phones go quiet during the 4 dark guard phases
        # (~1.2s); _ever_active tracks all indices that have ever been above gate so
        # their samples are still recorded while std is temporarily below gate.
        if computed_stds is not None:
            gate      = cfg["min_recent_std"]
            hist_secs = cfg["history_seconds"]
            active_idx = np.where(computed_stds >= gate)[0]
            # Sort highest-std first so real phones (std 0.3–0.5) always enter
            # _ever_active before noise at the gate floor (std 0.05–0.08).
            if len(active_idx) > 1:
                active_idx = active_idx[np.argsort(-computed_stds[active_idx])]
            # Cap new _ever_active entries per second (not per frame) so the limit
            # is independent of detection fps.  Points already in the set are
            # unaffected.  Real phones have 6–10× higher std than gate-floor noise
            # and are sorted first, so they always claim their slots before noise.
            # At 300 phones × ~1-2 pts each = ~600 real entries needed; at 30/sec
            # those populate within ~20s — fine given the 13.2s warmup window.
            # Noise (std 0.05-0.08) only enters after real phones are admitted and
            # is fast-evicted (5s) once it accumulates decode failures.
            _MAX_NEW_PER_SEC = 30
            _new_window_secs = 1.0
            if not hasattr(self, '_new_ever_active_count'):
                self._new_ever_active_count = 0
                self._new_ever_active_window_ts = ts
            if (ts - self._new_ever_active_window_ts) >= _new_window_secs:
                self._new_ever_active_count    = 0
                self._new_ever_active_window_ts = ts
            for i in active_idx:
                pt = self._points[i]
                pt.recent_std = float(computed_stds[i])  # catch first-time entries not yet in _ever_active
                pt.last_active_ts = ts
                pt.add_sample(float(brightnesses[i]), ts, hist_secs)
                if i not in self._ever_active:
                    if self._new_ever_active_count >= _MAX_NEW_PER_SEC:
                        continue   # noise flood — skip, don't track for decode
                    self._new_ever_active_count += 1
                self._ever_active.add(i)
            # Record samples for points that have ever been above gate but are
            # currently quiet.  No time cutoff: once a phone has crossed the gate
            # it records for the rest of the session, giving the backward-scan
            # decoder a full 13.2s window even for borderline-std distant phones.
            # Decoded phones are skipped — their history is no longer consumed.
            for i in self._ever_active:
                if computed_stds[i] < gate:
                    pt = self._points[i]
                    if pt.decoded_id is not None:
                        continue
                    pt.add_sample(float(brightnesses[i]), ts, hist_secs)
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
            # Asymmetric EMA: rises fast (α=0.4) when noise spikes, falls slowly (α=0.05).
            # Fast-up means the gate adapts within 2-3 frames after a sudden brightness
            # change, preventing a sustained flood of background noise into _ever_active.
            # Slow-down preserves sensitivity after the spike passes.
            p90 = float(np.percentile(stds, 90))
            alpha = 0.4 if p90 > self._noise_floor else 0.05
            self._noise_floor = (1 - alpha) * self._noise_floor + alpha * p90
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

            # Guard-phase extension: also attempt decode on recently-active points
            # that are currently below gate.  A phone entering its 4-phase dark guard
            # (~1.2s) goes quiet right when the warmup threshold may be crossing 13.2s,
            # meaning Gate 2 blocks every attempt during that window and an entire cycle
            # is lost.  Points in _ever_active have proven signal — if they went quiet
            # within the last 1.8s (1.5× guard duration) we bypass Gate 2 only.
            _GUARD_WINDOW = 4 * PHASE_MS / 1000 * 1.5   # 1.8s
            for i in self._ever_active:
                if computed_stds[i] >= gate:
                    continue   # already handled in main loop above
                pt = self._points[i]
                if pt.decoded_id is not None:
                    continue
                if pt.last_active_ts <= 0 or (ts - pt.last_active_ts) > _GUARD_WINDOW:
                    continue
                will_decode = (ts - pt.last_decode_attempt) >= cfg["decode_interval"]
                if will_decode and time.time() >= deadline:
                    continue
                pt.try_decode(ts, cfg, skip_std_gate=True)
        else:
            for pt in self._points:
                pt.try_decode(ts, cfg)

        # 5. Collect DetectedDevice per blink_id.
        #    A close/large phone covers several grid points that all decode the
        #    same ID.  Average their pixel positions for a centroid estimate and
        #    take the max confidence across all matching points.
        #    Scan _ever_active (≤ active phone count) instead of all 25K points —
        #    every decoded point enters _ever_active when it first crosses the gate
        #    and eviction never removes decoded entries.
        id_pts:  dict[int, list] = {}   # blink_id → [_GridPoint, ...]
        decoded_pts_new: list = []
        for i in self._ever_active:
            pt = self._points[i]
            if pt.decoded_id is None:
                continue
            decoded_pts_new.append(pt)
            id_pts.setdefault(pt.decoded_id, []).append(pt)

        id_map: dict[int, DetectedDevice] = {}
        for bid, pts in id_pts.items():
            if bid not in self._locked_positions:
                cx = sum(p.px for p in pts) / len(pts)
                cy = sum(p.py for p in pts) / len(pts)
                self._locked_positions[bid] = (cx, cy)
            else:
                cx, cy = self._locked_positions[bid]
            best_conf = max(p.confidence for p in pts)
            id_map[bid] = DetectedDevice(
                blink_id=bid,
                cx_px=cx,
                cy_px=cy,
                confidence=best_conf,
            )

        # Spatial dedup: if two different IDs have centroids within CLUSTER_R pixels
        # of each other, keep only the higher-confidence one.  Prevents phantom IDs
        # caused by phase-shifted decodes of the same phone — most common with IDs
        # whose Manchester pattern is purely alternating (e.g. ID=0 → all-dark-bright,
        # whose 1-phase-shifted complement decodes as ID=511 = all-ones).
        CLUSTER_R = 120  # px — same radius used in draw_overlay stream dedup
        detections = sorted(id_map.values(), key=lambda d: d.confidence, reverse=True)
        kept: list[DetectedDevice] = []
        for det in detections:
            if any(
                abs(det.cx_px - k.cx_px) < CLUSTER_R and abs(det.cy_px - k.cy_px) < CLUSTER_R
                for k in kept
            ):
                continue
            kept.append(det)

        self.last_results = kept
        self._decoded_pts = decoded_pts_new

        # 6. Diagnostic logging
        if ts - self._last_log_ts >= cfg["log_interval"]:
            self._last_log_ts = ts
            self._log_diagnostics()

        # 7. Build recent-std heatmap — only when debug capture is active.
        if need_debug:
            dbg_img = self._std_heatmap(h, w)
            return self.last_results, DebugImages(gray=gray, contrast=dbg_img)
        return self.last_results, DebugImages()

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

        # Warn if the best candidate has low signal range.
        # Causes: auto-exposure compressing amplitude, low screen brightness,
        # or phone screen dimmed by ambient light sensor.
        # Expected range with manual exposure and full brightness: ~0.99.
        top = by_std[0]
        if n_above > 0 and top.history and len(top.history) >= 2:
            raw_vals = [b for _, b in top.history]
            top_range = max(raw_vals) - min(raw_vals)
            self.signal_range = top_range
            if top_range < 0.5:
                log.info(f"[blink] WARNING: low signal range={top_range:.2f} — "
                         f"weak signal (low screen brightness, AE, or ambient dimming). "
                         f"Detection will be slower.")
        elif n_above == 0:
            self.signal_range = 1.0   # no active phones — don't show amber

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
        show_ids: bool = True,
        valid_ids: set | None = None,
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
        for pt in (self._decoded_pts if show_ids else []):
            if valid_ids is not None and pt.decoded_id not in valid_ids:
                continue
            if pt.decoded_id is None:   # may have been cleared since last frame
                continue
            if pt.decoded_id in drawn_ids:
                continue
            drawn_ids.add(pt.decoded_id)
            # Use locked centroid (frozen at first decode) so the label doesn't
            # jump when noise points later decode the same ID at different coords.
            if pt.decoded_id in self._locked_positions:
                raw_x = int(self._locked_positions[pt.decoded_id][0])
                raw_y = int(self._locked_positions[pt.decoded_id][1])
            else:
                raw_x, raw_y = pt.px, pt.py
            px, py = to_canvas(raw_x, raw_y)
            _draw_id_box(frame, px, py, str(pt.decoded_id + 1), (0, 220, 80))

        # Actively blinking but not yet decoded — show a scrolling binary stream.
        # Filter: must swing from near-zero (dark phase) to bright (white phase).
        # Deduplicate by proximity so one phone = one stream, not one per grid pt.
        STREAM_DISPLAY_N = 12
        # Check window must span past the guard (NUM_GUARD phases × ~3 frames/phase at 11fps ≈ 13 frames)
        # so the max-min check includes pre-guard Manchester frames and doesn't drop to zero.
        STREAM_CHECK_N = 22
        CLUSTER_R     = 120   # px — grid points within this distance = same phone

        # Gate: only draw a stream if the point looks like a real phone.
        # Age alone isn't enough — sustained LEDs/reflections also pass age.
        # decode_failures is the stronger signal: a real phone decodes within
        # ~26s (2 cycles); noise accumulates many failures and never decodes.
        STREAM_MIN_AGE_S    = 4.0
        STREAM_MAX_FAILURES = 6    # suppress after this many consecutive decode fails
        STREAM_MAX_CANDS    = 12   # cap work regardless of how many points are above gate

        # numpy-gate: find above-threshold indices in one vectorised pass,
        # then range-check only those ~0-50 points instead of all 25K.
        # All filters (age, failures, decoded) are applied inside the generator
        # so suppressed points never pay for the history slice + max/min scan.
        _range_min = max(min_std * 1.2, 0.08)
        if self._last_stds is not None:
            cand_idx = np.where(self._last_stds >= min_std)[0]
            candidates = sorted(
                (self._points[i] for i in cand_idx
                 if (self._points[i].decoded_id is None
                     and self._points[i].decode_failures < STREAM_MAX_FAILURES
                     and len(self._points[i].history) >= 2
                     and (self._points[i].history[-1][0] - self._points[i].history[0][0]) >= STREAM_MIN_AGE_S
                     and (max(b for _, b in self._points[i].history[-STREAM_CHECK_N:])
                          - min(b for _, b in self._points[i].history[-STREAM_CHECK_N:]))
                     > _range_min)),
                key=lambda p: p.recent_std,
                reverse=True,
            )[:STREAM_MAX_CANDS]
        else:
            candidates = []

        seen_canvas: list[tuple[int, int]] = []
        for pt in candidates:
            cx, cy = to_canvas(pt.px, pt.py)
            if any(abs(cx - ex) < CLUSTER_R and abs(cy - ey) < CLUSTER_R
                   for ex, ey in seen_canvas):
                continue
            seen_canvas.append((cx, cy))

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

    def clear_id(self, blink_id: int):
        """Clear a decoded ID from all grid points — called when a decode is rejected
        as not matching any connected client, so the point can re-attempt decode."""
        self._locked_positions.pop(blink_id, None)
        for pt in self._points:
            if pt.decoded_id == blink_id:
                pt.decoded_id = None
                pt.confidence = 0.0
                pt.decode_failures = 0

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
        self._ever_active.clear()
        self._locked_positions.clear()
        self._diff_prev_gray   = None
        self._diff_accum       = None
        self._diff_accum_count = 0
        self._diff_discovered.clear()
        self._diff_centroids.clear()

