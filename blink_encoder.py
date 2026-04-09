"""
PixelMesh V2 — Blink Encoding / Decoding
Closely follows the PixelPhones approach (Seb Lee-Delisle).

Signal structure per cycle:
  [6 dark guard frames]  — gap/sync marker (long dark run)
  Manchester([start=1] + [numBits ID] + [numBits ID] + [end=0])

  Each Manchester bit = 2 screen phases:
    bit 1 → [bright, dark]
    bit 0 → [dark,  bright]

  Total phases = 6 + (1 + numBits + numBits + 1) × 2
               = 6 + (2 + numBits×2)×2
  For numBits=8: 6 + 36 = 42 phases × PHASE_MS ms each

Decoder is time-based: uses actual frame timestamps and PHASE_MS as the
ground truth for phase boundaries, so it works correctly at any camera fps.
"""

NUM_BITS  = 8        # supports IDs 0-255
PHASE_MS  = 450      # milliseconds per screen phase
NUM_GUARD = 6        # dark guard frames before Manchester data

# Total Manchester bits: start(1) + data(N) + data(N) + end(1) = 2+2N
_MANCHESTER_BITS   = 2 + NUM_BITS * 2          # = 12
_MANCHESTER_PHASES = _MANCHESTER_BITS * 2      # = 24
CYCLE_LEN = NUM_GUARD + _MANCHESTER_PHASES     # = 30


# ------------------------------------------------------------------ #
# Encoding
# ------------------------------------------------------------------ #

def encode_id(device_id: int) -> list[int]:
    """
    Return a list of screen phases (1=bright, 0=dark) for one full cycle.
    """
    if not (0 <= device_id < 2 ** NUM_BITS):
        raise ValueError(f"device_id must be 0–{2**NUM_BITS - 1}, got {device_id}")

    # Build binary string: start + data + data + end
    bits = format(device_id, f'0{NUM_BITS}b')          # e.g. "00101" for ID=5
    binary_str = '1' + bits + bits + '0'               # e.g. "1001010010"

    # Manchester encode: '1'→[1,0], '0'→[0,1]
    phases = [0] * NUM_GUARD                            # dark guard
    for ch in binary_str:
        if ch == '1':
            phases.extend([1, 0])
        else:
            phases.extend([0, 1])

    return phases   # length = CYCLE_LEN


# ------------------------------------------------------------------ #
# Decoding — time-based, fps-independent
# ------------------------------------------------------------------ #

def _run_length(binary: list[int]) -> list[tuple[int, int]]:
    """Run-length encode a binary list → [(value, length), ...]"""
    if not binary:
        return []
    runs = []
    cur_val, cur_len = binary[0], 1
    for b in binary[1:]:
        if b == cur_val:
            cur_len += 1
        else:
            runs.append((cur_val, cur_len))
            cur_val, cur_len = b, 1
    runs.append((cur_val, cur_len))
    return runs


def _try_decode_at_threshold(
    times: list[float],
    norm:  list[float],
    threshold: float,
) -> tuple[int, float] | None:
    """
    Time-based Manchester decode at one brightness threshold.

    Uses PHASE_MS as the authoritative phase duration rather than
    estimating from frame counts — immune to variable camera fps.
    """
    binary = [1 if b >= threshold else 0 for b in norm]
    runs   = _run_length(binary)

    phase_secs          = PHASE_MS / 1000.0
    min_guard_secs      = phase_secs * 2        # at least 2 guard phases
    expected_guard_secs = phase_secs * NUM_GUARD

    # Walk runs, tracking the frame index where each run starts
    frame_pos = 0
    for val, length in runs:
        run_start = frame_pos
        frame_pos += length

        if val != 0 or length < 2:
            continue

        run_end = run_start + length   # exclusive index

        # Guard duration check (time-based)
        if run_end > len(times):
            break
        guard_secs = times[run_end - 1] - times[run_start]
        if guard_secs < min_guard_secs:
            continue

        # Estimate when the Manchester data starts.
        # Anchor from the END of the dark run (times[run_end-1]) rather than
        # the start: phones arrive mid-cycle so we often only see the TAIL of
        # the guard, making run_start unreliable.  The last guard frame is
        # always ~0.5 phase before Manchester begins.
        for t_offset in (-0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75):
            t_manchester = times[run_end - 1] + (0.5 + t_offset) * phase_secs

            bits  = []
            valid = True
            for bit_i in range(_MANCHESTER_BITS):
                p1_t0 = t_manchester + bit_i * 2 * phase_secs
                p1_t1 = p1_t0 + phase_secs
                p2_t0 = p1_t1
                p2_t1 = p2_t0 + phase_secs

                win1 = [norm[i] for i in range(run_end, len(times))
                        if p1_t0 <= times[i] < p1_t1]
                win2 = [norm[i] for i in range(run_end, len(times))
                        if p2_t0 <= times[i] < p2_t1]

                if not win1 or not win2:
                    valid = False
                    break

                p1 = 1 if sum(win1) / len(win1) >= 0.5 else 0
                p2 = 1 if sum(win2) / len(win2) >= 0.5 else 0

                if   p1 == 1 and p2 == 0:
                    bits.append(1)
                elif p1 == 0 and p2 == 1:
                    bits.append(0)
                else:
                    valid = False
                    break

            if not valid or len(bits) != _MANCHESTER_BITS:
                continue

            if bits[0] != 1 or bits[-1] != 0:
                continue

            data1 = bits[1 : 1 + NUM_BITS]
            data2 = bits[1 + NUM_BITS : 1 + NUM_BITS * 2]
            if data1 != data2:
                continue

            device_id = 0
            for b in data1:
                device_id = (device_id << 1) | b

            confidence = min(1.0, guard_secs / expected_guard_secs)
            return (device_id, confidence)

    return None


def decode_phases(
    ts_history: list[tuple[float, float]],
) -> tuple[int, float] | None:
    """
    Decode a device_id from a timestamped brightness history.
    ts_history: list of (timestamp_seconds, brightness_0_to_1)
    Returns (device_id, confidence) or None.
    """
    if len(ts_history) < CYCLE_LEN:
        return None

    times = [t for t, _ in ts_history]
    vals  = [b for _, b in ts_history]

    lo = min(vals)
    hi = max(vals)
    if hi - lo < 0.08:
        return None

    norm = [(b - lo) / (hi - lo) for b in vals]

    best: tuple[int, float] | None = None

    for threshold in (0.25, 0.40, 0.50, 0.60, 0.75):
        result = _try_decode_at_threshold(times, norm, threshold)
        if result is not None:
            if best is None or result[1] > best[1]:
                best = result

    return best
