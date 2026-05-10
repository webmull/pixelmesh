"""Compute a per-pixel temporal-std heatmap from a recording, restricted to
a region of interest. Tells us — independent of any gridding decisions —
where the actual blink signal is in the frame, and how strong it is.

If the heatmap shows a clear high-std blob at the top-left phone's location,
the signal exists; the question becomes whether the 8 px / 8×8-patch sampling
landed close enough to it. We can then sweep patch size and grid alignment
on that same blob to answer the "would finer grid have caught it" question.

Usage:
    python3 tools/signal_heatmap.py debug.mp4
    python3 tools/signal_heatmap.py debug.mp4 --roi 100,80,500,260
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--roi", default="100,80,500,260",
                    help="x1,y1,x2,y2 region of interest in pixels (default: top-left audience zone)")
    ap.add_argument("--max-frames", type=int, default=900,
                    help="cap frames analysed (~30s at 30fps)")
    ap.add_argument("--out", default="/tmp/signal_heatmap.png")
    args = ap.parse_args()

    x1, y1, x2, y2 = map(int, args.roi.split(","))
    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        raise SystemExit(f"could not open {args.video}")

    print(f"ROI x={x1}..{x2}, y={y1}..{y2}  ({x2-x1}×{y2-y1} px)")

    # Accumulate per-pixel sum and sum-of-squares for std calculation
    h, w = y2 - y1, x2 - x1
    sum_b   = np.zeros((h, w), dtype=np.float64)
    sum_b2  = np.zeros((h, w), dtype=np.float64)
    count   = 0

    while count < args.max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        patch = gray[y1:y2, x1:x2]
        sum_b  += patch
        sum_b2 += patch * patch
        count  += 1

    cap.release()
    if count == 0:
        raise SystemExit("no frames read")

    mean = sum_b / count
    var  = sum_b2 / count - mean * mean
    std  = np.sqrt(np.maximum(var, 0))

    print(f"frames analysed: {count}")
    print(f"std: min={std.min():.4f}  max={std.max():.4f}  mean={std.mean():.4f}")
    print(f"std percentiles: p50={np.percentile(std, 50):.4f}  "
          f"p90={np.percentile(std, 90):.4f}  "
          f"p99={np.percentile(std, 99):.4f}")

    # Find the top peaks
    flat = std.flatten()
    top_n = 8
    top_idx = np.argpartition(flat, -top_n)[-top_n:]
    top_idx = top_idx[np.argsort(-flat[top_idx])]
    print(f"\nTop {top_n} std peaks (in ROI coords):")
    for i in top_idx:
        py, px = divmod(i, w)
        gx, gy = px + x1, py + y1
        print(f"  std={std[py, px]:.3f}  ({gx:>4}, {gy:>4})  ROI({px:>3}, {py:>3})")

    # Save heatmap as PNG
    norm = np.clip(std / max(std.max(), 0.05), 0, 1)
    heatmap = (norm * 255).astype(np.uint8)
    heatmap_color = cv2.applyColorMap(heatmap, cv2.COLORMAP_INFERNO)
    cv2.imwrite(args.out, heatmap_color)
    print(f"\nheatmap saved to {args.out}")


if __name__ == "__main__":
    main()
