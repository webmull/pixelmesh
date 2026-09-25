# (c) Adam Davis - adamdavis.co.uk
"""The clock-sync control, with no camera and no server in sight.

Sync used to be a flip: the S key and the checkbox called toggle_sync(), and
because sync is already on after every detection run, the first press mid-show
turned it OFF, which drops every phone into the pre-show scramble. The Leeds
footage (24 Sep 2026) carries that fingerprint nineteen times. Now the Re-sync
button means "re-sync now" and can never switch sync off; there is no hotkey,
and off is its own deliberate call.
"""
import pytest


@pytest.fixture
def ctl(monkeypatch):
    """controller with the network and status line stubbed, camera present."""
    controller = pytest.importorskip("controller")
    posts = []
    monkeypatch.setattr(controller, "post_json_async",
                        lambda path, payload=None, **kw: posts.append((path, payload)))
    monkeypatch.setattr(controller, "set_status", lambda *_: None)
    monkeypatch.setattr(controller, "_no_camera", lambda: False)
    with controller.state.lock:
        controller.state.syncing = False
    return controller, posts


def _syncing(controller):
    with controller.state.lock:
        return controller.state.syncing


def test_resync_never_switches_sync_off(ctl):
    controller, posts = ctl
    for _ in range(3):
        controller.resync()
    assert posts == [("/admin/sync", {"sync": True})] * 3
    assert _syncing(controller) is True


def test_resync_turns_sync_on_when_it_was_off(ctl):
    controller, posts = ctl
    assert _syncing(controller) is False
    controller.resync()
    assert posts[-1] == ("/admin/sync", {"sync": True})
    assert _syncing(controller) is True


def test_switching_off_is_a_separate_deliberate_call(ctl):
    controller, posts = ctl
    controller.resync()
    controller.sync_off()
    assert posts[-1] == ("/admin/sync", {"sync": False})
    assert _syncing(controller) is False
    controller.resync()
    assert _syncing(controller) is True


def test_detection_end_always_resyncs_even_when_already_on(ctl):
    controller, posts = ctl
    with controller.state.lock:
        controller.state.syncing = True
    controller._auto_enable_sync()
    assert posts == [("/admin/sync", {"sync": True})]


def test_resync_does_not_need_the_camera(ctl, monkeypatch):
    controller, posts = ctl
    monkeypatch.setattr(controller, "_no_camera", lambda: True)
    controller.resync()
    assert posts == [("/admin/sync", {"sync": True})]


def test_the_flip_is_gone(ctl):
    controller, _ = ctl
    assert not hasattr(controller, "toggle_sync")
