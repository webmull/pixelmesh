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
  For numBits=9: 4 + 40 = 44 phases × 300ms each = 13.2s

Decoder is time-based: uses actual frame timestamps and PHASE_MS as the
ground truth for phase boundaries, so it works correctly at any camera fps.
"""

NUM_BITS  = 9        # supports IDs 0-511
PHASE_MS  = 300      # milliseconds per screen phase
NUM_GUARD = 4        # dark guard frames before Manchester data

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
) -> tuple[int, float, str] | tuple[None, None, str]:
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

    best_fail = "no_guard"   # most informative failure seen so far

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
            best_fail = f"short_guard={guard_secs:.2f}s"
            continue

        best_fail = "guard_ok_no_bits"

        # Estimate when the Manchester data starts.
        # Anchor from the END of the dark run (times[run_end-1]) rather than
        # the start: phones arrive mid-cycle so we often only see the TAIL of
        # the guard, making run_start unreliable.  The last guard frame is
        # always ~0.5 phase before Manchester begins.
        for t_offset in (-0.75, -0.625, -0.5, -0.375, -0.25, -0.125,
                         0.0,
                         0.125, 0.25, 0.375, 0.5, 0.625, 0.75):
            t_manchester = times[run_end - 1] + (0.5 + t_offset) * phase_secs

            bits  = []
            valid = True
            fail  = ""
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
                    fail  = f"empty_win bit={bit_i}"
                    valid = False
                    break

                p1 = 1 if sum(win1) / len(win1) >= 0.5 else 0
                p2 = 1 if sum(win2) / len(win2) >= 0.5 else 0

                if   p1 == 1 and p2 == 0:
                    bits.append(1)
                elif p1 == 0 and p2 == 1:
                    bits.append(0)
                else:
                    fail  = f"phase_ambig bit={bit_i} p1={p1} p2={p2}"
                    valid = False
                    break

            if not valid or len(bits) != _MANCHESTER_BITS:
                if fail:
                    best_fail = fail
                continue

            if bits[0] != 1 or bits[-1] != 0:
                best_fail = f"bad_markers s={bits[0]} e={bits[-1]}"
                continue

            data1 = bits[1 : 1 + NUM_BITS]
            data2 = bits[1 + NUM_BITS : 1 + NUM_BITS * 2]

            # Count bit errors between the two copies of the ID.
            # Allow 1-bit tolerance for noisy/compressed signals (far away,
            # bright ambient) — majority-vote between data1 and data2.
            errors = sum(a != b for a, b in zip(data1, data2))
            if errors > 1:
                best_fail = f"copy_mismatch err={errors}"
                continue

            # Resolve each bit: if copies agree use that value; if they
            # disagree take data1 (arbitrary — only 1 error allowed so the
            # correct bit is unknown, but confidence will be penalised).
            resolved = [a if a == b else a for a, b in zip(data1, data2)]

            device_id = 0
            for b in resolved:
                device_id = (device_id << 1) | b

            # Penalise confidence for each bit error so high-noise decodes
            # lose out to clean ones when multiple guards are found.
            confidence = min(1.0, guard_secs / expected_guard_secs) * (1.0 - errors * 0.3)
            return (device_id, confidence, "")

    return (None, None, best_fail)


def decode_phases(
    ts_history: list[tuple[float, float]],
) -> tuple[int, float] | None:
    """
    Decode a device_id from a timestamped brightness history.
    ts_history: list of (timestamp_seconds, brightness_0_to_1)
    Returns (device_id, confidence) or None.
    """
    result, reason = decode_phases_verbose(ts_history)
    return result


def decode_phases_verbose(
    ts_history: list[tuple[float, float]],
) -> tuple[tuple[int, float] | None, str]:
    """
    Like decode_phases but also returns a human-readable failure reason.
    Returns ((device_id, confidence), "") on success or (None, reason) on failure.
    """
    if len(ts_history) < CYCLE_LEN:
        return None, f"short_hist={len(ts_history)}<{CYCLE_LEN}"

    times = [t for t, _ in ts_history]
    vals  = [b for _, b in ts_history]

    lo = min(vals)
    hi = max(vals)
    if hi - lo < 0.03:
        return None, f"flat_range={hi-lo:.3f}"

    norm = [(b - lo) / (hi - lo) for b in vals]

    best:        tuple[int, float] | None = None
    best_reason: str = "no_threshold_succeeded"

    for threshold in (0.25, 0.35, 0.40, 0.50, 0.60, 0.65, 0.75):
        dev_id, conf, reason = _try_decode_at_threshold(times, norm, threshold)
        if dev_id is not None:
            if best is None or conf > best[1]:
                best = (dev_id, conf)
        else:
            # Keep the most informative failure (guard_ok > short_guard > no_guard)
            priority = {"guard_ok_no_bits": 3, "phase_ambig": 2,
                        "empty_win": 2, "copy_mismatch": 2, "bad_markers": 2}
            cur_p  = next((v for k, v in priority.items() if reason.startswith(k)), 1)
            best_p = next((v for k, v in priority.items() if best_reason.startswith(k)), 0)
            if cur_p > best_p:
                best_reason = f"t={threshold:.2f}:{reason}"

    if best is not None:
        return best, ""
    return None, best_reason
