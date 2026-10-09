"""Ingest stage: fetch RSS feeds and write fresh candidates.

Reads the ``sources`` block from ``pipeline/config.yaml`` and validates it at
startup (every entry needs name, site, feed_url, tier 1/2/3, owner, region,
categories). Each feed is fetched once with a GamersXpress User-Agent, a timeout,
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
REQUIRED_FIELDS: tuple[str, ...] = ("name", "site", "feed_url", "tier", "owner", "region", "categories")
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
                "published": published.isoformat(),
                "age_hours": round((now - published).total_seconds() / 3600, 2),
                "source_name": source["name"],
                "tier": source["tier"],
                "owner": source["owner"],
                "region": source["region"],
            }
        )
    return FeedCandidates(len(entries), newest.isoformat() if newest else None, kept)


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
    "process_feed",
    "classify",
    "run",
]