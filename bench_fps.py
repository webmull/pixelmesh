# (c) Adam Davis - adamdavis.co.uk
"""
Microbenchmark for proposed detection-thread optimisations.
Run with:  python bench_fps.py
"""
import time
import numpy as np
import cv2

REPS = 500
H, W = 1080, 1920
N_POINTS = 25920       # grid_step=8, ~135×192 points
N_ACTIVE = 50          # typical above-gate count during detection
R = 4                  # sample_radius
GATE = 0.08

# ── helpers ────────────────────────────────────────────────────────────────

def timeit(fn, reps=REPS):
    # warmup
    for _ in range(max(5, reps // 20)):
        fn()
    t0 = time.perf_counter()
    for _ in range(reps):
        fn()
    return (time.perf_counter() - t0) / reps * 1000   # ms per call


def fmt(label, before_ms, after_ms):
    saving = before_ms - after_ms
    pct    = saving / before_ms * 100 if before_ms else 0
    print(f"  {label:<42}  {before_ms:>7.3f} ms  →  {after_ms:>7.3f} ms  "
          f"({saving:+.3f} ms, {pct:.0f}%)")


# ── synthetic data ──────────────────────────────────────────────────────────

rng = np.random.default_rng(42)

# Realistic std array: most near 0, ~50 above gate
stds = rng.uniform(0.0, 0.02, N_POINTS).astype(np.float32)
active_idx = rng.choice(N_POINTS, N_ACTIVE, replace=False)
stds[active_idx] = rng.uniform(GATE, 0.35, N_ACTIVE)

brightnesses = rng.uniform(0.0, 1.0, N_POINTS).astype(np.float32)
gray = rng.integers(0, 255, (H, W), dtype=np.uint8)
raw_bgr = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)

# Fake grid points (px, py) matching stds array
xs = list(range(4, W, 8))
ys = list(range(216, H, 8))  # roi_top_frac=0.20 → skip top 216px
grid_pts = [(px, py) for py in ys for px in xs][:N_POINTS]

# ── 1. Heatmap ──────────────────────────────────────────────────────────────

print("\n── 1. Heatmap (detection thread, ~50 fps) ──────────────────────────")

r_circle = max(1, 8 // 2 - 2)  # grid_step=8
vs = np.clip(stds * 3.0, 0.0, 1.0) * 255
above = np.where(stds >= GATE * 0.5)[0]

def before_heatmap():
    img = np.zeros((H, W), dtype=np.uint8)
    for i in above:
        cv2.circle(img, grid_pts[i], r_circle, int(vs[i]), -1)

def after_heatmap():
    pass   # skipped entirely when debug capture is off

fmt("heatmap (zeros alloc + circle loop)", timeit(before_heatmap), timeit(after_heatmap))

# ── 2. History-recording loop ───────────────────────────────────────────────

print("\n── 2. History-recording loop (detection thread, ~50 fps) ───────────")

ts = time.time()
hist_secs = 30.0
# Simulate _GridPoint.last_active_ts — most = 0, N_ACTIVE recently active
last_active = np.zeros(N_POINTS, dtype=np.float64)
last_active[active_idx] = ts - rng.uniform(0, 5, N_ACTIVE)

class _FakePoint:
    __slots__ = ("last_active_ts",)
    def __init__(self, lat): self.last_active_ts = lat
    def add_sample(self, b, t, hs): pass   # no-op cost

points = [_FakePoint(last_active[i]) for i in range(N_POINTS)]

def before_history():
    for pt, b, s in zip(points, brightnesses, stds):
        if s >= GATE:
            pt.last_active_ts = ts
            pt.add_sample(float(b), ts, hist_secs)
        elif pt.last_active_ts > 0 and (ts - pt.last_active_ts) < hist_secs:
            pt.add_sample(float(b), ts, hist_secs)

def after_history():
    # Gate with numpy first, then iterate only the small active sets
    a_idx = np.where(stds >= GATE)[0]
    for i in a_idx:
        pt = points[i]
        pt.last_active_ts = ts
        pt.add_sample(float(brightnesses[i]), ts, hist_secs)
    # "recently active but currently quiet" — only those that have ever been active
    r_idx = np.where((stds < GATE) & (last_active > 0))[0]
    for i in r_idx:
        pt = points[i]
        if ts - pt.last_active_ts < hist_secs:
            pt.add_sample(float(brightnesses[i]), ts, hist_secs)

fmt("history recording loop", timeit(before_history), timeit(after_history))

# ── 3. Gray pad allocation ──────────────────────────────────────────────────

print("\n── 3. Gray pad buffer (detection thread, ~50 fps) ───────────────────")

pad_h, pad_w = H + 2*R, W + 2*R
_gray_pad_buf = np.empty((pad_h, pad_w), dtype=np.uint8)

def before_pad():
    return np.pad(gray, R, mode="edge")

def after_pad():
    # In-place fill: copy interior, then replicate edges
    _gray_pad_buf[R:R+H, R:R+W] = gray
    _gray_pad_buf[:R,   R:R+W] = gray[:1,  :]
    _gray_pad_buf[R+H:, R:R+W] = gray[-1:, :]
    _gray_pad_buf[:, :R]        = _gray_pad_buf[:, R:R+1]
    _gray_pad_buf[:, R+W:]      = _gray_pad_buf[:, R+W-1:R+W]
    return _gray_pad_buf

fmt("gray pad (alloc+copy vs in-place fill)", timeit(before_pad), timeit(after_pad))

# ── 4. Gamma + contrast when blacked out (display thread, ~60 fps) ──────────

print("\n── 4. Gamma + contrast under blackout (display thread, ~60 fps) ─────")

GAMMA_TABLE = np.array(
    [((i / 255.0) ** (1.0/1.1)) * 255 for i in range(256)], dtype="uint8"
)

def before_blackout():
    f = cv2.LUT(raw_bgr, GAMMA_TABLE)
    f = cv2.convertScaleAbs(f, alpha=1.08, beta=0)
    canvas = f[:720, :1280].copy()   # approximate build_canvas crop
    canvas[:] = 0

def after_blackout():
    canvas = np.zeros((720, 1280, 3), dtype=np.uint8)

fmt("gamma+contrast+crop when blacked out", timeit(before_blackout), timeit(after_blackout))

# ── summary ─────────────────────────────────────────────────────────────────

print()
