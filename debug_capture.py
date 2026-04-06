"""
PixelMesh V2 — Debug Capture

Each call to start_run() creates a new timestamped subfolder under debug/.
Saves raw frame, grayscale, threshold mask, overlay, and a JSON summary per frame.
"""

import os
import json
import time
import cv2
import numpy as np
from datetime import datetime
from log import log

DEBUG_DIR = os.path.join(os.path.dirname(__file__), "debug")


class DebugCapture:
    def __init__(self):
        self.run_dir   = None
        self.frame_idx = 0
        self.active    = False
        self._manifest = []   # list of per-frame summaries for final report

    # ---------------------------------------------------------------- #

    def start_run(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir   = os.path.join(DEBUG_DIR, ts)
        self.frame_idx = 0
        self._manifest = []
        self.active    = True
        os.makedirs(self.run_dir, exist_ok=True)
        log.info(f"[debug] run started → {self.run_dir}")
        return self.run_dir

    def stop_run(self):
        if not self.active:
            return
        self.active = False
        # Write manifest summary
        summary_path = os.path.join(self.run_dir, "summary.json")
        with open(summary_path, "w") as f:
            json.dump({
                "frames": len(self._manifest),
                "run_dir": self.run_dir,
                "frames_data": self._manifest,
            }, f, indent=2)
        log.info(f"[debug] run stopped — {len(self._manifest)} frames → {self.run_dir}")

    # ---------------------------------------------------------------- #

    def save_frame(
        self,
        raw:      np.ndarray,          # original BGR camera frame
        gray:     np.ndarray,          # grayscale
        thresh:   np.ndarray,          # contrast heatmap (replaces old binary thresh)
        overlay:  np.ndarray,          # annotated preview canvas (BGR)
        blobs:    list,                # list of _GridPoint objects
        detections: list,              # list of DetectedDevice
    ):
        if not self.active:
            return

        i   = self.frame_idx
        pfx = os.path.join(self.run_dir, f"{i:04d}")
        self.frame_idx += 1

        # --- Save images (downscale raw to keep file sizes small) ---
        small_raw = cv2.resize(raw, (960, 540))
        cv2.imwrite(f"{pfx}_raw.jpg",    small_raw,  [cv2.IMWRITE_JPEG_QUALITY, 80])
        cv2.imwrite(f"{pfx}_gray.jpg",   cv2.resize(gray, (960, 540)))
        cv2.imwrite(f"{pfx}_contrast.jpg", cv2.resize(thresh, (960, 540)))
        cv2.imwrite(f"{pfx}_overlay.jpg", overlay,   [cv2.IMWRITE_JPEG_QUALITY, 85])

        # --- Histogram of grayscale (for threshold tuning) ---
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten().tolist()

        # --- JSON frame summary (only include interesting grid points) ---
        blob_data = []
        for pt in blobs:
            vals = [round(v, 3) for _, v in pt.history]
            if not vals:
                continue
            lo = min(vals)
            hi = max(vals)
            contrast = hi - lo
            if contrast < 0.03 and pt.decoded_id is None:
                continue   # skip flat/uninteresting points to keep JSON small
            blob_data.append({
                "px":           pt.px,
                "py":           pt.py,
                "history_len":  len(vals),
                "brightness_range": [lo, hi],
                "contrast":     round(contrast, 3),
                "last_20":      vals[-20:],
                "decoded_id":   pt.decoded_id,
                "confidence":   round(pt.confidence, 3),
                "fail":         pt.decode_fail_reason,
            })

        det_data = [
            {"blink_id": d.blink_id, "cx": round(d.cx_px, 1),
             "cy": round(d.cy_px, 1), "confidence": round(d.confidence, 3)}
            for d in detections
        ]

        frame_summary = {
            "frame":       i,
            "timestamp":   time.time(),
            "grid_points": blob_data,
            "detections":  det_data,
            "active_points": int((thresh > 0).sum()),
            "gray_hist_peak_bin": int(np.argmax(hist)),
        }

        with open(f"{pfx}.json", "w") as f:
            json.dump(frame_summary, f, indent=2)

        self._manifest.append({
            "frame":      i,
            "grid_points": len(blob_data),
            "detections": det_data,
        })
