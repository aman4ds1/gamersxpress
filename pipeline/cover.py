"""Cover stage: render a branded 1200x630 WebP cover per article.

One local image is generated for every article (PLAN.md section 13): a
deterministic background derived from the category, the article headline
wrapped to fit, the category label and the site name, using a bundled
open-licensed font (``assets/fonts/DejaVuSans-Bold.ttf``, Bitstream Vera/DejaVu
license, see ``assets/fonts/LICENSE_DEJAVU.txt``).

Nothing is scraped or downloaded and no AI image is used: the cover is drawn
locally with Pillow and stays under 100 KB. :func:`front_matter` returns the
``image`` and ``imageAlt`` fields for an article; ``imageAlt`` honestly
describes the cover that was actually drawn.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "public" / "covers"
DEFAULT_SITE_NAME = "GamersXpress"
FONT_FILENAME = "DejaVuSans-Bold.ttf"

WIDTH = 1200
HEIGHT = 630
MARGIN = 80
MAX_COVER_BYTES = 100 * 1024

MIN_TITLE_SIZE = 34
MAX_TITLE_SIZE = 84

logger = logging.getLogger("gamersxpress.pipeline.cover")

# Mirrors src/data/categories.ts CATEGORY_LABELS.
CATEGORY_LABELS = {
    "gaming-news": "Gaming News",
    "pc": "PC",
    "playstation": "PlayStation",
    "xbox": "Xbox",
    "nintendo": "Nintendo",
    "hardware": "Hardware",
    "tech": "Tech",
    "esports": "Esports",
    "india": "India",
}

# Deterministic gradient per category: (top, bottom, accent).
PALETTE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = {
    "gaming-news": ((17, 24, 39), (2, 6, 23), (56, 189, 248)),
    "pc": ((30, 41, 59), (2, 6, 23), (96, 165, 250)),
    "playstation": ((30, 27, 75), (2, 6, 23), (37, 99, 235)),
    "xbox": ((20, 45, 27), (2, 10, 5), (34, 197, 94)),
    "nintendo": ((80, 7, 36), (24, 2, 12), (244, 63, 94)),
    "hardware": ((51, 65, 85), (15, 23, 42), (148, 163, 184)),
    "tech": ((8, 47, 73), (2, 6, 23), (14, 165, 233)),
    "esports": ((56, 30, 8), (23, 10, 2), (245, 158, 11)),
    "india": ((69, 26, 3), (23, 10, 2), (249, 115, 22)),
}
_PALETTE_FALLBACKS = [
    ((17, 24, 39), (2, 6, 23), (56, 189, 248)),
    ((30, 27, 75), (2, 6, 23), (99, 102, 241)),
    ((8, 47, 73), (2, 6, 23), (14, 165, 233)),
    ((24, 24, 27), (2, 2, 2), (168, 85, 247)),
]


class CoverError(Exception):
    """The bundled font is missing or the cover could not be written."""


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, (category or "News").replace("-", " ").title())


def _palette(category: str) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    if category in PALETTE:
        return PALETTE[category]
    digest = hashlib.sha256(category.encode("utf-8")).digest()
    return _PALETTE_FALLBACKS[digest[0] % len(_PALETTE_FALLBACKS)]


def load_font(size: int, fonts_dir: str | Path | None = None) -> ImageFont.FreeTypeFont:
    path = Path(fonts_dir or DEFAULT_FONTS_DIR) / FONT_FILENAME
    try:
        return ImageFont.truetype(str(path), size)
    except OSError as exc:
        raise CoverError(f"bundled font not found: {path}") from exc


def _break_long_word(word: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for char in word:
        if current and font.getlength(current + char) > max_width:
            pieces.append(current)
            current = char
        else:
            current += char
    if current:
        pieces.append(current)
    return pieces


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy wrap so every returned line is at most ``max_width`` pixels wide."""
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if font.getlength(candidate) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            if font.getlength(word) <= max_width:
                current = word
            else:
                pieces = _break_long_word(word, font, max_width)
                lines.extend(pieces[:-1])
                current = pieces[-1] if pieces else ""
        if current:
            lines.append(current)
    return lines


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def _fit_headline(
    text: str,
    fonts_dir: str | Path | None,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    size = MAX_TITLE_SIZE
    while size >= MIN_TITLE_SIZE:
        font = load_font(size, fonts_dir)
        lines = wrap_text(text, font, max_width)
        line_height = _line_height(font)
        if len(lines) * line_height <= max_height and all(font.getlength(line) <= max_width for line in lines):
            return font, lines, line_height
        size -= 2
    font = load_font(MIN_TITLE_SIZE, fonts_dir)
    lines = wrap_text(text, font, max_width)
    return font, lines, _line_height(font)


def _gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), top)
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        ratio = y / max(HEIGHT - 1, 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (WIDTH, y)], fill=color)
    return image


def render_cover(
    headline: str,
    category: str,
    *,
    slug: str,
    output_dir: str | Path | None = None,
    fonts_dir: str | Path | None = None,
    site_name: str = DEFAULT_SITE_NAME,
) -> Path:
    """Draw and save the WebP cover, returning the written path."""
    top, bottom, accent = _palette(category)
    image = _gradient(top, bottom)
    draw = ImageDraw.Draw(image)

    draw.rectangle([0, 0, 14, HEIGHT], fill=accent)
    draw.rectangle([MARGIN, 150, MARGIN + 96, 156], fill=accent)

    label_font = load_font(34, fonts_dir)
    draw.text((MARGIN, 86), category_label(category).upper(), font=label_font, fill=accent)

    max_width = WIDTH - 2 * MARGIN
    headline_top = 200
    headline_bottom = HEIGHT - 130
    font, lines, line_height = _fit_headline(
        headline, fonts_dir, max_width, headline_bottom - headline_top
    )
    y = headline_top
    for line in lines:
        draw.text((MARGIN, y), line, font=font, fill=(248, 250, 252))
        y += line_height

    site_font = load_font(32, fonts_dir)
    draw.text((MARGIN, HEIGHT - 82), site_name, font=site_font, fill=(203, 213, 225))

    output_dir = Path(output_dir or DEFAULT_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{slug}.webp"

    quality = 82
    while True:
        image.save(path, format="WEBP", quality=quality, method=6)
        if path.stat().st_size <= MAX_COVER_BYTES or quality <= 50:
            break
        quality -= 8
    logger.info("cover written: %s (%d bytes)", path, path.stat().st_size)
    return path


def image_alt(headline: str, category: str, *, site_name: str = DEFAULT_SITE_NAME) -> str:
    """Honest description of the generated cover image."""
    return f'{site_name} cover image for "{headline}" with the {category_label(category)} label.'


def front_matter(
    slug: str,
    headline: str,
    category: str,
    *,
    site_name: str = DEFAULT_SITE_NAME,
) -> dict:
    """Return the ``image`` and ``imageAlt`` front-matter fields for an article."""
    return {"image": f"/covers/{slug}.webp", "imageAlt": image_alt(headline, category, site_name=site_name)}


def generate_cover(
    slug: str,
    headline: str,
    category: str,
    *,
    output_dir: str | Path | None = None,
    fonts_dir: str | Path | None = None,
    site_name: str = DEFAULT_SITE_NAME,
) -> dict:
    """Render the cover and return its path plus the front-matter fields."""
    path = render_cover(
        headline,
        category,
        slug=slug,
        output_dir=output_dir,
        fonts_dir=fonts_dir,
        site_name=site_name,
    )
    return {
        **front_matter(slug, headline, category, site_name=site_name),
        "path": str(path),
        "bytes": path.stat().st_size,
    }


__all__ = [
    "CATEGORY_LABELS",
    "DEFAULT_FONTS_DIR",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_SITE_NAME",
    "FONT_FILENAME",
    "HEIGHT",
    "MAX_COVER_BYTES",
    "WIDTH",
    "CoverError",
    "category_label",
    "front_matter",
    "generate_cover",
    "image_alt",
    "load_font",
    "render_cover",
    "wrap_text",
]
