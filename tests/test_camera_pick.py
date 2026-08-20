# (c) Adam Davis - adamdavis.co.uk
"""
Tests for camera selection: only the Elgato, ever.

There are two claims worth pinning down, and neither is provable by reading
the happy path.

The first is that nothing else gets switched on. The old find_cameras()
probed indices 0-7 with VideoCapture to see which answered, which physically
activates every camera it touches - on the show machine that is a Logitech
and the built-in FaceTime camera, once per scan, repeating every few seconds
for as long as the Elgato was unplugged. Resolution is by name now, so the
test that matters is that open_camera is never called with anything that is
not the Elgato, including when the Elgato is absent entirely.

The second is that "Elgato Virtual Camera" must not win. It is installed
alongside the real Facecam, it contains the string "elgato", and if it is
picked the detector gets a software device instead of a camera.

controller.py imports Dear PyGui and refuses to load without the run.sh
launch marker, so both are set before the import.

Run with:  python -m pytest tests/
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PIXELMESH_LAUNCHED", "1")

import pytest

controller = pytest.importorskip(
    "controller", reason="controller needs Dear PyGui and OpenCV"
)

ADAMS_RIG = {
    0: "Logitech Webcam C925e",
    1: "Elgato Facecam 4K [USB2]",
    2: "Elgato Virtual Camera",
    3: "FaceTime HD Camera",
}


@pytest.fixture
def devices(monkeypatch):
    """Swap in a fake device list and reset the change-logging memo."""
    box = {"list": {}}
    monkeypatch.setattr(controller, "_video_device_names", lambda: box["list"])
    monkeypatch.setattr(controller, "_last_scan_signature", None, raising=False)
    return box


@pytest.fixture
def opened(monkeypatch):
    """Record every open_camera call and hand back a sentinel handle."""
    calls = []

    def fake_open(idx, device_name="elgato"):
        calls.append((idx, device_name))
        return object()

    monkeypatch.setattr(controller, "open_camera", fake_open)
    return calls


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    # controller owns the AppState singleton; each test starts with nothing open.
    monkeypatch.setattr(controller.state, "selected_camera_idx", -1, raising=False)
    monkeypatch.setattr(controller.state, "selected_camera_label", "", raising=False)


# ---------------------------------------------------------------- #
# Resolution
# ---------------------------------------------------------------- #

def test_picks_the_facecam_not_the_virtual_camera(devices):
    devices["list"] = ADAMS_RIG
    idx, label = controller.find_elgato()
    assert (idx, label) == (1, "Elgato Facecam 4K [USB2]")


def test_virtual_camera_never_wins_even_when_listed_first(devices):
    devices["list"] = {0: "Elgato Virtual Camera", 1: "Elgato Facecam 4K"}
    idx, label = controller.find_elgato()
    assert label == "Elgato Facecam 4K"


def test_no_elgato_resolves_to_nothing(devices):
    # Every entry here is a camera someone might reasonably expect us to fall
    # back to. None of them is acceptable: the audience phones need the
    # Elgato's locked exposure.
    devices["list"] = {
        0: "Logitech Webcam C925e",
        1: "Elgato Virtual Camera",
        2: "FaceTime HD Camera",
    }
    assert controller.find_elgato() == (None, "")


def test_empty_device_list_resolves_to_nothing(devices):
    devices["list"] = {}
    assert controller.find_elgato() == (None, "")


def test_index_is_taken_from_the_list_not_assumed(devices):
    # AVFoundation does not order devices stably: the Elgato has been observed
    # at index 0 and index 1 on the same machine minutes apart.
    devices["list"] = {0: "Elgato Facecam 4K", 1: "FaceTime HD Camera"}
    assert controller.find_elgato()[0] == 0
    controller._last_scan_signature = None
    devices["list"] = {0: "FaceTime HD Camera", 1: "Elgato Facecam 4K"}
    assert controller.find_elgato()[0] == 1


# ---------------------------------------------------------------- #
# Opening
# ---------------------------------------------------------------- #

def test_nothing_is_opened_when_the_elgato_is_absent(devices, opened):
    devices["list"] = {0: "Logitech Webcam C925e", 1: "FaceTime HD Camera"}
    holder = {}
    assert controller._scan_and_pick_elgato(holder) is False
    assert opened == [], "a non-Elgato camera was opened"
    assert holder.get("cap") is None


def test_opens_the_elgato_by_resolved_index_and_name(devices, opened):
    devices["list"] = ADAMS_RIG
    holder = {}
    assert controller._scan_and_pick_elgato(holder) is True
    assert opened == [(1, "Elgato Facecam 4K [USB2]")]
    assert holder["cap"] is not None


def test_repeat_scans_do_not_reopen_a_working_camera(devices, opened):
    devices["list"] = ADAMS_RIG
    holder = {}
    controller._scan_and_pick_elgato(holder)
    for _ in range(5):
        assert controller._scan_and_pick_elgato(holder, retrying=True) is True
    assert len(opened) == 1


def test_renumbering_does_not_reopen_a_working_camera(devices, opened):
    """Plugging in an unrelated USB camera can renumber the Elgato. The open
    handle is bound to the device, not the number, so this must not tear down
    a working capture mid-show."""
    devices["list"] = ADAMS_RIG
    holder = {}
    controller._scan_and_pick_elgato(holder)
    controller._last_scan_signature = None
    devices["list"] = {0: "Elgato Facecam 4K [USB2]", 1: "Logitech Webcam C925e"}
    assert controller._scan_and_pick_elgato(holder, retrying=True) is True
    assert len(opened) == 1, "renumbering caused a needless reopen"


def test_reacquires_after_the_handle_is_dropped(devices, opened):
    """What the capture thread does when reads stop working: it nulls the
    handle. The next scan has to open the Elgato again."""
    devices["list"] = ADAMS_RIG
    holder = {}
    controller._scan_and_pick_elgato(holder)
    holder["cap"] = None                      # unplugged, capture thread gave up
    assert controller._scan_and_pick_elgato(holder, retrying=True) is True
    assert len(opened) == 2
    assert opened[-1] == (1, "Elgato Facecam 4K [USB2]")


def test_unplug_then_replug_never_falls_back(devices, opened):
    devices["list"] = ADAMS_RIG
    holder = {}
    controller._scan_and_pick_elgato(holder)

    # Elgato pulled: the capture thread drops the handle, and every scan
    # while it is gone must open nothing at all.
    holder["cap"] = None
    controller._last_scan_signature = None
    devices["list"] = {0: "Logitech Webcam C925e", 1: "FaceTime HD Camera"}
    for _ in range(3):
        assert controller._scan_and_pick_elgato(holder, retrying=True) is False
    assert len(opened) == 1, "fell back to another camera while waiting"

    # Back in, at a different index this time.
    controller._last_scan_signature = None
    devices["list"] = {0: "Logitech Webcam C925e", 1: "FaceTime HD Camera",
                       2: "Elgato Facecam 4K [USB2]"}
    assert controller._scan_and_pick_elgato(holder, retrying=True) is True
    assert opened[-1] == (2, "Elgato Facecam 4K [USB2]")


def test_holder_none_opens_nothing(devices, opened):
    devices["list"] = ADAMS_RIG
    assert controller._scan_and_pick_elgato(None) is False
    assert opened == []
