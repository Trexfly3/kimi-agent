"""Regenerate the desktop-app icons from the design in kimi_agent/gui/static/icon.svg.

    uv run --no-project --with pillow python scripts/make_icons.py

Writes icon.png (Linux menu entry), icon.ico (Windows shortcuts) and icon.icns (macOS app bundle).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

STATIC = Path(__file__).resolve().parent.parent / "kimi_agent" / "gui" / "static"
BACKGROUND = "#D97757"
# The "K" from icon.svg, in its 64x64 coordinate space: three round-capped strokes, 6.5 wide.
STROKES = [((23, 17), (23, 47)), ((23, 34.5), (41, 17)), ((29.5, 28.5), (42.5, 47))]
STROKE_WIDTH = 6.5
CORNER_RADIUS = 15
MASTER = 4096  # draw big, then scale down for smooth edges


def draw_icon(size: int, inset: float = 0.0) -> Image.Image:
    """The icon at ``size`` pixels; ``inset`` leaves that fraction of transparent margin on each side."""
    image = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    offset = MASTER * inset
    scale = (MASTER - 2 * offset) / 64

    def point(x: float, y: float) -> tuple[float, float]:
        return offset + x * scale, offset + y * scale

    draw.rounded_rectangle([point(0, 0), point(64, 64)], radius=CORNER_RADIUS * scale, fill=BACKGROUND)
    width = STROKE_WIDTH * scale
    for start, end in STROKES:
        draw.line([point(*start), point(*end)], fill="white", width=round(width))
        for x, y in (point(*start), point(*end)):
            r = width / 2
            draw.ellipse([x - r, y - r, x + r, y + r], fill="white")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    draw_icon(256).save(STATIC / "icon.png", optimize=True)
    ico_sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    draw_icon(256).save(STATIC / "icon.ico", sizes=[(s, s) for s in ico_sizes],
                        append_images=[draw_icon(s) for s in ico_sizes[:-1]])
    # macOS icons sit on Apple's grid: an 824px rounded square centred in a 1024px canvas.
    mac = [draw_icon(s, inset=100 / 1024) for s in (16, 32, 64, 128, 256, 512)]
    draw_icon(1024, inset=100 / 1024).save(STATIC / "icon.icns", append_images=mac)
    for name in ("icon.png", "icon.ico", "icon.icns"):
        print(f"wrote {STATIC / name} ({(STATIC / name).stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
