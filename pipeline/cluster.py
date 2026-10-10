"""Cluster stage: dedupe candidates into stories (PLAN.md section 7.3).

The pool at ``data/pool.json`` is the running list of candidate items across
runs; :func:`merge_items` folds a fresh ingest batch into it (deduping by
normalized link and dropping anything past the stale window). :func:`cluster_items`
groups the pool into stories, and skips any story whose id is inside the
cooldown window recorded in ``data/seen.json`` (so the same story is not covered
twice).

Two items may only be merged when ALL of these hold (regression cluster
e3b478ab26ee mixed unrelated games because generic capitalized words like
"sold"/"copies" counted as shared proper nouns):

1. They share at least one specific named entity -- a game, product, company
   or event name from :func:`named_entities`. Generic news words (sold,
   copies, million, new, update, review, ...) never count.
2. Their story types match, or the titles are near-identical rewrites of one
   event (see :func:`story_type`): reviews, sales figures and announcements
   about the same game are different stories.
3. The title is fuzzy-similar to the cluster's *seed* item (the newest item,
   which founded the bucket). Membership is never transitive: an item similar
   to some other member but not to the seed starts its own cluster.

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
# Two items that fuse on entities alone need at least this much title overlap,
# so a shared game/product name is not enough by itself.
ENTITY_SIMILARITY_FLOOR = 0.3
SHARED_ENTITIES_REQUIRED = 2
# One unmistakable shared subject (a game/product name, not a platform) fuses
# stories only when the titles are at least this similar.
SINGLE_ENTITY_SIMILARITY_FLOOR = 0.35
# Near-identical rewrites of one event may merge even when a keyword classifier
# disagrees on the story type ("clearly describe the same event").
SAME_EVENT_SIMILARITY = 0.85

logger = logging.getLogger("gamersxpress.pipeline.cluster")

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")
_LEADING_STOP = {
    "the", "a", "an", "new", "first", "why", "how", "what", "when", "who",
    "this", "that", "these", "those", "is", "are", "and", "but", "for",
}

# Words that look like proper nouns because headlines are title-cased, but
# carry no story identity. They never form or extend a named entity, and a
# token in this set breaks any multi-word entity it appears inside of.
_GENERIC_WORDS = {
    "sold", "sell", "sells", "selling", "sales", "sale", "copies", "copy",
    "units", "unit", "million", "billion", "thousand", "new", "update",
    "updates", "review", "reviews", "reviewed", "announce", "announced",
    "announces", "announcement", "launch", "launched", "launches", "release",
    "released", "releases", "delay", "delayed", "delays", "postpone",
    "postponed", "postpones", "reveal", "revealed", "reveals", "confirm",
    "confirmed", "confirms", "report", "reports", "reported", "reportedly",
    "details", "detail", "date", "dates", "price", "prices", "trailer",
    "trailers", "gameplay", "demo", "beta", "patch", "patches", "players",
    "player", "revenue", "generated", "generates", "coming", "gets", "got",
    "make", "makes", "made", "tops", "top", "faster", "than", "any", "more",
    "most", "best", "first", "series", "entry", "previous", "days", "day",
    "month", "months", "week", "weeks", "hour", "hours", "one", "two",
    "three", "the", "a", "an", "of", "and", "or", "but", "for", "to", "on",
    "in", "at", "is", "are", "was", "were", "has", "have", "had", "this",
    "that", "with", "as", "its", "it", "from", "after", "before",
}

# Lowercase words that may sit inside a multi-word name ("Gears of War").
_ENTITY_CONNECTORS = {"of", "the", "and", "a", "an", "vs", "for", "de"}

_REVIEW_MARKERS = ("review", "reviews", "reviewed", "verdict", "hands-on", "hands", "preview")
_SALES_MARKERS = (
    "sold", "sell", "sells", "selling", "sales", "sale", "copies", "copy",
    "units", "revenue", "grossed", "tops", "million", "billion", "bestseller",
)
_DELAY_MARKERS = ("delay", "delayed", "delays", "postpone", "postponed", "postpones")
_SALES_PATTERNS = (
    re.compile(r"\$\s?\d"),
    re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*[mbk]\b", re.IGNORECASE),
)
_STORY_TYPES = ("sales", "review", "delay")  # checked in this order for equal positions

# Platforms, stores and vendor names that preface many unrelated stories. Two
# titles share at least one named entity, but this alone must not fuse stories.
# A shared incidental name only merges with a higher similarity score.
_INCIDENTAL_ENTITIES = {
    "xbox game pass", "game pass", "playstation plus", "ps plus",
    "nintendo switch online", "playstation blog", "xbox wire", "nintendo",
    "xbox", "playstation", "steam", "geforce now", "playstation store",
    "xbox store", "nintendo eshop", "nvidia", "amd", "intel", "playstation network",
    "xbox live", "pc", "switch", "steam deck", "epic games store", "playstation blog",
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


def _is_name_token(word: str) -> bool:
    """True for tokens that can be part of a name: Caps, camelCase, ACRONYM, digits."""
    if not word:
        return False
    if word.isdigit():
        return True
    if any(char.isupper() for char in word[1:]):  # iPhone, PS5, RTX
        return True
    if word.isupper() and len(word) > 1:  # XBOX, DLC
        return True
    return word[:1].isupper()


def _ordered_entities(title: str, *, include_leading: bool = False) -> list[str]:
    """Named entities in ``title`` in order of first appearance."""
    words = _WORD_RE.findall(str(title or ""))
    phrases: list[list[str]] = []
    current: list[str] = []
    for word in words:
        lower = word.lower()
        if current and lower in _ENTITY_CONNECTORS:
            current.append(lower)
        elif lower in _GENERIC_WORDS:
            if current:
                phrases.append(current)
                current = []
        elif _is_name_token(word):
            if word.isdigit() and not current:
                continue  # numbers never start an entity
            current.append(lower)
        else:
            if current:
                phrases.append(current)
                current = []
    if current:
        phrases.append(current)

    entities: list[str] = []
    for phrase in phrases:
        while phrase and phrase[0] in _ENTITY_CONNECTORS:
            phrase = phrase[1:]
        while phrase and phrase[-1] in _ENTITY_CONNECTORS:
            phrase = phrase[:-1]
        if not phrase or not any(char.isalpha() for char in "".join(phrase)):
            continue
        if len(phrase) == 1:
            word = phrase[0]
            if not include_leading and words and words[0].lower() == word:
                continue
            entities.append(word)
        else:
            entities.append(" ".join(phrase))
    return entities


def named_entities(title: str, *, include_leading: bool = False) -> set[str]:
    """Specific named entities in ``title``: games, products, companies, events.

    A multi-word run of name tokens (joined by lowercase connectors such as
    "of") becomes one entity ("gears of war", "xbox game pass"). A single
    capitalized word becomes an entity only when it is not a generic news word
    (:data:`_GENERIC_WORDS`) and not a bare number.

    The first word of a title is dropped as a *single* entity because headlines
    are title-cased; pass ``include_leading=True`` when the title defines a
    story's subject (e.g. to find a cluster's primary entity).
    """
    return set(_ordered_entities(title, include_leading=include_leading))


def story_type(title: str) -> str:
    """Classify a headline as ``review``, ``sales`` or ``announcement``.

    Reviews, sales figures and announcements about the same game are distinct
    stories even when they share every named entity; :func:`same_story` only
    merges different types when the titles are near-identical rewrites.
    """
    text = str(title or "")
    normalized = normalize_title(text)
    words = normalized.split()
    positions: list[tuple[int, str]] = []
    for index, word in enumerate(words):
        if word in _SALES_MARKERS:
            positions.append((index, "sales"))
        if word in _REVIEW_MARKERS:
            positions.append((index, "review"))
        if word in _DELAY_MARKERS:
            positions.append((index, "delay"))
    if any(pattern.search(text) for pattern in _SALES_PATTERNS):
        positions.append((-1, "sales"))
    if not positions:
        return "announcement"
    return min(positions, key=lambda entry: (entry[0], _STORY_TYPES.index(entry[1])))[1]


def contains_entity(haystack: str, entity: str) -> bool:
    """True when normalized ``entity`` appears in normalized ``haystack`` on word boundaries."""
    haystack = normalize_title(haystack)
    entity = normalize_title(entity)
    if not haystack or not entity:
        return False
    return re.search(rf"\b{re.escape(entity)}\b", haystack) is not None


def shared_entities(a_title: str, b_title: str) -> set[str]:
    """Named entities two titles have in common, tolerant of extra modifiers.

    A title often glues trailing title-cased words onto a name ("gears of war
    e-day how's"); an entity counts as shared when it equals, contains, or is
    contained in an entity of the other title. The shorter (more general) form
    is kept, which makes the result symmetric.
    """
    matched: set[str] = set()
    for entity_a in named_entities(a_title):
        for entity_b in named_entities(b_title):
            if entity_a == entity_b:
                matched.add(entity_a)
            elif contains_entity(entity_b, entity_a):
                matched.add(entity_a)
            elif contains_entity(entity_a, entity_b):
                matched.add(entity_b)
    return matched


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
    """True when two items describe the same story.

    Rules (regression cluster e3b478ab26ee mixed unrelated games):

    * Two equal titles are always the same story.
    * Otherwise they must share at least one named entity (generic words like
      "sold"/"copies"/"review" never count).
    * Reviews, sales figures and announcements are separate story types unless
      the titles are near-identical rewrites of one event.
    * The titles must then be similar enough: at or past ``threshold``, or with
      enough shared entities. A single unmistakable (non-incidental) entity can
      carry a merge only with a lower similarity floor.
    """
    title_a = str(a.get("title") or "")
    title_b = str(b.get("title") or "")
    normalized_a, normalized_b = normalize_title(title_a), normalize_title(title_b)
    if normalized_a and normalized_a == normalized_b:
        return True

    shared = shared_entities(title_a, title_b)
    if not shared:
        return False

    score = similarity(title_a, title_b)
    if story_type(title_a) != story_type(title_b) and score < SAME_EVENT_SIMILARITY:
        return False

    if score >= threshold:
        return True
    if len(shared) >= SHARED_ENTITIES_REQUIRED and score >= ENTITY_SIMILARITY_FLOOR:
        return True
    return _single_subject_merge(shared, title_a, title_b, score)


def _single_subject_merge(shared: set[str], a_title: str, b_title: str, score: float) -> bool:
    """A lone shared entity fuses stories only when it leads both headlines.

    The shared entity must be a real multi-word name (not a platform/store and
    not a bare company name) and appear as the subject, before any other entity,
    in both titles. This merges "Gears of War: E-Day sold 168,000 copies" and
    "Gears of War: E-Day - how's it selling?" without fusing unrelated stories
    that merely mention the same brand ("Microsoft workers sued" vs "Microsoft
    launches RTX Spark PCs").
    """
    if score < SINGLE_ENTITY_SIMILARITY_FLOOR:
        return False
    leading_a = _ordered_entities(a_title, include_leading=True)
    leading_b = _ordered_entities(b_title, include_leading=True)
    if not leading_a or not leading_b:
        return False
    first_a, first_b = leading_a[0], leading_b[0]
    for entity in shared:
        if entity in _INCIDENTAL_ENTITIES:
            continue
        if len(entity.split()) < 2:
            continue
        if contains_entity(first_a, entity) and contains_entity(first_b, entity):
            return True
    return False


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
        for entity in named_entities(item.get("title", ""), include_leading=True):
            collected.setdefault(entity, entity.title())
    return sorted(collected.values())


def cluster_items(
    items: Iterable[dict],
    *,
    now: dt.datetime | None = None,
    seen: dict | None = None,
    cooldown: dt.timedelta = SEEN_COOLDOWN,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[dict]:
    """Group items into stories, newest first, skipping recently-seen stories.

    Each bucket is anchored to its seed (the newest item). An item joins a
    bucket only when :func:`same_story` holds against that seed, so clusters
    never chain item-to-item. Items are compared newest-first; an item that
    matches no seed founds a new bucket.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    seen = seen or {"clusters": {}}
    ordered = sorted(items, key=lambda item: str(item.get("published") or ""), reverse=True)

    buckets: list[dict] = []
    for item in ordered:
        for bucket in buckets:
            if same_story(item, bucket["seed"], threshold=threshold):
                bucket["items"].append(item)
                break
        else:
            buckets.append(
                {
                    "id": cluster_id_for(item.get("title", "")),
                    "title": item.get("title", ""),
                    "seed": item,
                    "items": [item],
                }
            )

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
                "title": bucket["seed"].get("title", ""),
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
    "contains_entity",
    "load_pool",
    "load_seen",
    "mark_seen",
    "merge_items",
    "named_entities",
    "normalize_title",
    "proper_nouns",
    "same_story",
    "save_pool",
    "save_seen",
    "shared_entities",
    "similarity",
    "story_type",
    "tokens",
]
