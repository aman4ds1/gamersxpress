"""Real images for articles: processing, front matter, the publish check and a command to add one.

An article may carry a ``hero`` image: a photo or key art shown at the top of the page and on its
card, with a credit line. It is separate from ``image``, the generated text cover that is only the
social-sharing picture. A hero always lives in ``public/images/<slug>.webp`` (our own copy, never a
link to someone else's server) and always carries a description for screen readers, a credit and a
license. The licenses that may be used are listed in ``config.yaml`` under ``images``; an image from
any other source (another outlet's photo, for instance) is not allowed.

Add an image to an article by hand::

    python -m pipeline.images add --article src/content/articles/<slug>.md --file photo.jpg \\
        --alt "A person holding the console" --credit "Nintendo" --license "Press kit" \\
        --source-url https://example.com/press

The picture is resized, converted to WebP with its metadata removed, and the article's front matter
is updated. :func:`hero_failures` is what the publish gate runs on every article that has a hero.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = PROJECT_ROOT / "public"
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

#: Where a hero file may live, relative to the site root; also the shape of ``hero.src``.
SRC_RE = re.compile(r"^/images/[a-z0-9][a-z0-9-]*\.webp$")
MIN_ALT_CHARS = 10
MAX_TITLE_CHARS = 200
#: The only place a ``licenseUrl`` may point: the Creative Commons license texts.
LICENSE_URL_PREFIX = "https://creativecommons.org/"

DEFAULTS: dict[str, Any] = {
    "allowed_licenses": [
        "Public domain", "CC0", "CC BY 4.0", "CC BY-SA 4.0", "CC BY 3.0", "CC BY-SA 3.0", "CC BY 2.0",
        "CC BY-SA 2.0", "Press kit", "Steam store", "Own work", "Licensed",
    ],
    "min_width": 800,
    "max_width": 1600,
    # A tall photo is fine (a console stood upright is a portrait picture) because the page shows it
    # in a bounded frame, but nothing absurd: width / height must be between these two.
    "min_aspect": 0.5,
    "max_aspect": 3.5,
    # A tall picture is scaled down to this height, so it stays a light file.
    "max_height": 1600,
    "max_bytes": 400_000,
    "quality": 82,
}


#: Roman numerals that follow a game's name as a sequel number.
ROMAN_NUMERALS = frozenset({"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"})
#: Words that follow a hardware product's name to make it a different model.
HARDWARE_MODEL_WORDS = frozenset({
    "pro", "slim", "oled", "lite", "mini", "max", "ultra", "plus", "super", "xl", "digital", "edition", "ti", "xt", "s", "x",
}) | ROMAN_NUMERALS


def continues_as_another_model(title_tokens: list[str], needle: list[str], words: frozenset[str] = ROMAN_NUMERALS) -> bool:
    """Whether ``needle`` appears in the headline immediately followed by a number or a model word.

    "Steam Deck" in "Steam Deck 2 teased" is the older sibling of what the headline is about, and
    "Alan Wake" in "Alan Wake 3 announced" is an earlier game. A picture of that product would
    mislead, so a name that is the beginning of a longer model name is not used. ``words`` are the
    non-number words that count as a model suffix (roman numerals for games, plus Pro/Slim/OLED...
    for hardware); any digit-only token always counts.
    """
    if not needle or len(needle) > len(title_tokens):
        return False
    for index in range(len(title_tokens) - len(needle) + 1):
        if title_tokens[index : index + len(needle)] != needle:
            continue
        following = title_tokens[index + len(needle)] if index + len(needle) < len(title_tokens) else None
        if following is not None and (following.isdigit() or following in words):
            return True
    return False


def safe_print(text: object = "", *, file: Any = None) -> None:
    """``print`` that never fails on a console that cannot show a character.

    A Windows console in a legacy code page (cp932, cp1252...) raises ``UnicodeEncodeError`` on a
    character it has no glyph for, such as a Korean letter in a Wikimedia file name. The message is
    shown with ``?`` in place of what the console cannot encode.
    """
    stream = file if file is not None else sys.stdout
    message = str(text)
    try:
        print(message, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(message.encode(encoding, "replace").decode(encoding), file=stream)


def guarded_trace(callback: Any, log: Any = None) -> Any:
    """Wrap a trace callback so that a failure while reporting a step can never stop the step itself."""

    def say(message: str) -> None:
        try:
            callback(message)
        except Exception:
            if log is not None:
                log.debug("trace output failed", exc_info=True)

    return say


class ImageError(ValueError):
    """An image that cannot be used, with the reason."""


def load_image_config(path: str | Path | None = None) -> dict[str, Any]:
    """The ``images`` settings from config.yaml over the built-in defaults."""
    settings = dict(DEFAULTS)
    try:
        data = yaml.safe_load(Path(path or CONFIG_PATH).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    section = data.get("images") if isinstance(data, dict) else None
    if isinstance(section, dict):
        for key, default in DEFAULTS.items():
            value = section.get(key)
            if isinstance(default, list) and isinstance(value, list) and value:
                settings[key] = [str(item) for item in value]
            elif isinstance(default, int) and isinstance(value, int) and not isinstance(value, bool) and value > 0:
                settings[key] = value
            elif isinstance(default, float) and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                settings[key] = float(value)
    return settings


def aspect_problem(width: int, height: int, settings: dict | None = None) -> str | None:
    """Why a picture of this size is the wrong shape for a hero, or None when it is fine."""
    cfg = settings or load_image_config()
    if not width or not height:
        return "the picture has no size"
    ratio = width / height
    if ratio < cfg["min_aspect"]:
        return f"{width}x{height} is too tall and narrow (width must be at least {cfg['min_aspect']} times the height)"
    if ratio > cfg["max_aspect"]:
        return f"{width}x{height} is too panoramic (width must be at most {cfg['max_aspect']} times the height)"
    return None


def process_image(source: bytes | str | Path, dest: str | Path, *, settings: dict | None = None) -> dict[str, int]:
    """Resize ``source`` into a WebP file at ``dest``; return its ``width``, ``height`` and ``bytes``.

    The picture is rotated as its camera data says, then that data (location, device) is dropped,
    scaled down to fit ``max_width`` x ``max_height``, and encoded at the best quality that fits
    ``max_bytes``. A picture narrower than ``min_width`` is refused: it would be blurry at the top of a
    page. So is one of an absurd shape (more than twice as tall as wide, or more than 3.5 times as wide as
    tall). An ordinary portrait photo is accepted; the page shows it in a bounded frame.
    """
    cfg = settings or load_image_config()
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as error:  # Pillow raises several types for unreadable data
        raise ImageError(f"not a readable image: {error}") from error
    image = ImageOps.exif_transpose(image)
    if image.width < cfg["min_width"]:
        raise ImageError(f"image is {image.width}px wide (min {cfg['min_width']})")
    shape = aspect_problem(image.width, image.height, cfg)
    if shape:
        raise ImageError(shape)
    scale = min(1.0, cfg["max_width"] / image.width, cfg["max_height"] / image.height)
    if scale < 1.0:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    image = image.convert("RGB")
    encoded = b""
    for quality in (cfg["quality"], 75, 68, 60, 52):
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=quality, method=6)
        encoded = buffer.getvalue()
        if len(encoded) <= cfg["max_bytes"]:
            break
    else:
        raise ImageError(f"image is still {len(encoded)} bytes at the lowest quality (max {cfg['max_bytes']})")
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)
    return {"width": image.width, "height": image.height, "bytes": len(encoded)}


def set_hero(article: str, hero: dict[str, Any]) -> str:
    """The article text with its front matter's ``hero`` block set to ``hero`` (replacing any old one)."""
    match = re.match(r"\A(---\n)(.*?\n)(---\n?)(.*)\Z", article, re.DOTALL)
    if not match:
        raise ImageError("the article has no front matter")
    opening, front, closing, body = match.groups()
    kept: list[str] = []
    skipping = False
    for line in front.splitlines(keepends=True):
        if re.match(r"^hero:\s*$", line):
            skipping = True
            continue
        if skipping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skipping = False
        kept.append(line)
    block = yaml.safe_dump({"hero": hero}, sort_keys=False, allow_unicode=True, width=10_000)
    return opening + "".join(kept) + block + closing + body


def hero_failures(hero: Any, public_dir: str | Path | None = None, settings: dict | None = None) -> list[str]:
    """Failure messages for an article's ``hero`` (none when there is no hero)."""
    if hero is None:
        return []
    if not isinstance(hero, dict):
        return ["hero must be an object with src, alt, credit and license"]
    cfg = settings or load_image_config()
    failures: list[str] = []
    for key in ("src", "alt", "credit", "license"):
        if not str(hero.get(key) or "").strip():
            failures.append(f"hero.{key} is missing")
    src = str(hero.get("src") or "")
    if src and not SRC_RE.match(src):
        failures.append(f"hero.src must be a local /images/<name>.webp file, not {src!r}")
    alt = str(hero.get("alt") or "").strip()
    if alt and len(alt) < MIN_ALT_CHARS:
        failures.append(f"hero.alt is too short to describe the picture ({len(alt)} characters)")
    license_name = str(hero.get("license") or "").strip()
    allowed = {item.lower() for item in cfg["allowed_licenses"]}
    if license_name and license_name.lower() not in allowed:
        failures.append(f"hero.license {license_name!r} is not an allowed license ({', '.join(cfg['allowed_licenses'])})")
    source_url = hero.get("sourceUrl")
    if source_url and not str(source_url).startswith("https://"):
        failures.append("hero.sourceUrl must be an https link")
    license_url = hero.get("licenseUrl")
    if license_url and not str(license_url).startswith(LICENSE_URL_PREFIX):
        failures.append(f"hero.licenseUrl must be a link to {LICENSE_URL_PREFIX}")
    work_title = hero.get("title")
    if work_title is not None and (not isinstance(work_title, str) or not work_title.strip() or len(work_title) > MAX_TITLE_CHARS):
        failures.append(f"hero.title must be a short text (at most {MAX_TITLE_CHARS} characters)")
    if src and SRC_RE.match(src):
        path = Path(public_dir or PUBLIC_DIR) / src.lstrip("/")
        if not path.is_file():
            failures.append(f"hero file {src} does not exist")
        else:
            size = path.stat().st_size
            if size > cfg["max_bytes"]:
                failures.append(f"hero file is {size} bytes (max {cfg['max_bytes']})")
            try:
                with Image.open(path) as image:
                    width, height = image.size
            except Exception:
                failures.append(f"hero file {src} is not a readable image")
            else:
                if width < cfg["min_width"]:
                    failures.append(f"hero image is {width}px wide (min {cfg['min_width']})")
                shape = aspect_problem(width, height, cfg)
                if shape:
                    failures.append(f"hero image: {shape}")
                if (hero.get("width"), hero.get("height")) != (width, height):
                    failures.append(f"hero width and height must match the file ({width}x{height})")
    return failures


def add_hero(
    article_path: str | Path,
    file: str | Path,
    *,
    alt: str,
    credit: str,
    license: str,
    source_url: str | None = None,
    title: str | None = None,
    license_url: str | None = None,
    public_dir: str | Path | None = None,
    settings: dict | None = None,
) -> dict[str, Any]:
    """Process ``file`` into ``public/images/<slug>.webp`` and set the article's ``hero``; return it."""
    cfg = settings or load_image_config()
    article = Path(article_path)
    slug = article.stem
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", slug):
        raise ImageError(f"unusable article name: {slug!r}")
    if license.strip().lower() not in {item.lower() for item in cfg["allowed_licenses"]}:
        raise ImageError(f"{license!r} is not an allowed license ({', '.join(cfg['allowed_licenses'])})")
    root = Path(public_dir or PUBLIC_DIR)
    info = process_image(file, root / "images" / f"{slug}.webp", settings=cfg)
    hero: dict[str, Any] = {"src": f"/images/{slug}.webp", "alt": alt.strip(), "credit": credit.strip(), "license": license.strip()}
    if license_url:
        hero["licenseUrl"] = license_url.strip()
    if title:
        hero["title"] = title.strip()
    if source_url:
        hero["sourceUrl"] = source_url.strip()
    hero.update(width=info["width"], height=info["height"])
    problems = hero_failures(hero, root, cfg)
    if problems:
        (root / "images" / f"{slug}.webp").unlink(missing_ok=True)
        raise ImageError("; ".join(problems))
    article.write_text(set_hero(article.read_text(encoding="utf-8"), hero), encoding="utf-8")
    return hero


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.images", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add a hero image to an article")
    add.add_argument("--article", required=True, help="path to src/content/articles/<slug>.md")
    add.add_argument("--file", required=True, help="the picture to use")
    add.add_argument("--alt", required=True, help="what the picture shows, for screen readers")
    add.add_argument("--credit", required=True, help="who to credit, e.g. 'Nintendo' or 'Jane Doe'")
    add.add_argument("--license", required=True, help="one of the allowed licenses in config.yaml")
    add.add_argument("--source-url", default=None, help="where the picture came from (https)")
    add.add_argument("--title", default=None, help="the work's title, for Creative Commons credits")
    add.add_argument("--license-url", default=None, help="link to the Creative Commons license text")
    add.add_argument("--public-dir", default=None, help="the site's public folder (default: ./public)")
    args = parser.parse_args(argv)
    try:
        hero = add_hero(
            args.article, args.file, alt=args.alt, credit=args.credit, license=args.license,
            source_url=args.source_url, title=args.title, license_url=args.license_url, public_dir=args.public_dir,
        )
    except (ImageError, OSError) as error:
        safe_print(f"error: {error}", file=sys.stderr)
        return 1
    safe_print(f"added {hero['src']} ({hero['width']}x{hero['height']}) to {args.article}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --- a default picture when a story has no photo of its own ----------------------------------------------------------

#: the category used when an article has none (the pipeline's own default)
DEFAULT_PICTURE_CATEGORY = "gaming-news"


def default_hero(*, category: str | None, slug: str, public_dir: str | Path) -> dict[str, Any] | None:
    """GamersXpress's own picture for the article's category, for a story with no photo of its own.

    It is our own artwork (see pipeline/default_images.py), so its licence is "Own work" and no one needs to be
    credited beyond the site. Returns the same shape as the Steam and Commons lookups, or None if the file is missing.
    """
    name = category or DEFAULT_PICTURE_CATEGORY
    source = Path(public_dir) / "images" / f"default-{name}.webp"
    if not source.is_file():
        source = Path(public_dir) / "images" / f"default-{DEFAULT_PICTURE_CATEGORY}.webp"
    try:
        with Image.open(source) as picture:
            width, height = picture.size
    except (OSError, ValueError):
        return None
    label = name.replace("-", " ")
    hero: dict[str, Any] = {
        "src": f"/images/{slug}.webp",
        "alt": f"GamersXpress illustration for {label} news",
        "credit": "GamersXpress",
        "license": "Own work",
        "width": width,
        "height": height,
    }
    return {"hero": hero, "path": source, "name": name, "file": source.name}
