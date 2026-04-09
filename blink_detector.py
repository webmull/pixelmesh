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

from blink_encoder import decode_phases, CYCLE_LEN, PHASE_MS

from log import log

DEFAULTS = dict(
    grid_step       = 30,    # px: distance between grid sample points
    history_seconds = 35.0,  # seconds of brightness history to keep (≥ 2 cycles at PHASE_MS=500ms)
    decode_interval = 0.5,   # seconds between decode attempts per point
    min_history     = 150,   # unused — decode gate is now time-based (_MIN_HISTORY_SECS)
    min_recent_std  = 0.10,  # initial gate — overridden adaptively after warmup
    recent_n        = 24,    # samples in the recent window (~1.6s at 15fps)
    brightness_pct  = 80,    # percentile for brightness sample in patch
    sample_radius   = 30,    # px: radius around grid point to sample
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


        result = decode_phases(self.history)
        if result is not None:
            self.decoded_id, self.confidence = result
            self.decode_fail_reason = ""
        else:
            lo, hi = min(vals), max(vals)
            self.decode_fail_reason = (
                f"no_decode hist={len(vals)} std={self.recent_std:.2f} "
                f"range={hi-lo:.2f}"
            )


class BlinkDetector:
    def __init__(self):
        self._points:      list[_GridPoint] = []
        self.last_results: list[DetectedDevice] = []
        self.cfg           = dict(DEFAULTS)
        self._grid_shape   = (0, 0, 0, 0)
        self._last_log_ts  = 0.0
        self._noise_floor  = DEFAULTS["min_recent_std"]  # adaptive EMA estimate

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

        # 3. Sample brightness — vectorised across all grid points.
        #    Pad so every point gets a full 2r×2r patch regardless of position.
        gray_pad = np.pad(gray, r, mode="edge")
        side = 2 * r
        patches = np.stack([
            gray_pad[pt.py: pt.py + side, pt.px: pt.px + side]
            for pt in self._points
        ])  # (N, side, side)
        brightnesses = np.percentile(
            patches.reshape(len(self._points), -1), pct, axis=1
        ) / 255.0  # (N,)

        for pt, b in zip(self._points, brightnesses):
            pt.add_sample(float(b), ts, cfg["history_seconds"])

        # Update recent_std for all points in one vectorised pass.
        # Collect last-n history for every point that has enough samples.
        recent_mat = np.array([
            [bv for _, bv in pt.history[-n:]]
            if len(pt.history) >= n else None
            for pt in self._points
        ], dtype=object)
        for pt, row in zip(self._points, recent_mat):
            pt.recent_std = float(np.std(row)) if row is not None else 0.0

        # 4. Adapt gate to current noise floor (p90 of all recent_std values).
        #    Most points are background, so p90 ≈ scene noise regardless of exposure.
        #    Gate = noise_floor × 5, floored at 0.04 to avoid being too eager.
        stds = [pt.recent_std for pt in self._points if pt.recent_std > 0]
        if len(stds) >= 20:
            # p75 is robust — phones would need to cover 25%+ of the frame to bias it.
            # Slow EMA (α=0.02, τ≈50 frames) prevents transient phone activity spiking the gate.
            p75 = float(np.percentile(stds, 75))
            self._noise_floor = 0.98 * self._noise_floor + 0.02 * p75
            cfg["min_recent_std"] = max(self._noise_floor * 5, 0.04)

        # 5. Attempt decode on high-variance points
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
        for pt in self._points:
            v = int(min(pt.recent_std * 3.0, 1.0) * 255)
            if v < 10:
                continue
            cv2.circle(img, (pt.px, pt.py), self.cfg["grid_step"] // 2 - 2, v, -1)
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

                # Small green dot + ID label, nothing else
                cv2.circle(frame, (px, py), 4, (0, 220, 80), -1)
                cv2.putText(frame, str(pt.decoded_id),
                            (px + 8, py + 5), font, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(frame, str(pt.decoded_id),
                            (px + 8, py + 5), font, 0.45, (80, 255, 80), 1, cv2.LINE_AA)

        # Actively blinking but not yet decoded — show a scrolling binary stream.
        # Filter: must swing from near-zero (dark phase) to bright (white phase).
        # Deduplicate by proximity so one phone = one stream, not one per grid pt.
        STREAM_N      = 12
        CLUSTER_R     = 120   # px — grid points within this distance = same phone
        candidates = sorted(
            (p for p in self._points
             if (p.recent_std >= min_std
                 and p.history
                 and (max(b for _, b in p.history[-STREAM_N:])
                      - min(b for _, b in p.history[-STREAM_N:])) > min_std * 1.2)),
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

            vals = [b for _, b in pt.history[-STREAM_N:]]
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
