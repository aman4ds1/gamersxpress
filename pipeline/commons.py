"""Automatic Wikimedia Commons photos for hardware stories (images stage 3).

When a hardware story's headline names a product that has its own category on Wikimedia Commons
(for example "PlayStation 5" or "Steam Deck"), a freely licensed photo from that category is
resized into our own ``public/images/<slug>.webp`` and attached as the article's ``hero``, with the
credit Creative Commons asks for: the work's title, its author, a link to the source page and the
license name with a link to the license text.

A wrong picture is worse than none, so every step is strict:

* only articles in the configured categories (``images.commons.categories``, default hardware and
  tech) are considered;
* the Commons category must be named *exactly* like the product in the headline (so "Nintendo
  Switch 2" never finds the original Switch's photos) and must sit under a hardware parent category
  (consoles, graphics cards, controllers...), which keeps out games, people and companies;
* a photo must itself say what it shows: every word of the product name must be in its file title or
  description;
* its license must be one we allow (public domain, CC0, CC BY, CC BY-SA), it must name an author
  unless it is public domain, and it must carry no personality-rights restriction;
* it must be a real photo, at least 1200 px wide, not a logo, diagram, render, toy or box shot.

Anything that goes wrong (no category, no suitable photo, a network error) yields no hero and never
stops a story from publishing. All network access goes through one function,
``fetch(url) -> (status, content_type, body)``; the default one fetches only Wikimedia's own hosts
over https, refuses redirects and bodies over 8 MB, and identifies itself as Wikimedia asks.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml

_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import cluster  # noqa: E402
import images as images_stage  # noqa: E402

logger = logging.getLogger("gamersxpress.pipeline.commons")

CONFIG_PATH = _PIPELINE_DIR / "config.yaml"
API_HOST = "commons.wikimedia.org"
UPLOAD_HOST = "upload.wikimedia.org"
API_URL = "https://commons.wikimedia.org/w/api.php?"
FILE_PAGE_URL = "https://commons.wikimedia.org/wiki/{title}"
USER_AGENT = "GamersXpress/1.0 (https://gamersxpress.com; image lookup; python-urllib)"
MAX_FETCH_BYTES = 8_000_000
THUMB_WIDTH = 1600
MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MIN_FILES_IN_CATEGORY = 3

#: A parent category marks the product's category as hardware when its name contains one of these
#: phrases ("Video game consoles", "Nvidia graphics cards")...
HARDWARE_PARENT_PHRASES = (
    "game console", "game controller", "gamepad", "video game hardware", "graphics card", "graphics processing unit",
    "video card", "computer hardware", "microprocessor", "solid-state drive", "computer monitor",
    "virtual reality headset", "computer keyboard", "computer mice", "computer mouse", "motherboard",
)
#: ...or ends with one of these words ("PlayStation consoles", "Xbox controllers"), which keeps out
#: "Console games" and "Video games by console".
HARDWARE_PARENT_ENDINGS = ("consoles", "controllers", "gpus", "processors", "headsets")
#: A photo whose file title has one of these words is not a photograph of the product itself.
EXCLUDED_TITLE_WORDS = frozenset({
    "logo", "icon", "diagram", "schematic", "map", "screenshot", "render", "concept", "cosplay", "cosplayer",
    "figure", "figurine", "statue", "toy", "lego", "painting", "poster", "box", "packaging", "advert",
    "advertisement", "unboxing", "drawing", "sketch", "illustration", "collage", "montage",
})

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "categories": ["hardware", "tech"],
    "share_alike": True,
    "min_width": 1200,
    "max_files": 40,
    "max_candidates": 5,
    "max_queries": 3,
    "timeout_seconds": 10,
}

Fetch = Callable[[str], "tuple[int, str, bytes]"]


class CommonsError(RuntimeError):
    """A network or format problem talking to Wikimedia Commons."""


def load_commons_config(path: str | Path | None = None) -> dict[str, Any]:
    """The ``images.commons`` settings from config.yaml over the defaults."""
    settings = dict(DEFAULTS)
    try:
        data = yaml.safe_load(Path(path or CONFIG_PATH).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    section = ((data.get("images") or {}).get("commons")) if isinstance(data, dict) else None
    if isinstance(section, dict):
        for key in ("enabled", "share_alike"):
            if isinstance(section.get(key), bool):
                settings[key] = section[key]
        categories = section.get("categories")
        if isinstance(categories, list):
            settings["categories"] = [str(item) for item in categories]
        for key in ("min_width", "max_files", "max_candidates", "max_queries", "timeout_seconds"):
            value = section.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                settings[key] = value
    return settings


def allowed_url(url: object) -> bool:
    """Whether ``url`` is an https link to Commons' API or its upload server."""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() in {API_HOST, UPLOAD_HOST} and not parts.username


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could leave Wikimedia's hosts
        return None


def default_fetch(url: str, *, timeout: int = DEFAULTS["timeout_seconds"]) -> tuple[int, str, bytes]:
    """GET ``url`` (Wikimedia hosts only) and return ``(status, content type, body)``."""
    if not allowed_url(url):
        raise CommonsError(f"refusing to fetch {url!r}: not an https Wikimedia link")
    opener = urllib.request.build_opener(_NoRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,image/*"})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_FETCH_BYTES + 1)
            if len(body) > MAX_FETCH_BYTES:
                raise CommonsError(f"{url} is larger than {MAX_FETCH_BYTES} bytes")
            return response.status, response.headers.get("Content-Type", ""), body
    except urllib.error.HTTPError as error:
        return error.code, "", b""
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CommonsError(f"could not reach Wikimedia Commons: {error}") from error


# --- text helpers --------------------------------------------------------------------------------


def normalize(text: object) -> list[str]:
    """Lowercase word tokens: accents folded, trademark signs and apostrophes dropped."""
    cleaned = re.sub(r"[™®©℠'’‘`]", "", str(text or ""))
    return re.findall(r"[a-z0-9]+", cluster.strip_diacritics(cleaned).lower())


def clean_text(value: object) -> str:
    """Plain text from Commons' HTML snippets (an author link, a description)."""
    text = re.sub(r"<[^>]*>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def file_title(page_title: str) -> str:
    """A readable title from ``File:Some_name.jpg``."""
    name = re.sub(r"^File:", "", str(page_title or ""))
    name = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name)
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


# --- licenses ------------------------------------------------------------------------------------

_CC_RE = re.compile(r"^CC[ -]BY(-SA)?[ -](\d\.\d)$", re.IGNORECASE)


def license_info(short_name: object, config: dict | None = None) -> dict | None:
    """``{"label", "url", "attribution"}`` for a license we may use, or None.

    Only exact Creative Commons short names are accepted (``CC BY-SA 4.0``; ported or unversioned
    ones are not), plus CC0 and public domain. A version must be on the allowed list in config.yaml
    under ``images``, and ShareAlike can be switched off.
    """
    cfg = config or load_commons_config()
    name = re.sub(r"\s+", " ", str(short_name or "")).strip()
    allowed = {item.lower() for item in images_stage.load_image_config()["allowed_licenses"]}
    lowered = name.lower()
    if lowered in {"cc0", "cc0 1.0", "cc0 1.0 universal"} and "cc0" in allowed:
        return {"label": "CC0", "url": "https://creativecommons.org/publicdomain/zero/1.0/", "attribution": False}
    if lowered in {"public domain", "pd"} and "public domain" in allowed:
        return {"label": "Public domain", "url": None, "attribution": False}
    match = _CC_RE.match(name)
    if not match:
        return None
    share_alike, version = bool(match.group(1)), match.group(2)
    if share_alike and not cfg["share_alike"]:
        return None
    label = f"CC BY{'-SA' if share_alike else ''} {version}"
    if label.lower() not in allowed:
        return None
    return {"label": label, "url": f"https://creativecommons.org/licenses/{'by-sa' if share_alike else 'by'}/{version}/", "attribution": True}


# --- matching ------------------------------------------------------------------------------------


def product_phrases(title: str, entities: list[str] | None, limit: int) -> list[str]:
    """Product names to look for: the headline's proper-name phrases of two or more words, longest first."""
    title_tokens = normalize(title)

    def in_title(phrase: str) -> bool:
        tokens = normalize(phrase)
        return bool(tokens) and any(title_tokens[i : i + len(tokens)] == tokens for i in range(len(title_tokens) - len(tokens) + 1))

    candidates = list(cluster.story_phrases(title)) + [entity for entity in entities or [] if in_title(entity)]
    seen: set[str] = set()
    phrases: list[str] = []
    for phrase in sorted(candidates, key=lambda item: -len(item)):
        key = " ".join(normalize(phrase))
        if images_stage.continues_as_another_model(title_tokens, normalize(phrase), images_stage.HARDWARE_MODEL_WORDS):
            continue  # "Steam Deck" in "Steam Deck 2 teased" is the older model: its photo would mislead
        if len(normalize(phrase)) >= 2 and key not in seen:
            seen.add(key)
            phrases.append(phrase)
    return phrases[:limit]


def is_hardware_category(parents: list[str]) -> bool:
    """Whether any parent category's name marks the category as hardware."""
    phrases = [" ".join(normalize(phrase)) for phrase in HARDWARE_PARENT_PHRASES]
    for parent in parents:
        tokens = normalize(re.sub(r"^Category:", "", str(parent)))
        name = " ".join(tokens)
        if any(phrase in name for phrase in phrases) or (tokens and tokens[-1] in HARDWARE_PARENT_ENDINGS):
            return True
    return False


def says_product(info: dict, phrase: str) -> bool:
    """Whether the photo's own title or description names every word of the product."""
    wanted = set(normalize(phrase))
    have = set(normalize(info.get("title"))) | set(normalize(info.get("description")))
    return bool(wanted) and wanted <= have


def unusable_reason(info: dict, phrase: str, config: dict) -> str | None:
    """Why a photo must not be used, or None when it may be."""
    if info.get("mime") not in MIME_TYPES:
        return f"not a photo format ({info.get('mime')})"
    if (info.get("width") or 0) < config["min_width"]:
        return f"only {info.get('width')} px wide (min {config['min_width']})"
    if not info.get("thumb_url"):
        return "no picture link"
    if license_info(info.get("license"), config) is None:
        return f"license {info.get('license')!r} is not allowed"
    licence = license_info(info.get("license"), config)
    if licence["attribution"] and not info.get("artist"):
        return "no named author to credit"
    if "personality" in str(info.get("restrictions") or "").lower():
        return "has a personality-rights restriction"
    bad = EXCLUDED_TITLE_WORDS & set(normalize(info.get("title")))
    if bad:
        return f"not a plain product photo ({', '.join(sorted(bad))})"
    if not says_product(info, phrase):
        return f"its title and description do not name {phrase!r}"
    return None


def rank(info: dict, phrase: str) -> tuple:
    """Sort key, best first: product named in the title, landscape, then width."""
    width, height = info.get("width") or 1, info.get("height") or 1
    landscape = 1.3 <= width / height <= 2.4
    named_in_title = set(normalize(phrase)) <= set(normalize(info.get("title")))
    return (0 if named_in_title else 1, 0 if landscape else 1, -width, info.get("title", ""))


# --- talking to Commons ----------------------------------------------------------------------------


def _api(fetch: Fetch, **params: Any) -> dict:
    query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    status, _type, body = fetch(API_URL + query)
    if status != 200:
        raise CommonsError(f"Commons answered {status}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise CommonsError("Commons did not return JSON") from error
    if not isinstance(data, dict) or "error" in data:
        raise CommonsError(f"Commons reported an error: {str((data or {}).get('error'))[:120] if isinstance(data, dict) else data}")
    return data


def find_category(phrase: str, fetch: Fetch) -> dict | None:
    """``{"title", "files", "parents"}`` for the hardware category named exactly ``phrase``, else None."""
    data = _api(fetch, action="query", titles=f"Category:{phrase}", prop="categoryinfo|categories", cllimit="50", clshow="!hidden")
    pages = (data.get("query") or {}).get("pages") or []
    page = pages[0] if pages and isinstance(pages[0], dict) else None
    if not page:
        return None   # a category that does not exist has no files, which the checks below refuse
    title = str(page.get("title") or "")
    if normalize(re.sub(r"^Category:", "", title)) != normalize(phrase):
        return None
    files = int((page.get("categoryinfo") or {}).get("files") or 0)
    parents = [str(item.get("title") or "") for item in page.get("categories") or [] if isinstance(item, dict)]
    if files < MIN_FILES_IN_CATEGORY or not is_hardware_category(parents):
        return None
    return {"title": title, "files": files, "parents": parents}


def _meta(metadata: dict, key: str) -> str:
    value = metadata.get(key)
    return clean_text(value.get("value") if isinstance(value, dict) else value)


def list_files(category_title: str, fetch: Fetch, limit: int) -> list[dict]:
    """The category's direct file members with their picture links and license data."""
    data = _api(
        fetch, action="query", generator="categorymembers", gcmtitle=category_title, gcmtype="file", gcmlimit=str(limit),
        prop="imageinfo", iiprop="url|size|mime|extmetadata", iiurlwidth=str(THUMB_WIDTH),
    )
    results: list[dict] = []
    for page in (data.get("query") or {}).get("pages") or []:
        infos = page.get("imageinfo") if isinstance(page, dict) else None
        if not infos or not isinstance(infos[0], dict):
            continue
        info, metadata = infos[0], infos[0].get("extmetadata") or {}
        page_title = str(page.get("title") or "")
        results.append({
            "page_title": page_title,
            "title": file_title(page_title),
            "thumb_url": info.get("thumburl") or info.get("url"),
            "width": info.get("width"),
            "height": info.get("height"),
            "mime": info.get("mime"),
            "license": _meta(metadata, "LicenseShortName"),
            "artist": _meta(metadata, "Artist"),
            "description": _meta(metadata, "ImageDescription"),
            "restrictions": _meta(metadata, "Restrictions"),
        })
    return results


def find_hero(
    *,
    title: str,
    entities: list[str] | None,
    slug: str,
    public_dir: str | Path,
    category: str | None = None,
    fetch: Fetch | None = None,
    settings: dict | None = None,
    image_settings: dict | None = None,
    trace: Callable[[str], None] | None = None,
) -> dict | None:
    """Look for a freely licensed photo of the product in the headline; return ``{"hero", "path", ...}`` or None."""
    say = trace or logger.info
    cfg = settings or load_commons_config()
    if not cfg["enabled"]:
        return None
    if category is not None and category not in cfg["categories"]:
        say(f"commons: skipped, the article category {category!r} is not one of {cfg['categories']}")
        return None
    fetch = fetch or (lambda url: default_fetch(url, timeout=cfg["timeout_seconds"]))
    try:
        phrases = product_phrases(title, entities, cfg["max_queries"])
        say(f"commons: looking for a product category among {phrases}")
        for phrase in phrases:
            found = find_category(phrase, fetch)
            if found is None:
                say(f"commons: no hardware category named exactly {phrase!r}")
                continue
            say(f"commons: {found['title']} ({found['files']} files, under {', '.join(found['parents'][:3])})")
            files = list_files(found["title"], fetch, cfg["max_files"])
            usable = []
            for info in files:
                reason = unusable_reason(info, phrase, cfg)
                if reason:
                    say(f"commons: skipped {info['title']!r}: {reason}")
                else:
                    usable.append(info)
            say(f"commons: {len(usable)} of {len(files)} photos are usable")
            for info in sorted(usable, key=lambda item: rank(item, phrase))[: cfg["max_candidates"]]:
                hero = _try_photo(info, phrase, slug, public_dir, fetch, image_settings, say, cfg)
                if hero:
                    return hero
        say(f"commons: no usable photo for {title!r}")
    except CommonsError as error:
        (trace or logger.warning)(f"commons: {error}")
    except Exception:  # an image must never stop a story from publishing
        logger.exception("commons: unexpected error")
    return None


def _try_photo(info, phrase, slug, public_dir, fetch, image_settings, say, cfg) -> dict | None:
    url = str(info["thumb_url"])
    if not allowed_url(url):
        say(f"commons: skipped {info['title']!r}: picture link is not on Wikimedia's servers")
        return None
    try:
        status, content_type, body = fetch(url)
    except CommonsError as error:
        say(f"commons: {error}")
        return None
    say(f"commons: tried {url}: {status} {content_type or '-'}")
    if status != 200 or not content_type.lower().startswith("image/"):
        return None
    destination = Path(public_dir) / "images" / f"{slug}.webp"
    try:
        size = images_stage.process_image(body, destination, settings=image_settings)
    except images_stage.ImageError as error:
        say(f"commons: that picture is unusable: {error}")
        return None
    licence = license_info(info["license"], cfg)
    page_url = FILE_PAGE_URL.format(title=urllib.parse.quote(info["page_title"].replace(" ", "_"), safe=":/_(),'-."))
    hero: dict[str, Any] = {
        "src": f"/images/{slug}.webp",
        "alt": f"Photo of the {phrase}",
        "credit": info["artist"][:100] if info["artist"] else "Wikimedia Commons",
        "license": licence["label"],
    }
    if licence["url"]:
        hero["licenseUrl"] = licence["url"]
    hero.update(title=info["title"][:120], sourceUrl=page_url, width=size["width"], height=size["height"])
    say(f"commons: {info['title']!r} by {hero['credit']} ({hero['license']}) -> {destination} ({size['width']}x{size['height']})")
    return {"hero": hero, "path": destination, "name": phrase, "file": info["page_title"]}


# --- a command to check it by hand -------------------------------------------------------------------


def main(argv: list[str] | None = None, *, fetch: Fetch | None = None) -> int:
    """``python -m pipeline.commons check "<headline>"``: try the lookup for one headline and show every step.

    Exit code 0 when a photo was found and passes the hero check, 1 when there is none, 2 when Wikimedia
    could not be reached.
    """
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(prog="python -m pipeline.commons", description=main.__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="look up a Commons photo for a headline")
    check.add_argument("headline", help='the article headline, e.g. "PlayStation 5 Pro price rises in Europe"')
    check.add_argument("--entity", action="append", default=[], help="an entity of the story (repeatable)")
    check.add_argument("--category", default="hardware", help="the article's category (default: hardware)")
    check.add_argument("--out", default=None, help="folder to save the picture in (default: a temporary one)")
    args = parser.parse_args(argv)

    lines: list[str] = []

    def trace(message: str) -> None:
        lines.append(message)
        print(message)

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="gx-commons-check-"))
    settings = {**load_commons_config(), "enabled": True}
    result = find_hero(title=args.headline, entities=args.entity, slug="commons-check", public_dir=out, category=args.category,
                       fetch=fetch, settings=settings, trace=trace)
    if result is None:
        unreachable = any("could not reach Wikimedia" in line for line in lines)
        print("\nRESULT: Wikimedia Commons could not be reached" if unreachable else "\nRESULT: no photo for this headline")
        return 2 if unreachable else 1
    problems = images_stage.hero_failures(result["hero"], out)
    print(f"\nhero: {json.dumps(result['hero'], indent=2)}")
    print(f"saved: {result['path']}")
    if problems:
        print("hero check FAILED: " + "; ".join(problems))
        return 1
    print("hero check: passed\nRESULT: working")
    return 0


if __name__ == "__main__":
    sys.exit(main())
