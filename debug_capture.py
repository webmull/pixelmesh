# (c) Adam Davis - adamdavis.co.uk
"""
PixelMesh V2 — Debug Capture

Each call to start_run() creates a new friendly-named subfolder under debug/
(e.g. "autumn-fox-42").  Only the 15 most recent runs are kept; older ones are
deleted automatically when a new run starts.
Per-frame JPEGs and JSONs go into a frames/ subfolder.
The overlay video is written as run.mp4 (H.264) by piping raw frames to
ffmpeg in real-time — no intermediate file, no codec dependency in OpenCV.
"""

import os
import json
import random
import time
import shutil
import subprocess
import cv2
import numpy as np
from log import log

_ADJECTIVES = [
    "autumn", "brave", "calm", "dapper", "eager", "fierce", "golden", "happy",
    "indigo", "jolly", "keen", "lively", "misty", "noble", "ochre", "proud",
    "quiet", "russet", "silver", "teal", "urban", "velvet", "wandering",
    "xenial", "yellow", "zesty",
]
_NOUNS = [
    "badger", "crow", "dune", "ember", "fox", "glacier", "hawk", "iris",
    "jasper", "kite", "lynx", "mesa", "nova", "otter", "pine", "quartz",
    "raven", "storm", "thorn", "umber", "viper", "wolf", "xenon", "yarrow",
    "zenith",
]

DEBUG_DIR = os.path.join(os.path.dirname(__file__), "debug")

_FFMPEG = (
    shutil.which("ffmpeg")
    or "/opt/homebrew/bin/ffmpeg"
    or "/usr/local/bin/ffmpeg"
)


class DebugCapture:
    def __init__(self):
        self.run_dir    = None
        self.frames_dir = None
        self.frame_idx  = 0
        self.active     = False
        self._manifest  = []
        self._ffmpeg_proc: subprocess.Popen | None = None

    # ---------------------------------------------------------------- #

    @staticmethod
    def _friendly_name() -> str:
        adj  = random.choice(_ADJECTIVES)
        noun = random.choice(_NOUNS)
        num  = random.randint(10, 99)
        return f"{adj}-{noun}-{num}"

    def start_run(self) -> str:
        name            = self._friendly_name()
        self.run_dir    = os.path.join(DEBUG_DIR, name)
        self.frames_dir = os.path.join(self.run_dir, "frames")
        self.frame_idx  = 0
        self._manifest  = []
        self.active     = True
        self._ffmpeg_proc = None   # opened lazily on first record_frame
        os.makedirs(self.frames_dir, exist_ok=True)
        log.info(f"[debug] run started → {name}")
        return self.run_dir

    def stop_run(self):
        if not self.active:
            return
        self.active = False
        if self._ffmpeg_proc is not None:
            try:
                self._ffmpeg_proc.stdin.close()
                self._ffmpeg_proc.wait(timeout=30)
                log.info(f"[debug] video → {self.run_dir}/run.mp4")
            except Exception as e:
                log.warning(f"[debug] ffmpeg close error: {e}")
                self._ffmpeg_proc.kill()
                self._ffmpeg_proc.wait()   # reap zombie after forced kill
            self._ffmpeg_proc = None
        summary_path = os.path.join(self.run_dir, "summary.json")
        with open(summary_path, "w") as f:
            json.dump({
                "frames": len(self._manifest),
                "run_dir": self.run_dir,
                "frames_data": self._manifest,
            }, f, indent=2)
        log.info(f"[debug] run stopped — {len(self._manifest)} frames → {self.run_dir}")

    # ---------------------------------------------------------------- #

    def record_frame(self, overlay: np.ndarray):
        """Pipe one overlay frame to ffmpeg → run.mp4 (every frame, not throttled)."""
        if not self.active:
            return
        if self._ffmpeg_proc is None:
            if not os.path.isfile(_FFMPEG):
                return
            h, w = overlay.shape[:2]
            mp4_path = os.path.join(self.run_dir, "run.mp4")
            self._ffmpeg_proc = subprocess.Popen(
                [
                    _FFMPEG, "-y",
                    "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{w}x{h}", "-pix_fmt", "bgr24", "-r", "30",
                    "-i", "pipe:0",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-movflags", "+faststart",
                    mp4_path,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(f"[debug] ffmpeg pipe opened → {mp4_path} ({w}×{h})")
        try:
            self._ffmpeg_proc.stdin.write(overlay.tobytes())
        except BrokenPipeError:
            log.warning("[debug] ffmpeg pipe broken")
            self._ffmpeg_proc = None

    # ---------------------------------------------------------------- #

    def save_frame(
        self,
        raw:        np.ndarray,
        gray:       np.ndarray,
        thresh:     np.ndarray,
        overlay:    np.ndarray,
        blobs:      list,
        detections: list,
    ):
        if not self.active:
            return

        i   = self.frame_idx
        pfx = os.path.join(self.frames_dir, f"{i:04d}")
        self.frame_idx += 1

        # --- Save images (downscale raw to keep file sizes small) ---
        small_raw = cv2.resize(raw, (960, 540))
        cv2.imwrite(f"{pfx}_raw.jpg",      small_raw,  [cv2.IMWRITE_JPEG_QUALITY, 80])
        cv2.imwrite(f"{pfx}_gray.jpg",     cv2.resize(gray, (960, 540)))
        cv2.imwrite(f"{pfx}_contrast.jpg", cv2.resize(thresh, (960, 540)))
        cv2.imwrite(f"{pfx}_overlay.jpg",  overlay,    [cv2.IMWRITE_JPEG_QUALITY, 85])

        # --- Histogram of grayscale ---
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten().tolist()

        # --- JSON frame summary (only interesting grid points) ---
        blob_data = []
        for pt in blobs:
            vals = [round(v, 3) for _, v in pt.history]
            if not vals:
                continue
            lo, hi = min(vals), max(vals)
            contrast = hi - lo
            if contrast < 0.03 and pt.decoded_id is None:
                continue
            blob_data.append({
                "px":               pt.px,
                "py":               pt.py,
                "history_len":      len(vals),
                "brightness_range": [lo, hi],
                "contrast":         round(contrast, 3),
                "last_20":          vals[-20:],
                "decoded_id":       pt.decoded_id,
                "confidence":       round(pt.confidence, 3),
                "fail":             pt.decode_fail_reason,
            })

        det_data = [
            {"blink_id": d.blink_id, "cx": round(d.cx_px, 1),
             "cy": round(d.cy_px, 1), "confidence": round(d.confidence, 3)}
            for d in detections
        ]

        with open(f"{pfx}.json", "w") as f:
            json.dump({
                "frame":              i,
                "timestamp":          time.time(),
                "grid_points":        blob_data,
                "detections":         det_data,
                "active_points":      int((thresh > 0).sum()),
                "gray_hist_peak_bin": int(np.argmax(hist)),
            }, f, indent=2)

        self._manifest.append({
            "frame":       i,
            "grid_points": len(blob_data),
            "detections":  det_data,
        })
