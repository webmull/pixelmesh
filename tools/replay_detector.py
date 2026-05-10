"""Replay a recorded video through BlinkDetector with optional cfg overrides
to test whether smaller grid_step / sample_radius would catch phones the
original run missed. Bypasses the controller's connected-client filter so
every decoded ID is reported, regardless of whether a real phone owned it
during the original session.

Usage:
    python3 tools/replay_detector.py debug.mp4
    python3 tools/replay_detector.py debug.mp4 --grid 4 --radius 3
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from blink_detector import BlinkDetector  # noqa: E402


def replay(video_path: Path, grid_step: int, sample_radius: int,
           label: str) -> dict:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise SystemExit(f"could not open {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    detector = BlinkDetector()
    detector.cfg["grid_step"]       = grid_step
    detector.cfg["sample_radius"]   = sample_radius

    base_ts = time.time()
    first_decode_ts: dict[int, float] = {}     # blink_id -> time-since-start
    all_decodes:    list[tuple[float, int, int, int, float]] = []  # (t, bid, px, py, conf)

    print(f"\n=== {label}: grid_step={grid_step} sample_radius={sample_radius} ===")
    print(f"video: {video_path.name}  fps={fps:.1f}  frames={n_frames}")

    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        ts = base_ts + frame_idx / fps
        results, _ = detector.process_frame(frame, ts=ts, need_debug=False)
        for det in results:
            bid = int(det.blink_id)
            elapsed = ts - base_ts
            all_decodes.append((elapsed, bid, int(det.cx_px), int(det.cy_px),
                                float(det.confidence)))
            if bid not in first_decode_ts:
                first_decode_ts[bid] = elapsed
                print(f"  t={elapsed:6.2f}s  bid={bid:>3}  "
                      f"({int(det.cx_px):>4}, {int(det.cy_px):>4})  "
                      f"conf={det.confidence:.2f}")
        frame_idx += 1
        if frame_idx % int(fps * 5) == 0:
            stds = [pt.recent_std for pt in detector._points
                    if hasattr(pt, "recent_std")]
            above = sum(1 for s in stds if s >= detector.cfg.get("min_recent_std", 0.05))
            mx = max(stds) if stds else 0.0
            # Look at one above-gate point's history span and decode reason
            longest_hist = 0
            sample_reason = ""
            for pt in detector._points:
                if hasattr(pt, "history") and pt.history:
                    span = pt.history[-1][0] - pt.history[0][0]
                    if span > longest_hist:
                        longest_hist = span
                        sample_reason = getattr(pt, "decode_fail_reason", "")
            print(f"\n  t={frame_idx / fps:5.1f}s "
                  f"above_gate={above}  max_std={mx:.3f}  "
                  f"gate={detector.cfg.get('min_recent_std', 0.05):.3f}  "
                  f"longest_hist={longest_hist:.1f}s  "
                  f"reason={sample_reason!r}  "
                  f"decoded={len(first_decode_ts)}")

    print(f"\n  total decodes: {len(all_decodes)}, unique IDs: {len(first_decode_ts)}")
    cap.release()
    return {
        "first_decode_ts": first_decode_ts,
        "all_decodes":     all_decodes,
        "label":           label,
    }


def compare(baseline: dict, experimental: dict):
    base_ids = set(baseline["first_decode_ts"])
    exp_ids  = set(experimental["first_decode_ts"])

    print("\n" + "=" * 60)
    print("COMPARISON")
    print("=" * 60)
    print(f"Baseline   ({baseline['label']}):     "
          f"{len(base_ids)} unique IDs")
    print(f"Experimental ({experimental['label']}): "
          f"{len(exp_ids)} unique IDs")

    only_exp = exp_ids - base_ids
    only_base = base_ids - exp_ids
    if only_exp:
        print(f"\nOnly in experimental ({len(only_exp)}):")
        for bid in sorted(only_exp):
            t   = experimental["first_decode_ts"][bid]
            decodes = [d for d in experimental["all_decodes"] if d[1] == bid]
            xs = [d[2] for d in decodes]; ys = [d[3] for d in decodes]
            print(f"  bid={bid:>3}  first={t:.2f}s  "
                  f"pos≈({sum(xs)//len(xs)}, {sum(ys)//len(ys)})  "
                  f"decodes={len(decodes)}")
    else:
        print("\nNo IDs unique to experimental.")

    if only_base:
        print(f"\nOnly in baseline ({len(only_base)}):")
        for bid in sorted(only_base):
            print(f"  bid={bid}  (lost in experimental)")

    common = base_ids & exp_ids
    if common:
        faster_in_exp = []
        for bid in common:
            db = baseline["first_decode_ts"][bid]
            de = experimental["first_decode_ts"][bid]
            if de < db - 0.5:
                faster_in_exp.append((bid, db, de))
        if faster_in_exp:
            print(f"\nFaster in experimental (by ≥0.5s):")
            for bid, db, de in sorted(faster_in_exp, key=lambda x: x[2] - x[1]):
                print(f"  bid={bid:>3}  baseline={db:.2f}s → exp={de:.2f}s "
                      f"(Δ={de - db:+.2f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--grid",   type=int, default=4,
                    help="experimental grid_step (default: 4)")
    ap.add_argument("--radius", type=int, default=3,
                    help="experimental sample_radius (default: 3)")
    args = ap.parse_args()

    baseline = replay(args.video, grid_step=8, sample_radius=4,
                      label="baseline")
    experimental = replay(args.video,
                          grid_step=args.grid, sample_radius=args.radius,
                          label=f"exp(grid={args.grid}, r={args.radius})")
    compare(baseline, experimental)


if __name__ == "__main__":
    main()
