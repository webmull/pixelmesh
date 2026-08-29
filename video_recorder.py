# (c) Adam Davis - adamdavis.co.uk
import os
import queue
import shutil
import subprocess
import threading
import time

import numpy as np

from log import log

_REC_DIR = os.path.join(os.path.dirname(__file__), "debug", "recordings")


def _find_ffmpeg() -> str | None:
    for p in (shutil.which("ffmpeg"),
              "/opt/homebrew/bin/ffmpeg",
              "/usr/local/bin/ffmpeg"):
        if p and os.path.isfile(p):
            return p
    return None


_FFMPEG = _find_ffmpeg()


class VideoRecorder:
    """Pipes raw canvas frames to an ffmpeg subprocess.

    Thread safety
        record() is called from the capture thread ~60 times a second.
        start()/stop() are called from whichever thread the operator used:
        the GUI thread (hotkey V), the MIDI thread (pedal) or the mode-control
        thread (POST /admin/mode).  Without a lock, stop() setting _proc = None
        between record()'s None-check and its write raises AttributeError on
        the capture thread, and closing stdin mid-write corrupts the frame.

        The lock is held across the pipe write - that is the point, it is what
        stops stop() closing the pipe underneath a partial frame.  It is NOT
        held across stop()'s ffmpeg drain, which can take seconds: that would
        stall the capture thread and freeze the audience feed.  stop() takes
        ownership of the handle under the lock and does the slow part outside.
    """

    def __init__(self):
        self.active  = False
        self._proc   = None
        self._path   = ""
        self._size   = None   # (w, h) set on first frame
        self._lock   = threading.Lock()
        self._issued = set()  # paths handed out this session - see start()
        # Frames are handed to a writer thread rather than written by the
        # caller. The caller is the capture thread, and ffmpeg's stdin only
        # drains as fast as libx264 encodes: the moment the encoder fell
        # behind, write() blocked with the lock held and the entire capture
        # thread stalled - detection, the feed and the texture all froze at
        # once, during a recording, which is exactly when a show is live.
        # debug_capture.py has used this writer-thread shape for the same
        # reason since the 50-100ms save_frame stalls were measured.
        # maxsize=2, drop-oldest: a slow encoder loses frames, never the show.
        self._queue: queue.Queue = queue.Queue(maxsize=2)
        self._writer_started = False
        self._writing = False   # writer mid-frame; read by stop()'s drain

    def start(self) -> str:
        os.makedirs(_REC_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        # The stamp only resolves to the second, so two recordings started
        # inside the same second would land on one filename and the second
        # would overwrite the first - silently, since ffmpeg is passed -y.
        # Easy to hit now that recording can be toggled over HTTP rather than
        # only by a foot pedal.
        #
        # Checking the filesystem is not enough on its own: ffmpeg opens the
        # file lazily on the first frame, so a recording stopped before any
        # frame arrived leaves nothing on disk to collide with. _issued
        # remembers the names handed out regardless.
        path = os.path.join(_REC_DIR, f"{stamp}.mp4")
        n = 2
        while os.path.exists(path) or path in self._issued:
            path = os.path.join(_REC_DIR, f"{stamp}_{n}.mp4")
            n += 1
        self._issued.add(path)
        with self._lock:
            self._path = path
            self._proc = None   # opened lazily on first frame so size is known
            self._size = None
            self.active = True
            path = self._path
        log.info(f"[rec] recording started → {path}")
        return path

    def record(self, canvas: np.ndarray):
        # Non-blocking hand-off; the writer thread does the pipe write. The
        # canvas is fresh-allocated per frame by the capture path, so keeping
        # a reference here needs no copy.
        if not self.active:
            return
        if not self._writer_started:
            self._writer_started = True
            threading.Thread(target=self._writer_loop, daemon=True,
                             name="rec-writer").start()
        try:
            self._queue.put_nowait(canvas)
        except queue.Full:
            try:
                self._queue.get_nowait()   # drop the oldest, keep the newest
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(canvas)
            except queue.Full:
                pass

    def _writer_loop(self):
        # One persistent daemon thread for the process lifetime. After stop()
        # any frames still queued hit active=False inside _record_locked and
        # fall through harmlessly, so no sentinel or join dance is needed and
        # stop()'s take-ownership-under-the-lock protocol is unchanged.
        while True:
            canvas = self._queue.get()
            self._writing = True
            try:
                with self._lock:
                    self._record_locked(canvas)
            finally:
                self._writing = False

    def _record_locked(self, canvas: np.ndarray):
        if not self.active:
            return
        h, w = canvas.shape[:2]
        if self._proc is None:
            if not _FFMPEG:
                log.warning("[rec] ffmpeg not found — cannot record")
                self.active = False
                return
            self._size = (w, h)
            # use_wallclock_as_timestamps timestamps each piped raw frame at
            # arrival time, so playback matches real-world duration regardless
            # of camera fps (varies 14–60 in our setup).
            self._proc = subprocess.Popen(
                [
                    _FFMPEG, "-y",
                    "-f", "rawvideo", "-vcodec", "rawvideo",
                    "-s", f"{w}x{h}", "-pix_fmt", "bgr24",
                    "-use_wallclock_as_timestamps", "1",
                    "-i", "pipe:0",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    # Without this libx264 keeps the full chroma from bgr24 and
                    # writes yuv444p, which no browser will decode - Chrome and
                    # Safari both need 4:2:0. Every recording made before this
                    # plays in VLC and shows a black frame on a web page.
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    self._path,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(f"[rec] ffmpeg pipe opened ({w}×{h})")
        if canvas.shape[:2] != (self._size[1], self._size[0]):
            return   # size changed mid-recording — skip frame
        try:
            # The array is already C-contiguous bytes in bgr24 layout; writing
            # it directly avoids tobytes() copying 6.2MB per frame.
            buf = canvas if canvas.flags["C_CONTIGUOUS"] else np.ascontiguousarray(canvas)
            self._proc.stdin.write(buf)
        except BrokenPipeError:
            log.warning("[rec] ffmpeg pipe broken")
            self._proc = None
            self.active = False

    def stop(self) -> str:
        # Give the writer a bounded moment to flush what is queued. The old
        # synchronous record() guaranteed a frame had reached ffmpeg by the
        # time it returned; with the writer thread, a record() immediately
        # followed by stop() would otherwise take the pipe away before the
        # frame was ever written - the shutdown tests catch exactly that.
        # Bounded, never join(): if ffmpeg is wedged mid-write we drop the
        # queued frames and proceed, because a hung stop() is the precise
        # failure this file's callers spent a watchdog eliminating.
        deadline = time.time() + 2.0
        while (self._writing or not self._queue.empty()) and time.time() < deadline:
            time.sleep(0.01)

        # Take ownership of the handle under the lock so no other thread can
        # be mid-write, then release it before draining ffmpeg.
        with self._lock:
            self.active = False
            proc, self._proc = self._proc, None
            path, self._path = self._path, ""
            self._size = None

        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=30)
                log.info(f"[rec] saved → {path}")
            except Exception as e:
                log.warning(f"[rec] ffmpeg close error: {e}")
                proc.kill()
                proc.wait()   # reap zombie after forced kill
        return path
