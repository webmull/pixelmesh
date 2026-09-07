# (c) Adam Davis - adamdavis.co.uk
"""
Tests for starting the avatar race without being told who is in the room.

POST /admin/game/start takes blink_ids, and the sidebar button works them out
from the controller's own calibrated positions. The talk deck cannot: it knows
nothing about the room, and it is what starts the race now, three seconds after
it arrives on the race slide. So an empty payload means "everyone", derived
server-side, and these pin what "everyone" is - live phones the camera placed,
ordered left to right, which is what puts the on-stage lanes in the same order
as the seats.

Handlers are called directly, matching the other suites.

Run with:  python -m pytest tests/
"""

import asyncio
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def fresh_server():
    import server
    importlib.reload(server)
    return server


@pytest.fixture(autouse=True)
def clean_race():
    """fresh_server() reloads server, not game, and game holds the round in
    module globals. Left set, game_active leaks into whatever runs next - it
    made end_show stop a race that no test had started, and broadcast an extra
    message a neighbouring suite was counting. Cleared both sides of every
    test so the leak cannot travel in either direction."""
    import game
    game.game_active = False
    game.race_positions = {}
    yield
    game.game_active = False
    game.race_positions = {}


def phone(srv, dev, blink_id, u, silent_for=0.0):
    """A phone that joined, was placed by the camera, and last spoke just now."""
    srv.connections[dev] = object()
    srv.last_seen[dev] = time.time() - silent_for
    srv.blink_assignments[dev] = blink_id
    srv.positions[dev] = {"u": u, "v": 0.5}


class TestRoomBlinkIds:
    def test_orders_lanes_left_to_right(self):
        """Audience-left ends up first, so the projection mirrors the room."""
        srv = fresh_server()
        import game
        phone(srv, "right", 7, 0.9)
        phone(srv, "left", 3, 0.1)
        phone(srv, "middle", 5, 0.5)
        assert game._room_blink_ids() == [3, 5, 7]

    def test_a_phone_the_camera_never_placed_gets_no_lane(self):
        srv = fresh_server()
        import game
        phone(srv, "found", 1, 0.2)
        srv.connections["never-found"] = object()
        srv.last_seen["never-found"] = time.time()
        srv.blink_assignments["never-found"] = 2       # joined, never located
        assert game._room_blink_ids() == [1]

    def test_a_ghost_gets_no_lane(self):
        """The reason this uses live_devices and not the connection table: a
        phone that dropped and came back under a new identity would otherwise
        be given a lane with nobody standing in it."""
        srv = fresh_server()
        import game
        phone(srv, "here", 2, 0.4)
        phone(srv, "old-identity", 1, 0.1, silent_for=srv.LIVE_TIMEOUT + 5)
        assert game._room_blink_ids() == [2]

    def test_an_empty_room_is_empty_not_an_error(self):
        fresh_server()
        import game
        assert game._room_blink_ids() == []


class TestStartPayload:
    def test_explicit_ids_still_win(self):
        """The sidebar button keeps sending its own list, and it is respected
        exactly as before."""
        srv = fresh_server()
        import game
        phone(srv, "a", 1, 0.1)
        phone(srv, "b", 2, 0.9)
        asyncio.run(game._start_race_round({"blink_ids": [9]}))
        assert sorted(game.race_positions) == [9]

    def test_no_ids_means_the_room(self):
        srv = fresh_server()
        import game
        phone(srv, "a", 1, 0.1)
        phone(srv, "b", 2, 0.9)
        asyncio.run(game._start_race_round({}))
        assert sorted(game.race_positions) == [1, 2]


class _Req:
    """Stand-in request. Tunnelled traffic arrives from loopback too, so the
    forwarding headers are the only thing separating it from a local caller."""
    def __init__(self, headers=None, host="127.0.0.1"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})()


class TestTheRouteIsOpenButOnlyLocally:
    def test_start_is_token_free(self):
        srv = fresh_server()
        assert "/admin/game/start" in srv._ADMIN_PUBLIC
        assert "/admin/game/start" in srv._ADMIN_CORS   # the deck is another origin

    def test_stop_is_not(self):
        """Only starting is opened up. Stopping a round mid-race stays behind
        the token, or goes through /admin/end."""
        srv = fresh_server()
        assert "/admin/game/stop" not in srv._ADMIN_PUBLIC

    def test_a_tunnelled_start_is_refused(self):
        fresh_server()
        import game
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            asyncio.run(game.game_start(_Req({"x-forwarded-for": "203.0.113.7"})))
        assert e.value.status_code == 403

    def test_a_local_start_is_allowed(self):
        srv = fresh_server()
        import game
        phone(srv, "a", 1, 0.3)
        asyncio.run(game.game_start(_Req()))
        assert game.game_active
        assert sorted(game.race_positions) == [1]
