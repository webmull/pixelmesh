#!/usr/bin/env python3
"""Render text-free spatial-calibration animations from the indigo-thorn-78 run.

Outputs two looping GIFs into public/stats/ (no text anywhere — captions live in
the article):
  spatial_calibration.gif  — phones located one-by-one, slowly, in real order
  time_vs_space.gif         — left pulses in unison (time), right reveals (space)

Dot SHADE encodes decode speed: light grey = found fast, dark grey = found slow
(same mapping as 04_spatial.svg). Positions and order are real, from summary.json.
"""
import json, os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARY = os.path.join(ROOT, "debug/indigo-thorn-78/summary.json")
OUT = os.path.join(ROOT, "public/stats")
RUN_S = 263.9
TTD_FAST, TTD_SLOW = 13.5, 162.7
PHANTOMS = {39, 47, 216}
SS = 2   # supersample for anti-aliasing

# palette (matches the stats pack)
BG, PANEL, BORDER = (251, 250, 247), (250, 250, 246), (214, 214, 214)
GRIDDOT = (228, 226, 219)
HALO, RING = (255, 255, 255), (158, 158, 158)
GREY_FAST, GREY_SLOW = 178, 30   # light = fast decode, dark = slow

# ---- real data ----------------------------------------------------------
d = json.load(open(SUMMARY))
fd = d["frames_data"]
first = {}
for fr in fd:
    for det in fr["detections"]:
        b = det["blink_id"]
        if b not in first and b not in PHANTOMS:
            first[b] = (fr["frame"], det["cx"], det["cy"])
raw = sorted((v[0], k, v[1], v[2]) for k, v in first.items())
fr_lo, fr_hi = raw[0][0], raw[-1][0]
PHONES = []   # (t_real, cx, cy, grey)
for frm, bid, cx, cy in raw:
    t = TTD_FAST + (frm - fr_lo) / (fr_hi - fr_lo) * (TTD_SLOW - TTD_FAST)
    s = (t - TTD_FAST) / (TTD_SLOW - TTD_FAST)         # 0 fast .. 1 slow
    g = round(GREY_FAST + (GREY_SLOW - GREY_FAST) * s)
    PHONES.append((t, cx, cy, (g, g, g)))
N = len(PHONES)
print(f"{N} phones, t {PHONES[0][0]:.1f}..{PHONES[-1][0]:.1f}s")

def ease_out(p): return 1 - (1 - p) ** 2
def lerp(a, b, t): return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))

def new_frame(w, h):
    img = Image.new("RGB", (w*SS, h*SS), BG)
    return img, ImageDraw.Draw(img, "RGBA")

def adot(dr, x, y, r, fill, a):
    x, y, r = x*SS, y*SS, r*SS
    dr.ellipse([x-r, y-r, x+r, y+r], fill=fill + (int(a*255),))

def ring(dr, x, y, r, a):
    dr.ellipse([(x-r)*SS, (y-r)*SS, (x+r)*SS, (y+r)*SS],
               outline=RING + (int(a*255),), width=int(2*SS))

def panel(dr, x0, y0, x1, y1):
    dr.rounded_rectangle([x0*SS, y0*SS, x1*SS, y1*SS], radius=10*SS,
                         fill=PANEL, outline=BORDER, width=SS)

def grid(dr, x0, y0, x1, y1, step):
    x = x0
    while x <= x1:
        y = y0
        while y <= y1:
            adot(dr, x, y, 0.7, GRIDDOT, 1.0)
            y += step
        x += step

def finish(img, w, h):
    return img.resize((w, h), Image.LANCZOS)

def draw_locked(dr, x, y, g, at, a, fade, ring_span):
    p = ease_out(min((at - a) / fade, 1.0))
    r = 5 + 6 * p
    rp = (at - a) / ring_span
    if rp < 1.0:
        ring(dr, x, y, r + 8 + rp * 30, (1 - rp) * 0.75)
    adot(dr, x, y, r + 3.5, HALO, 0.95 * p)
    adot(dr, x, y, r, g, p)
    adot(dr, x, y, max(r - 2, 1), lerp(g, (0, 0, 0), 0.18), p)

def save_gif(frames, durs, out):
    keys = [frames[len(frames)//3], frames[2*len(frames)//3], frames[-1]]
    w, h = frames[0].size
    mont = Image.new("RGB", (w, h*len(keys)))
    for i, k in enumerate(keys):
        mont.paste(k, (0, i*h))
    pal = mont.quantize(colors=64, method=Image.FASTOCTREE)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    q[0].save(out, save_all=True, append_images=q[1:], loop=0,
              duration=durs, disposal=2, optimize=True)
    print("wrote", out, f"({len(q)} frames, {os.path.getsize(out)//1024} KB)")

# =========================================================================
# GIF A — spatial_calibration.gif  (slow, no text)
# =========================================================================
def render_spatial():
    W, M = 960, 24
    PW = W - 2*M
    PH = round(PW * 1080 / 1920)
    H = 2*M + PH
    PX0, PY0, PX1, PY1 = M, M, M+PW, M+PH
    def cam(cx, cy): return PX0 + cx/1920*PW, PY0 + cy/1080*PH

    REVEAL_REAL = 166.0                      # real seconds shown (just past last dot)
    ANIM = 18.0                              # slow reveal
    FADE = 1.2                               # per-dot fade-in seconds
    appear = [(min(t, REVEAL_REAL)/REVEAL_REAL*ANIM, cx, cy, g) for (t, cx, cy, g) in PHONES]
    FPS = 12.0
    dt = 1.0 / FPS
    nframes = int(ANIM / dt) + 1
    frames, durs = [], []
    for fi in range(nframes):
        at = fi * dt
        img, dr = new_frame(W, H)
        panel(dr, PX0, PY0, PX1, PY1)
        grid(dr, PX0+26, PY0+26, PX1-26, PY1-26, 44)
        for a, cx, cy, g in appear:
            if at < a:
                continue
            x, y = cam(cx, cy)
            draw_locked(dr, x, y, g, at, a, FADE, 1.4)
        frames.append(finish(img, W, H))
        durs.append(int(dt*1000))
    full_last = frames[-1]
    empty = frames[0]                        # fi=0 is just panel+grid
    for _ in range(12):                      # brief settle
        frames.append(full_last); durs.append(70)
    for k in range(1, 19):                   # seamless fade-out back to empty
        frames.append(Image.blend(full_last, empty, k/18)); durs.append(55)
    save_gif(frames, durs, os.path.join(OUT, "spatial_calibration.gif"))

# =========================================================================
# GIF B — time_vs_space.gif  (slow, no text)
# =========================================================================
def render_contrast():
    PW = 480
    PH = round(PW * 1080 / 1920)
    M, GAP, TOP = 28, 36, 28
    LX0 = M
    RX0 = M + PW + GAP
    W = M + PW + GAP + PW + M
    H = TOP + PH + TOP
    PY0, PY1 = TOP, TOP + PH
    def cam(x0, cx, cy): return x0 + cx/1920*PW, PY0 + cy/1080*PH

    LOOP_S = 9.5
    FPS = 12.0
    dt = 1.0 / FPS
    nframes = int(LOOP_S / dt)
    reveal_span = 6.5
    FADE = 0.9
    FADE_OUT0, FADE_OUT1 = 7.5, 9.5          # right panel clears for a seamless loop
    appear = [(t/TTD_SLOW*reveal_span, cx, cy, g) for (t, cx, cy, g) in PHONES]
    beat = 1.8
    def cam_r(cx, cy): return RX0 + cx/1920*PW, PY0 + cy/1080*PH
    frames, durs = [], []
    for fi in range(nframes):
        at = fi * dt
        img, dr = new_frame(W, H)
        panel(dr, LX0, PY0, LX0+PW, PY1)
        panel(dr, RX0, PY0, RX0+PW, PY1)
        grid(dr, LX0+22, PY0+22, LX0+PW-22, PY1-22, 38)
        grid(dr, RX0+22, PY0+22, RX0+PW-22, PY1-22, 38)
        # LEFT — time: every phone present, pulsing white in unison
        ph = (at % beat) / beat
        pulse = max(0.0, 1 - ph/0.4) if ph < 0.4 else 0.0
        for _, cx, cy, g in PHONES:
            x, y = cam(LX0, cx, cy)
            adot(dr, x, y, 7, HALO, 0.85*pulse)
            adot(dr, x, y, 5, lerp(g, (255,255,255), pulse), 1.0)
        # RIGHT — space: flicker then lock, one-by-one; then clear for the loop
        ramp = 1.0
        if at > FADE_OUT0:
            ramp = max(0.0, 1 - (at - FADE_OUT0) / (FADE_OUT1 - FADE_OUT0))
        for a, cx, cy, g in appear:
            if at < a:
                continue
            x, y = cam_r(cx, cy)
            p = ease_out(min((at - a) / FADE, 1.0)) * ramp
            rp = (at - a) / 0.9
            if rp < 1.0:
                ring(dr, x, y, 9 + rp*26, (1 - rp) * 0.75 * ramp)
            adot(dr, x, y, 7.5, HALO, 0.95*p)
            adot(dr, x, y, 5, g, p)
        frames.append(finish(img, W, H))
        durs.append(int(dt*1000))
    save_gif(frames, durs, os.path.join(OUT, "time_vs_space.gif"))

if __name__ == "__main__":
    render_spatial()
    render_contrast()
