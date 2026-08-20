# (c) Adam Davis - adamdavis.co.uk
"""
Tests for why an effect button sometimes did nothing.

/admin/blink_map lists CONNECTED clients, and phones leave that list
constantly - every iOS backgrounding, every reload, every network blip. The
poll loop treated any absence as permanent and deleted the phone's entry from
calibrated_positions, and detection never put it back: the decode path freezes
each blink_id in _detected_ids the first time it is seen and skips it forever
after. So the controller's copy of the room drained one blip at a time while
every phone stayed connected and happy.

trigger_effect gated on that copy, so the drain ended with effect buttons that
posted nothing. It set a status-bar string and returned, and set_status does
not log, so the whole failure was one line in the corner of the operator's
screen and nothing in the log at all.

Two things are pinned here: positions survive a blip, and an effect fires for
a room full of connected phones regardless of what detection bookkeeping the
controller still happens to be holding.

Run with:  python -m pytest tests/
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PIXELMESH_LAUNCHED", "1")

import pytest

controller = pytest.importorskip(
    "controller", reason="controller needs Dear PyGui and OpenCV"
)
import effects
import game


@pytest.fixture
def rig(monkeypatch):
    """A controller mid-show: two phones found, both connected."""
    posted = []

    monkeypatch.setattr(effects, "_state", controller.state)
    monkeypatch.setattr(effects, "_set_status", controller.set_status)
    # An empty cache is authoritative and yields defaults for every param.
    # Leaving it None sends _get down the live dpg.get_value path, which
    # segfaults outside a running GUI rather than raising.
    monkeypatch.setattr(effects, "_param_cache", {})
    monkeypatch.setattr(effects, "post_json_async",
                        lambda path, payload: posted.append((path, payload)))
    monkeypatch.setattr(game, "clear_winner_highlight", lambda: None)

    monkeypatch.setattr(controller, "_detected_ids", {1, 2})
    monkeypatch.setattr(controller, "_valid_blink_ids", {1, 2})
    monkeypatch.setattr(controller, "_blink_absent_since", {})

    with controller.state.lock:
        controller.state.calibrated_positions.clear()
        controller.state.calibrated_positions.update(
            {1: {"u": 0.2, "v": 0.5}, 2: {"u": 0.8, "v": 0.5}})
        controller.state.client_count = 2

    def poll(connected):
        """One poll_clients tick with `connected` phones on the blink map."""
        with controller.state.lock:
            controller.state.client_count = len(connected)
        monkeypatch.setitem(
            controller._refresh_valid_blink_ids.__globals__, "fetch_json",
            lambda path, timeout=0.5: {
                "map": {str(b): f"dev{b}" for b in connected}})
        controller._refresh_valid_blink_ids()

    def held():
        with controller.state.lock:
            return sorted(controller.state.calibrated_positions)

    def click(name):
        posted.clear()
        effects.trigger_effect(name)
        return len(posted)

    yield type("Rig", (), {"poll": staticmethod(poll), "held": staticmethod(held),
                           "click": staticmethod(click), "posted": posted})

    with controller.state.lock:
        controller.state.calibrated_positions.clear()


# ---------------------------------------------------------------- #
# Positions survive a blip
# ---------------------------------------------------------------- #

def test_position_survives_one_missed_poll(rig):
    rig.poll({1})            # phone 2 backgrounded for a tick
    assert rig.held() == [1, 2]


def test_positions_survive_every_phone_blipping(rig):
    """The regression exactly as it happened: each phone drops for a single
    poll and comes straight back. Before the grace period this left the
    controller with no positions at all and every phone still connected."""
    for connected in ({1}, {1, 2}, {2}, {1, 2}):
        rig.poll(connected)
    assert rig.held() == [1, 2]


def test_position_dropped_once_the_phone_is_really_gone(rig):
    rig.poll({1})
    controller._blink_absent_since[2] = time.time() - controller.POSITION_GRACE_SECS - 1
    rig.poll({1})
    assert rig.held() == [1]


def test_returning_within_grace_clears_the_countdown(rig):
    rig.poll({1})
    assert 2 in controller._blink_absent_since
    rig.poll({1, 2})
    assert 2 not in controller._blink_absent_since


def test_failed_poll_does_not_touch_positions(rig, monkeypatch):
    monkeypatch.setitem(controller._refresh_valid_blink_ids.__globals__,
                        "fetch_json", lambda path, timeout=0.5: None)
    controller._refresh_valid_blink_ids()
    assert rig.held() == [1, 2]


# ---------------------------------------------------------------- #
# The effect gate
# ---------------------------------------------------------------- #

def test_effect_fires_after_the_blips(rig):
    for connected in ({1}, {1, 2}, {2}, {1, 2}):
        rig.poll(connected)
    assert rig.click("wave") == 1


def test_effect_fires_with_no_calibrated_positions_at_all(rig):
    """A phone renders wave from its own u,v, which lives on the server. The
    controller having forgotten where everyone is must not veto that."""
    with controller.state.lock:
        controller.state.calibrated_positions.clear()
        controller.state.client_count = 40
    assert rig.click("wave") == 1


def test_effect_blocked_when_the_room_is_empty(rig):
    with controller.state.lock:
        controller.state.client_count = 0
    assert rig.click("wave") == 0


def test_groups_still_requires_positions(rig):
    """groups sorts the room left-to-right to assign columns; with nothing to
    sort it computes n=0, which reaches the client as a divide-by-zero."""
    with controller.state.lock:
        controller.state.calibrated_positions.clear()
        controller.state.client_count = 40
    assert rig.click("groups") == 0


def test_groups_fires_when_positions_are_present(rig):
    assert rig.click("groups") == 1
    payload = rig.posted[-1][1]
    assert payload["spatial_freq"] >= 1
    assert payload["groups"]
