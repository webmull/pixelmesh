# (c) Adam Davis - adamdavis.co.uk
"""
pixelmesh V2 — Post-show report generator.

Writes a plain-text summary to debug/reports/ on every reset.
Reports are kept indefinitely.
"""

import os
import time
import statistics

_REPORT_DIR = os.path.join(os.path.dirname(__file__), "debug", "reports")


def generate(
    *,
    detected_ids:      set,
    detection_timings: dict,   # blink_id → (elapsed_s, confidence)
    detection_start:   float,  # epoch; 0 = detection never ran this session
    like_count:        int,
    total_connected:   int,    # phones that received a blink_id this session
) -> str:
    """Write report to debug/reports/ and return the file path."""
    os.makedirs(_REPORT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path  = os.path.join(_REPORT_DIR, f"{stamp}.txt")
    text  = _build(
        detected_ids      = detected_ids,
        detection_timings = detection_timings,
        detection_start   = detection_start,
        like_count        = like_count,
        total_connected   = total_connected,
    )
    with open(path, "w") as f:
        f.write(text)
    return path


# ------------------------------------------------------------------ #

def _build(*, detected_ids, detection_timings, detection_start,
           like_count, total_connected) -> str:

    W   = 46
    sep = "═" * W

    lines = [
        sep,
        "   PIXELMESH — SHOW REPORT",
        f"   {time.strftime('%d %b %Y, %H:%M')}",
        sep,
        "",
    ]

    # ---- Audience ----
    n_detected = len(detected_ids)
    n_missed   = max(0, total_connected - n_detected)
    pct        = f"{n_detected / total_connected * 100:.0f}%" if total_connected else "—"

    lines += [
        "AUDIENCE",
        f"  Connected:      {total_connected:>4} phones",
        f"  Detected:       {n_detected:>4}  ({pct})",
        f"  Missed:         {n_missed:>4}",
        "",
    ]

    # ---- Detection ----
    if detection_start and detection_timings:
        elapsed_vals = [t for t, _ in detection_timings.values()]
        median_t     = statistics.median(elapsed_vals)
        fastest_bid  = min(detection_timings, key=lambda b: detection_timings[b][0])
        slowest_bid  = max(detection_timings, key=lambda b: detection_timings[b][0])
        f_t, f_c     = detection_timings[fastest_bid]
        s_t, s_c     = detection_timings[slowest_bid]
        started_str  = time.strftime("%H:%M:%S", time.localtime(detection_start))

        if total_connected and n_detected >= total_connected:
            last_t    = max(elapsed_vals)
            end_epoch = detection_start + last_t
            mins, secs = divmod(int(last_t), 60)
            completed = (f"{time.strftime('%H:%M:%S', time.localtime(end_epoch))}"
                         f"  ({mins}m {secs:02d}s)")
        else:
            completed = "—  (not all found)"

        lines += [
            "DETECTION",
            f"  Started:        {started_str}",
            f"  Completed:      {completed}",
            f"  Median time:    {median_t:.1f}s",
            f"  Fastest:        {f_t:.1f}s  — Phone {fastest_bid + 1}"
            f"  (conf {f_c:.2f})",
            f"  Slowest:        {s_t:.1f}s  — Phone {slowest_bid + 1}"
            f"  (conf {s_c:.2f})",
            "",
        ]

    # ---- Engagement ----
    lines += [
        "ENGAGEMENT",
        f"  Likes:          {like_count:>4}",
        "",
    ]

    lines.append(sep)
    return "\n".join(lines) + "\n"


