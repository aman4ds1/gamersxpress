"""Gather stage: fetch full text of every source in a cluster.

Takes a cluster (the output of the dedupe/cluster stage) and fetches the full
text of each item's URL. robots.txt is respected before every fetch (via
``urllib.robotparser``); denied or unresolvable robots files default to allow.
Fetches use trafilatura, which downloads the page and extracts the article
text. Failures are recorded per source and never crash the stage.

Output is written to ``data/gathered/<cluster-id>.json``:

    {
      "id": "<cluster-id>",
      "gathered_at": "...",
      "sources": [
        {
          "link": "...", "source_name": "...", "tier": 1, "owner": "...",
          "region": "us", "status": "ok", "text": "<full text>", "error": null
        }
      ]
    }

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
from trafilatura.downloads import DEFAULT_CONFIG as TRAFILATURA_DEFAULT_CONFIG

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "gathered"

USER_AGENT = "GamersXpress/0.1 (+https://gamersxpress.com; news-pipeline)"
FETCH_TIMEOUT_SECONDS = 20
REQUEST_DELAY_SECONDS = 1.0

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


def gather(
    cluster: dict,
    *,
    output_dir: str | Path | None = None,
    id: str | None = None,
    now: dt.datetime | None = None,
    robots: bool = True,
    robots_call: Callable[[str], bool] = robots_allowed,
    fetcher: Callable[[str], Optional[str]] = fetch_html,
    extractor: Callable[[str], Optional[str]] = extract_text,
    delay: float = REQUEST_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Fetch the full text of every item in ``cluster`` and save it."""
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    name = re.sub(r"[^0-9A-Za-z._-]", "-", str(id or cluster.get("id") or "unnamed"))
    now = now or dt.datetime.now(dt.timezone.utc)

    sources: list[dict] = []
    for item in cluster.get("items") or []:
        url = item.get("link")
        base = {
            "link": url,
            "source_name": item.get("source_name", "?"),
            "tier": item.get("tier"),
            "owner": item.get("owner", "?"),
            "region": item.get("region", "?"),
        }
        if robots and not robots_call(url):
            source = {**base, "status": "denied-robots", "text": "", "error": "disallowed by robots.txt"}
            logger.info("source '%s' %s skipped: robots.txt", base["source_name"], url)
        else:
            source = _fetch_one(base, url, fetcher, extractor)
        sources.append(source)
        sleep(delay)

    payload = {"id": name, "gathered_at": now.isoformat(), "sources": sources}
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ok = sum(1 for source in sources if source["status"] == "ok")
    logger.info("gather complete: %d source(s), %d with text -> %s", len(sources), ok, path)
    return payload


def _fetch_one(
    base: dict,
    url: str,
    fetcher: Callable[[str], Optional[str]],
    extractor: Callable[[str], Optional[str]],
) -> dict:
    try:
        html = fetcher(url)
    except Exception as exc:  # noqa: BLE001 - record, never crash the stage
        return {**base, "status": "fetch-error", "text": "", "error": str(exc)}
    if not html:
        return {**base, "status": "fetch-error", "text": "", "error": "no response body"}
    try:
        text = extractor(html)
    except Exception as exc:  # noqa: BLE001 - record, never crash the stage
        return {**base, "status": "extract-error", "text": "", "error": str(exc)}
    if not text:
        return {**base, "status": "empty", "text": "", "error": None}
    return {**base, "status": "ok", "text": text, "error": None}


__all__ = [
    "gather",
    "fetch_html",
    "extract_text",
    "robots_allowed",
    "ROBOTS_CACHE",
]