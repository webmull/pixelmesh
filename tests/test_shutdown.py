# (c) Adam Davis - adamdavis.co.uk
"""
Tests for graceful shutdown - the path that closes an open recording when the
controller is asked to quit.

An mp4 is only playable once ffmpeg has written its moov atom, and ffmpeg only
does that when its stdin closes and it is given time to finish. Three separate
things have to hold:

  1. VideoRecorder.stop() must actually finalise the file, and must be safe to
     call while the capture thread is writing frames at 60fps.
  2. The controller must catch SIGTERM and run that stop before exiting - and
     must do it without touching state.lock or DearPyGui, because a Python
     signal handler runs ON the main thread and can land inside a lock the
     main thread already holds.
  3. run.sh must send SIGTERM rather than SIGKILL, and wait.

The invariants in (2) and (3) are enforced by reading the source. That is
deliberate: controller.py cannot be imported here (it needs DearPyGui, a
display, and PIXELMESH_LAUNCHED), and both invariants are the kind that fail
silently - a handler that deadlocks looks like a hang, and a SIGKILL that
truncates a file looks like nothing at all until you try to play it.

Run with:  python -m pytest tests/
"""

import ast
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

import video_recorder
from video_recorder import VideoRecorder

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_FFMPEG = video_recorder._FFMPEG
_FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

needs_ffmpeg = pytest.mark.skipif(
    not _FFMPEG or not os.path.isfile(_FFPROBE),
    reason="ffmpeg/ffprobe not installed",
)


def is_playable(path: str) -> bool:
    """True if the file has a readable duration - i.e. ffmpeg wrote its moov
    atom. A truncated recording fails here, which is the whole point."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    out = subprocess.run(
        [_FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return out.returncode == 0 and out.stdout.strip()[:1].isdigit()


def frame(w=320, h=180):
    return np.zeros((h, w, 3), dtype=np.uint8)


@pytest.fixture
def rec(tmp_path, monkeypatch):
    """A recorder writing into a temp dir instead of debug/recordings."""
    monkeypatch.setattr(video_recorder, "_REC_DIR", str(tmp_path))
    return VideoRecorder()


# ------------------------------------------------------------------ #
# The file is actually finalised
# ------------------------------------------------------------------ #

class TestFinalises:
    @needs_ffmpeg
    def test_stop_produces_a_playable_file(self, rec):
        rec.start()
        for _ in range(20):
            rec.record(frame())
        path = rec.stop()
        assert is_playable(path), "stop() left a file with no moov atom"

    @needs_ffmpeg
    def test_file_is_unplayable_before_stop(self, rec):
        """The control case. If this ever passes without stop(), the test
        above proves nothing."""
        rec.start()
        for _ in range(20):
            rec.record(frame())
        assert not is_playable(rec._path)
        rec.stop()

    @needs_ffmpeg
    def test_stop_returns_the_path_it_wrote(self, rec):
        rec.start()
        rec.record(frame())
        path = rec.stop()
        assert path and os.path.isfile(path)

    def test_stop_without_a_start_is_harmless(self, rec):
        assert rec.stop() == ""

    @needs_ffmpeg
    def test_stop_is_idempotent(self, rec):
        """Both the signal handler and the shutdown block call it."""
        rec.start()
        rec.record(frame())
        first = rec.stop()
        assert rec.stop() == ""          # second call is a no-op
        assert is_playable(first)        # and did not corrupt the first

    @needs_ffmpeg
    def test_record_after_stop_does_not_reopen_ffmpeg(self, rec):
        """The capture thread can still be mid-loop when stop() lands. If a
        late frame reopened the pipe it would create a second, headerless
        file over the top of the finished one."""
        rec.start()
        rec.record(frame())
        path = rec.stop()
        rec.record(frame())
        assert rec._proc is None
        assert rec.active is False
        assert is_playable(path)

    def test_stop_clears_state_for_the_next_run(self, rec):
        rec.start()
        rec.stop()
        assert rec._proc is None and rec._path == "" and rec._size is None


class TestFilenamesAreUnique:
    """The timestamp only resolves to the second. Two recordings started
    inside the same second used to share a filename, and ffmpeg is passed -y,
    so the second silently overwrote the first."""

    def test_two_starts_in_the_same_second_get_different_files(self, rec):
        a = rec.start()
        rec.stop()
        b = rec.start()
        rec.stop()
        assert a != b

    def test_many_rapid_starts_are_all_distinct(self, rec):
        paths = []
        for _ in range(6):
            paths.append(rec.start())
            rec.stop()
        assert len(set(paths)) == len(paths)

    @needs_ffmpeg
    def test_an_earlier_recording_is_not_overwritten(self, rec, tmp_path):
        first = rec.start()
        for _ in range(10):
            rec.record(frame())
        rec.stop()
        size_before = os.path.getsize(first)

        second = rec.start()
        for _ in range(10):
            rec.record(frame())
        rec.stop()

        assert first != second
        assert os.path.getsize(first) == size_before, "first recording was clobbered"
        assert is_playable(first) and is_playable(second)

    def test_collides_with_an_existing_file_on_disk(self, rec, tmp_path):
        """Covers a restart landing in the same second as the previous run,
        where _issued is empty but the file is already there."""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        squatter = tmp_path / f"{stamp}.mp4"
        squatter.write_bytes(b"existing")
        path = rec.start()
        rec.stop()
        assert path != str(squatter)
        assert squatter.read_bytes() == b"existing"


# ------------------------------------------------------------------ #
# Thread safety - stop() lands while the capture thread is writing
# ------------------------------------------------------------------ #

class TestConcurrency:
    @needs_ffmpeg
    def test_stop_during_continuous_recording(self, rec):
        """The real shape: capture thread writing at speed, stop() from
        another thread. Without a lock this raises AttributeError on
        self._proc.stdin the moment stop() nulls _proc."""
        errors = []
        stop_writing = threading.Event()

        def writer():
            try:
                while not stop_writing.is_set():
                    rec.record(frame())
            except Exception as e:            # noqa: BLE001 - that is the test
                errors.append(e)

        rec.start()
        t = threading.Thread(target=writer, daemon=True)
        t.start()
        time.sleep(0.3)
        path = rec.stop()
        stop_writing.set()
        t.join(timeout=5)

        assert not errors, f"capture thread raised during stop(): {errors}"
        assert is_playable(path)

    @needs_ffmpeg
    def test_repeated_start_stop_under_load(self, rec):
        """Toggling via the mode API is easier to do quickly than via a
        pedal. Each cycle must leave its own complete file."""
        stop_writing = threading.Event()
        errors, paths = [], []

        def writer():
            try:
                while not stop_writing.is_set():
                    rec.record(frame())
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        for _ in range(3):
            rec.start()
            time.sleep(0.15)
            paths.append(rec.stop())
        stop_writing.set()
        t.join(timeout=5)

        assert not errors, f"raised under repeated toggling: {errors}"
        assert len({p for p in paths if p}) == len([p for p in paths if p])
        for p in paths:
            assert is_playable(p), f"{p} was left unfinalised"

    def test_lock_is_released_after_stop(self, rec):
        """stop() must not hold the lock across ffmpeg's drain - that would
        block the capture thread and freeze the audience feed."""
        rec.start()
        rec.stop()
        assert rec._lock.acquire(blocking=False)
        rec._lock.release()

    @needs_ffmpeg
    def test_capture_thread_is_not_blocked_for_the_whole_drain(self, rec):
        """The guarantee that matters at 60fps: a record() call arriving
        while stop() is draining ffmpeg must not wait on the drain."""
        rec.start()
        for _ in range(40):
            rec.record(frame())

        done = threading.Event()
        threading.Thread(target=lambda: (rec.stop(), done.set()), daemon=True).start()
        time.sleep(0.02)

        t0 = time.time()
        rec.record(frame())      # returns immediately: active is already False
        elapsed = time.time() - t0
        done.wait(timeout=30)
        assert elapsed < 1.0, f"record() blocked {elapsed:.2f}s behind stop()"


# ------------------------------------------------------------------ #
# The signal handler, end to end in a real process
# ------------------------------------------------------------------ #

_CHILD = """
import os, signal, sys, time
sys.path.insert(0, {root!r})
import numpy as np
import video_recorder
video_recorder._REC_DIR = {out!r}
from video_recorder import VideoRecorder

rec = VideoRecorder()

def on_term(signum, _frame):
    rec.stop()          # what controller._finalise_recordings does
    os._exit(0)

signal.signal(signal.SIGTERM, on_term)
path = rec.start()
open({flag!r}, "w").write(path)
f = np.zeros((180, 320, 3), dtype=np.uint8)
while True:
    rec.record(f)
    time.sleep(0.005)
"""


# The wakeup-pipe path in isolation: the Python-level handler is a deliberate
# no-op, so the ONLY thing that can save the recording is the watcher thread
# reading the pipe. The main thread is also parked, though note that a blocked
# threading.Lock is interruptible in CPython and is therefore not a faithful
# stand-in for a wedged C call - it is here to keep the shape realistic, not
# to prove anything on its own.
#
# What this does prove: finalisation does not depend on the main thread ever
# running the handler. On the real controller SIGTERM produced no shutdown log
# line and no exit, and the root cause was never confirmed; this makes the
# outcome not depend on knowing it.
_CHILD_WEDGED = """
import os, signal, sys, threading, time
sys.path.insert(0, {root!r})
import numpy as np
import video_recorder
video_recorder._REC_DIR = {out!r}
from video_recorder import VideoRecorder

rec = VideoRecorder()

r, w = os.pipe()
os.set_blocking(w, False)
os.set_blocking(r, True)
signal.set_wakeup_fd(w)
signal.signal(signal.SIGTERM, lambda *a: None)

def watcher():
    os.read(r, 1)
    rec.stop()
    os._exit(0)

threading.Thread(target=watcher, daemon=True).start()

def writer():
    f = np.zeros((180, 320, 3), dtype=np.uint8)
    while True:
        rec.record(f)
        time.sleep(0.005)

path = rec.start()
threading.Thread(target=writer, daemon=True).start()
open({flag!r}, "w").write(path)

# Main thread parked in a C call, never returning to the interpreter.
lock = threading.Lock()
lock.acquire()
lock.acquire()
"""


@needs_ffmpeg
class TestSignalHandling:
    def _spawn(self, tmp_path, script):
        flag = str(tmp_path / "ready")
        src = script.format(root=os.path.abspath(_ROOT), out=str(tmp_path), flag=flag)
        p = subprocess.Popen([sys.executable, "-c", textwrap.dedent(src)])
        for _ in range(100):
            if os.path.exists(flag):
                break
            time.sleep(0.05)
        else:
            p.kill()
            pytest.fail("child never started recording")
        time.sleep(0.5)
        return p, open(flag).read().strip()

    def test_sigterm_finalises_the_recording(self, tmp_path):
        p, path = self._spawn(tmp_path, _CHILD)
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=30)
        assert is_playable(path), "SIGTERM handler did not finalise the file"

    def test_handler_exits_the_process(self, tmp_path):
        """A handler that saves the file but never exits would just hang
        run.sh until it escalated to SIGKILL."""
        p, _ = self._spawn(tmp_path, _CHILD)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=30) == 0

    def test_watcher_thread_alone_finalises_the_recording(self, tmp_path):
        """With the Python handler stubbed out, the wakeup pipe is the only
        route to stop(). This is the guarantee the fix rests on: saving the
        file does not require the main thread to run anything."""
        p, path = self._spawn(tmp_path, _CHILD_WEDGED)
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=30)
        assert is_playable(path), "watcher thread did not finalise the recording"

    def test_watcher_thread_alone_exits_the_process(self, tmp_path):
        """A process that saves the file but never exits still leaves run.sh
        waiting out its grace period - which is what the hang looked like."""
        p, _ = self._spawn(tmp_path, _CHILD_WEDGED)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=15) == 0


# ------------------------------------------------------------------ #
# Controller invariants, enforced against the source
# ------------------------------------------------------------------ #

def controller_ast():
    with open(os.path.join(_ROOT, "controller.py")) as fh:
        return ast.parse(fh.read())


def func(name):
    for node in ast.walk(controller_ast()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"controller.py has no function {name}()")


class TestControllerShutdown:
    def test_finalise_recordings_exists(self):
        assert func("_finalise_recordings")

    def test_finalise_never_touches_state_lock(self):
        """A signal handler runs ON the main thread, so it can land inside a
        `with state.lock` block the main thread already holds. threading.Lock
        is not reentrant, so asking for it there deadlocks the process at
        exactly the moment it is trying to save the file."""
        for node in ast.walk(func("_finalise_recordings")):
            if isinstance(node, ast.Attribute) and node.attr == "lock":
                if isinstance(node.value, ast.Name) and node.value.id == "state":
                    pytest.fail("_finalise_recordings must not take state.lock")

    def test_finalise_makes_no_dearpygui_calls(self):
        """It runs from a signal handler, which may interrupt a render frame."""
        for node in ast.walk(func("_finalise_recordings")):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id != "dpg", "_finalise_recordings called dpg"

    def test_finalise_stops_both_recorders(self):
        body = ast.dump(func("_finalise_recordings"))
        assert "vid_rec" in body, "plain recorder not finalised"
        assert "dbg_cap" in body, "debug capture not finalised"

    def test_finalise_is_guarded_against_running_twice(self):
        """Both the handler and the shutdown block call it."""
        assert "_shutdown_once" in ast.dump(func("_finalise_recordings"))

    def test_handler_finalises_before_anything_that_can_block(self):
        """dpg.stop_dearpygui() does nothing when the window is minimised,
        because macOS is not driving the render loop. If the handler waited on
        that first, an unattended recording - the exact case this is for -
        would never be saved."""
        calls = [n for n in ast.walk(func("_on_terminate")) if isinstance(n, ast.Call)]
        names = [n.func.id for n in calls if isinstance(n.func, ast.Name)]
        dpg_calls = [n for n in calls
                     if isinstance(n.func, ast.Attribute)
                     and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == "dpg"]
        assert "_finalise_recordings" in names
        if dpg_calls:
            assert calls.index(next(c for c in calls
                                    if isinstance(c.func, ast.Name)
                                    and c.func.id == "_finalise_recordings")) \
                   < calls.index(dpg_calls[0]), \
                   "handler must save the file before calling into DearPyGui"

    def test_sigterm_and_sigint_are_both_registered(self):
        src = open(os.path.join(_ROOT, "controller.py")).read()
        assert "signal.SIGTERM" in src
        assert "signal.SIGINT" in src
        assert "signal.signal(" in src

    def test_shutdown_does_not_depend_on_the_main_thread(self):
        """The plain handler runs on the main thread, only between bytecodes,
        so anything that stops the main thread reaching a bytecode boundary
        stops the recording being saved. set_wakeup_fd is written by CPython's
        C-level handler the instant the signal lands, so a watcher thread can
        act regardless. Losing this puts the recording back at the mercy of
        whatever the render loop happens to be doing."""
        src = open(os.path.join(_ROOT, "controller.py")).read()
        assert "signal.set_wakeup_fd" in src
        assert "_signal_watcher" in src

    def test_the_watcher_finalises_before_touching_dearpygui(self):
        calls = [n for n in ast.walk(func("_signal_watcher")) if isinstance(n, ast.Call)]
        names = [n.func.id for n in calls if isinstance(n.func, ast.Name)]
        assert "_finalise_recordings" in names

    def test_normal_shutdown_also_finalises(self):
        """Quitting by closing the window used to close debug capture but
        leave a plain recording open."""
        src = open(os.path.join(_ROOT, "controller.py")).read()
        assert src.count("_finalise_recordings()") >= 2, \
            "the finally block should call it as well as the signal handler"

    def test_debug_capture_does_not_auto_start(self):
        """It never stops when detection stops, so auto-starting it left it
        recording for the whole session."""
        src = open(os.path.join(_ROOT, "controller.py")).read()
        assert "_DEBUG_AUTO_ON_DETECT = False" in src


# ------------------------------------------------------------------ #
# run.sh - the other half of the bargain
# ------------------------------------------------------------------ #

def run_sh():
    with open(os.path.join(_ROOT, "run.sh")) as fh:
        return fh.read()


class TestRunScript:
    def test_controller_gets_sigterm_not_sigkill(self):
        src = run_sh()
        assert 'pkill -f "controller.py"' in src, "controller must get SIGTERM"

    def test_sigkill_only_appears_after_the_grace_period(self):
        """A -9 before the wait would defeat the handler entirely."""
        src = run_sh()
        first_term = src.index('pkill -f "controller.py"')
        first_kill = src.index('pkill -9 -f "controller.py"')
        assert first_term < first_kill

    def test_there_is_a_grace_period(self):
        assert "_TERM_GRACE_SECS" in run_sh()

    def test_grace_period_covers_the_controllers_own_timeouts(self):
        """Debug capture joins its writer for 5s then waits up to 30s on
        ffmpeg. Escalating sooner would kill it mid-write."""
        import re
        m = re.search(r"_TERM_GRACE_SECS=(\d+)", run_sh())
        assert m and int(m.group(1)) >= 35

    def test_it_waits_for_the_controller_to_actually_exit(self):
        src = run_sh()
        assert 'pgrep -f "controller.py"' in src
