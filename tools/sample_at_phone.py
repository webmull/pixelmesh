"""Sample brightness over time at one location with different grid/patch
configurations and report how strong each one's signal is. Mirrors the
detector's percentile sampling so the numbers compare directly with what
the detector would have seen.

Usage:
    python3 tools/sample_at_phone.py debug.mp4 305 156
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


def sample_patch_pct(gray, cx, cy, radius, pct):
    """Return the pct-th percentile of an n×n patch centered at (cx,cy)."""
    h, w = gray.shape
    x1 = max(0, cx - radius)
    x2 = min(w, cx + radius)
    y1 = max(0, cy - radius)
    y2 = min(h, cy + radius)
    return np.percentile(gray[y1:y2, x1:x2], pct)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("phone_x", type=int)
    ap.add_argument("phone_y", type=int)
    ap.add_argument("--max-frames", type=int, default=900)
    args = ap.parse_args()

    px, py = args.phone_x, args.phone_y
    print(f"Testing phone at pixel ({px}, {py}) over up to {args.max_frames} frames\n")

    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        raise SystemExit(f"could not open {args.video}")

    # Configurations to compare. Each is (label, grid_step, sample_radius, pct).
    # The "raw pixel" config samples one pixel — best-case ceiling.
    configs = [
        ("raw centre pixel", 1, 0, None),
        ("8×8 patch r=4, p3 (original)", 8, 4, 3),
        ("4×4 patch r=2, p3 (k=0 buggy)", 4, 2, 3),
        ("4×4 patch r=2, p10 (k=1)", 4, 2, 10),
        ("4×4 patch r=2, p15 (k=2)", 4, 2, 15),
        ("4×4 patch r=2, p20 (k=3)", 4, 2, 20),
        ("4×4 patch r=2, p25 (k=4)", 4, 2, 25),
    ]

    # For each config, find the *nearest grid centre* (mimicking how the
    # detector lays out its grid) and sample the patch around it.
    results = {label: [] for label, *_ in configs}
    for _label, step, r, pct in configs:
        pass  # Per-config grid alignment computed below

    frame_idx = 0
    while frame_idx < args.max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0

        for label, step, r, pct in configs:
            # Grid centres at (step//2, 3*step//2, ...). Find nearest to (px,py).
            offset = step // 2
            gx = round((px - offset) / step) * step + offset
            gy = round((py - offset) / step) * step + offset
            if r == 0:
                v = float(gray[gy, gx]) if pct is None else float(gray[gy, gx])
            else:
                v = float(sample_patch_pct(gray, gx, gy, r, pct or 3))
            results[label].append(v)

        frame_idx += 1

    cap.release()
    if frame_idx == 0:
        raise SystemExit("no frames read")

    print(f"frames sampled: {frame_idx}\n")
    print(f"{'config':<40s} {'std':>8} {'min':>6} {'max':>6} {'range':>7} {'gx,gy':>10}")
    print("-" * 80)
    for label, step, r, pct in configs:
        offset = step // 2
        gx = round((px - offset) / step) * step + offset
        gy = round((py - offset) / step) * step + offset
        vals = np.array(results[label])
        print(f"{label:<40s} {vals.std():>8.4f} {vals.min():>6.3f} "
              f"{vals.max():>6.3f} {vals.max() - vals.min():>7.3f} "
              f"{gx:>4},{gy:>4}")


if __name__ == "__main__":
    main()
