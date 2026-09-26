# (c) Adam Davis - adamdavis.co.uk
"""
What a phone is told when its socket comes back.

Built for a room with poor signal, where a phone's socket drops and returns
many times in a show. Everything the phone needs to be right again has to be
in the hello replay, because the broadcasts it missed are gone.

Two additions are pinned here:

- A race in progress is replayed. race_start was a one-shot broadcast, so a
  phone that dropped as the round began sat on its located card for the whole
  race while its lane on the stage stood empty.
- A heartbeat ping is answered. The client used to send pings into the void;
  now every ping earns a pong, so a phone can tell a live socket from one the
  server dropped and close the dead one itself.

The WebSocket handler is driven directly with a scripted fake socket, the
same way the other suites call handlers rather than spinning up uvicorn.

Run with:  python -m pytest tests/
"""

import asyncio
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import WebSocketDisconnect


def fresh_server():
    import server
    importlib.reload(server)
    return server


@pytest.fixture(autouse=True)
def clean_race():
    import game
    game.game_active = False
    game.race_positions = {}
    yield
    game.game_active = False
    game.race_positions = {}


class FakeWS:
    """A socket that speaks a scripted set of frames and then hangs up."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def accept(self):
        pass

    async def receive_json(self):
        if not self._frames:
            raise WebSocketDisconnect()
        return self._frames.pop(0)

    async def send_json(self, obj):
        self.sent.append(obj)

    async def send_text(self, text):
        import json
        self.sent.append(json.loads(text))

    async def close(self, *a, **kw):
        pass

    def types(self):
        return [m["type"] for m in self.sent]


def run_phone(srv, frames):
    ws = FakeWS(frames)
    asyncio.run(srv.websocket_endpoint(ws))
    return ws


class TestRaceReplay:
    def test_a_phone_joining_mid_race_gets_the_round(self):
        srv = fresh_server()
        import game
        srv.blink_assignments["a"] = 3
        srv.blink_reverse[3] = "a"
        srv.positions["a"] = {"u": 0.2, "v": 0.5}
        game.game_active = True
        game.race_positions = {3: 0.4, 7: 0.1}
        game.race_start_at = time.time() - 5

        ws = run_phone(srv, [{"type": "hello", "device_id": "a"}])

        starts = [m for m in ws.sent if m["type"] == "race_start"]
        assert len(starts) == 1
        msg = starts[0]
        assert sorted(msg["blink_ids"]) == [3, 7]
        assert msg["start_at"] == int(game.race_start_at * 1000)
        # Hue for the phone the camera placed, none for the one it did not:
        # both renderers fall back to the hash together for that one.
        assert "3" in msg["hues"] and "7" not in msg["hues"]

    def test_replay_comes_after_assigned(self):
        """The client's assigned handler sets the view; race_start has to land
        after it or the card would be overwritten by the assign."""
        srv = fresh_server()
        import game
        game.game_active = True
        game.race_positions = {1: 0.0}
        game.race_start_at = time.time()

        ws = run_phone(srv, [{"type": "hello", "device_id": "late"}])
        t = ws.types()
        assert t.index("assigned") < t.index("race_start")

    def test_no_race_means_no_race_start(self):
        srv = fresh_server()
        ws = run_phone(srv, [{"type": "hello", "device_id": "a"}])
        assert "race_start" not in ws.types()

    def test_a_finished_race_is_not_replayed(self):
        """game_active goes False at the finish; the winner banner is the
        operator's to clear, not something a late joiner should re-enter."""
        srv = fresh_server()
        import game
        game.game_active = False
        game.race_positions = {1: 1.0, 2: 0.6}
        ws = run_phone(srv, [{"type": "hello", "device_id": "a"}])
        assert "race_start" not in ws.types()


class TestPingPong:
    def test_every_ping_is_answered(self):
        srv = fresh_server()
        ws = run_phone(srv, [
            {"type": "hello", "device_id": "a"},
            {"type": "ping"},
            {"type": "ping"},
        ])
        assert ws.types().count("pong") == 2

    def test_ping_still_refreshes_last_seen(self):
        srv = fresh_server()
        ws = FakeWS([{"type": "hello", "device_id": "a"}, {"type": "ping"}])

        async def drive():
            await srv.websocket_endpoint(ws)

        before = time.time()
        asyncio.run(drive())
        assert srv.last_seen["a"] >= before
        assert "pong" in ws.types()

    def test_a_ping_before_hello_is_answered_too(self):
        """Nothing to stamp without a device_id, but the socket is still live
        and the phone still deserves to know it."""
        srv = fresh_server()
        ws = run_phone(srv, [{"type": "ping"}])
        assert ws.types() == ["pong"]
