"""Generate a marketing GIF: a phone showing the 'Found you!' screen with
dots (other phones) joining in over time, plus a pulsing green 'you' dot."""

import math
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "presentation" / "found.gif"
OUT.parent.mkdir(parents=True, exist_ok=True)

W, H = 440, 880

PHONE_X, PHONE_Y = 30, 30
PHONE_W, PHONE_H = W - 60, H - 60
BEZEL = 16
SCREEN_X = PHONE_X + BEZEL
SCREEN_Y = PHONE_Y + BEZEL
SCREEN_W = PHONE_W - BEZEL * 2
SCREEN_H = PHONE_H - BEZEL * 2

CANVAS_X = SCREEN_X + 24
CANVAS_Y = SCREEN_Y + 170
CANVAS_W = SCREEN_W - 48
CANVAS_H = 320

FONT_BOLD = "/System/Library/Fonts/HelveticaNeue.ttc"
FONT_REG = "/System/Library/Fonts/HelveticaNeue.ttc"


def font(size, bold=False):
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size,
                                  index=1 if bold else 0)
    except Exception:
        return ImageFont.load_default()


def rounded_rect(draw, xy, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


# ---- crowd layout (deterministic) ----
random.seed(7)
NUM_DOTS = 28
crowd = []
cx = CANVAS_X + CANVAS_W / 2
cy = CANVAS_Y + CANVAS_H / 2
for i in range(NUM_DOTS):
    while True:
        x = random.uniform(CANVAS_X + 18, CANVAS_X + CANVAS_W - 18)
        y = random.uniform(CANVAS_Y + 18, CANVAS_Y + CANVAS_H - 18)
        if math.hypot(x - cx, y - cy) > 38:  # keep clear of 'you' dot
            ok = all(math.hypot(x - px, y - py) > 24 for px, py in crowd)
            if ok:
                crowd.append((x, y))
                break

FRAMES = 80
FRAME_MS = 50  # 20fps


def draw_phone(base, draw):
    # Outer bezel
    rounded_rect(draw, (PHONE_X, PHONE_Y, PHONE_X + PHONE_W, PHONE_Y + PHONE_H),
                 radius=46, fill=(28, 28, 30), outline=(60, 60, 64), width=2)
    # Screen
    rounded_rect(draw, (SCREEN_X, SCREEN_Y, SCREEN_X + SCREEN_W, SCREEN_Y + SCREEN_H),
                 radius=32, fill=(0, 0, 0))
    # Notch
    notch_w, notch_h = 110, 22
    nx = SCREEN_X + SCREEN_W // 2 - notch_w // 2
    ny = SCREEN_Y + 8
    rounded_rect(draw, (nx, ny, nx + notch_w, ny + notch_h), radius=11,
                 fill=(18, 18, 20))


def text_centered(draw, y, txt, fnt, fill):
    bbox = draw.textbbox((0, 0), txt, font=fnt)
    tw = bbox[2] - bbox[0]
    draw.text((SCREEN_X + SCREEN_W // 2 - tw // 2, y), txt, font=fnt, fill=fill)


def draw_screen_text(draw):
    text_centered(draw, SCREEN_Y + 56, "Found you!", font(30, bold=True),
                  (240, 240, 240))
    text_centered(draw, SCREEN_Y + 100,
                  "Here's your position in the crowd",
                  font(15), (170, 170, 170))

    # divider
    dy = SCREEN_Y + SCREEN_H - 168
    draw.line((SCREEN_X + 60, dy, SCREEN_X + SCREEN_W - 60, dy),
              fill=(60, 60, 60), width=1)

    text_centered(draw, dy + 18, "You're all set", font(20, bold=True),
                  (220, 220, 220))
    text_centered(draw, dy + 50, "Hold your screen up when the show begins",
                  font(13), (140, 140, 140))
    text_centered(draw, dy + 90, "Brightness to full  ·  Turn off auto-lock",
                  font(11), (90, 90, 90))


def draw_dots(base, frame_idx):
    # Crowd dots fade in over the first 70% of the loop, one at a time.
    reveal_end = int(FRAMES * 0.75)
    per_dot = reveal_end / NUM_DOTS
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    for i, (x, y) in enumerate(crowd):
        appear_at = i * per_dot
        t = (frame_idx - appear_at) / max(per_dot * 0.9, 1)
        if t <= 0:
            continue
        t = min(t, 1.0)
        # ease-out scale + alpha
        alpha = int(220 * t)
        r = 4.5 * (0.4 + 0.6 * t)
        # soft glow
        glow_r = r * 2.4
        gd = ImageDraw.Draw(overlay)
        gd.ellipse((x - glow_r, y - glow_r, x + glow_r, y + glow_r),
                   fill=(255, 255, 255, int(40 * t)))
        od.ellipse((x - r, y - r, x + r, y + r),
                   fill=(255, 255, 255, alpha))

    # 'You' dot — pulsing green at 2Hz
    phase = (frame_idx / FRAMES) * 2 * math.pi * 2  # 2 cycles per loop
    pulse = 0.5 + 0.5 * math.sin(phase)
    base_r = 11
    halo_r = base_r + 9 * pulse
    halo_alpha = int(120 * (1 - pulse))
    od.ellipse((cx - halo_r - 6, cy - halo_r - 6,
                cx + halo_r + 6, cy + halo_r + 6),
               fill=(40, 220, 90, max(0, halo_alpha // 3)))
    od.ellipse((cx - halo_r, cy - halo_r, cx + halo_r, cy + halo_r),
               fill=(40, 220, 90, halo_alpha))
    od.ellipse((cx - base_r, cy - base_r, cx + base_r, cy + base_r),
               fill=(60, 230, 110, 255))
    # inner highlight
    od.ellipse((cx - base_r + 3, cy - base_r + 3,
                cx - base_r + 8, cy - base_r + 8),
               fill=(255, 255, 255, 180))

    base.alpha_composite(overlay)


def render_frame(i):
    img = Image.new("RGBA", (W, H), (10, 10, 12, 255))
    draw = ImageDraw.Draw(img)
    draw_phone(img, draw)
    draw_screen_text(draw)
    draw_dots(img, i)
    return img.convert("RGB")


frames = [render_frame(i) for i in range(FRAMES)]

# Quantise to a shared palette for smaller file + cleaner colours
quantised = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
             for f in frames]

quantised[0].save(
    OUT,
    save_all=True,
    append_images=quantised[1:],
    duration=FRAME_MS,
    loop=0,
    optimize=True,
    disposal=2,
)
print(f"wrote {OUT}  ({OUT.stat().st_size/1024:.1f} KB, {len(frames)} frames)")
