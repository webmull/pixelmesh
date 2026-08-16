# (c) Adam Davis - adamdavis.co.uk
"""
Tests for POST /admin/end - the closing card.

The route is one call on purpose: stopping effects and sending the card as two
separate requests leaves a gap the room can see, so the test that matters most
here is that a single call does both, in an order that cannot leave a phone
dark-then-lit.

Handlers are called directly, matching the other suites. broadcast() is
replaced with a recorder so the message sequence can be asserted without a
WebSocket.

Run with:  python -m pytest tests/
"""

import asyncio
import importlib
import time
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def fresh_server():
    import server
    importlib.reload(server)
    return server


def with_recorder(srv):
    """Record what goes out, both fan-out and per-connection.

    show_end is sent per connection rather than broadcast, because found_ms
    differs per device - so the test has to watch both channels.
    """
    sent = []
    per = []

    async def _rec(msg):
        sent.append(msg)

    async def _tsend(ws, msg):
        per.append((ws, msg))
        return True

    srv.broadcast = _rec
    srv._timed_send_json = _tsend
    return sent, per


def add_phone(srv, device_id, blink_id):
    """Register a connected phone the way a real join would."""
    srv.blink_assignments[device_id] = blink_id
    srv.connections[device_id] = object()
    return srv.connections[device_id]


def ends_for(per):
    return [m for _ws, m in per if m["type"] == "show_end"]


class _LocalReq:
    """Minimal stand-in for a request that came from this machine."""
    class _C: host = "127.0.0.1"
    client = _C()
    headers: dict = {}


def end(srv, request=None):
    return asyncio.run(srv.end_show(request or _LocalReq()))


# ------------------------------------------------------------------ #
# What it sends
# ------------------------------------------------------------------ #

class TestBroadcast:
    def test_effect_stop_is_a_fan_out_and_show_end_is_not(self):
        """effect_stop is identical for everyone so it broadcasts; show_end
        carries this phone's own found time and must not."""
        srv = fresh_server()
        add_phone(srv, "a", 0)
        sent, per = with_recorder(srv)
        end(srv)
        assert "effect_stop" in [m["type"] for m in sent]
        assert "show_end" not in [m["type"] for m in sent]
        assert len(ends_for(per)) == 1

    def test_every_connected_phone_gets_one(self):
        srv = fresh_server()
        for i, d in enumerate(("a", "b", "c")):
            add_phone(srv, d, i)
        _sent, per = with_recorder(srv)
        end(srv)
        assert len(ends_for(per)) == 3

    def test_show_end_carries_the_room_total(self):
        srv = fresh_server()
        for i, d in enumerate(("a", "b", "c")):
            add_phone(srv, d, i)
        _sent, per = with_recorder(srv)
        end(srv)
        assert ends_for(per)[0]["total_connected"] == 3

    def test_each_phone_gets_its_own_found_time(self):
        """The reason this is not a broadcast."""
        srv = fresh_server()
        add_phone(srv, "quick", 0)
        add_phone(srv, "slow", 1)
        srv.found_ms.update({"quick": 1200, "slow": 8400})
        _sent, per = with_recorder(srv)
        end(srv)
        got = {ws: m["found_ms"] for ws, m in per if m["type"] == "show_end"}
        assert sorted(got.values()) == [1200, 8400]

    def test_a_phone_never_found_gets_null_not_zero(self):
        """Zero would read as "found instantly" on the card. None becomes a
        dash, which is the truth: some phones are never located."""
        srv = fresh_server()
        add_phone(srv, "missed", 0)
        _sent, per = with_recorder(srv)
        end(srv)
        assert ends_for(per)[0]["found_ms"] is None

    def test_payload_carries_nothing_identifying(self):
        srv = fresh_server()
        add_phone(srv, "device-abc", 7)
        srv.positions["device-abc"] = {"u": 0.5, "v": 0.5}
        _sent, per = with_recorder(srv)
        end(srv)
        msg = ends_for(per)[0]
        assert set(msg) == {"type", "total_connected", "found_ms"}
        assert "device-abc" not in repr(msg)

    def test_total_is_not_live_connections(self):
        """Someone who joined and closed the tab still took part."""
        srv = fresh_server()
        srv.blink_assignments.update({"gone": 0, "here": 1})
        srv.connections["here"] = object()
        _sent, per = with_recorder(srv)
        end(srv)
        assert ends_for(per)[0]["total_connected"] == 2


# ------------------------------------------------------------------ #
# Found time
# ------------------------------------------------------------------ #

class TestFoundTime:
    def test_recorded_when_a_phone_is_first_located(self):
        srv = fresh_server()
        add_phone(srv, "a", 0)
        srv.blink_reverse[0] = "a"
        asyncio.run(srv.detect({"detecting": True}))
        asyncio.run(srv.update_positions({"positions": {"0": {"u": .5, "v": .5}}}))
        assert "a" in srv.found_ms
        assert srv.found_ms["a"] >= 0

    def test_not_inflated_by_re_confirmation(self):
        """The controller re-sends known positions. Counting those would keep
        pushing the number up for someone found immediately."""
        srv = fresh_server()
        add_phone(srv, "a", 0)
        srv.blink_reverse[0] = "a"
        asyncio.run(srv.detect({"detecting": True}))
        asyncio.run(srv.update_positions({"positions": {"0": {"u": .5, "v": .5}}}))
        first = srv.found_ms["a"]
        time.sleep(0.05)
        asyncio.run(srv.update_positions({"positions": {"0": {"u": .5, "v": .5}}}))
        assert srv.found_ms["a"] == first

    def test_a_new_detection_run_re_measures(self):
        """Otherwise a phone found in run one keeps that time after a reset."""
        srv = fresh_server()
        add_phone(srv, "a", 0)
        srv.blink_reverse[0] = "a"
        asyncio.run(srv.detect({"detecting": True}))
        asyncio.run(srv.update_positions({"positions": {"0": {"u": .5, "v": .5}}}))
        assert srv.found_ms
        asyncio.run(srv.detect({"detecting": True}))
        assert srv.found_ms == {}

    def test_reset_clears_it(self):
        srv = fresh_server()
        srv.found_ms["a"] = 1234
        with_recorder(srv)
        asyncio.run(srv.reset())
        assert srv.found_ms == {}

    def test_never_negative(self):
        """Clock skew or a position arriving before the start stamp should not
        produce a negative time on someone's souvenir."""
        srv = fresh_server()
        add_phone(srv, "a", 0)
        srv.blink_reverse[0] = "a"
        asyncio.run(srv.detect({"detecting": True}))
        srv.detection_started_at = time.time() + 5      # start stamp in the future
        srv.found_ms.clear()
        asyncio.run(srv.update_positions({"positions": {"0": {"u": .5, "v": .5}}}))
        assert srv.found_ms["a"] >= 0


# ------------------------------------------------------------------ #
# What it does to show state
# ------------------------------------------------------------------ #

class TestState:
    def test_clears_the_running_effect(self):
        srv = fresh_server()
        with_recorder(srv)
        asyncio.run(srv.start_effect("wave", {}))
        assert srv.current_effect_state is not None
        end(srv)
        assert srv.current_effect_state is None

    def test_leaves_the_show_in_the_ended_mode(self):
        srv = fresh_server()
        with_recorder(srv)
        end(srv)
        assert srv.mode == srv.MODE_ENDED

    def test_ended_is_its_own_mode(self):
        """Not reusing WAITING: a phone that reconnects after the end should
        be able to tell "show over" from "show not started"."""
        srv = fresh_server()
        assert srv.MODE_ENDED not in (srv.MODE_WAITING, srv.MODE_DETECTION,
                                      srv.MODE_SHOWTIME)

    def test_show_stats_still_answers_afterwards(self):
        """The talk deck keeps polling after the show ends; this must not
        leave it reading a broken payload."""
        srv = fresh_server()
        with_recorder(srv)
        end(srv)
        s = asyncio.run(srv.show_stats())
        assert s["effect"] is None
        assert isinstance(s["total_connected"], int)

    def test_is_idempotent(self):
        """The operator will press it twice. Nothing should break, and every
        phone should get the card again rather than half of them."""
        srv = fresh_server()
        add_phone(srv, "a", 0)
        _sent, per = with_recorder(srv)
        end(srv)
        end(srv)
        assert len(ends_for(per)) == 2
        assert srv.current_effect_state is None

    def test_returns_the_total_to_the_caller(self):
        srv = fresh_server()
        srv.blink_assignments.update({"a": 0, "b": 1})
        with_recorder(srv)
        assert end(srv) == {"ok": True, "total_connected": 2}


# ------------------------------------------------------------------ #
# Surviving a reconnect
# ------------------------------------------------------------------ #

class TestReconnect:
    def test_a_reconnecting_phone_is_sent_the_card_again(self):
        """show_end is a one-shot broadcast. Phones drop - screen locks, a
        walk to the bar, patchy conference wifi - and one that came back would
        otherwise sit on a blank idle screen for the rest of the night. The
        server's reconnect-sync block has to know about this mode."""
        import inspect
        srv = fresh_server()
        src = inspect.getsource(srv)
        block = src[src.index("Sync current mode / effect"):]
        block = block[:block.index("MODE_WAITING")]
        assert "MODE_ENDED" in block, "reconnect sync does not handle the ended mode"
        assert "show_end" in block

    def test_the_ended_branch_sends_the_room_total_too(self):
        """A reconnecting phone needs the same number as everyone else, or its
        card says a different thing to the one next to it."""
        import inspect
        srv = fresh_server()
        src = inspect.getsource(srv)
        block = src[src.index("elif mode == MODE_ENDED:"):]
        block = block[:block.index("MODE_WAITING")]
        assert "total_connected" in block


# ------------------------------------------------------------------ #
# Access
# ------------------------------------------------------------------ #

class TestAccess:
    def test_is_token_free_so_the_deck_can_call_it(self):
        srv = fresh_server()
        assert "/admin/end" in srv._ADMIN_PUBLIC

    def test_refuses_anything_that_came_through_the_tunnel(self):
        """The locality check is the ONLY thing guarding this route, and it
        ends the show for the whole room. ngrok forwards pixelmesh.show to
        127.0.0.1, so loopback alone proves nothing - the forwarding header is
        what gives a public request away."""
        from fastapi import HTTPException

        class Tunnelled(_LocalReq):
            headers = {"x-forwarded-for": "203.0.113.9"}

        srv = fresh_server()
        with_recorder(srv)
        with pytest.raises(HTTPException) as e:
            end(srv, Tunnelled())
        assert e.value.status_code == 403

    def test_refuses_a_non_loopback_client(self):
        """Someone else on the venue wifi hitting the laptop directly."""
        from fastapi import HTTPException

        class Lan(_LocalReq):
            class _C: host = "192.168.1.50"
            client = _C()

        srv = fresh_server()
        with_recorder(srv)
        with pytest.raises(HTTPException) as e:
            end(srv, Lan())
        assert e.value.status_code == 403

    def test_a_refused_call_does_not_end_the_show(self):
        """A 403 must not have already stopped the effects on its way out."""
        from fastapi import HTTPException

        class Tunnelled(_LocalReq):
            headers = {"x-forwarded-for": "203.0.113.9"}

        srv = fresh_server()
        with_recorder(srv)
        asyncio.run(srv.start_effect("wave", {}))
        with pytest.raises(HTTPException):
            end(srv, Tunnelled())
        assert srv.current_effect_state is not None
        assert srv.mode != srv.MODE_ENDED
