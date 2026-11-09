"""Automatic Steam store images for PC game stories (images stage 2).

When a story is about a game that has a Steam page, its key art is fetched from Valve's public
store data, resized into our own ``public/images/<slug>.webp`` and attached to the article as its
``hero`` (credit: the developer; license: ``Steam store``; link: the store page). The picture is
Valve's CDN copy of the developer's own promotional art, used to illustrate coverage of that game.
It is not a formal license, so this stage has a switch: ``images.steam.enabled`` in config.yaml.

The match is deliberately strict. A Steam app is used only when

* its name appears in the story's headline as a whole phrase (so "Path of Exile 2" matches the
  app of that name, and a headline that merely contains an everyday word does not);
* it is a game, not DLC, a demo, a soundtrack or a bundle; and
* it carries no adult-only sexual content descriptor.

A one-word game name must also be a proper name the headline stands on, not an ordinary word.

Anything that goes wrong (no match, no usable picture, a network error) yields no hero and never
stops a story from publishing: the article simply has no picture on the page.

All network access goes through one function, ``fetch(url) -> (status, content_type, body)``. The
default one refuses anything but https, any host outside Steam's, redirects, and bodies over 8 MB;
tests replace it.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import sys

import yaml

# Run as ``python -m pipeline.steam``, the sibling modules are not importable yet.
_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import cluster  # noqa: E402
import images as images_stage  # noqa: E402

logger = logging.getLogger("gamersxpress.pipeline.steam")

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
STORE_HOST = "store.steampowered.com"
ASSET_HOST_SUFFIX = ".steamstatic.com"
SEARCH_URL = "https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=US"
DETAILS_URL = "https://store.steampowered.com/api/appdetails?appids={app_id}&l=english"
STORE_PAGE_URL = "https://store.steampowered.com/app/{app_id}/"
LIBRARY_HERO_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_hero.jpg"
USER_AGENT = "GamersXpress/1.0 (+https://gamersxpress.com)"
MAX_FETCH_BYTES = 8_000_000
#: Steam content descriptors for adult-only and frequent sexual content: no picture for these.
ADULT_DESCRIPTORS = frozenset({3, 4})
#: How many screenshots to try after the wide library art.
MAX_SCREENSHOTS = 3

DEFAULTS: dict[str, Any] = {"enabled": True, "max_queries": 3, "timeout_seconds": 10}

Fetch = Callable[[str], "tuple[int, str, bytes]"]


class SteamError(RuntimeError):
    """A network or format problem talking to Steam."""


def load_steam_config(path: str | Path | None = None) -> dict[str, Any]:
    """The ``images.steam`` settings from config.yaml over the defaults."""
    settings = dict(DEFAULTS)
    try:
        data = yaml.safe_load(Path(path or CONFIG_PATH).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    section = ((data.get("images") or {}).get("steam")) if isinstance(data, dict) else None
    if isinstance(section, dict):
        if isinstance(section.get("enabled"), bool):
            settings["enabled"] = section["enabled"]
        for key in ("max_queries", "timeout_seconds"):
            value = section.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                settings[key] = value
    return settings


def allowed_url(url: object) -> bool:
    """Whether ``url`` is an https link to Steam's store or one of its asset servers."""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == STORE_HOST or host.endswith(ASSET_HOST_SUFFIX)) and not parts.username


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could leave Steam's hosts
        return None


def default_fetch(url: str, *, timeout: int = DEFAULTS["timeout_seconds"]) -> tuple[int, str, bytes]:
    """GET ``url`` (Steam hosts only) and return ``(status, content type, body)``."""
    if not allowed_url(url):
        raise SteamError(f"refusing to fetch {url!r}: not an https Steam link")
    opener = urllib.request.build_opener(_NoRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,image/*"})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_FETCH_BYTES + 1)
            if len(body) > MAX_FETCH_BYTES:
                raise SteamError(f"{url} is larger than {MAX_FETCH_BYTES} bytes")
            return response.status, response.headers.get("Content-Type", ""), body
    except urllib.error.HTTPError as error:
        return error.code, "", b""
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SteamError(f"could not reach Steam: {error}") from error


# --- matching ------------------------------------------------------------------------------------


def normalize(text: object) -> list[str]:
    """Lowercase word tokens: accents folded, trademark signs and apostrophes dropped."""
    # Trademark signs go first: folding accents would turn "™" into the letters "tm".
    cleaned = re.sub(r"[™®©℠'’‘`]", "", str(text or ""))
    return re.findall(r"[a-z0-9]+", cluster.strip_diacritics(cleaned).lower())


def _contains(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(haystack[i : i + len(needle)] == needle for i in range(len(haystack) - len(needle) + 1))


def proper_names(title: str, entities: list[str] | None) -> list[list[str]]:
    """The normalized proper-name phrases a headline stands on: its phrases and listed entities."""
    names = [normalize(phrase) for phrase in cluster.story_phrases(title)]
    title_tokens = normalize(title)
    names += [normalize(entity) for entity in entities or [] if _contains(title_tokens, normalize(entity))]
    return [name for name in names if name]


def search_terms(title: str, entities: list[str] | None, limit: int) -> list[str]:
    """What to search Steam for: the headline's proper-name phrases, longest first."""
    seen: set[str] = set()
    terms: list[str] = []
    candidates = list(cluster.story_phrases(title)) + [e for e in entities or [] if _contains(normalize(title), normalize(e))]
    for phrase in sorted(candidates, key=lambda item: -len(item)):
        key = " ".join(normalize(phrase))
        if key and key not in seen:
            seen.add(key)
            terms.append(phrase)
    return terms[:limit]


def match_game(items: list[dict], title: str, entities: list[str] | None = None) -> dict | None:
    """The search result whose name is in the headline as a whole phrase, longest name first."""
    title_tokens = normalize(title)
    names = proper_names(title, entities)
    best: tuple[int, dict] | None = None
    for item in items:
        tokens = normalize(item.get("name"))
        if not tokens or not _contains(title_tokens, tokens):
            continue
        if images_stage.continues_as_another_model(title_tokens, tokens):
            continue  # "Alan Wake" in "Alan Wake 3 announced" is an earlier game, not the one in the news
        if len(tokens) == 1 and tokens not in names:
            continue
        if best is None or len(tokens) > best[0]:
            best = (len(tokens), item)
    return best[1] if best else None


# --- talking to Steam ----------------------------------------------------------------------------


def _json(fetch: Fetch, url: str) -> Any:
    status, _type, body = fetch(url)
    if status != 200:
        raise SteamError(f"{url} answered {status}")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise SteamError(f"{url} did not return JSON") from error


def search_games(term: str, fetch: Fetch) -> list[dict]:
    """``{"id", "name"}`` for each app the store search returns for ``term``."""
    data = _json(fetch, SEARCH_URL.format(term=urllib.parse.quote(term)))
    results = []
    for item in (data.get("items") if isinstance(data, dict) else None) or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int) and item.get("name"):
            results.append({"id": item["id"], "name": str(item["name"])})
    return results


def game_details(app_id: int, fetch: Fetch) -> dict | None:
    """The app's details when it is a game that may be shown; None for anything else."""
    data = _json(fetch, DETAILS_URL.format(app_id=app_id))
    entry = data.get(str(app_id)) if isinstance(data, dict) else None
    details = entry.get("data") if isinstance(entry, dict) and entry.get("success") else None
    if not isinstance(details, dict) or details.get("type") != "game":
        return None
    descriptors = (details.get("content_descriptors") or {}).get("ids") or []
    if ADULT_DESCRIPTORS & {item for item in descriptors if isinstance(item, int)}:
        return None
    return details


def image_candidates(app_id: int, details: dict) -> list[str]:
    """Picture URLs to try, best first: the wide library art, then screenshots, then the backdrop."""
    urls = [LIBRARY_HERO_URL.format(app_id=app_id)]
    for shot in (details.get("screenshots") or [])[:MAX_SCREENSHOTS]:
        if isinstance(shot, dict) and shot.get("path_full"):
            urls.append(str(shot["path_full"]))
    if details.get("background_raw"):
        urls.append(str(details["background_raw"]))
    return [url for url in urls if allowed_url(url)]


def _credit(details: dict) -> str:
    for key in ("developers", "publishers"):
        names = [str(name).strip() for name in details.get(key) or [] if str(name).strip()]
        if names:
            return ", ".join(names[:2])
    return "Valve"


def find_hero(
    *,
    title: str,
    entities: list[str] | None,
    slug: str,
    public_dir: str | Path,
    fetch: Fetch | None = None,
    settings: dict | None = None,
    image_settings: dict | None = None,
    trace: Callable[[str], None] | None = None,
) -> dict | None:
    """Look up the story's game on Steam and write its picture; return ``{"hero", "path", ...}`` or None.

    ``public_dir`` is the folder the picture goes into (``<public_dir>/images/<slug>.webp``). The
    returned ``hero`` is the front-matter block for the article. Every step is reported through
    ``trace`` (the pipeline log by default; the ``check`` command prints it).
    """
    say = trace or logger.info
    cfg = settings or load_steam_config()
    if not cfg["enabled"]:
        return None
    fetch = fetch or (lambda url: default_fetch(url, timeout=cfg["timeout_seconds"]))
    try:
        game = None
        terms = search_terms(title, entities, cfg["max_queries"])
        say(f"steam: searching for {terms}")
        for term in terms:
            results = search_games(term, fetch)
            listed = f" ({', '.join(item['name'] for item in results[:5])})" if results else ""
            say(f"steam: searched {term!r}: {len(results)} result(s){listed}")
            game = match_game(results, title, entities)
            if game:
                break
        if game is None:
            say(f"steam: no game in the headline {title!r}")
            return None
        say(f"steam: matched {game['name']} (app {game['id']})")
        details = game_details(game["id"], fetch)
        if details is None:
            say(f"steam: {game['name']} ({game['id']}) is not a game that may be shown (DLC, demo, adult content or no data)")
            return None
        say(f"steam: {game['name']} is a game by {_credit(details)}")
        destination = Path(public_dir) / "images" / f"{slug}.webp"
        for url in image_candidates(game["id"], details):
            try:
                status, content_type, body = fetch(url)
            except SteamError as error:
                say(f"steam: {error}")
                continue
            say(f"steam: tried {url}: {status} {content_type or '-'}")
            if status != 200 or not content_type.lower().startswith("image/"):
                continue
            try:
                info = images_stage.process_image(body, destination, settings=image_settings)
            except images_stage.ImageError as error:
                say(f"steam: that picture is unusable: {error}")
                continue
            name = str(details.get("name") or game["name"])
            hero = {
                "src": f"/images/{slug}.webp",
                "alt": f"Key art for {name} from its Steam store page",
                "credit": _credit(details),
                "license": "Steam store",
                "sourceUrl": STORE_PAGE_URL.format(app_id=game["id"]),
                "width": info["width"],
                "height": info["height"],
            }
            say(f"steam: {name} ({game['id']}) -> {destination} ({info['width']}x{info['height']}, {info['bytes']} bytes)")
            return {"hero": hero, "path": destination, "app_id": game["id"], "name": name}
        say(f"steam: no usable picture for {game['name']} ({game['id']})")
    except SteamError as error:
        (trace or logger.warning)(f"steam: {error}")
    except Exception:  # an image must never stop a story from publishing
        logger.exception("steam: unexpected error")
    return None


# --- a command to check it by hand -------------------------------------------------------------------


def main(argv: list[str] | None = None, *, fetch: Fetch | None = None) -> int:
    """``python -m pipeline.steam check "<headline>"``: try the lookup for one headline and show every step.

    Exit code 0 when a picture was found and passes the hero check, 1 when there is no match or no
    usable picture, 2 when Steam could not be reached.
    """
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(prog="python -m pipeline.steam", description=main.__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="look up the Steam art for a headline")
    check.add_argument("headline", help='the article headline, e.g. "Path of Exile 2 launches on December 11, 2026"')
    check.add_argument("--entity", action="append", default=[], help="an entity of the story (repeatable)")
    check.add_argument("--out", default=None, help="folder to save the picture in (default: a temporary one)")
    args = parser.parse_args(argv)

    lines: list[str] = []

    def trace(message: str) -> None:
        lines.append(message)
        print(message)

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="gx-steam-check-"))
    settings = {**load_steam_config(), "enabled": True}
    result = find_hero(title=args.headline, entities=args.entity, slug="steam-check", public_dir=out,
                       fetch=fetch, settings=settings, trace=trace)
    if result is None:
        unreachable = any("could not reach Steam" in line for line in lines)
        print("\nRESULT: Steam could not be reached" if unreachable else "\nRESULT: no picture for this headline")
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
