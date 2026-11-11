"""GamersXpress's own default picture for each article category.

A story gets one of these when neither Steam art nor a Wikimedia Commons photo fits it. The pictures are made
here, once, in the site's style, so every one is our own work (licence "Own work") and the pipeline never
downloads anything for them.

Run it after changing the look or the colours:

    python -m pipeline.default_images            # writes public/images/default-<category>.webp
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 675
QUALITY = 84
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)

#: category -> (display label, accent colour). The colours match CATEGORY_COLORS in src/data/categories.ts;
#: a test checks that the two lists name the same categories.
LOOK: dict[str, tuple[str, str]] = {
    "gaming-news": ("Gaming News", "#B45309"),
    "pc": ("PC", "#6D28D9"),
    "playstation": ("PlayStation", "#0057B8"),
    "xbox": ("Xbox", "#107C10"),
    "nintendo": ("Nintendo", "#C8102E"),
    "hardware": ("Hardware", "#0E7490"),
    "tech": ("Tech", "#4338CA"),
    "esports": ("Esports", "#BE185D"),
    "india": ("India", "#C2410C"),
}


def default_filename(category: str) -> str:
    return f"default-{category}.webp"


def _font(size: int) -> ImageFont.ImageFont:
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def render(category: str) -> Image.Image:
    """The picture for one category: a dark night-sky gradient, pixel blocks and pellets in its colour."""
    label, colour = LOOK[category]
    accent = _rgb(colour)
    image = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    top, bottom = (11, 12, 16), (24, 28, 40)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        draw.line([(0, y), (WIDTH, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))

    # pixel blocks drifting across the top, in the category colour (a fixed pattern: the same picture every run)
    rng = random.Random(category)
    cell = 48
    for gx in range(0, WIDTH // cell + 1):
        for gy in range(0, 7):
            if rng.random() < 0.22:
                x, y = gx * cell, gy * cell
                draw.rectangle([x + 6, y + 6, x + cell - 6, y + cell - 6], fill=tuple(round(c * 0.22) for c in accent))

    # a row of pellets along the bottom, and a bright pixel block as the mark
    for x in range(40, WIDTH - 40, 56):
        draw.rectangle([x, HEIGHT - 70, x + 10, HEIGHT - 60], fill=(245, 215, 122))
    draw.rectangle([72, 112, 120, 160], fill=accent)
    draw.rectangle([128, 112, 176, 160], fill=(255, 255, 255))

    draw.text((72, 262), label, font=_font(118), fill=(255, 255, 255))
    draw.text((76, 404), "GAMERSXPRESS", font=_font(40), fill=accent)
    return image


def write_all(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for category in LOOK:
        path = folder / default_filename(category)
        render(category).save(path, "WEBP", quality=QUALITY, method=6)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    for path in write_all(root / "public" / "images"):
        print(f"{path.relative_to(root)}  {path.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
