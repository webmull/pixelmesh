# (c) Adam Davis - adamdavis.co.uk
"""
Tests for /admin/mode - remote on/off switches for controller-owned things.

The handlers are called directly rather than over HTTP, matching
test_show_stats.py: they take a plain dict and return a plain dict, so there
is nothing an HTTP client would exercise that this does not, and it keeps the
suite free of an httpx dependency the project does not otherwise carry.

The middleware IS exercised over ASGI, because that is the only way to prove
the preflight and token behaviour that makes this route usable from a browser.

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


def get(srv):
    return asyncio.run(srv.get_mode())


def post(srv, payload):
    return asyncio.run(srv.set_mode_request(payload))


def ack(srv, payload):
    return asyncio.run(srv.ack_mode(payload))


# ------------------------------------------------------------------ #
# Shape
# ------------------------------------------------------------------ #

class TestShape:
    def test_supported_modes_out_of_the_box(self):
        """detection and recording are the two the API ships with."""
        assert set(fresh_server().MODES) == {"detection", "recording"}

    def test_get_reports_every_supported_mode(self):
        srv = fresh_server()
        snap = get(srv)
        assert set(snap["modes"]) == set(srv.MODES)
        assert snap["supported"] == list(srv.MODES)

    def test_fresh_server_has_nothing_requested(self):
        """seq 0 is what lets the controller adopt state on connect without
        firing it. If a fresh server ever reported seq > 0, every controller
        launch would apply a request nobody made."""
        for entry in get(fresh_server())["modes"].values():
            assert entry["seq"] == 0
            assert entry["enabled"] is False
            assert entry["actual"] is None

    def test_entry_shape(self):
        srv = fresh_server()
        post(srv, {"detection": True})
        entry = get(srv)["modes"]["detection"]
        assert set(entry) == {"enabled", "seq", "actual"}


# ------------------------------------------------------------------ #
# Setting modes
# ------------------------------------------------------------------ #

class TestSetting:
    def test_enable_detection(self):
        srv = fresh_server()
        snap = post(srv, {"detection": True})
        assert snap["modes"]["detection"]["enabled"] is True
        assert snap["modes"]["detection"]["seq"] == 1

    def test_disable_recording(self):
        srv = fresh_server()
        post(srv, {"recording": True})
        snap = post(srv, {"recording": False})
        assert snap["modes"]["recording"]["enabled"] is False
        assert snap["modes"]["recording"]["seq"] == 2

    def test_several_modes_in_one_call(self):
        srv = fresh_server()
        snap = post(srv, {"detection": True, "recording": True})
        assert snap["modes"]["detection"]["enabled"] is True
        assert snap["modes"]["recording"]["enabled"] is True

    def test_untouched_mode_keeps_its_seq(self):
        """Setting detection must not make the controller re-apply recording."""
        srv = fresh_server()
        post(srv, {"recording": True})
        before = get(srv)["modes"]["recording"]["seq"]
        post(srv, {"detection": True})
        assert get(srv)["modes"]["recording"]["seq"] == before

    def test_seq_bumps_even_when_value_is_unchanged(self):
        """The operator can stop a recording locally with the pedal. Asking
        for it back must reach the controller, and seq is the only thing that
        carries that - so a repeat request is not a no-op."""
        srv = fresh_server()
        post(srv, {"recording": True})
        snap = post(srv, {"recording": True})
        assert snap["modes"]["recording"]["seq"] == 2

    def test_seq_only_ever_increases(self):
        srv = fresh_server()
        seqs = [post(srv, {"detection": b})["modes"]["detection"]["seq"]
                for b in (True, False, True, False)]
        assert seqs == sorted(seqs) == [1, 2, 3, 4]


# ------------------------------------------------------------------ #
# Rejection
# ------------------------------------------------------------------ #

class TestRejection:
    def test_unknown_mode_rejected(self):
        from fastapi import HTTPException
        srv = fresh_server()
        with pytest.raises(HTTPException) as e:
            post(srv, {"detektion": True})
        assert e.value.status_code == 400
        assert "detektion" in e.value.detail

    def test_unknown_mode_rejects_the_whole_request(self):
        """Partial application would leave the caller unsure what landed."""
        srv = fresh_server()
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            post(srv, {"detection": True, "nonsense": True})
        assert get(srv)["modes"]["detection"]["seq"] == 0

    @pytest.mark.parametrize("val", ["true", 1, 0, None, "on", []])
    def test_non_boolean_rejected(self, val):
        """A string is far more likely a caller bug than an intent to enable."""
        from fastapi import HTTPException
        srv = fresh_server()
        with pytest.raises(HTTPException) as e:
            post(srv, {"detection": val})
        assert e.value.status_code == 400

    def test_empty_body_rejected(self):
        from fastapi import HTTPException
        srv = fresh_server()
        with pytest.raises(HTTPException):
            post(srv, {})

    def test_error_names_the_supported_modes(self):
        """A 400 should tell the caller what they could have said."""
        from fastapi import HTTPException
        srv = fresh_server()
        with pytest.raises(HTTPException) as e:
            post(srv, {"nope": True})
        assert "detection" in e.value.detail and "recording" in e.value.detail


# ------------------------------------------------------------------ #
# Acknowledgement - what is actually true vs what was asked for
# ------------------------------------------------------------------ #

class TestAck:
    def test_ack_records_actual_state(self):
        srv = fresh_server()
        ack(srv, {"recording": True})
        assert get(srv)["modes"]["recording"]["actual"] is True

    def test_actual_can_disagree_with_enabled(self):
        """The point of the field: recording was requested but there is no
        camera, so the controller reports back that it did not start."""
        srv = fresh_server()
        post(srv, {"recording": True})
        ack(srv, {"recording": False})
        entry = get(srv)["modes"]["recording"]
        assert entry["enabled"] is True and entry["actual"] is False

    def test_ack_does_not_bump_seq(self):
        """If it did, the controller would re-apply its own report forever."""
        srv = fresh_server()
        post(srv, {"detection": True})
        ack(srv, {"detection": True})
        assert get(srv)["modes"]["detection"]["seq"] == 1

    def test_ack_ignores_unknown_and_non_boolean(self):
        srv = fresh_server()
        ack(srv, {"bogus": True, "detection": "yes"})
        snap = get(srv)["modes"]
        assert "bogus" not in snap
        assert snap["detection"]["actual"] is None

    def test_ack_tolerates_empty_payload(self):
        srv = fresh_server()
        assert ack(srv, {})["modes"]["detection"]["actual"] is None


# ------------------------------------------------------------------ #
# Reset
# ------------------------------------------------------------------ #

class TestReset:
    def test_reset_does_not_zero_the_seq_counters(self):
        """The controller keeps its own cursor in memory. Zeroing the server's
        would leave it ahead, so every later request would look stale and
        silently do nothing."""
        srv = fresh_server()
        post(srv, {"detection": True, "recording": True})
        asyncio.run(srv.reset())
        for entry in get(srv)["modes"].values():
            assert entry["seq"] == 1


# ------------------------------------------------------------------ #
# Access control and CORS
#
# Over ASGI, because the middleware is the thing under test.
# ------------------------------------------------------------------ #

def call(srv, method, path, headers=None):
    """Drive one request through the full middleware stack."""
    received = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(msg):
        received.append(msg)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "scheme": "http",
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
        "headers": [(k.lower().encode(), v.encode())
                    for k, v in (headers or {}).items()],
    }
    asyncio.run(srv.app(scope, receive, send))
    start = next(m for m in received if m["type"] == "http.response.start")
    return start["status"], {k.decode().lower(): v.decode()
                             for k, v in start["headers"]}


class TestAccessControl:
    def test_mode_is_not_public(self):
        """It can stop detection mid-show, so it must stay authenticated.
        show_stats is read-only and public; this is neither."""
        srv = fresh_server()
        assert "/admin/mode" not in srv._ADMIN_PUBLIC

    def test_mode_is_cors_enabled(self):
        assert "/admin/mode" in fresh_server()._ADMIN_CORS

    def test_public_routes_stay_cors_enabled(self):
        srv = fresh_server()
        assert srv._ADMIN_PUBLIC <= srv._ADMIN_CORS

    def test_no_token_is_rejected(self):
        status, _ = call(fresh_server(), "POST", "/admin/mode")
        assert status == 403

    def test_wrong_token_is_rejected(self):
        status, _ = call(fresh_server(), "POST", "/admin/mode",
                         {"X-Admin-Token": "not-the-token"})
        assert status == 403

    def test_correct_token_is_accepted(self):
        srv = fresh_server()
        status, _ = call(srv, "GET", "/admin/mode",
                         {"X-Admin-Token": srv._ADMIN_TOKEN})
        assert status == 200

    def test_403_still_carries_cors_headers(self):
        """Without them the browser reports an opaque CORS failure and hides
        the status, so a caller cannot tell a bad token from a broken server."""
        _, headers = call(fresh_server(), "POST", "/admin/mode")
        assert headers.get("access-control-allow-origin") == "*"

    def test_ack_is_not_reachable_from_a_browser(self):
        """Nothing in a browser should be able to claim to be the controller."""
        srv = fresh_server()
        assert "/admin/mode/ack" not in srv._ADMIN_CORS


class TestPreflight:
    def test_preflight_succeeds_without_a_token(self):
        """A CORS preflight carries no credentials by specification. If the
        token check ran first, every browser call would fail at the OPTIONS
        probe and the route would be unusable cross-origin."""
        status, _ = call(fresh_server(), "OPTIONS", "/admin/mode")
        assert status == 204

    def test_preflight_allows_the_token_header(self):
        """Chrome will not send X-Admin-Token unless it is named here."""
        _, headers = call(fresh_server(), "OPTIONS", "/admin/mode")
        allowed = headers["access-control-allow-headers"].lower()
        assert "x-admin-token" in allowed
        assert "content-type" in allowed

    def test_preflight_allows_post(self):
        _, headers = call(fresh_server(), "OPTIONS", "/admin/mode")
        assert "POST" in headers["access-control-allow-methods"]

    def test_preflight_on_a_non_cors_admin_route_is_not_waved_through(self):
        srv = fresh_server()
        status, _ = call(srv, "OPTIONS", "/admin/reset")
        assert status == 403

    def test_successful_response_carries_cors_headers(self):
        srv = fresh_server()
        _, headers = call(srv, "GET", "/admin/mode",
                          {"X-Admin-Token": srv._ADMIN_TOKEN})
        assert headers.get("access-control-allow-origin") == "*"


class TestShowStatsUnaffected:
    """The deck polls show_stats every few seconds. The middleware rewrite
    must not have changed it."""

    def test_still_public(self):
        status, _ = call(fresh_server(), "GET", "/admin/show_stats")
        assert status == 200

    def test_still_cross_origin_readable(self):
        _, headers = call(fresh_server(), "GET", "/admin/show_stats")
        assert headers.get("access-control-allow-origin") == "*"
