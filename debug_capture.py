# (c) Adam Davis - adamdavis.co.uk
"""
pixelmesh V2 — Debug Capture

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
import threading
from queue import Queue, Full

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

# Max number of debug-run subfolders kept under DEBUG_DIR.  Older runs are
# pruned on each start_run().
_MAX_RUNS = 15

# Subfolders inside DEBUG_DIR that are NOT debug runs and must never be pruned.
_PRESERVED_DIRS = {"calibration_logs", "recordings", "reports"}


def _find_ffmpeg() -> str | None:
    for p in (shutil.which("ffmpeg"),
              "/opt/homebrew/bin/ffmpeg",
              "/usr/local/bin/ffmpeg"):
        if p and os.path.isfile(p):
            return p
    return None


_FFMPEG = _find_ffmpeg()


class DebugCapture:
    def __init__(self):
        self.run_dir    = None
        self.frames_dir = None
        self.frame_idx  = 0
        self.active     = False
        self._manifest  = []
        self._ffmpeg_proc: subprocess.Popen | None = None
        # Writer thread + bounded queue so record_frame never blocks the
        # camera loop on ffmpeg's stdin.  Maxsize=2 keeps memory tiny and
        # drops the oldest pending frame if ffmpeg falls behind.
        self._write_q:  Queue | None = None
        self._save_q:   Queue | None = None
        self._writer:   threading.Thread | None = None

    # ---------------------------------------------------------------- #

    @staticmethod
    def _friendly_name() -> str:
        adj  = random.choice(_ADJECTIVES)
        noun = random.choice(_NOUNS)
        num  = random.randint(10, 99)
        return f"{adj}-{noun}-{num}"

    @staticmethod
    def _prune_old_runs():
        """Delete debug-run subfolders past the _MAX_RUNS most recent."""
        try:
            entries = []
            for name in os.listdir(DEBUG_DIR):
                if name in _PRESERVED_DIRS or name.startswith("."):
                    continue
                full = os.path.join(DEBUG_DIR, name)
                if os.path.isdir(full):
                    entries.append((os.path.getmtime(full), full))
            entries.sort(reverse=True)
            for _, path in entries[_MAX_RUNS:]:
                shutil.rmtree(path, ignore_errors=True)
                log.info(f"[debug] pruned old run → {os.path.basename(path)}")
        except Exception as e:
            log.warning(f"[debug] prune failed: {e}")

    def start_run(self) -> str:
        self._prune_old_runs()
        name            = self._friendly_name()
        self.run_dir    = os.path.join(DEBUG_DIR, name)
        self.frames_dir = os.path.join(self.run_dir, "frames")
        self.frame_idx  = 0
        self._manifest  = []
        self.active     = True
        self._ffmpeg_proc = None   # opened lazily on first record_frame
        self._write_q   = Queue(maxsize=2)
        self._save_q    = Queue(maxsize=2)
        threading.Thread(target=self._saver_loop, daemon=True,
                         name="dbg-save").start()
        self._writer    = threading.Thread(
            target=self._writer_loop, daemon=True, name="dbg-write",
        )
        self._writer.start()
        os.makedirs(self.frames_dir, exist_ok=True)
        log.info(f"[debug] run started → {name}")
        return self.run_dir

    def stop_run(self):
        if not self.active:
            return
        self.active = False
        # Sentinel tells the writer to flush + exit; join briefly so the
        # ffmpeg close below sees no further writes.
        if self._save_q is not None:
            try:
                self._save_q.put_nowait(None)
            except Exception:
                pass
        if self._write_q is not None:
            try:
                self._write_q.put_nowait(None)
            except Full:
                pass
        if self._writer is not None:
            self._writer.join(timeout=5)
            self._writer = None
        self._write_q = None
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
        """Hand one overlay frame to the writer thread; never blocks the camera loop."""
        if not self.active or self._write_q is None:
            return
        try:
            self._write_q.put_nowait(overlay)
        except Full:
            # ffmpeg fell behind — drop the oldest queued frame and replace it.
            try:
                self._write_q.get_nowait()
            except Exception:
                pass
            try:
                self._write_q.put_nowait(overlay)
            except Full:
                pass

    def _writer_loop(self):
        """Drain the queue, lazily opening ffmpeg on the first real frame."""
        while True:
            frame = self._write_q.get()
            if frame is None:
                return
            if self._ffmpeg_proc is None:
                if not _FFMPEG:
                    return
                h, w = frame.shape[:2]
                mp4_path = os.path.join(self.run_dir, "run.mp4")
                # use_wallclock_as_timestamps: stamp each piped frame at
                # arrival time so playback matches real-world duration
                # regardless of variable camera fps.
                self._ffmpeg_proc = subprocess.Popen(
                    [
                        _FFMPEG, "-y",
                        "-f", "rawvideo", "-vcodec", "rawvideo",
                        "-s", f"{w}x{h}", "-pix_fmt", "bgr24",
                        "-use_wallclock_as_timestamps", "1",
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
                self._ffmpeg_proc.stdin.write(frame.tobytes())
            except BrokenPipeError:
                log.warning("[debug] ffmpeg pipe broken")
                self._ffmpeg_proc = None
                return

    # ---------------------------------------------------------------- #

    def save_frame(self, raw, gray, thresh, overlay, blobs, detections):
        """Enqueue for the saver thread. Four JPEG encodes plus a
        per-point JSON dump used to run on the display thread and were
        measured stalling it (cost scales with active-point count);
        drop-oldest semantics under load, like the ffmpeg writer.
        History values are snapshotted here because the detector keeps
        mutating point histories after this call returns."""
        if not self.active or self._save_q is None:
            return
        snap = []
        for pt in blobs:
            if not pt.history:
                continue
            # Raw slice only - rounding happens on the saver thread.
            snap.append((pt.px, pt.py, pt.history[-40:],
                         pt.decoded_id, pt.confidence, pt.decode_fail_reason))
        item = (raw, gray, thresh, overlay, snap, list(detections))
        try:
            self._save_q.put_nowait(item)
        except Full:
            try:
                self._save_q.get_nowait()
            except Exception:
                pass
            try:
                self._save_q.put_nowait(item)
            except Full:
                pass

    def _saver_loop(self):
        while True:
            item = self._save_q.get()
            if item is None:
                return
            try:
                self._save_frame_impl(*item)
            except Exception:
                pass

    def _save_frame_impl(self, raw, gray, thresh, overlay, snap, detections):
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
        # Snapshot tuples from save_frame; history capped at the last 40
        # samples and the point list at the 400 highest-contrast entries
        # so noisy scenes (bright monitors: 1400+ active points) cannot
        # produce multi-hundred-ms dumps.
        scored = []
        for px, py, hist, decoded_id, confidence, fail in snap:
            vals = [round(v, 3) for _, v in hist]
            lo, hi = min(vals), max(vals)
            contrast = hi - lo
            if contrast < 0.03 and decoded_id is None:
                continue
            scored.append((contrast, {
                "px":               px,
                "py":               py,
                "history_len":      len(vals),
                "brightness_range": [lo, hi],
                "contrast":         round(contrast, 3),
                "last_20":          vals[-20:],
                "decoded_id":       decoded_id,
                "confidence":       round(confidence, 3),
                "fail":             fail,
            }))
        scored.sort(key=lambda t: (t[1]["decoded_id"] is None, -t[0]))
        blob_data = [b for _, b in scored[:400]]

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
