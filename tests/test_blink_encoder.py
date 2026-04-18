# (c) Adam Davis — adamdavis.co.uk
"""
Tests for blink_encoder.py — encode/decode round-trips and structural invariants.

Run with:  python -m pytest tests/
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from blink_encoder import (
    encode_id, decode_phases,
    NUM_BITS, NUM_GUARD, PHASE_MS, CYCLE_LEN,
    _MANCHESTER_BITS, _MANCHESTER_PHASES,
)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def simulate_camera(phases: list[int], fps: float = 11.0, cycles: int = 2,
                    bright: float = 0.9, dark: float = 0.05,
                    noise: float = 0.02, t0: float = 0.0) -> list[tuple[float, float]]:
    """
    Simulate a camera observing a phone blinking `phases` for `cycles` full cycles.
    Returns a list of (timestamp, brightness) as decode_phases expects.
    """
    import random
    rng = random.Random(42)
    history = []
    frame_interval = 1.0 / fps
    total_phases = phases * cycles
    cycle_duration = len(phases) * PHASE_MS / 1000.0
    total_duration = cycle_duration * cycles
    t = t0
    while t < t0 + total_duration:
        elapsed = t - t0
        phase_idx = int(elapsed / (PHASE_MS / 1000.0)) % len(phases)
        b = bright if total_phases[phase_idx % len(phases)] == 1 else dark
        b += rng.uniform(-noise, noise)
        b = max(0.0, min(1.0, b))
        history.append((t, b))
        t += frame_interval
    return history


def simulate_camera_clean(phases: list[int], fps: float = 11.0, cycles: int = 2,
                           t0: float = 0.0) -> list[tuple[float, float]]:
    """Noiseless simulation for structural tests."""
    return simulate_camera(phases, fps=fps, cycles=cycles,
                           bright=1.0, dark=0.0, noise=0.0, t0=t0)


# ------------------------------------------------------------------ #
# Structural invariants
# ------------------------------------------------------------------ #

class TestStructure:
    def test_cycle_len_formula(self):
        assert CYCLE_LEN == NUM_GUARD + _MANCHESTER_PHASES

    def test_manchester_phases_formula(self):
        assert _MANCHESTER_PHASES == _MANCHESTER_BITS * 2

    def test_manchester_bits_formula(self):
        assert _MANCHESTER_BITS == 2 + NUM_BITS * 2

    def test_encode_returns_correct_length(self):
        for device_id in (0, 1, 255, 511):
            if device_id < 2 ** NUM_BITS:
                phases = encode_id(device_id)
                assert len(phases) == CYCLE_LEN, \
                    f"ID {device_id}: expected {CYCLE_LEN} phases, got {len(phases)}"

    def test_guard_phases_are_dark(self):
        for device_id in (0, 1, 2 ** NUM_BITS - 1):
            phases = encode_id(device_id)
            for i in range(NUM_GUARD):
                assert phases[i] == 0, f"Guard phase {i} should be dark for ID {device_id}"

    def test_phases_are_binary(self):
        phases = encode_id(0)
        assert all(p in (0, 1) for p in phases)

    def test_manchester_data_no_two_consecutive_same(self):
        """Manchester encoding guarantees a transition every bit — no 3 identical phases in a row."""
        for device_id in (0, 1, 127, 2 ** NUM_BITS - 1):
            phases = encode_id(device_id)
            data = phases[NUM_GUARD:]
            for i in range(len(data) - 2):
                assert not (data[i] == data[i+1] == data[i+2]), \
                    f"Three consecutive {data[i]}s at index {i} for ID {device_id}"

    def test_invalid_id_raises(self):
        with pytest.raises(ValueError):
            encode_id(2 ** NUM_BITS)

    def test_negative_id_raises(self):
        with pytest.raises(ValueError):
            encode_id(-1)


# ------------------------------------------------------------------ #
# Round-trip: encode → simulate → decode
# ------------------------------------------------------------------ #

class TestRoundTrip:
    @pytest.mark.parametrize("device_id", [0, 1, 5, 42, 127, 255, 300, 511])
    def test_roundtrip_boundary_ids(self, device_id):
        if device_id >= 2 ** NUM_BITS:
            pytest.skip(f"ID {device_id} out of range for NUM_BITS={NUM_BITS}")
        phases = encode_id(device_id)
        history = simulate_camera(phases, fps=11.0, cycles=3)
        result = decode_phases(history)
        assert result is not None, f"Failed to decode ID {device_id}"
        decoded_id, confidence = result
        assert decoded_id == device_id, \
            f"Decoded {decoded_id}, expected {device_id}"
        assert 0.0 < confidence <= 1.0

    @pytest.mark.parametrize("fps", [10.0, 11.0, 15.0, 24.0, 30.0])
    def test_roundtrip_various_fps(self, fps):
        device_id = 42
        phases = encode_id(device_id)
        history = simulate_camera(phases, fps=fps, cycles=3)
        result = decode_phases(history)
        assert result is not None, f"Failed to decode at {fps}fps"
        assert result[0] == device_id

    def test_roundtrip_with_noise(self):
        """Decoder should handle realistic camera noise."""
        device_id = 17
        phases = encode_id(device_id)
        history = simulate_camera(phases, fps=11.0, cycles=3, noise=0.05)
        result = decode_phases(history)
        assert result is not None
        assert result[0] == device_id

    def test_no_decode_with_insufficient_history(self):
        """Fewer frames than CYCLE_LEN should return None."""
        phases = encode_id(7)
        history = simulate_camera_clean(phases, fps=11.0, cycles=1)
        short = history[:CYCLE_LEN - 1]
        assert decode_phases(short) is None

    def test_no_decode_with_flat_signal(self):
        """Constant brightness (no blink) should return None."""
        history = [(float(i) / 11.0, 0.5) for i in range(CYCLE_LEN * 2)]
        assert decode_phases(history) is None

    def test_all_ids_roundtrip(self):
        """Every valid ID must encode and decode correctly."""
        failures = []
        for device_id in range(2 ** NUM_BITS):
            phases = encode_id(device_id)
            history = simulate_camera(phases, fps=11.0, cycles=3)
            result = decode_phases(history)
            if result is None or result[0] != device_id:
                got = result[0] if result else None
                failures.append(f"ID {device_id} → decoded {got}")
        assert not failures, f"{len(failures)} IDs failed:
" + "
".join(failures[:20])
