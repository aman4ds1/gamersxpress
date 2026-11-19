"""Owner-requested topics ("write about GTA 6 now"), started from Telegram.

A manual request replaces only the *choosing* of a story. Ingest, clustering and
scoring are skipped; instead one cluster is built from

* pooled items whose headline matches the topic (best matches, one per outlet
  first, so the facts sheet sees independent owners), and
* any URLs the owner sent.

That cluster then goes through the normal gather -> facts -> readiness -> write
-> verify -> gates -> publish stages, so every safety rule still applies: the
writer sees a verified facts sheet only, the verifier is another model family,
and an article that cannot be confirmed or does not reach the source floor is
not published. The owner's ``idea`` only steers the angle; it never becomes a
fact (see ``angle_suffix``).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable
from urllib.parse import urlparse

import cluster

#: Alternative spellings, so "GTA 6" also finds "GTA VI" and "Grand Theft Auto 6".
ALIASES: dict[str, list[str]] = {
    "gta 6": ["gta vi", "grand theft auto 6", "grand theft auto vi"],
    "gta vi": ["gta 6", "grand theft auto 6", "grand theft auto vi"],
    "grand theft auto 6": ["gta 6", "gta vi", "grand theft auto vi"],
    "grand theft auto vi": ["gta 6", "gta vi", "grand theft auto 6"],
}

MAX_ITEMS = 8
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")


def parse_urls(text: str | None) -> list[str]:
    """Distinct http(s) URLs in ``text``, in order."""
    seen: list[str] = []
    for url in _URL_RE.findall(str(text or "")):
        url = url.rstrip(".,;")
        if url not in seen:
            seen.append(url)
    return seen


def _words(text: str) -> list[str]:
    return cluster.normalize_title(text).split()


def phrases_for(topic: str) -> list[list[str]]:
    """The topic and its known aliases, each as a word list."""
    key = " ".join(_words(topic))
    out = [key.split()] if key else []
    for alias in ALIASES.get(key, []):
        out.append(alias.split())
    return out


def _has_phrase(words: list[str], phrase: list[str]) -> bool:
    """True when every word of ``phrase`` appears in ``words`` (any order)."""
    return bool(phrase) and all(word in words for word in phrase)


def matching_items(topic: str, idea: str, items: Iterable[dict], *, limit: int = MAX_ITEMS) -> list[dict]:
    """Pooled items about ``topic``: best idea overlap first, one per outlet before any repeat."""
    phrases = phrases_for(topic)
    if not phrases:
        return []
    idea_words = {w for w in _words(idea) if len(w) >= 4}
    scored: list[tuple[int, str, dict]] = []
    for item in items:
        words = _words(str(item.get("title") or ""))
        if not any(_has_phrase(words, phrase) for phrase in phrases):
            continue
        if not item.get("link"):
            continue
        overlap = len(idea_words & set(words))
        scored.append((overlap, str(item.get("published") or ""), item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    chosen: list[dict] = []
    owners: set[str] = set()
    for _overlap, _published, item in scored:
        owner = str(item.get("owner") or item.get("source_domain") or "")
        if owner in owners:
            continue
        owners.add(owner)
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen


def url_item(url: str, topic: str, now: dt.datetime) -> dict:
    """A pool-shaped item for an owner-supplied URL. Ownership/tier are credited later by ingest."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return {
        "title": topic,
        "link": url,
        "content": "",
        "published": now.isoformat(),
        "age_hours": 0.0,
        "source_name": host,
        "tier": 3,
        "owner": host,
        "region": None,
        "discovered_via": "telegram",
        "source_domain": host,
        "fetchable": True,
    }


def build_clusters(
    topic: str,
    idea: str,
    urls: Iterable[str],
    pool_items: Iterable[dict],
    *,
    now: dt.datetime,
) -> list[dict]:
    """One cluster for the requested topic, or ``[]`` when nothing usable was found."""
    items = [url_item(url, topic, now) for url in urls]
    known = {item["link"] for item in items}
    items += [i for i in matching_items(topic, idea, pool_items) if i.get("link") not in known]
    if not items:
        return []
    cluster_id = cluster.cluster_id_for_urls(str(item.get("link") or "") for item in items)
    entities = sorted(set(cluster._entities(items)) | {e.title() for e in cluster.named_entities(topic, include_leading=True)})
    return [
        {
            "id": cluster_id,
            "title": topic,
            "items": items,
            "entities": entities,
            "story_type": cluster.story_type(topic),
            "category": cluster.category_of(topic, entities),
            "newest": max((str(i.get("published") or "") for i in items), default="") or None,
            "age_hours": 0.0,
            "tiers": sorted({i.get("tier") for i in items if i.get("tier") is not None}),
            "owners": sorted({i.get("owner") for i in items if i.get("owner")}),
            "manual": True,
        }
    ]


def angle_suffix(idea: str) -> str:
    """Prompt text that lets the owner steer the angle without supplying facts."""
    idea = " ".join(str(idea or "").split())[:600]
    if not idea:
        return ""
    return (
        "\n\nEDITOR'S ANGLE (steers emphasis and structure only): "
        f"{idea}\n"
        "Treat this as a request about what to focus on. It is NOT a source: state nothing "
        "from it as fact unless the facts sheet above supports it."
    )


def parse_request(env: dict[str, Any]) -> dict | None:
    """Read the manual request from ``MANUAL_TOPIC`` / ``MANUAL_IDEA`` / ``MANUAL_URLS``."""
    topic = " ".join(str(env.get("MANUAL_TOPIC") or "").split())[:200]
    if not topic:
        return None
    return {
        "topic": topic,
        "idea": " ".join(str(env.get("MANUAL_IDEA") or "").split())[:600],
        "urls": parse_urls(env.get("MANUAL_URLS")),
    }
