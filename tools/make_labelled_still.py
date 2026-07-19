#!/usr/bin/env python3
"""Static, labelled companion to spatial_calibration.gif.

Every detected phone as a dot at its real camera-frame position, shaded by
decode speed, with its blink-ID labelled. Framing is cropped to the crowd so
44 labels have room; greedy placement + leader lines keep them from colliding.
Outputs public/stats/07_spatial_labelled.png
"""
import json, os, math
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARY = os.path.join(ROOT, "debug/indigo-thorn-78/summary.json")
OUT = os.path.join(ROOT, "public/stats/07_spatial_labelled.png")
TTD_FAST, TTD_SLOW = 13.5, 162.7
PHANTOMS = {39, 47, 216}
SS = 2

BG, PANEL, BORDER = (251, 250, 247), (250, 250, 246), (214, 214, 214)
INK, SUB = (13, 13, 13), (110, 110, 110)
HALO, LEAD = (255, 255, 255), (190, 190, 188)
GREY_FAST, GREY_SLOW = 178, 30

def F(path, size):
    try: return ImageFont.truetype(path, size*SS)
    except Exception: return ImageFont.load_default()
f_title = F("/System/Library/Fonts/Helvetica.ttc", 40)
f_sub   = F("/System/Library/Fonts/SFNS.ttf", 23)
f_lab   = F("/System/Library/Fonts/Helvetica.ttc", 23)
f_leg   = F("/System/Library/Fonts/SFNS.ttf", 20)

# ---- data ----
d = json.load(open(SUMMARY))
first = {}
for fr in d["frames_data"]:
    for det in fr["detections"]:
        b = det["blink_id"]
        if b not in first and b not in PHANTOMS:
            first[b] = (fr["frame"], det["cx"], det["cy"])
raw = sorted((v[0], k, v[1], v[2]) for k, v in first.items())
fr_lo, fr_hi = raw[0][0], raw[-1][0]
PH = []  # id, cx, cy, grey
for frm, bid, cx, cy in raw:
    s = (frm - fr_lo) / (fr_hi - fr_lo)
    g = round(GREY_FAST + (GREY_SLOW - GREY_FAST) * s)
    PH.append((bid, cx, cy, (g, g, g)))

W, H = 1920, 1080
PX0, PY0, PX1, PY1 = 120, 210, 1800, 980
# crop framing to the crowd bbox (+margin), preserve aspect, fit + centre
xs = [p[1] for p in PH]; ys = [p[2] for p in PH]
mx, my = (max(xs)-min(xs))*0.08, (max(ys)-min(ys))*0.18
bx0, bx1 = min(xs)-mx, max(xs)+mx
by0, by1 = min(ys)-my, max(ys)+my
bw, bh = bx1-bx0, by1-by0
iw, ih = PX1-PX0-120, PY1-PY0-120
sc = min(iw/bw, ih/bh)
ox = PX0 + (PX1-PX0 - bw*sc)/2
oy = PY0 + (PY1-PY0 - bh*sc)/2
def cam(cx, cy): return ox + (cx-bx0)*sc, oy + (cy-by0)*sc

img = Image.new("RGB", (W*SS, H*SS), BG)
dr = ImageDraw.Draw(img, "RGBA")
def s(v): return v*SS
def text(xy, t, font, fill, anchor="la"):
    dr.text((s(xy[0]), s(xy[1])), t, font=font, fill=fill, anchor=anchor)

text((120, 64), "Where the audience sat — all 44 phones, by ID", f_title, INK)
text((120, 120), "real camera-frame layout (cropped to the crowd) · dot shade = time-to-decode, light = fast",
     f_sub, SUB)
dr.line([s(120), s(170), s(1800), s(170)], fill=BORDER, width=SS)
dr.rounded_rectangle([s(PX0), s(PY0), s(PX1), s(PY1)], radius=s(12),
                     fill=PANEL, outline=BORDER, width=SS)

R = 13
def dot_rect(cx, cy, r): return (cx-r, cy-r, cx+r, cy+r)
def overlaps(a, b, pad=3):
    return not (a[2]+pad < b[0] or b[2]+pad < a[0] or a[3]+pad < b[1] or b[3]+pad < a[1])

# greedy label placement -------------------------------------------------
pts = [(cam(cx, cy), bid, col) for (bid, cx, cy, col) in PH]
placed = []                      # label rects
dot_rects = [dot_rect(p[0][0], p[0][1], R+2) for p in pts]
DIRS = [(0,-1),(0,1),(1,-0.4),(-1,-0.4),(1,0.6),(-1,0.6),(1,0),(-1,0)]
RADII = [R+14, R+30, R+48, R+70, R+96, R+126]
labels = []                      # (lx, ly, text, leader_from)
order = sorted(range(len(pts)), key=lambda i: (pts[i][0][1], pts[i][0][0]))
for i in order:
    (x, y), bid, col = pts[i]
    t = str(bid)
    tb = dr.textbbox((0, 0), t, font=f_lab)
    tw, th = (tb[2]-tb[0])/SS, (tb[3]-tb[1])/SS
    bw_, bh_ = tw+12, th+8
    chosen = None
    for ri, rad in enumerate(RADII):
        for dx, dy in DIRS:
            n = math.hypot(dx, dy) or 1
            lx, ly = x + dx/n*rad, y + dy/n*rad
            rect = (lx-bw_/2, ly-bh_/2, lx+bw_/2, ly+bh_/2)
            if rect[0] < PX0+8 or rect[2] > PX1-8 or rect[1] < PY0+8 or rect[3] > PY1-8:
                continue
            if any(overlaps(rect, pr) for pr in placed):
                continue
            if any(overlaps(rect, drc, 1) for j, drc in enumerate(dot_rects) if j != i):
                continue
            chosen = (lx, ly, rect, ri)
            break
        if chosen:
            break
    if not chosen:                # fallback: stack upward regardless
        lx, ly = x, y - (R+14)
        rect = (lx-bw_/2, ly-bh_/2, lx+bw_/2, ly+bh_/2)
        chosen = (lx, ly, rect, 3)
    lx, ly, rect, ri = chosen
    placed.append(rect)
    labels.append((lx, ly, t, (x, y), ri, bw_, bh_))

# draw leaders first (under dots), then dots, then labels
for lx, ly, t, (x, y), ri, bw_, bh_ in labels:
    if ri >= 1:
        ang = math.atan2(ly-y, lx-x)
        dr.line([s(x+math.cos(ang)*R), s(y+math.sin(ang)*R),
                 s(lx-math.cos(ang)*bw_*0.42), s(ly-math.sin(ang)*bh_*0.5)],
                fill=LEAD+(220,), width=int(1.4*SS))
for (x, y), bid, col in pts:
    dr.ellipse([s(x-R-4), s(y-R-4), s(x+R+4), s(y+R+4)], fill=HALO+(240,))
    dr.ellipse([s(x-R), s(y-R), s(x+R), s(y+R)], fill=col+(255,))
for lx, ly, t, src, ri, bw_, bh_ in labels:
    dr.rounded_rectangle([s(lx-bw_/2), s(ly-bh_/2), s(lx+bw_/2), s(ly+bh_/2)],
                         radius=s(5), fill=(251, 250, 247, 215))
    text((lx, ly-1), t, f_lab, INK, anchor="mm")

# legend (fast -> slow gradient bar)
lgx, lgy, lgw = 120, 1015, 360
for k in range(lgw):
    gg = round(GREY_FAST + (GREY_SLOW-GREY_FAST)*k/lgw)
    dr.line([s(lgx+k), s(lgy), s(lgx+k), s(lgy+18)], fill=(gg, gg, gg, 255), width=SS)
text((lgx, lgy+26), "fast · 14 s", f_leg, SUB)
text((lgx+lgw, lgy+26), "slow · 163 s", f_leg, SUB, anchor="ra")
text((1800, lgy+26), "44 phones · one camera · 4½ minutes", f_leg, SUB, anchor="ra")

img.resize((W, H), Image.LANCZOS).save(OUT)
print("wrote", OUT, f"({os.path.getsize(OUT)//1024} KB)")
