"""Track a single grid point's brightness over the full recording with
different sample_radius values. Plots the brightness trajectory and reports
windowed std. Bypasses the detector entirely so rate limiters / eviction /
budget can't muddy the picture.

Output: per-config series printed as a sparkline + stats."""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


def percentile_patch(gray, cx, cy, radius, pct):
    h, w = gray.shape
    x1 = max(0, cx - radius); x2 = min(w, cx + radius)
    y1 = max(0, cy - radius); y2 = min(h, cy + radius)
    return np.percentile(gray[y1:y2, x1:x2], pct)


def windowed_std(values, window):
    out = np.zeros(len(values))
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        out[i] = np.std(values[lo:i + 1])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("phone_x", type=int)
    ap.add_argument("phone_y", type=int)
    ap.add_argument("--max-frames", type=int, default=1500)
    args = ap.parse_args()

    px, py = args.phone_x, args.phone_y
    print(f"Tracking grid point at nearest-step alignment of ({px}, {py})\n")

    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        raise SystemExit(f"could not open {args.video}")

    configs = [
        ("r=4 (current)", 4, 3),
        ("r=2 (proposed)", 2, 3),
        ("r=1 single-pixel-ish", 1, 3),
    ]
    series = {label: [] for label, *_ in configs}

    fi = 0
    while fi < args.max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        for label, r, pct in configs:
            # Grid alignment: step=8, offset=4 → grid centres at 4, 12, 20, ...
            gx = round((px - 4) / 8) * 8 + 4
            gy = round((py - 4) / 8) * 8 + 4
            v = percentile_patch(gray, gx, gy, r, pct)
            series[label].append(float(v))
        fi += 1
    cap.release()

    print(f"frames: {fi}\n")

    # Recent_n=24 windowed std (matches detector's recent_std calculation)
    for label, _, _ in configs:
        v = np.array(series[label])
        w_std = windowed_std(v, 24)
        # Number of frames where windowed std crosses the gate floor 0.05
        above = int(np.sum(w_std >= 0.05))
        # Number above 0.10 (decode-meaningful threshold)
        above_010 = int(np.sum(w_std >= 0.10))
        print(f"{label}")
        print(f"  brightness: min={v.min():.3f}  max={v.max():.3f}  "
              f"range={v.max() - v.min():.3f}  std={v.std():.4f}")
        print(f"  windowed std (n=24): min={w_std.min():.4f}  "
              f"max={w_std.max():.4f}  mean={w_std.mean():.4f}")
        print(f"  frames with windowed_std >= 0.05 (gate): "
              f"{above} / {fi}  ({100*above/fi:.0f}%)")
        print(f"  frames with windowed_std >= 0.10: "
              f"{above_010} / {fi}  ({100*above_010/fi:.0f}%)")

        # Sparkline of windowed_std (every 10th frame)
        sub = w_std[::10]
        chars = " ▁▂▃▄▅▆▇█"
        max_v = max(0.4, sub.max())
        bins = (sub / max_v * (len(chars) - 1)).astype(int).clip(0, len(chars) - 1)
        sparkline = "".join(chars[b] for b in bins)
        print(f"  sparkline (every 10 frames): {sparkline}")
        print()


if __name__ == "__main__":
    main()
