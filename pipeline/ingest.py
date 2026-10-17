"""Ingest stage: fetch RSS feeds and write fresh candidates.

Reads the ``sources`` block from ``pipeline/config.yaml`` and validates it at
startup (every entry needs name, site, feed_url, tier 1/2/3, owner, region,
categories, and topic "gaming" or "mixed"). Each feed is fetched once with a
GamersXpress User-Agent, a timeout,
and a short delay between requests. Items older than 48 hours are dropped, links
are normalized, and the kept items are written to
``data/candidates/<timestamp>.json``.

Every run also writes ``data/reports/feed-health.json`` (one row per feed:
status, content-type, byte count, outcome, entry counts, newest entry) and prints
the same information as a table.

Conditional requests: when ``data/feed-cache.json`` holds an ETag or a
Last-Modified value for a feed from a previous run, ``If-None-Match`` and
``If-Modified-Since`` are sent. The cache is committed and updated each run.

Outcomes: ``ok``, ``blocked`` (403/429/503 or an HTML body where XML was
expected), ``invalid-xml``, ``timeout``, ``empty``, ``http-error``, and
``not-modified`` (HTTP 304). Broken and blocked feeds are logged and skipped
without failing the run, and blocked feeds are never retried within a run.

Freshness is tier-aware. The ``freshness`` block in config.yaml sets a window per
tier (how old a feed item may be before ingest drops it) and a separate
``stale_after`` cap (how old a story's NEWEST item may be before scoring treats
it as stale). Every kept item carries ``age_hours`` so recency can be weighed
downstream.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

import feedparser
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config.yaml")
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "candidates"
DEFAULT_CACHE_PATH = PROJECT_ROOT / "data" / "feed-cache.json"
DEFAULT_HEALTH_PATH = PROJECT_ROOT / "data" / "reports" / "feed-health.json"

VALID_TIERS: tuple[int, ...] = (1, 2, 3)
VALID_TOPICS: tuple[str, ...] = ("gaming", "mixed")
REQUIRED_FIELDS: tuple[str, ...] = ("name", "site", "feed_url", "tier", "owner", "region", "categories", "topic")
USER_AGENT = "GamersXpress/0.1 (+https://gamersxpress.com; news-pipeline)"
FETCH_TIMEOUT_SECONDS = 20
REQUEST_DELAY_SECONDS = 1.0
KEEP_WINDOW = dt.timedelta(hours=48)
BLOCK_STATUSES: tuple[int, ...] = (403, 429, 503)

DEFAULT_FRESHNESS_WINDOWS: dict[int, dt.timedelta] = {
    1: dt.timedelta(days=7),
    2: dt.timedelta(hours=48),
    3: dt.timedelta(hours=24),
}
DEFAULT_STALE_AFTER = dt.timedelta(days=7)

_DURATION_UNITS = {
    "week": 7 * 24, "weeks": 7 * 24,
    "day": 24, "days": 24,
    "hour": 1, "hours": 1,
    "minute": 1 / 60, "minutes": 1 / 60,
    "second": 1 / 3600, "seconds": 1 / 3600,
}

VALID_OUTCOMES = ("ok", "blocked", "invalid-xml", "timeout", "empty", "http-error", "not-modified")

logger = logging.getLogger("gamersxpress.pipeline.ingest")


class IngestError(Exception):
    """Feed-level failure; logged and skipped, never fatal."""


class ConfigError(IngestError):
    """The sources block in config.yaml is invalid; the run must not start."""


class FetchError(IngestError):
    """A feed could not be fetched. ``kind`` is 'timeout' or 'network'."""

    def __init__(self, message: str, *, kind: str = "network") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class HttpResponse:
    status: int
    content_type: Optional[str]
    body: bytes
    etag: Optional[str] = None
    last_modified: Optional[str] = None


@dataclass
class FeedCandidates:
    total_entries: int
    newest: Optional[str]
    items: list[dict]


@dataclass
class FeedResult:
    name: str
    tier: int
    url: str
    status: Optional[int]
    content_type: Optional[str]
    bytes_received: Optional[int]
    outcome: str
    total_entries: int
    newest: Optional[str]
    window_count: int
    window_hours: float = 0.0
    error: Optional[str] = None


def load_sources(path: str | Path | None = None) -> list[dict]:
    """Load and validate the sources block, refusing to run on any problem."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ConfigError(f"{path}: 'sources' must be a non-empty list")

    problems: list[str] = []
    for index, entry in enumerate(sources):
        label = entry.get("name", f"sources[{index}]") if isinstance(entry, dict) else "?"
        if not isinstance(entry, dict):
            problems.append(f"{path}: sources[{index}] is not a mapping ({entry!r})")
            continue
        for field in REQUIRED_FIELDS:
            value = entry.get(field)
            if value is None or value == "":
                problems.append(f"{path}: {label} is missing '{field}'")
        tier = entry.get("tier")
        if tier not in VALID_TIERS:
            problems.append(
                f"{path}: {label} has invalid tier {tier!r}; expected one of {VALID_TIERS}"
            )
        if not isinstance(entry.get("categories"), list) or not entry.get("categories"):
            problems.append(f"{path}: {label} categories must be a non-empty list")
        if entry.get("topic") not in VALID_TOPICS:
            problems.append(
                f"{path}: {label} has invalid topic {entry.get('topic')!r}; expected one of {VALID_TOPICS}"
            )
    if problems:
        raise ConfigError("; ".join(problems))
    return sources


def _parse_duration(value: object, *, what: str) -> dt.timedelta:
    """Parse a "2 days"-style string (or bare hours number) into a timedelta."""
    if isinstance(value, bool):
        raise ConfigError(f"{what}: booleans are not durations")
    if isinstance(value, (int, float)):
        hours = float(value)
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*", value)
        if not match:
            raise ConfigError(f"{what}: cannot parse duration {value!r}")
        number = float(match.group(1))
        unit = match.group(2).lower()
        if unit not in _DURATION_UNITS:
            raise ConfigError(f"{what}: unknown duration unit {unit!r}")
        hours = number * _DURATION_UNITS[unit]
    else:
        raise ConfigError(f"{what}: unsupported duration {value!r}")
    if hours <= 0:
        raise ConfigError(f"{what}: duration must be positive")
    return dt.timedelta(hours=hours)


def load_freshness(config_path: str | Path | None = None) -> tuple[dict[int, dt.timedelta], dt.timedelta]:
    """Load (windows per tier, stale_after) from the ``freshness`` block.

    Windows are how old an item may be before ingest drops it; ``stale_after`` is
    how old a story's newest item may be before it is stale. Missing or invalid
    entries refuse to run; an absent ``freshness`` block uses defaults.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    raw = data.get("freshness") or {}
    if not raw:
        return dict(DEFAULT_FRESHNESS_WINDOWS), DEFAULT_STALE_AFTER
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'freshness' must be a mapping")
    if "windows" not in raw or "stale_after" not in raw:
        raise ConfigError(f"{path}: 'freshness' must define both 'windows' and 'stale_after'")

    wins = raw["windows"]
    if not isinstance(wins, dict):
        raise ConfigError(f"{path}: freshness.windows must be a mapping of tier to duration")

    windows: dict[int, dt.timedelta] = {}
    for tier, value in wins.items():
        try:
            tier_key = int(tier)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: freshness.windows key {tier!r} is not a tier number") from exc
        if tier_key not in VALID_TIERS:
            raise ConfigError(f"{path}: freshness.windows key {tier_key} not in tiers {VALID_TIERS}")
        windows[tier_key] = _parse_duration(value, what=f"{path}: freshness.windows[{tier_key}]")
    for tier in VALID_TIERS:
        if tier not in windows:
            raise ConfigError(f"{path}: freshness.windows is missing tier {tier}")

    stale_after = _parse_duration(raw["stale_after"], what=f"{path}: freshness.stale_after")
    return windows, stale_after


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop 'www.' and fragments, for dedupe."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower().removeprefix("www."),
         parts.path or "/", parts.query, "")
    )


#: Country-code second-level labels whose registrable domain is three labels
#: (``esports-news.co.uk``), not two. Kept small on purpose: the pipeline only
#: needs to compare the domains of configured sources and feed item links, and a
#: wrong guess would silently re-attribute a page. See :func:`registered_domain`.
_MULTI_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.in", "net.in", "org.in", "gen.in", "firm.in", "ind.in",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "co.nz", "net.nz", "org.nz", "govt.nz",
    "co.za", "org.za",
    "com.br", "net.br", "org.br",
    "com.cn", "com.tw", "com.sg", "com.hk", "com.my", "com.tr",
    "co.kr", "or.kr",
})


def registered_domain(value: str) -> str:
    """The registrable domain of a URL or bare host, lowercased, no ``www.``.

    ``news.xbox.com`` -> ``xbox.com``, ``www.pushsquare.com`` -> ``pushsquare.com``
    and ``esports-news.co.uk`` -> ``esports-news.co.uk``. Uses a small built-in
    list of multi-label suffixes so no extra dependency is needed; an unknown
    host falls back to its last two labels, and ``""`` stays ``""``.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    parts = urllib.parse.urlsplit(text if "//" in text else f"//{text}")
    host = (parts.netloc or parts.path).strip().lower()
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _http_fetch(feed_url: str, *, etag: Optional[str] = None, last_modified: Optional[str] = None) -> HttpResponse:
    """Fetch one feed, sending conditional headers when we already have either."""
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(feed_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            return HttpResponse(
                response.status,
                response.headers.get("Content-Type"),
                response.read(),
                response.headers.get("ETag"),
                response.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            exc.code,
            exc.headers.get("Content-Type"),
            exc.read(),
            exc.headers.get("ETag"),
            exc.headers.get("Last-Modified"),
        )
    except TimeoutError as exc:
        raise FetchError("timed out", kind="timeout") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error: {exc.reason}", kind="network") from exc


def _load_cache(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return {"feeds": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("feed cache %s unreadable (%s); starting fresh", path, exc)
        return {"feeds": {}}
    if not isinstance(data, dict):
        return {"feeds": {}}
    data.setdefault("feeds", {})
    return data


def _save_cache(cache: dict, path: str | Path, now: dt.datetime) -> None:
    cache["updated_at"] = now.isoformat()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


_FEED_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "section", "article", "blockquote",
     "h1", "h2", "h3", "h4", "h5", "h6"}
)


class _FeedTextExtractor(HTMLParser):
    """Collect a feed entry's HTML as plain text, keeping paragraph breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _FEED_BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _FEED_BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)


def html_to_text(value: object) -> str:
    """Convert feed HTML to plain text, one line per block, empty lines gone."""
    raw = str(value or "")
    if not raw:
        return ""
    parser = _FeedTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed snippet is not worth failing on
        return ""
    lines = [
        re.sub(r"[ \t\f\v\u00a0]+", " ", line).strip()
        for line in "".join(parser._parts).splitlines()
    ]
    return "\n".join(line for line in lines if line)


def _entry_content(item: dict) -> str:
    """The feed entry's full content (``content:encoded`` / ``content``) as text.

    feedparser maps both ``content:encoded`` and ``content`` to ``item['content']``
    (a list of dicts with a ``value``); the long-form body is preferred over the
    short summary so a feed that carries the article can stand in when the page
    itself is only a navigation shell.
    """
    for entry in item.get("content") or []:
        value = entry.get("value") if isinstance(entry, dict) else entry
        text = html_to_text(value)
        if text:
            return text
    return ""


def _item_published(item: dict) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = item.get(key)
        if parsed:
            return dt.datetime(
                parsed.tm_year, parsed.tm_mon, parsed.tm_mday,
                parsed.tm_hour, parsed.tm_min, parsed.tm_sec,
                tzinfo=dt.timezone.utc,
            )
    return None


def process_feed(source: dict, content: bytes, now: dt.datetime, window: dt.timedelta | None = None) -> FeedCandidates:
    """Parse one feed body and filter its items to the tier's freshness window."""
    if window is None:
        window = KEEP_WINDOW
    parsed = feedparser.parse(content)
    entries = parsed.get("entries") or []
    if parsed.get("bozo") and not entries:
        raise FetchError(f"unparseable or empty feed: {parsed.get('bozo_exception')}")
    if not entries:
        return FeedCandidates(0, None, [])

    cutoff = now - window
    newest: dt.datetime | None = None
    kept: list[dict] = []
    for item in entries:
        published = _item_published(item)
        if published is not None and (newest is None or published > newest):
            newest = published
        if published is None or published < cutoff:
            continue
        kept.append(
            {
                "title": (item.get("title") or "").strip(),
                "link": normalize_url(item.get("link") or ""),
                "content": _entry_content(item),
                "published": published.isoformat(),
                "age_hours": round((now - published).total_seconds() / 3600, 2),
                "source_name": source["name"],
                "tier": source["tier"],
                "owner": source["owner"],
                "region": source["region"],
                "discovered_via": source["name"],
                "source_domain": registered_domain(source.get("site") or ""),
                "fetchable": bool(source.get("fetch", True)),
            }
        )
    return FeedCandidates(len(entries), newest.isoformat() if newest else None, kept)


def _source_index(sources: list[dict] | None) -> tuple[dict[str, dict], dict[str, dict]]:
    """Index configured sources by registrable domain and by name."""
    by_domain: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        domain = registered_domain(source.get("site") or "")
        if domain and domain not in by_domain:
            by_domain[domain] = source
        name = source.get("name")
        if name:
            by_name[str(name)] = source
    return by_domain, by_name


def _tier_of(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def reattribute_item(
    item: dict,
    *,
    by_domain: dict[str, dict],
    by_name: dict[str, dict],
    link_key: str = "link",
) -> dict:
    """Attribute an item to the source that owns its (final) link.

    A feed item is initially credited to the feed it arrived from. If the link
    it points at belongs to a *different* configured source, credit that source
    instead (a Wire item linking to IGN becomes IGN). A link on an unconfigured
    domain is demoted to tier 3 so it can never confirm a story.

    Items with no known feed domain (e.g. synthetic dry-run fixtures) are left
    untouched. The function is idempotent.
    """
    link = item.get(link_key) or item.get("link")
    link_domain = registered_domain(str(link or ""))
    if not link_domain:
        return item
    feed_name = item.get("source_name")
    source_domain = registered_domain(str(item.get("source_domain") or ""))
    if not source_domain and feed_name:
        feed = by_name.get(str(feed_name))
        if feed is not None:
            source_domain = registered_domain(str(feed.get("site") or ""))
    if not source_domain:
        return item
    item.setdefault("discovered_via", feed_name)
    if link_domain == source_domain:
        return item
    target = by_domain.get(link_domain)
    item["external"] = True
    if target is not None:
        item["source_name"] = target.get("name", item.get("source_name"))
        item["tier"] = target.get("tier", item.get("tier"))
        item["owner"] = target.get("owner", item.get("owner"))
        if target.get("region"):
            item["region"] = target["region"]
        item["fetchable"] = bool(target.get("fetch", True))
    else:
        item["source_name"] = link_domain
        item["tier"] = 3
        item["owner"] = link_domain
    return item


def reattribute_items(items: list[dict] | None, sources: list[dict] | None) -> list[dict]:
    """Re-attribute a pool of feed items in place; returns the same list."""
    by_domain, by_name = _source_index(sources)
    for item in items or []:
        if isinstance(item, dict):
            reattribute_item(item, by_domain=by_domain, by_name=by_name)
    return list(items or [])


def reattribute_gathered(gathered: dict, sources: list[dict] | None) -> dict:
    """Re-attribute gathered sources using their post-redirect ``final_url``."""
    by_domain, by_name = _source_index(sources)
    for source in gathered.get("sources") or []:
        if isinstance(source, dict):
            reattribute_item(source, by_domain=by_domain, by_name=by_name, link_key="final_url")
    return gathered


def _looks_like_html(resp: HttpResponse) -> bool:
    content_type = (resp.content_type or "").lower()
    if "html" in content_type:
        return True
    head = resp.body[:256].lstrip().lower()
    return head.startswith(b"<!doctype html") or b"<html" in head


def classify(resp: HttpResponse, source: dict, now: dt.datetime, window: dt.timedelta | None = None) -> tuple[str, FeedCandidates]:
    """Turn an HTTP response into (outcome, candidates). Raises nothing."""
    if resp.status in BLOCK_STATUSES:
        return "blocked", FeedCandidates(0, None, [])
    if resp.status == 304:
        return "not-modified", FeedCandidates(0, None, [])
    if not 200 <= resp.status < 300:
        return "http-error", FeedCandidates(0, None, [])
    if _looks_like_html(resp):
        return "blocked", FeedCandidates(0, None, [])
    if not resp.body:
        return "empty", FeedCandidates(0, None, [])
    try:
        candidates = process_feed(source, resp.body, now, window=window)
    except FetchError:
        return "invalid-xml", FeedCandidates(0, None, [])
    if not candidates.total_entries:
        return "empty", FeedCandidates(0, None, [])
    return "ok", candidates


def _write_candidates(output_dir: Path, timestamp: str, items: list[dict]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{timestamp}.json"
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _write_health_report(path: str | Path, results: list[FeedResult], now: dt.datetime, freshness: dict | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = {outcome: 0 for outcome in VALID_OUTCOMES}
    for result in results:
        summary[result.outcome] += 1
    payload = {
        "generated_at": now.isoformat(),
        "feeds": [vars(result) for result in results],
        "summary": {"total": len(results), **{k: v for k, v in summary.items() if v}},
    }
    if freshness is not None:
        payload["freshness"] = freshness
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _print_feed_table(results: list[FeedResult]) -> None:
    name_width = max([len("FEED"), *(len(r.name) for r in results)])
    truncate_content_type = lambda ct: (ct or "-")[:28]
    truncate_newest = lambda when: (when or "-")[:16]

    header = (
        f"{'FEED':<{name_width}}  {'TIER':>4}  {'HTTP':>4}  {'CONTENT-TYPE':<28}  "
        f"{'BYTES':>7}  {'OUTCOME':<12}  {'ENTRIES':>7}  {'NEWEST':<16}  {'WINDOW':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.name:<{name_width}}  {r.tier:>4}  {str(r.status) if r.status is not None else '-':>4}  "
            f"{truncate_content_type(r.content_type):<28}  "
            f"{r.bytes_received if r.bytes_received is not None else 0:>7}  "
            f"{r.outcome:<12}  {r.total_entries:>7}  {truncate_newest(r.newest):<16}  {r.window_count:>6}"
        )


def run(
    config_path: str | Path | None = None,
    *,
    output_dir: str | Path | None = None,
    cache_path: str | Path | None = None,
    health_path: str | Path | None = None,
    now: dt.datetime | None = None,
    fetcher: Callable[..., HttpResponse] | None = None,
    delay: float = REQUEST_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    print_table: bool = True,
) -> dict:
    """Fetch every source, filter the last 48 hours, write candidates + reports.

    Each feed is fetched at most once per run. ETag / Last-Modified values from
    ``data/feed-cache.json`` are sent as conditional headers, and any new values
    from the response are saved back to the cache.
    """
    sources = load_sources(config_path)
    windows, stale_after = load_freshness(config_path)
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE_PATH
    health_path = Path(health_path) if health_path else DEFAULT_HEALTH_PATH
    now = now or dt.datetime.now(dt.timezone.utc)
    fetcher = fetcher or _http_fetch

    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    items: list[dict] = []
    feed_results: list[FeedResult] = []
    cache = _load_cache(cache_path)
    feeds_cache = cache.setdefault("feeds", {})

    for source in sources:
        feed_url = source["feed_url"]
        window = windows.get(source["tier"], KEEP_WINDOW)
        window_hours = round(window.total_seconds() / 3600, 2)
        prior = feeds_cache.get(feed_url, {})
        try:
            resp = fetcher(feed_url, etag=prior.get("etag"), last_modified=prior.get("last_modified"))
        except FetchError as exc:
            outcome = "timeout" if exc.kind == "timeout" else "http-error"
            feed_results.append(
                FeedResult(source["name"], source["tier"], feed_url, None, None, 0,
                           outcome, 0, None, 0, window_hours, str(exc))
            )
            logger.error("feed '%s' failed (%s): %s", source["name"], outcome, exc)
            sleep(delay)
            continue

        entry = feeds_cache.setdefault(feed_url, {})
        if resp.etag is not None:
            entry["etag"] = resp.etag
        if resp.last_modified is not None:
            entry["last_modified"] = resp.last_modified

        outcome, candidates = classify(resp, source, now, window=window)
        items.extend(candidates.items)
        feed_results.append(
            FeedResult(
                source["name"], source["tier"], feed_url,
                resp.status, resp.content_type, len(resp.body),
                outcome, candidates.total_entries, candidates.newest, len(candidates.items),
                window_hours,
            )
        )
        logger.info(
            "feed '%s': outcome=%s status=%s entries=%d window=%d (%.0fh)",
            source["name"], outcome, resp.status, candidates.total_entries, len(candidates.items), window_hours,
        )
        sleep(delay)

    _save_cache(cache, cache_path, now)
    output_path = _write_candidates(output_dir, timestamp, items)
    freshness_info = {
        "windows": {tier: round(win.total_seconds() / 3600, 2) for tier, win in sorted(windows.items())},
        "stale_after_hours": round(stale_after.total_seconds() / 3600, 2),
    }
    health_path = _write_health_report(health_path, feed_results, now, freshness=freshness_info)
    if print_table:
        _print_feed_table(feed_results)

    logger.info(
        "ingest complete: %d feed(s), %d item(s) kept -> %s (health: %s)",
        len(feed_results), len(items), output_path, health_path,
    )
    return {
        "timestamp": timestamp,
        "output_path": str(output_path),
        "cache_path": str(cache_path),
        "health_path": str(health_path),
        "items": items,
        "feeds": [vars(result) for result in feed_results],
        "freshness": freshness_info,
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConfigError",
    "FetchError",
    "IngestError",
    "HttpResponse",
    "FeedCandidates",
    "FeedResult",
    "load_sources",
    "load_freshness",
    "normalize_url",
    "registered_domain",
    "process_feed",
    "html_to_text",
    "reattribute_item",
    "reattribute_items",
    "reattribute_gathered",
    "classify",
    "run",
]