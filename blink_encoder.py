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

import numpy as _np

NUM_BITS  = 9        # supports IDs 0-511
PHASE_MS  = 300      # milliseconds per screen phase
NUM_GUARD = 4        # dark guard frames before Manchester data

# Total Manchester bits: start(1) + data(N) + data(N) + end(1) = 2+2N
_MANCHESTER_BITS   = 2 + NUM_BITS * 2          # = 20  (for NUM_BITS=9)
_MANCHESTER_PHASES = _MANCHESTER_BITS * 2      # = 40
CYCLE_LEN = NUM_GUARD + _MANCHESTER_PHASES     # = 44


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
    times:    list[float],
    norm:     list[float],
    threshold: float,
    times_np = None,   # pre-converted numpy arrays (passed from decode_phases_verbose)
    norm_np  = None,   # to avoid 7× redundant conversion per decode call
) -> tuple[int, float, str] | tuple[None, None, str]:
    """
    Time-based Manchester decode at one brightness threshold.

    Uses PHASE_MS as the authoritative phase duration rather than
    estimating from frame counts — immune to variable camera fps.
    """
    binary = [1 if b >= threshold else 0 for b in norm]
    runs   = _run_length(binary)

    phase_secs          = PHASE_MS / 1000.0
    min_guard_secs      = phase_secs * 1        # at least 1 guard phase worth of time
    expected_guard_secs = phase_secs * NUM_GUARD

    best_fail = "no_guard"   # most informative failure seen so far

    # Use pre-converted arrays if provided; otherwise convert here.
    # numpy boolean indexing for window scans releases the GIL and runs
    # ~10–20× faster than equivalent Python list comprehensions.
    if times_np is None:
        times_np = _np.array(times, dtype=_np.float64)
    if norm_np is None:
        norm_np  = _np.array(norm,  dtype=_np.float32)

    # Walk runs, tracking the frame index where each run starts
    frame_pos = 0
    for val, length in runs:
        run_start = frame_pos
        frame_pos += length

        if val != 0 or length < 2:
            continue

        run_end = run_start + length   # exclusive index

        # Guard duration check (time-based).
        # Also accept the guard when sample count >= NUM_GUARD regardless of time
        # span — cameras sometimes deliver bursts of frames in rapid succession
        # (e.g. 5 frames in 110 ms) whose time span is far below min_guard_secs,
        # but the sample count confirms a genuine multi-phase guard period.
        if run_end > len(times):
            break
        guard_secs = times[run_end - 1] - times[run_start]
        if guard_secs < min_guard_secs and length < NUM_GUARD:
            best_fail = f"short_guard={guard_secs:.2f}s"
            continue

        best_fail = "guard_ok_no_bits"

        # Slice the tail once per guard candidate (reused across all t_offsets).
        t_tail = times_np[run_end:]
        n_tail = norm_np[run_end:]

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

                win1 = n_tail[(t_tail >= p1_t0) & (t_tail < p1_t1)]
                win2 = n_tail[(t_tail >= p2_t0) & (t_tail < p2_t1)]

                if len(win1) == 0 or len(win2) == 0:
                    fail  = f"empty_win bit={bit_i}"
                    valid = False
                    break

                p1 = 1 if float(win1.mean()) >= 0.5 else 0
                p2 = 1 if float(win2.mean()) >= 0.5 else 0

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

        # ---- backward scan: pre-guard Manchester data ----
        # When a phone's cycle began before detection started, the guard run
        # ends up near the END of the history window.  The complete previous
        # cycle's Manchester data lies BEFORE the guard — the decoder's
        # forward-only scan misses it entirely (empty_win bit=0).
        #
        # Here we anchor t_manchester from the guard START and scan the
        # pre-guard data.  Leading bits whose windows fall before history[0]
        # are handled using protocol-mandated values:
        #   bit 0  = start marker = 1  (always — safe to assume)
        #   bits 1…NUM_BITS-1 (first copy) = inferred from matching second-copy bits
        # The second copy and end marker must be fully observed.
        _manchester_secs = _MANCHESTER_PHASES * phase_secs  # 12.0 s

        pre_guard_secs = (times[run_start] - times[0]) if run_start > 0 else 0.0
        fwd_secs       = (times[-1] - times[run_end - 1]) if run_end <= len(times) else 0.0

        # Only run when:
        #  1. forward data is short — forward scan cannot succeed
        #  2. enough pre-guard history — second copy of ID is fully visible
        #  3. guard looks like a real 4-phase guard (≥ 0.9 s).  Spurious double-dark
        #     boundaries from adjacent Manchester 0-bits last only ~0.5–0.6 s; anchoring
        #     a backward scan on them produces wrong IDs.
        if (fwd_secs < _manchester_secs * 0.5
                and pre_guard_secs > _manchester_secs * 0.55
                and guard_secs >= expected_guard_secs * 0.75):
            t_head = times_np[:run_start]
            n_head = norm_np[:run_start]

            for t_offset in (-0.75, -0.625, -0.5, -0.375, -0.25, -0.125,
                             0.0,
                             0.125, 0.25, 0.375, 0.5, 0.625, 0.75):
                # Anchor: guard start is (0.5+t_offset) phases after last Manchester sample.
                t_manchester_b = (times[run_start]
                                  - (0.5 + t_offset) * phase_secs
                                  - _manchester_secs)

                bits_b    = []
                n_assumed = 0   # leading bits inferred from protocol
                valid_b   = True
                fail_b    = ""

                for bit_i in range(_MANCHESTER_BITS):
                    p1_t0 = t_manchester_b + bit_i * 2 * phase_secs
                    p1_t1 = p1_t0 + phase_secs
                    p2_t0 = p1_t1
                    p2_t1 = p2_t0 + phase_secs

                    w1 = n_head[(t_head >= p1_t0) & (t_head < p1_t1)]
                    w2 = n_head[(t_head >= p2_t0) & (t_head < p2_t1)]

                    if len(w1) == 0 or len(w2) == 0:
                        # Window falls before history — use protocol knowledge.
                        if bit_i == 0:
                            bits_b.append(1)   # start marker is always 1
                            n_assumed += 1
                            continue
                        if 1 <= bit_i <= NUM_BITS:
                            # First-copy bit — value unknown; infer from second copy later.
                            bits_b.append(None)
                            n_assumed += 1
                            continue
                        # Second copy or end marker is missing — cannot decode.
                        fail_b  = f"back:empty_win bit={bit_i}"
                        valid_b = False
                        break

                    p1_b = 1 if float(w1.mean()) >= 0.5 else 0
                    p2_b = 1 if float(w2.mean()) >= 0.5 else 0

                    if   p1_b == 1 and p2_b == 0: bits_b.append(1)
                    elif p1_b == 0 and p2_b == 1: bits_b.append(0)
                    else:
                        # Start marker is always 1 by protocol — assume rather than fail.
                        if bit_i == 0:
                            bits_b.append(1)
                            n_assumed += 1
                            continue
                        fail_b  = f"back:phase_ambig bit={bit_i}"
                        valid_b = False
                        break

                if not valid_b or len(bits_b) != _MANCHESTER_BITS:
                    if fail_b:
                        best_fail = fail_b
                    continue

                # End marker must be observed (not assumed) and equal 0.
                if bits_b[-1] != 0:
                    best_fail = f"back:bad_end e={bits_b[-1]}"
                    continue
                # Start marker: if observed it must be 1; if assumed it's guaranteed 1.
                if n_assumed == 0 and bits_b[0] != 1:
                    best_fail = f"back:bad_start s={bits_b[0]}"
                    continue

                data1_b = bits_b[1 : 1 + NUM_BITS]
                data2_b = bits_b[1 + NUM_BITS : 1 + NUM_BITS * 2]

                # Second copy must be fully observed (no Nones).
                if any(b is None for b in data2_b):
                    best_fail = "back:incomplete_data2"
                    continue

                # Merge copies: use observed data1 bits; fill missing ones from data2
                # (unobservable ≠ wrong — don't count them as errors).
                errors_b = 0
                resolved_b = []
                for b1, b2 in zip(data1_b, data2_b):
                    if b1 is None:
                        resolved_b.append(b2)
                    elif b1 == b2:
                        resolved_b.append(b1)
                    else:
                        resolved_b.append(b1)   # keep data1 on genuine mismatch
                        errors_b += 1

                if errors_b > 1:
                    best_fail = f"back:copy_mismatch err={errors_b}"
                    continue

                device_id_b = 0
                for b in resolved_b:
                    device_id_b = (device_id_b << 1) | b

                # 5 % confidence penalty per assumed bit; additional 30 % per copy error.
                confidence_b = (min(1.0, guard_secs / expected_guard_secs)
                                * (1.0 - errors_b * 0.3)
                                * (0.95 ** n_assumed))

                return (device_id_b, confidence_b, "")

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

    lo     = min(vals)
    hi_all = max(vals)

    if hi_all - lo < 0.03:
        return None, f"flat_range={hi_all-lo:.3f}"

    # Use the most recent cycle window for `hi` rather than the all-time max.
    # Phone screens auto-dim over time (ambient light sensor), which can drop
    # the bright-phase brightness by 4-5×.  With the old all-time max as `hi`,
    # current bright phases (e.g. 0.22) normalise to 0.22/0.99 = 0.22 — below
    # every decode threshold (min 0.25) — so they appear dark, creating a
    # spurious long "guard" run that breaks the decoder.
    # Using the recent-window max keeps `hi` calibrated to the phone's current
    # brightness.  Old bright values normalise to >1 (still ≥ 0.25) so the
    # binary sequence for older samples is unchanged.
    _one_cycle_s = CYCLE_LEN * PHASE_MS / 1000  # 13.2 s
    if len(times) > 1 and (times[-1] - times[0]) > _one_cycle_s:
        _cutoff     = times[-1] - _one_cycle_s
        _recent_hi  = max(b for t, b in ts_history if t >= _cutoff)
        hi = _recent_hi if _recent_hi > lo + 0.03 else hi_all
    else:
        hi = hi_all

    norm = [(b - lo) / (hi - lo) for b in vals]

    best:        tuple[int, float] | None = None
    best_reason: str = "no_threshold_succeeded"

    # Pre-convert once; _try_decode_at_threshold uses these for numpy window scans.
    times_np = _np.array(times, dtype=_np.float64)
    norm_np  = _np.array(norm,  dtype=_np.float32)

    for threshold in (0.25, 0.35, 0.40, 0.50, 0.60, 0.65, 0.75):
        dev_id, conf, reason = _try_decode_at_threshold(times, norm, threshold,
                                                        times_np=times_np,
                                                        norm_np=norm_np)
        if dev_id is not None:
            if best is None or conf > best[1]:
                best = (dev_id, conf)
            # High-confidence decode — no point trying remaining thresholds.
            # At good signal quality (ISO 624, fixed exposure) threshold 0.25
            # always yields conf ≥ 0.95.  Skipping the other 6 thresholds gives
            # ~7× speedup per call, critical when 300+ phones need simultaneous
            # first-time decoding after the 13.2s warmup expires.
            if best[1] >= 0.95:
                break
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
