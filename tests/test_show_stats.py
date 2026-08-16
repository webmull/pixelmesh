# (c) Adam Davis - adamdavis.co.uk
"""
Tests for /admin/show_stats - the one public admin route.

It is called directly rather than over HTTP: the handler takes no request
argument, so there is nothing an HTTP client would exercise that this does
not, and it keeps the suite free of an httpx dependency the project does not
otherwise carry.

Run with:  python -m pytest tests/
"""

import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def fresh_server():
    """Re-import server with clean module state for each test."""
    import server
    importlib.reload(server)
    return server


def stats(srv):
    return asyncio.run(srv.show_stats())


# ------------------------------------------------------------------ #
# Shape
# ------------------------------------------------------------------ #

class TestShape:
    def test_all_keys_present(self):
        s = stats(fresh_server())
        assert set(s) == {
            "like_count", "total_connected", "detected",
            "connected_now", "spectators",
            "detecting", "effect", "effect_started",
            "server_stale",
        }

    def test_counts_are_ints_and_flags_are_typed(self):
        s = stats(fresh_server())
        for k in ("like_count", "total_connected", "detected",
                  "connected_now", "spectators"):
            assert isinstance(s[k], int), f"{k} should be an int, got {type(s[k])}"
        assert isinstance(s["detecting"], bool)
        assert s["effect"] is None or isinstance(s["effect"], str)

    def test_everything_is_zero_on_a_fresh_server(self):
        s = stats(fresh_server())
        assert s["like_count"] == 0
        assert s["total_connected"] == 0
        assert s["detected"] == 0
        assert s["connected_now"] == 0
        assert s["spectators"] == 0
        assert s["detecting"] is False
        assert s["effect"] is None
        assert s["effect_started"] is None

    def test_exposes_no_device_identifiers(self):
        """Public route: counts and effect names only, never a device_uuid."""
        srv = fresh_server()
        srv.connections["device-abc"] = object()
        srv.blink_assignments["device-abc"] = 7
        srv.positions["device-abc"] = {"u": 0.5, "v": 0.5}
        assert "device-abc" not in repr(stats(srv))

    def test_route_is_public(self):
        """If it ever leaves _ADMIN_PUBLIC the deck stops reading it."""
        assert "/admin/show_stats" in fresh_server()._ADMIN_PUBLIC


# ------------------------------------------------------------------ #
# Counts
# ------------------------------------------------------------------ #

class TestCounts:
    def test_connected_now_tracks_live_sockets(self):
        srv = fresh_server()
        srv.connections["a"] = object()
        srv.connections["b"] = object()
        assert stats(srv)["connected_now"] == 2

    def test_spectators_counted_separately_from_phones(self):
        srv = fresh_server()
        srv.connections["phone"] = object()
        srv.spectators["stage"] = object()
        s = stats(srv)
        assert s["connected_now"] == 1
        assert s["spectators"] == 1

    def test_total_connected_is_cumulative_not_live(self):
        """The distinction the two keys exist for: a phone that joins and
        then drops leaves total_connected untouched but empties
        connected_now."""
        srv = fresh_server()
        srv.blink_assignments["gone"] = 3
        srv.connections["gone"] = object()
        assert stats(srv)["connected_now"] == 1

        del srv.connections["gone"]                  # screen locked, walked out
        s = stats(srv)
        assert s["connected_now"] == 0
        assert s["total_connected"] == 1

    def test_detected_counts_located_phones(self):
        srv = fresh_server()
        srv.blink_assignments.update({"a": 1, "b": 2})
        srv.positions["a"] = {"u": 0.1, "v": 0.2}
        s = stats(srv)
        assert s["total_connected"] == 2
        assert s["detected"] == 1

    def test_detected_never_exceeds_total_connected_in_practice(self):
        srv = fresh_server()
        for i, d in enumerate(("a", "b", "c")):
            srv.blink_assignments[d] = i
            srv.positions[d] = {"u": 0.0, "v": 0.0}
        s = stats(srv)
        assert s["detected"] <= s["total_connected"]

    def test_like_count_reflects_the_counter(self):
        srv = fresh_server()
        srv.like_count = 160
        assert stats(srv)["like_count"] == 160


# ------------------------------------------------------------------ #
# Show state
# ------------------------------------------------------------------ #

class TestShowState:
    def test_detecting_follows_detection_active(self):
        srv = fresh_server()
        assert stats(srv)["detecting"] is False
        srv.detection_active = True
        assert stats(srv)["detecting"] is True

    def test_effect_is_the_name_not_the_payload(self):
        srv = fresh_server()
        srv.current_effect_state = {
            "type": "effect", "effect": "pulse",
            "start_time": 123, "speed": 0.4, "color_r": 48,
        }
        assert stats(srv)["effect"] == "pulse"

    def test_effect_is_none_when_nothing_is_playing(self):
        srv = fresh_server()
        srv.current_effect_state = None
        assert stats(srv)["effect"] is None

    def test_effect_survives_a_malformed_effect_state(self):
        """(current_effect_state or {}).get(...) must not raise if something
        ever puts a payload here without an effect name."""
        srv = fresh_server()
        srv.current_effect_state = {"type": "effect", "start_time": 1}
        assert stats(srv)["effect"] is None

    def test_effect_started_distinguishes_a_refire(self):
        """The name alone cannot: firing wave twice looks identical without
        a timestamp, and the deck needs to re-flash on the second one."""
        srv = fresh_server()
        asyncio.run(srv.start_effect("wave", {}))
        first = stats(srv)["effect_started"]
        assert isinstance(first, int)
        asyncio.run(srv.start_effect("wave", {}))
        second = stats(srv)["effect_started"]
        assert second >= first
        assert stats(srv)["effect"] == "wave"

    def test_reflects_a_real_fired_effect(self):
        """Goes through start_effect rather than setting the global, so the
        test breaks if the shape of current_effect_state ever changes."""
        srv = fresh_server()
        asyncio.run(srv.start_effect("wave", {"speed": 0.4}))
        s = stats(srv)
        assert s["effect"] == "wave"
        assert srv.mode == srv.MODE_SHOWTIME

    def test_effect_clears_on_reset(self):
        srv = fresh_server()
        asyncio.run(srv.start_effect("sparkle", {}))
        assert stats(srv)["effect"] == "sparkle"
        asyncio.run(srv.reset())
        s = stats(srv)
        assert s["effect"] is None
        assert s["detecting"] is False
        assert s["detected"] == 0


# ------------------------------------------------------------------ #
# Stale process
# ------------------------------------------------------------------ #

class TestStaleFlag:
    def test_a_fresh_process_is_not_stale(self):
        assert stats(fresh_server())["server_stale"] is False

    def test_goes_true_when_the_source_changes(self):
        """server.py is frozen at import - routes, permissions and payload
        shapes do not reload. Editing it mid-show leaves a process that no
        longer matches the files, which has repeatedly looked like a bug in
        code that was already correct. This is the only signal that says so."""
        import os
        srv = fresh_server()
        assert stats(srv)["server_stale"] is False
        srv._SERVER_MTIME_AT_IMPORT = os.path.getmtime(srv.__file__) - 60
        assert stats(srv)["server_stale"] is True

    def test_it_is_a_bool_not_a_timestamp(self):
        """The controller renders this straight into the status bar."""
        assert isinstance(stats(fresh_server())["server_stale"], bool)

    def test_survives_a_missing_file(self):
        """Never let a stat() failure take down the one route the deck polls
        several times a second."""
        srv = fresh_server()
        srv.__file__ = "/nonexistent/server.py"
        assert stats(srv)["server_stale"] is False


# ------------------------------------------------------------------ #
# Compatibility
# ------------------------------------------------------------------ #

class TestBackwardsCompatibility:
    def test_original_three_keys_survive(self):
        """The talk deck's join slide reads total_connected and like_count
        every three seconds. Renaming either silently blanks that slide,
        because the deck hides the counters when a poll fails."""
        s = stats(fresh_server())
        for k in ("like_count", "total_connected", "detected"):
            assert k in s
