"""Cluster stage: dedupe candidates into stories (PLAN.md section 7.3).

The pool at ``data/pool.json`` is the running list of candidate items across
runs; :func:`merge_items` folds a fresh ingest batch into it (deduping by
normalized link and dropping anything past the stale window). :func:`cluster_items`
groups the pool into stories, and skips any story that is already covered: it
shares a member URL with a covered story, or its primary entities and story
type match a covered story inside the cooldown window recorded in
``data/seen.json``. The skip is decided by that stored coverage content, not by
the membership-hash id, so a story that gains a new outlet's article later is
still recognized as covered.

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

A cluster id is a digest of its members' normalized, sorted URLs, so a changed
set of sources always produces a new id (a story re-ingested from the same
sources keeps its id, and any membership change makes it a new story). Cached
stage files (``data/gathered/<id>.json``, ``data/facts/<id>.json``) are never
reused blindly: each payload stores a fingerprint (see :func:`stage_fingerprint`)
and the gather/facts stages regenerate a file when its stored fingerprint no
longer matches, so a model or prompt-version change cannot leave a stale sheet
in use.
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
DEFAULT_FAILED_PATH = PROJECT_ROOT / "data" / "failed.json"
DEFAULT_FAILED_COOLDOWN_HOURS = 24

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

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
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


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _normalized_urls(urls: Iterable[str]) -> list[str]:
    """Deduped, normalized, sorted member URLs (the canonical form everywhere)."""
    return sorted({normalize_url(str(url or "")) for url in urls if (url or "").strip()})


def _normalized_entities(entities: Iterable[object]) -> list[str]:
    """Lowercased, deduped, sorted primary-entity names for stable comparison."""
    return sorted({str(entity).strip().lower() for entity in entities if str(entity).strip()})


def _story_key(primary_entities: Iterable[object], story_type: str | None = None) -> str:
    """Story key combining primary entities and story type."""
    ents = ",".join(_normalized_entities(primary_entities))
    stype = str(story_type or "").strip()
    return f"{ents}|{stype}" if stype else ents


def cluster_id_for_urls(urls: Iterable[str]) -> str:
    """Cluster id: a digest of the members' normalized, sorted, deduped URLs.

    Same membership always hashes to the same id; adding, removing or changing
    any member URL produces a different id, so a changed cluster writes under a
    new name and can never reuse another story's gathered/facts output.
    """
    return _digest("\n".join(_normalized_urls(urls)))


#: Bump whenever the fingerprint algorithm or stored shape changes; files from
#: an older format then read as mismatched and are regenerated.
FINGERPRINT_FORMAT = 1


def stage_fingerprint(*parts: object) -> str:
    """Deterministic digest of everything a stage's output depends on.

    Callers pass the cluster id plus the stage's config inputs (model chain,
    prompt version, schema text, constants that shape extraction). Any change
    to those inputs yields a new digest, so a cached file whose stored value
    differs is stale and must be regenerated.
    """
    canonical = json.dumps([*parts], ensure_ascii=False, sort_keys=True, default=str)
    return _digest(canonical)


def stored_fingerprint(path: str | Path) -> str | None:
    """Return the fingerprint stored in ``path``, or None when it is absent.

    Missing files, unreadable/corrupt files and files written by an older
    fingerprint format all read as None so callers treat them as a mismatch.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    stored = data.get("fingerprint")
    if not isinstance(stored, dict) or stored.get("format") != FINGERPRINT_FORMAT:
        return None
    value = stored.get("value")
    return value if isinstance(value, str) and value else None


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



# --- failed ------------------------------------------------------------------


def load_failed(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_FAILED_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"stories": {}}
    if not isinstance(data, dict):
        return {"stories": {}}
    data.setdefault("stories", {})
    return data


def save_failed(failed: dict, path: str | Path | None = None) -> Path:
    path = Path(path) if path else DEFAULT_FAILED_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(failed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def record_attempt(
    failed: dict,
    cluster_id: str,
    *,
    primary_entities: Iterable[object],
    story_type: str | None,
    reason: str,
    now: dt.datetime | None = None,
) -> dict:
    """Record a writer-produced-article failure for a story.

    Keyed by the story key (primary entities plus story type), which survives the
    membership-hash cluster id changing when the story gains or loses a source.
    Increments ``attempt_count``, stamps ``last_attempt`` and stores the failure
    ``reason`` (``article_rejected``, ``verifier_output_invalid`` or
    ``gate failure``).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    key = _story_key(primary_entities, story_type)
    entry = failed.setdefault("stories", {}).setdefault(key, {})
    entry["attempt_count"] = int(entry.get("attempt_count") or 0) + 1
    entry["last_attempt"] = now.isoformat()
    entry["failure_reason"] = str(reason)
    entry["cluster_id"] = str(cluster_id)
    entry["primary_entities"] = _normalized_entities(primary_entities)
    entry["story_type"] = story_type
    return failed


def clear_attempt(
    failed: dict,
    *,
    primary_entities: Iterable[object],
    story_type: str | None,
) -> dict:
    """Remove a story from the failed list once it later passes."""
    key = _story_key(primary_entities, story_type)
    (failed.get("stories") or {}).pop(key, None)
    return failed


def mark_seen(
    seen: dict,
    cluster_id: str,
    *,
    member_urls: Iterable[str] | None = None,
    primary_entities: Iterable[object] | None = None,
    story_type: str | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Record a covered cluster. The entry is keyed by the membership-hash id
    (so re-covering the same story overwrites its slot) but stores the coverage
    facts coverage decisions are based on: member URLs, primary entities, story
    type, and the seen dates. Content fields are stored normalized.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    entry = seen.setdefault("clusters", {}).setdefault(cluster_id, {})
    if member_urls is not None:
        entry["member_urls"] = _normalized_urls(member_urls)
    if primary_entities is not None:
        entry["primary_entities"] = _normalized_entities(primary_entities)
    if story_type is not None:
        entry["story_type"] = str(story_type)
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


def _entry_member_urls(entry: dict) -> set[str]:
    value = entry.get("member_urls")
    return set(_normalized_urls(value)) if isinstance(value, list) else set()


def _entry_primary_entities(entry: dict) -> set[str]:
    value = entry.get("primary_entities")
    return set(_normalized_entities(value)) if isinstance(value, list) else set()


def _seen_url_overlap(seen: dict, member_urls: Iterable[str]) -> bool:
    """(a) The cluster shares at least one member URL with a seen cluster.

    Rule (a) has no cooldown: a specific source URL that was already covered
    never gets covered again, regardless of how the cluster around it changed.
    Old-format entries (no ``member_urls``) carry no coverage data and never
    match.
    """
    urls = set(_normalized_urls(member_urls))
    if not urls:
        return False
    for entry in (seen.get("clusters") or {}).values():
        if urls & _entry_member_urls(entry):
            logger.info("cluster shares a member URL with a seen story")
            return True
    return False


def _seen_entity_type_match(
    seen: dict,
    *,
    primary_entities: Iterable[object],
    story_type: str,
    now: dt.datetime,
    cooldown: dt.timedelta,
) -> bool:
    """(b) Same primary entities AND same story type, seen within cooldown.

    A review and a sales story about the same game share entities but have
    different story types, so they are never treated as the same coverage
    decision. Differs in either field, or a cooldown that has lapsed, is not a
    match. Old-format entries (no ``primary_entities``/``story_type``) never
    match.
    """
    entities = set(_normalized_entities(primary_entities))
    if not entities or not story_type:
        return False
    for entry in (seen.get("clusters") or {}).values():
        if str(entry.get("story_type") or "") != str(story_type):
            continue
        if _entry_primary_entities(entry) != entities:
            continue
        last = _parse_time(entry.get("last_seen"))
        if last is not None and (now - last) < cooldown:
            logger.info(
                "cluster matches a seen story (entities=%s type=%s) within cooldown %s",
                sorted(entities), story_type, cooldown,
            )
            return True
    return False


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
    failed: dict | None = None,
    cooldown: dt.timedelta = SEEN_COOLDOWN,
    failed_cooldown: dt.timedelta | None = None,
    failed_cooldown_hours: int | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[dict]:
    """Group items into stories, newest first, skipping already-covered stories.

    Each bucket is anchored to its seed (the newest item). An item joins a
    bucket only when :func:`same_story` holds against that seed, so clusters
    never chain item-to-item. Items are compared newest-first; an item that
    matches no seed founds a new bucket.

    A cluster is treated as already covered (and skipped) when it shares a
    member URL with a seen cluster, or when it matches a seen cluster's primary
    entities AND story type within the cooldown window. The skip is decided by
    content, not by the membership-hash id: a story that grows by one outlet
    gets a new id but still shares URLs with the covered story, so it is not
    written again.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    seen = seen or {"clusters": {}}
    failed = failed or {"stories": {}}
    if failed_cooldown_hours is not None:
        failed_cooldown = dt.timedelta(hours=failed_cooldown_hours)
    if failed_cooldown is None:
        failed_cooldown = dt.timedelta(hours=DEFAULT_FAILED_COOLDOWN_HOURS)
    failed = failed or {"stories": {}}
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
                    "title": item.get("title", ""),
                    "seed": item,
                    "items": [item],
                }
            )

    clusters: list[dict] = []
    for bucket in buckets:
        members = bucket["items"]
        cluster_id = cluster_id_for_urls(str(member.get("link") or "") for member in members)
        member_urls = _normalized_urls(str(member.get("link") or "") for member in members)
        primary = _normalized_entities(_entities(members))
        cluster_type = story_type(str(bucket["seed"].get("title") or ""))
        story_key = _story_key(primary, cluster_type)
        failed_entry = (failed.get("stories") or {}).get(story_key)
        if failed_entry is not None:
            attempt_count = int(failed_entry.get("attempt_count") or 0)
            last_attempt = _parse_time(failed_entry.get("last_attempt"))
            if attempt_count >= 2 and last_attempt is not None:
                if (now - last_attempt) < failed_cooldown:
                    logger.info("skipping cluster %s: failed attempts within cooldown", cluster_id)
                    continue
        if _seen_url_overlap(seen, member_urls) or _seen_entity_type_match(
            seen,
            primary_entities=primary,
            story_type=cluster_type,
            now=now,
            cooldown=cooldown,
        ):
            logger.info("skipping cluster %s: already covered", cluster_id)
            continue
        ages = [age for age in (_age_hours(item, now) for item in members) if age is not None]
        newest = max((str(item.get("published") or "") for item in members), default="")
        clusters.append(
            {
                "id": cluster_id,
                "title": bucket["seed"].get("title", ""),
                "items": members,
                "entities": _entities(members),
                "story_type": cluster_type,
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
    "DEFAULT_FAILED_PATH",
    "DEFAULT_FAILED_COOLDOWN_HOURS",
    "SEEN_COOLDOWN",
    "FINGERPRINT_FORMAT",
    "cluster_id_for_urls",
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
    "stage_fingerprint",
    "stored_fingerprint",
    "story_type",
    "record_attempt",
    "clear_attempt",
    "load_failed",
    "save_failed",
    "_story_key",
    "tokens",
]
