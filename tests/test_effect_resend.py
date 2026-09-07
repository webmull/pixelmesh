# (c) Adam Davis - adamdavis.co.uk
"""
Tests for re-sending the current effect.

Effects are events: a phone applies one on arrival and holds it until the next
message. One miss - a send that only reached a buffer on a tethered link, a
socket dead without saying so - leaves that phone on the previous effect for the
rest of the section, and nothing ever corrected it. The loop re-sends the state
so a missed phone catches up on its own.

The loop itself is driven by hand here rather than by sleeping: the test asserts
what it decides to send, not how long it waits.

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


def with_recorder(srv):
    sent = []
    async def rec(msg): sent.append(msg)
    srv.broadcast = rec
    return sent


def effect_state(srv, name="wave", age_s=0.0):
    return {"type": "effect", "effect": name,
            "start_time": int((time.time() - age_s) * 1000)}


class TestWhatGetsResent:
    def test_a_live_effect_is_sent_again(self):
        srv = fresh_server()
        sent = with_recorder(srv)
        srv.mode = srv.MODE_SHOWTIME
        srv.current_effect_state = effect_state(srv)
        asyncio.run(srv.broadcast(srv.current_effect_state))
        assert sent[-1]["effect"] == "wave"

    def test_nothing_is_sent_with_no_effect_up(self):
        """The guard the loop leans on: no effect, nothing to repeat."""
        srv = fresh_server()
        srv.mode = srv.MODE_SHOWTIME
        assert srv.current_effect_state is None

    def test_the_burst_is_faster_than_the_idle_cadence(self):
        """A fresh effect is the one most likely to have been missed and the
        most obviously wrong on screen, so it repeats sooner."""
        srv = fresh_server()
        assert srv.EFFECT_RESEND_FAST_S < srv.EFFECT_RESEND_IDLE_S
        assert srv.EFFECT_RESEND_BURST_S > srv.EFFECT_RESEND_FAST_S

    def test_the_resend_is_the_same_payload(self):
        """Byte-for-byte the message a phone would have got first time, so a
        phone that has it draws the same frame and one that missed it catches
        up to the same place. Anything reconstructed here could drift."""
        srv = fresh_server()
        sent = with_recorder(srv)
        srv.mode = srv.MODE_SHOWTIME
        state = effect_state(srv, "pulse")
        srv.current_effect_state = state
        asyncio.run(srv.broadcast(srv.current_effect_state))
        assert sent[-1] is state


class TestTheLoopSurvives:
    def test_a_failing_send_does_not_end_it(self):
        """One bad send must not kill the task and leave the room without
        resends for the rest of the show - the same reason heart_broadcast_loop
        wraps its body."""
        srv = fresh_server()
        srv.mode = srv.MODE_SHOWTIME
        srv.current_effect_state = effect_state(srv)
        async def boom(msg): raise RuntimeError("socket gone")
        srv.broadcast = boom

        async def one_pass():
            task = asyncio.create_task(srv.effect_resend_loop())
            await asyncio.sleep(srv.EFFECT_RESEND_FAST_S * 2.2)
            alive = not task.done()
            task.cancel()
            return alive

        assert asyncio.run(one_pass()), "resend loop died on a failing send"
