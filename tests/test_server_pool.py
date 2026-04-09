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


class TestBlinkToDevice:
    def test_returns_none_for_unknown_id(self):
        srv = fresh_server()
        assert srv.blink_to_device(99) is None

    def test_reverse_lookup(self):
        srv = fresh_server()
        srv.blink_assignments["device-abc"] = 7
        assert srv.blink_to_device(7) == "device-abc"

    def test_reverse_lookup_multiple_devices(self):
        srv = fresh_server()
        srv.blink_assignments["dev-1"] = 1
        srv.blink_assignments["dev-2"] = 2
        srv.blink_assignments["dev-3"] = 3
        assert srv.blink_to_device(1) == "dev-1"
        assert srv.blink_to_device(2) == "dev-2"
        assert srv.blink_to_device(3) == "dev-3"
        assert srv.blink_to_device(99) is None

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
