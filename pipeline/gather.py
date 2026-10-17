"""Gather stage: fetch full text of every source in a cluster.

Takes a cluster (the output of the dedupe/cluster stage) and fetches the full
text of each item's URL. robots.txt is respected before every fetch (via
``urllib.robotparser``); denied or unresolvable robots files default to allow.
Pages are downloaded by :func:`fetch_html_and_url` (the default fetcher), which
follows redirects and reports the URL the response finally came from, and the
article text is extracted from that HTML with trafilatura. Failures are recorded
per source and never crash the stage.

Output is written to ``data/gathered/<cluster-id>.json``:

    {
      "id": "<cluster-id>",
      "gathered_at": "...",
      "fingerprint": {"format": 1, "value": "<hash of id + settings>"},
      "sources": [
        {
          "link": "...", "final_url": "...", "title": "...", "source_name": "...",
          "tier": 1, "owner": "...", "region": "us", "published": "...",
          "status": "ok", "text": "<full text>", "error": null,
          "text_origin": "page", "prose_chars": 1234, "prose_lines": 12,
          "body_status": "ok"
        }

``final_url`` is the URL the page finally resolved to after redirects (equal to
``link`` when there was none); the post-gather re-attribution credits the source
that owns ``final_url``, not the feed the item arrived from.
      ]
    }

When the file already exists and its stored :data:`fingerprint` still matches
the current one (the cluster id plus the settings that shape extraction), the
cached payload is returned without fetching anything; any mismatch -- including
a file written before fingerprints existed -- means the text is re-fetched and
the file overwritten, so stale gathered output is never reused.

Not every page's extracted text is article body: a navigation shell or a wall of
tile headlines can pass a naive character count. The text is measured in prose
lines (see :func:`prose_measure`) and each source carries ``prose_chars``,
``prose_lines``, ``body_status`` (``ok``, ``no_body`` or, once facts run,
``headline_only``) and ``text_origin`` (``page`` or ``feed``). When the page is
``no_body`` the feed entry's own ``content`` is used instead.

Source text is used ONLY by the facts stage (PLAN.md pipeline stage 5); it is
never passed to the writer.
"""

from __future__ import annotations

import configparser
import datetime as dt
import io
import json
import logging
import re
import time
import urllib.parse
import urllib.robotparser
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import trafilatura
import yaml
from trafilatura.downloads import DEFAULT_CONFIG as TRAFILATURA_DEFAULT_CONFIG

from cluster import FINGERPRINT_FORMAT, stage_fingerprint, stored_fingerprint
from ingest import registered_domain

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "gathered"

USER_AGENT = "GamersXpress/0.1 (+https://gamersxpress.com; news-pipeline)"
FETCH_TIMEOUT_SECONDS = 20
REQUEST_DELAY_SECONDS = 1.0
# Bump when fetch/extract handling that can change the stored text is tuned, so
# previously-gathered files read as stale and are re-fetched.
GATHER_SETTINGS_VERSION = 6

# A page's "text" is only usable as article body when enough of it is prose. A
# navigation shell, a wall of tile headlines or a "keep reading" strip can pass a
# naive character count, so the body is measured in prose lines instead: a line
# of at least :data:`PROSE_MIN_LINE_WORDS` words that ends in sentence
# punctuation and holds at least one lowercase word. A source is ``ok`` when its
# text has at least :data:`PROSE_MIN_CHARS` such characters across at least
# :data:`PROSE_MIN_LINES` lines; otherwise it is ``no_body`` (page and feed both
# empty of prose) and, once facts run, a ``headline_only`` source that may add
# only the claim its own title states.
PROSE_MIN_CHARS = 400
PROSE_MIN_LINES = 3
PROSE_MIN_LINE_WORDS = 8
#: Boilerplate lines shorter than this are dropped before measuring (a heading
#: or a title survives even when short).
PROSE_MIN_BOILERPLATE_WORDS = 4

#: Default boilerplate patterns, used when config.yaml has no ``gather`` block.
#: A line containing one of these as a whole-word phrase is dropped before the
#: prose measure, so navigation chrome and "keep reading" strips never count as
#: article body.
DEFAULT_BOILERPLATE_PATTERNS = (
    "skip to main content",
    "keep reading",
    "trending now",
    "opens in a new tab",
    "join game pass",
    "read more",
    "related",
    "share this",
    "subscribe",
    "cookie",
)

#: Sentence-final punctuation, optionally followed by a closing quote.
_SENTENCE_END_RE = re.compile(r'[.!?](?:["\')\u201d\u2019])?$')
#: A line of at least one lowercase word carries prose, not just Title Case chrome.
_HAS_LOWERCASE_WORD_RE = re.compile(r"(?<![A-Za-z])[a-z][A-Za-z'\-]*(?![A-Za-z])")

#: A dated URL path (``/2026/10/07/slug``) often carries a page's true
#: publication date, which the feed's own date can misreport (an "updated" or
#: crawl time). The date is captured here as ``url_date`` and the facts stage
#: decides which anchor to trust.
_URL_DATE_PATH_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})(?:/|$)")
#: Largest response body accepted for one source page. Mirrors trafilatura's
#: default ``MAX_FILE_SIZE`` (20 MB): the previous default fetcher streamed a
#: download up to this cap, and the stdlib fetcher enforces the same limit.
MAX_FILE_SIZE = 20_000_000

logger = logging.getLogger("gamersxpress.pipeline.gather")

ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser] = {}


def _trafilatura_settings(timeout: int = FETCH_TIMEOUT_SECONDS, user_agent: str = USER_AGENT) -> configparser.ConfigParser:
    """Trafilatura-compatible settings with our timeout and UA.

    A partial config must not be passed to trafilatura: it reads the whole
    [DEFAULT] section, so we round-trip its own default config and override.
    """
    buffer = io.StringIO()
    TRAFILATURA_DEFAULT_CONFIG.write(buffer)
    buffer.seek(0)
    settings = configparser.ConfigParser()
    settings.read_file(buffer)
    settings.set("DEFAULT", "download_timeout", str(int(timeout)))
    settings.set("DEFAULT", "user_agents", user_agent)
    return settings


def fetch_html(url: str) -> Optional[str]:
    """Download one page with trafilatura. Returns raw HTML or None."""
    return trafilatura.fetch_url(url, config=_trafilatura_settings())


def _looks_like_html(content_type: str, body: bytes) -> bool:
    """True when a response is HTML by its content type or opening bytes.

    An HTML content type is trusted; otherwise a body that opens like an HTML
    document is accepted. This mirrors the ingest stage's feed sniff and the
    "dubious HTML" test trafilatura applied to the previous default fetcher's
    output, so a JSON, image or PDF body is rejected instead of reaching
    extraction as if it were a page.
    """
    if "html" in (content_type or "").lower():
        return True
    head = body[:256].lstrip().lower()
    return head.startswith(b"<!doctype html") or b"<html" in head


def fetch_html_and_url(url: str) -> tuple[Optional[str], str]:
    """Download one page with urllib, returning ``(html, final_url)``.

    This is the default fetcher. It follows redirects and reports the URL the
    response finally came from, which the post-gather re-attribution needs to
    credit the page's real owner. It keeps the previous trafilatura default's
    User-Agent, timeout, size limit and HTML content-type check, but uses the
    stdlib so a blocked page (HTTP 403/429) raises on the first response and is
    never retried, and it never disguises itself as a browser.

    A non-HTML or oversized response is rejected with a logged warning (and a
    raised error the caller records); it is never silently retried through the
    old :func:`fetch_html` path, so a per-page difference stays visible.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        final_url = response.geturl() or url
        content_type = response.headers.get_content_type()
        try:
            charset = response.headers.get_content_charset()
        except Exception:  # noqa: BLE001 - fall back to utf-8
            charset = None
        # Read one byte past the cap so an oversized body is detected, not cut.
        raw = response.read(MAX_FILE_SIZE + 1)
    if len(raw) > MAX_FILE_SIZE:
        logger.warning(
            "fetch %s: response larger than %d bytes; rejecting (trafilatura default cap)",
            url, MAX_FILE_SIZE,
        )
        raise ValueError(f"response larger than {MAX_FILE_SIZE} bytes")
    if not _looks_like_html(content_type, raw):
        logger.warning(
            "fetch %s: body is not HTML (content-type=%s); rejecting", url, content_type,
        )
        raise ValueError(f"body is not HTML (content-type={content_type})")
    return raw.decode(charset or "utf-8", "replace"), final_url


def extract_text(html: str) -> Optional[str]:
    """Extract the article text from a downloaded page."""
    return trafilatura.extract(html) or None


def _load_robots_parser(host: str, user_agent: str, timeout: int) -> urllib.robotparser.RobotFileParser:
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(host.rstrip("/") + "/robots.txt")
    request = urllib.request.Request(
        parser.url, headers={"User-Agent": user_agent}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - default to allow if robots is unreadable
        logger.warning("robots.txt for %s unavailable (%s); defaulting to allow", host, exc)
        parser.modified()  # mark as checked so can_fetch falls through to allow
        return parser
    parser.parse(raw.splitlines())
    return parser


def robots_allowed(
    url: str,
    *,
    user_agent: str = USER_AGENT,
    timeout: int = FETCH_TIMEOUT_SECONDS,
    cache: dict | None = None,
) -> bool:
    """True when robots.txt permits fetching ``url`` (fetched once per host)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return True
    host = f"{parts.scheme}://{parts.netloc}"
    cache = ROBOTS_CACHE if cache is None else cache
    parser = cache.get(host)
    if parser is None:
        parser = _load_robots_parser(host, user_agent, timeout)
        cache[host] = parser
    return parser.can_fetch(user_agent, url)


def url_date(url: object, *, now: dt.datetime | None = None) -> Optional[str]:
    """The ISO date (``YYYY-MM-DD``) a dated URL path states, else ``None``.

    Reads a ``/YYYY/MM/DD/`` segment from the URL's path, validates that it is a
    real calendar date, and rejects a date in the future (relative to ``now``),
    so an obviously bogus path never becomes a publication anchor.
    """
    match = _URL_DATE_PATH_RE.search(urllib.parse.urlsplit(str(url or "")).path)
    if not match:
        return None
    try:
        date = dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    if now is not None:
        reference = now.date() if isinstance(now, dt.datetime) else now
        if date > reference:
            return None
    return date.isoformat()


def load_boilerplate_patterns(path: str | Path | None = None) -> list[str]:
    """The boilerplate line patterns from ``config.yaml`` ``gather``.

    Missing, unreadable or malformed config falls back to
    :data:`DEFAULT_BOILERPLATE_PATTERNS`, so the measure still strips the
    commonest chrome without a config file present.
    """
    config_path = Path(path) if path else PROJECT_ROOT / "pipeline" / "config.yaml"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return list(DEFAULT_BOILERPLATE_PATTERNS)
    patterns = (data.get("gather") or {}).get("boilerplate_patterns")
    if not isinstance(patterns, list):
        return list(DEFAULT_BOILERPLATE_PATTERNS)
    cleaned = [str(pattern).strip() for pattern in patterns if str(pattern).strip()]
    return cleaned or list(DEFAULT_BOILERPLATE_PATTERNS)


def _matches_boilerplate(line: str, patterns: list[str]) -> bool:
    """True when ``line`` is a boilerplate pattern (whole line or a leading CTA).

    Matching the whole line -- or the line's start, so "Read more about Xbox"
    still drops -- keeps a real sentence that merely contains a word like
    "related" from being discarded and shrinking the prose measure.
    """
    lowered = line.strip().lower()
    for pattern in patterns:
        needle = pattern.strip().lower()
        if not needle:
            continue
        if lowered == needle:
            return True
        if lowered.startswith(needle) and not lowered[len(needle):len(needle) + 1].isalnum():
            return True
    return False


def _is_heading(line: str, title: str | None = None) -> bool:
    """True when a short line is a markdown heading or the source's own title."""
    stripped = line.strip()
    if stripped.startswith("#"):
        return True
    if title and stripped.lower() == title.strip().lower():
        return True
    return False


def strip_boilerplate(text: object, patterns: list[str], *, title: str | None = None) -> str:
    """Drop navigation chrome and non-heading short lines before measuring.

    A line is removed when it matches a configured boilerplate pattern or holds
    fewer than :data:`PROSE_MIN_BOILERPLATE_WORDS` words and is not a heading.
    The stored ``text`` is left untouched; this only shapes the prose measure.
    """
    kept: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            kept.append(raw)
            continue
        if _matches_boilerplate(line, patterns):
            continue
        if len(line.split()) < PROSE_MIN_BOILERPLATE_WORDS and not _is_heading(line, title):
            continue
        kept.append(raw)
    return "\n".join(kept)


def _is_prose_line(line: str) -> bool:
    """True when a line reads as prose: long, lowercase-bearing, sentence-ended."""
    stripped = line.strip()
    if not stripped or not _SENTENCE_END_RE.search(stripped):
        return False
    if len(stripped.split()) < PROSE_MIN_LINE_WORDS:
        return False
    return bool(_HAS_LOWERCASE_WORD_RE.search(stripped))


def prose_measure(text: object) -> dict:
    """Measure prose in ``text``: its characters, line count and body status.

    ``body_status`` is ``ok`` when the text carries at least
    :data:`PROSE_MIN_CHARS` prose characters across at least
    :data:`PROSE_MIN_LINES` prose lines, else ``no_body``.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if _is_prose_line(line)]
    chars = sum(len(line) for line in lines)
    ok = chars >= PROSE_MIN_CHARS and len(lines) >= PROSE_MIN_LINES
    return {
        "prose_chars": chars,
        "prose_lines": len(lines),
        "body_status": "ok" if ok else "no_body",
    }


def measure_body(text: object, patterns: list[str], *, title: str | None = None) -> dict:
    """Strip boilerplate from ``text`` then measure its prose."""
    return prose_measure(strip_boilerplate(text, patterns, title=title))


def _no_body_fields(text_origin: str | None = None) -> dict:
    """The prose fields for a source that carries no article text at all."""
    return {
        "prose_chars": 0,
        "prose_lines": 0,
        "body_status": "no_body",
        "text_origin": text_origin,
    }


def gather(
    cluster: dict,
    *,
    output_dir: str | Path | None = None,
    id: str | None = None,
    now: dt.datetime | None = None,
    robots: bool = True,
    robots_call: Callable[[str], bool] = robots_allowed,
    fetcher: Callable[[str], object] = fetch_html_and_url,
    extractor: Callable[[str], Optional[str]] = extract_text,
    delay: float = REQUEST_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    boilerplate: list[str] | None = None,
) -> dict:
    """Fetch the full text of every item in ``cluster`` and save it."""
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    name = re.sub(r"[^0-9A-Za-z._-]", "-", str(id or cluster.get("id") or "unnamed"))
    now = now or dt.datetime.now(dt.timezone.utc)
    patterns = list(boilerplate) if boilerplate is not None else load_boilerplate_patterns()

    fingerprint = stage_fingerprint(
        name, "gather", GATHER_SETTINGS_VERSION, FETCH_TIMEOUT_SECONDS, USER_AGENT,
        PROSE_MIN_CHARS, PROSE_MIN_LINES, PROSE_MIN_LINE_WORDS,
        PROSE_MIN_BOILERPLATE_WORDS, tuple(patterns),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.json"
    if stored_fingerprint(path) == fingerprint:
        payload = json.loads(path.read_text(encoding="utf-8"))
        logger.info(
            "gather reused %s (fingerprint match): %d source(s) in cache",
            path, len(payload.get("sources") or []),
        )
        return payload

    sources: list[dict] = []
    for item in cluster.get("items") or []:
        url = item.get("link")
        base = {
            "link": url,
            "final_url": url,
            "title": item.get("title", ""),
            "source_name": item.get("source_name", "?"),
            "tier": item.get("tier"),
            "owner": item.get("owner", "?"),
            "region": item.get("region", "?"),
            "published": item.get("published"),
            "fetchable": item.get("fetchable", True),
        }
        if item.get("fetchable") is False:
            source = {**base, "status": "skipped", "text": "", "error": "source fetch disabled", **_no_body_fields()}
            logger.info("source '%s' %s skipped: fetch disabled", base["source_name"], url)
        elif robots and not robots_call(url):
            source = {**base, "status": "denied-robots", "text": "", "error": "disallowed by robots.txt", **_no_body_fields()}
            logger.info("source '%s' %s skipped: robots.txt", base["source_name"], url)
        else:
            source = _fetch_one(
                base,
                url,
                fetcher,
                extractor,
                content=str(item.get("content") or ""),
                patterns=patterns,
                title=str(base.get("title") or ""),
            )
        # The URL date comes from the final URL after redirects, when it has one.
        source["url_date"] = url_date(source.get("final_url") or url, now=now)
        sources.append(source)
        sleep(delay)

    payload = {
        "id": name,
        "gathered_at": now.isoformat(),
        "fingerprint": {"format": FINGERPRINT_FORMAT, "value": fingerprint},
        "sources": sources,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ok = sum(1 for source in sources if source["status"] == "ok")
    logger.info("gather complete: %d source(s), %d with text -> %s", len(sources), ok, path)
    return payload


def _fetch_one(
    base: dict,
    url: str,
    fetcher: Callable[[str], object],
    extractor: Callable[[str], Optional[str]],
    *,
    content: str = "",
    patterns: list[str] | None = None,
    title: str = "",
) -> dict:
    patterns = patterns if patterns is not None else list(DEFAULT_BOILERPLATE_PATTERNS)
    final_url = url
    try:
        result = fetcher(url)
        if isinstance(result, tuple):
            html = result[0]
            final_url = result[1] or url
        else:
            html = result
    except Exception as exc:  # noqa: BLE001 - record, never crash the stage
        return {**base, "status": "fetch-error", "text": "", "error": str(exc), "final_url": final_url, **_no_body_fields()}
    if not html:
        return {**base, "status": "fetch-error", "text": "", "error": "no response body", "final_url": final_url, **_no_body_fields()}
    try:
        text = extractor(html)
    except Exception as exc:  # noqa: BLE001 - record, never crash the stage
        return {**base, "status": "extract-error", "text": "", "error": str(exc), "final_url": final_url, **_no_body_fields()}
    if not text:
        return {**base, "status": "empty", "text": "", "error": None, "final_url": final_url, **_no_body_fields()}

    # The page's own text is preferred, but a navigation shell measures no_body:
    # when the feed carried the article (content:encoded), fall back to that.
    page = measure_body(text, patterns, title=title)
    if page["body_status"] == "ok":
        return {
            **base, "status": "ok", "text": text, "error": None, "final_url": final_url,
            "text_origin": "page", **page,
        }
    if content:
        feed = measure_body(content, patterns, title=title)
        return {
            **base, "status": "ok", "text": content, "error": None, "final_url": final_url,
            "text_origin": "feed", **feed,
        }
    # Neither the page nor the feed carried prose: the source may still add the
    # single claim its own title states.
    return {
        **base, "status": "ok", "text": text, "error": None, "final_url": final_url,
        "text_origin": "page", "prose_chars": page["prose_chars"],
        "prose_lines": page["prose_lines"], "body_status": "headline_only",
    }


def load_fetch_health(path: str | Path | None = None) -> dict:
    """Load ``data/fetch-health.json``; missing or malformed files start empty."""
    if path is None:
        return {"domains": {}}
    path = Path(path)
    if not path.exists():
        return {"domains": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"domains": {}}
    if not isinstance(data, dict):
        return {"domains": {}}
    if not isinstance(data.get("domains"), dict):
        data["domains"] = {}
    return data


def update_fetch_health(
    gathered: dict,
    path: str | Path,
    *,
    now: dt.datetime | None = None,
) -> dict:
    """Accumulate per-domain fetch success/failure counts for ``gathered``.

    Keys are registrable domains (``xbox.com``, not ``news.xbox.com``), so the
    owner can see which outlets are actually fetchable over time.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    data = load_fetch_health(path)
    domains: dict[str, dict] = data["domains"]
    for source in gathered.get("sources") or []:
        status = str(source.get("status") or "")
        if status == "skipped":
            continue
        domain = registered_domain(str(source.get("final_url") or source.get("link") or ""))
        if not domain:
            continue
        row = domains.setdefault(
            domain,
            {"successes": 0, "failures": 0, "last_failure": None,
             "last_failure_reason": None, "last_success": None},
        )
        if status == "ok":
            row["successes"] = int(row.get("successes") or 0) + 1
            row["last_success"] = now.isoformat()
        else:
            row["failures"] = int(row.get("failures") or 0) + 1
            row["last_failure"] = now.isoformat()
            row["last_failure_reason"] = source.get("error") or status
    data["updated_at"] = now.isoformat()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def print_fetch_health_summary(data: dict, *, logger_: logging.Logger | None = None) -> list[str]:
    """Return ``domain ok/fail`` lines for the fetch-health payload."""
    lines: list[str] = []
    for domain, row in sorted((data.get("domains") or {}).items()):
        ok = int(row.get("successes") or 0)
        fail = int(row.get("failures") or 0)
        lines.append(f"fetch health: {domain} {ok} ok / {fail} failed")
    log = logger_ or logger
    for line in lines:
        log.info(line)
    return lines


__all__ = [
    "gather",
    "fetch_html",
    "fetch_html_and_url",
    "extract_text",
    "robots_allowed",
    "url_date",
    "ROBOTS_CACHE",
    "load_fetch_health",
    "update_fetch_health",
    "print_fetch_health_summary",
    "prose_measure",
    "measure_body",
    "strip_boilerplate",
    "load_boilerplate_patterns",
]