"""Generate a Mac desktop wallpaper from the pixelmesh SVG assets."""

import io
from pathlib import Path

import cairosvg
from PIL import Image, ImageDraw, ImageFilter

# 14" MacBook Pro native resolution (also looks fine when scaled by macOS for
# external displays).
W, H = 3024, 1964

ICON_SVG = Path.home() / "Desktop" / "glowex" / "public" / "pixelmesh-icon.svg"
TEXT_SVG = Path.home() / "Desktop" / "glowex" / "public" / "pixelmesh_text.svg"

OUT = Path.home() / "Pictures" / "pixelmesh-wallpaper.png"
OUT.parent.mkdir(parents=True, exist_ok=True)


def render_svg(path: Path, target_w: int) -> Image.Image:
    png_bytes = cairosvg.svg2png(
        url=str(path),
        output_width=target_w,
        background_color="rgba(0,0,0,0)",
    )
    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def make_background() -> Image.Image:
    base_top    = (8, 10, 16)
    base_bottom = (2, 3, 6)
    bg = Image.new("RGB", (W, H))
    px = bg.load()
    for y in range(H):
        t = y / (H - 1)
        r = int(base_top[0] + (base_bottom[0] - base_top[0]) * t)
        g = int(base_top[1] + (base_bottom[1] - base_top[1]) * t)
        b = int(base_top[2] + (base_bottom[2] - base_top[2]) * t)
        for x in range(W):
            px[x, y] = (r, g, b)

    # Subtle radial vignette glow centred slightly above middle
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    cx, cy = W // 2, int(H * 0.42)
    max_r = int(max(W, H) * 0.55)
    for r in range(max_r, 0, -8):
        alpha = int(28 * (1 - r / max_r) ** 2)
        gd.ellipse((cx - r, cy - r, cx + r, cy + r),
                   fill=(70, 110, 200, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    bg = Image.alpha_composite(bg.convert("RGBA"), glow)
    return bg.convert("RGB")


def compose():
    bg = make_background().convert("RGBA")

    icon_w = int(W * 0.32)
    icon = render_svg(ICON_SVG, icon_w)

    # The icon SVG has a viewBox starting at y=80 — leaves blank space at
    # the top of the rendered image. Crop it so vertical centring is honest.
    icon = icon.crop(icon.getbbox())
    iw, ih = icon.size

    text_w = int(W * 0.28)
    text = render_svg(TEXT_SVG, text_w)
    text = text.crop(text.getbbox())
    tw, th = text.size

    gap = 70
    block_h = ih + gap + th
    top = (H - block_h) // 2 - int(H * 0.04)

    icon_x = (W - iw) // 2
    icon_y = top
    text_x = (W - tw) // 2
    text_y = icon_y + ih + gap

    bg.alpha_composite(icon, (icon_x, icon_y))
    bg.alpha_composite(text, (text_x, text_y))

    bg.convert("RGB").save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT}  ({OUT.stat().st_size/1024:.0f} KB, {W}×{H})")


if __name__ == "__main__":
    compose()
