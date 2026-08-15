# (c) Adam Davis - adamdavis.co.uk
"""
Tests for server.py — blink ID pool management and reverse lookup.

Run with:  python -m pytest tests/
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import importlib


def fresh_server():
    """Re-import server with clean module state for each test."""
    import server
    importlib.reload(server)
    return server


class TestBlinkPool:
    def test_pool_starts_full(self):
        srv = fresh_server()
        from blink_encoder import NUM_BITS
        assert len(srv.available_blinks) == 2 ** NUM_BITS

    def test_pool_contains_no_duplicates(self):
        srv = fresh_server()
        assert len(srv.available_blinks) == len(set(srv.available_blinks))

    def test_pool_range(self):
        srv = fresh_server()
        from blink_encoder import NUM_BITS
        assert set(srv.available_blinks) == set(range(2 ** NUM_BITS))

    def test_assigned_ids_are_unique(self):
        """Simulates assigning IDs to N devices — no duplicates."""
        srv = fresh_server()
        assigned = set()
        for _ in range(20):
            assert srv.available_blinks, "Pool exhausted unexpectedly"
            bid = srv.available_blinks.pop(0)
            assert bid not in assigned, f"Duplicate blink_id {bid} assigned"
            assigned.add(bid)

    def test_released_id_returns_to_pool(self):
        srv = fresh_server()
        bid = srv.available_blinks.pop(0)
        assert bid not in srv.available_blinks
        srv.available_blinks.append(bid)
        assert bid in srv.available_blinks


def _assign(srv, device: str, blink_id: int):
    """Assign a blink_id the way the socket handler does.

    blink_to_device used to scan blink_assignments; it now reads the
    blink_reverse index that is written alongside it (server.py:417). Tests
    that only wrote the forward dict were asserting against an index nothing
    had populated, which is why they failed rather than the code being wrong.
    """
    srv.blink_assignments[device] = blink_id
    srv.blink_reverse[blink_id] = device


class TestBlinkToDevice:
    def test_returns_none_for_unknown_id(self):
        srv = fresh_server()
        assert srv.blink_to_device(99) is None

    def test_reverse_lookup(self):
        srv = fresh_server()
        _assign(srv, "device-abc", 7)
        assert srv.blink_to_device(7) == "device-abc"

    def test_reverse_lookup_multiple_devices(self):
        srv = fresh_server()
        for i in (1, 2, 3):
            _assign(srv, f"dev-{i}", i)
        assert srv.blink_to_device(1) == "dev-1"
        assert srv.blink_to_device(2) == "dev-2"
        assert srv.blink_to_device(3) == "dev-3"
        assert srv.blink_to_device(99) is None

    def test_forward_and_reverse_stay_consistent(self):
        """The two dicts are maintained by hand, so they can drift."""
        srv = fresh_server()
        for i in (1, 2, 3):
            _assign(srv, f"dev-{i}", i)
        for device, bid in srv.blink_assignments.items():
            assert srv.blink_reverse[bid] == device

    def test_pool_and_assignments_are_disjoint(self):
        """IDs in use should not also be in the available pool."""
        srv = fresh_server()
        for i in range(5):
            bid = srv.available_blinks.pop(0)
            srv.blink_assignments[f"dev-{i}"] = bid
        assigned_ids = set(srv.blink_assignments.values())
        pool_ids     = set(srv.available_blinks)
        assert assigned_ids.isdisjoint(pool_ids), \
            f"Overlap between assigned and pool: {assigned_ids & pool_ids}"
