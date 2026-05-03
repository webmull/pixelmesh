"""Generate a marketing GIF: a phone running the blink-detection signal —
full-screen Manchester-encoded white/black phases for an example ID."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# Inlined to avoid numpy import. Mirrors blink_encoder.encode_id exactly.
NUM_BITS = 9
NUM_GUARD = 4


def encode_id(device_id: int) -> list[int]:
    bits = format(device_id, f"0{NUM_BITS}b")
    binary_str = "1" + bits + bits + "0"
    phases = [0] * NUM_GUARD
    for ch in binary_str:
        phases.extend([1, 0] if ch == "1" else [0, 1])
    return phases

OUT = Path(__file__).resolve().parent.parent / "presentation" / "blinking.gif"
OUT.parent.mkdir(parents=True, exist_ok=True)

W, H = 440, 880

PHONE_X, PHONE_Y = 30, 30
PHONE_W, PHONE_H = W - 60, H - 60
BEZEL = 16
SCREEN_X = PHONE_X + BEZEL
SCREEN_Y = PHONE_Y + BEZEL
SCREEN_W = PHONE_W - BEZEL * 2
SCREEN_H = PHONE_H - BEZEL * 2

FONT_PATH = "/System/Library/Fonts/HelveticaNeue.ttc"

DEVICE_ID = 42
PHASES = encode_id(DEVICE_ID)            # 44 phases (4 guard + 40 data)
FRAMES_PER_PHASE = 2                      # 100ms per phase in GIF time
FRAME_MS = 50                             # 20fps
FRAMES = len(PHASES) * FRAMES_PER_PHASE   # 88 frames, ~4.4s loop


def font(size, bold=False):
    try:
        return ImageFont.truetype(FONT_PATH, size, index=1 if bold else 0)
    except Exception:
        return ImageFont.load_default()


def rounded_rect(draw, xy, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def draw_phone(draw, screen_bright: bool):
    # Outer bezel
    rounded_rect(draw, (PHONE_X, PHONE_Y, PHONE_X + PHONE_W, PHONE_Y + PHONE_H),
                 radius=46, fill=(28, 28, 30), outline=(60, 60, 64), width=2)
    # Screen — full white or full black, like the real blink card
    fill = (255, 255, 255) if screen_bright else (0, 0, 0)
    rounded_rect(draw, (SCREEN_X, SCREEN_Y, SCREEN_X + SCREEN_W, SCREEN_Y + SCREEN_H),
                 radius=32, fill=fill)
    # Notch — sits on top of the screen, always dark
    notch_w, notch_h = 110, 22
    nx = SCREEN_X + SCREEN_W // 2 - notch_w // 2
    ny = SCREEN_Y + 8
    rounded_rect(draw, (nx, ny, nx + notch_w, ny + notch_h), radius=11,
                 fill=(18, 18, 20))


def draw_caption(draw, phase_idx: int):
    # Static caption above the phone
    title = f"transmitting ID {DEVICE_ID:03d}"
    tfont = font(15, bold=True)
    tw = draw.textbbox((0, 0), title, font=tfont)[2]
    draw.text((W // 2 - tw // 2, 6), title, font=tfont, fill=(180, 180, 180))

    # Phase progress strip below phone
    strip_y = PHONE_Y + PHONE_H + 14
    cell_w = (PHONE_W - 4) / len(PHASES)
    cell_h = 6
    for i, p in enumerate(PHASES):
        x0 = PHONE_X + 2 + i * cell_w
        x1 = x0 + cell_w - 1
        active = i == phase_idx
        if active:
            colour = (255, 255, 255) if p else (60, 60, 60)
        else:
            colour = (130, 130, 130) if p else (35, 35, 38)
        draw.rectangle((x0, strip_y, x1, strip_y + cell_h), fill=colour)
    # Guard region marker
    guard_end_x = PHONE_X + 2 + NUM_GUARD * cell_w
    draw.line((guard_end_x, strip_y - 2, guard_end_x, strip_y + cell_h + 2),
              fill=(80, 140, 220), width=1)

    # Label below strip
    sub = "Manchester  ·  300ms / phase  ·  9-bit ID × 2"
    sfont = font(11)
    sw = draw.textbbox((0, 0), sub, font=sfont)[2]
    draw.text((W // 2 - sw // 2, strip_y + cell_h + 6), sub, font=sfont,
              fill=(110, 110, 110))


def render_frame(i: int):
    phase_idx = (i // FRAMES_PER_PHASE) % len(PHASES)
    bright = bool(PHASES[phase_idx])
    img = Image.new("RGB", (W, H), (10, 10, 12))
    draw = ImageDraw.Draw(img)
    draw_phone(draw, bright)
    draw_caption(draw, phase_idx)
    return img


frames = [render_frame(i) for i in range(FRAMES)]

quantised = [f.quantize(colors=64, method=Image.Quantize.MEDIANCUT,
                        dither=Image.Dither.NONE) for f in frames]

quantised[0].save(
    OUT,
    save_all=True,
    append_images=quantised[1:],
    duration=FRAME_MS,
    loop=0,
    optimize=True,
    disposal=2,
)
print(f"wrote {OUT}  ({OUT.stat().st_size/1024:.1f} KB, {len(frames)} frames, "
      f"{FRAMES * FRAME_MS / 1000:.1f}s loop)")
