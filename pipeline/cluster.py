"""Cluster stage: dedupe candidates into stories (PLAN.md section 7.3).

The pool at ``data/pool.json`` is the running list of candidate items across
runs; :func:`merge_items` folds a fresh ingest batch into it (deduping by
normalized link and dropping anything past the stale window). :func:`cluster_items`
groups the pool into stories by fuzzy title match and shared proper nouns, and
skips any story whose id is inside the cooldown window recorded in
``data/seen.json`` (so the same story is not covered twice).

A cluster id is a stable hash of its newest item's normalized title, so a
re-ingested story keeps the same id across runs and the same gathered/facts
files line up.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from ingest import normalize_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POOL_PATH = PROJECT_ROOT / "data" / "pool.json"
DEFAULT_SEEN_PATH = PROJECT_ROOT / "data" / "seen.json"

SEEN_COOLDOWN = dt.timedelta(days=7)
DEFAULT_STALE_AFTER = dt.timedelta(days=7)
SIMILARITY_THRESHOLD = 0.6
SHARED_NOUNS_REQUIRED = 2

logger = logging.getLogger("gamersxpress.pipeline.cluster")

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")
_LEADING_STOP = {
    "the", "a", "an", "new", "first", "why", "how", "what", "when", "who",
    "this", "that", "these", "those", "is", "are", "and", "but", "for",
}


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, for comparison."""
    text = re.sub(r"[^a-z0-9\s]", " ", str(title or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def tokens(title: str) -> set[str]:
    return {
        word
        for word in normalize_title(title).split()
        if len(word) >= 3 and word not in _LEADING_STOP
    }


def proper_nouns(title: str) -> set[str]:
    """Capitalized words (lowercased), skipping the sentence-leading word."""
    words = _WORD_RE.findall(str(title or ""))
    nouns: set[str] = set()
    for index, word in enumerate(words):
        if index == 0:
            continue
        if word[:1].isupper():
            lowered = word.lower()
            if lowered not in _LEADING_STOP:
                nouns.add(lowered)
    return nouns


def similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def same_story(
    a: dict,
    b: dict,
    *,
    threshold: float = SIMILARITY_THRESHOLD,
) -> bool:
    """True when two items describe the same story (fuzzy title + proper nouns)."""
    score = similarity(a.get("title", ""), b.get("title", ""))
    if score >= threshold:
        return True
    shared = proper_nouns(a.get("title", "")) & proper_nouns(b.get("title", ""))
    return len(shared) >= SHARED_NOUNS_REQUIRED and score >= 0.3


def cluster_id_for(title: str) -> str:
    digest = hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()
    return digest[:12]


# --- pool --------------------------------------------------------------------


def load_pool(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_POOL_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": None, "items": []}
    if not isinstance(data, dict):
        return {"updated_at": None, "items": []}
    data.setdefault("items", [])
    return data


def save_pool(pool: dict, *, now: dt.datetime | None = None, path: str | Path | None = None) -> Path:
    path = Path(path) if path else DEFAULT_POOL_PATH
    now = now or dt.datetime.now(dt.timezone.utc)
    pool["updated_at"] = now.isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pool, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _age_hours(item: dict, now: dt.datetime) -> float | None:
    value = item.get("age_hours")
    if isinstance(value, (int, float)):
        return float(value)
    published = item.get("published")
    if not published:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (now - parsed).total_seconds() / 3600


def merge_items(
    pool: dict,
    items: Iterable[dict],
    *,
    now: dt.datetime | None = None,
    max_age: dt.timedelta = DEFAULT_STALE_AFTER,
) -> dict:
    """Dedupe fresh items into the pool, dropping anything past ``max_age``."""
    now = now or dt.datetime.now(dt.timezone.utc)
    max_hours = max_age.total_seconds() / 3600
    by_link: dict[str, dict] = {}
    order: list[str] = []
    for item in list(pool.get("items") or []) + list(items):
        link = normalize_url(str(item.get("link") or ""))
        if not link:
            continue
        age = _age_hours(item, now)
        if age is not None and age > max_hours:
            continue
        stored = {**item, "link": link}
        if link not in by_link:
            order.append(link)
            by_link[link] = stored
        else:
            existing = by_link[link]
            if str(item.get("published") or "") > str(existing.get("published") or ""):
                by_link[link] = stored
    merged = [by_link[link] for link in order]
    merged.sort(key=lambda item: str(item.get("published") or ""), reverse=True)
    pool["items"] = merged
    return pool


# --- seen --------------------------------------------------------------------


def load_seen(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_SEEN_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"clusters": {}}
    if not isinstance(data, dict):
        return {"clusters": {}}
    data.setdefault("clusters", {})
    return data


def save_seen(seen: dict, path: str | Path | None = None) -> Path:
    path = Path(path) if path else DEFAULT_SEEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def mark_seen(seen: dict, cluster_id: str, *, now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now(dt.timezone.utc)
    entry = seen.setdefault("clusters", {}).setdefault(cluster_id, {})
    entry.setdefault("first_seen", now.isoformat())
    entry["last_seen"] = now.isoformat()


def _parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _in_cooldown(seen: dict, cluster_id: str, now: dt.datetime, cooldown: dt.timedelta) -> bool:
    entry = (seen.get("clusters") or {}).get(cluster_id)
    if not entry:
        return False
    last = _parse_time(entry.get("last_seen"))
    return last is not None and (now - last) < cooldown


# --- clustering --------------------------------------------------------------


def _entities(items: list[dict]) -> list[str]:
    collected: dict[str, str] = {}
    for item in items:
        for noun in proper_nouns(item.get("title", "")):
            collected.setdefault(noun, noun.title())
    return sorted(collected.values())


def cluster_items(
    items: Iterable[dict],
    *,
    now: dt.datetime | None = None,
    seen: dict | None = None,
    cooldown: dt.timedelta = SEEN_COOLDOWN,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[dict]:
    """Group items into stories, newest first, skipping recently-seen stories."""
    now = now or dt.datetime.now(dt.timezone.utc)
    seen = seen or {"clusters": {}}
    ordered = sorted(items, key=lambda item: str(item.get("published") or ""), reverse=True)

    buckets: list[dict] = []
    for item in ordered:
        for bucket in buckets:
            if same_story(item, bucket["items"][0], threshold=threshold):
                bucket["items"].append(item)
                break
        else:
            buckets.append({"id": cluster_id_for(item.get("title", "")), "title": item.get("title", ""), "items": [item]})

    clusters: list[dict] = []
    for bucket in buckets:
        if _in_cooldown(seen, bucket["id"], now, cooldown):
            logger.info("skipping cluster %s: seen within %s", bucket["id"], cooldown)
            continue
        members = bucket["items"]
        ages = [age for age in (_age_hours(item, now) for item in members) if age is not None]
        newest = max((str(item.get("published") or "") for item in members), default="")
        clusters.append(
            {
                "id": bucket["id"],
                "title": bucket["items"][0].get("title", ""),
                "items": members,
                "entities": _entities(members),
                "newest": newest or None,
                "age_hours": min(ages) if ages else None,
                "tiers": sorted({item.get("tier") for item in members if item.get("tier") is not None}),
                "owners": sorted({item.get("owner") for item in members if item.get("owner")}),
            }
        )

    clusters.sort(
        key=lambda cluster: (
            (cluster["tiers"] or [9])[0],
            cluster["age_hours"] if cluster["age_hours"] is not None else 1e9,
        )
    )
    logger.info("clustered %d item(s) into %d new cluster(s)", len(list(items)), len(clusters))
    return clusters


__all__ = [
    "DEFAULT_POOL_PATH",
    "DEFAULT_SEEN_PATH",
    "SEEN_COOLDOWN",
    "cluster_id_for",
    "cluster_items",
    "load_pool",
    "load_seen",
    "mark_seen",
    "merge_items",
    "normalize_title",
    "proper_nouns",
    "same_story",
    "save_pool",
    "save_seen",
    "similarity",
    "tokens",
]
