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
2. They agree on the *event*, not just the subject (regression: three Arc
   Raiders headlines -- a PvE/PvP split, an anti-cheat stack, and a player
   count milestone -- must stay separate even though all three share the
   entity "arc raiders"). A lone shared entity merges only when it leads both
   titles AND the titles share a specific story type (review, sales, delay)
   or at least ``min_event_overlap`` event words: the content words left in
   each title after removing its named entities and the generic words
   (:func:`event_words`). Reviews, sales figures and announcements about the
   same game are therefore different stories; near-identical rewrites and
   titles with several shared entities still merge.
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
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import yaml

from ingest import normalize_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config.yaml")
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
# Two titles joined by a single shared entity must also agree on the event:
# this many of their event words (content words left after the named entities
# and generic words are removed) must overlap. Overridable through
# `cluster.min_event_overlap` in config.yaml.
DEFAULT_EVENT_OVERLAP = 1
# Near-identical rewrites of one event may merge even when a keyword classifier
# disagrees on the story type ("clearly describe the same event").
SAME_EVENT_SIMILARITY = 0.85

logger = logging.getLogger("gamersxpress.pipeline.cluster")

# A word token is any Unicode word character run (letters and digits, including
# accented letters such as "Tōkon") that may keep intra-word apostrophes or
# hyphens. The ASCII-only pattern this replaces split "Tōkon" at the "ō" and
# produced the truncated entity "marvel t".
_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")

# Explicit fold map for characters NFKD does not decompose (ø, æ, œ, ß, ...),
# so a name written with them matches its ASCII spelling in every comparison.
_DIACRITIC_FOLD = {
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ß": "ss", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th", "Þ": "TH",
    "ł": "l", "Ł": "L", "ı": "i", "İ": "I",
}


def strip_diacritics(text: object) -> str:
    """Fold accents and special letters to their closest ASCII form.

    NFKD plus combining-mark removal handles most accents ("Tōkon" -> "Tokon",
    "Grøndal" -> "Grondal" via the explicit fold map). Folding is for matching
    only: callers that display a name keep the original string.
    """
    folded = "".join(_DIACRITIC_FOLD.get(char, char) for char in str(text or ""))
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


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
# titles share at least one named entity, but a shared incidental name must
# never fuse stories on its own.
_INCIDENTAL_ENTITIES = {
    "xbox game pass", "game pass", "playstation plus", "ps plus",
    "nintendo switch online", "playstation blog", "xbox wire", "nintendo",
    "xbox", "playstation", "steam", "geforce now", "playstation store",
    "xbox store", "nintendo eshop", "nvidia", "amd", "intel", "playstation network",
    "xbox live", "pc", "switch", "steam deck", "epic games store", "playstation blog",
}


def normalize_title(title: str) -> str:
    """Lowercase, fold accents, strip punctuation, collapse whitespace.

    Accents are folded (:func:`strip_diacritics`) so "Tōkon" and "Tokon" compare
    equal, which keeps every downstream comparison diacritics-insensitive.
    """
    text = re.sub(r"[^a-z0-9\s]", " ", strip_diacritics(title).lower())
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
        lower = strip_diacritics(word).lower()
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
            if not include_leading and words and strip_diacritics(words[0]).lower() == word:
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


# --- story entities: name phrases and their connector sub-entities ------------
#
# :func:`named_entities` glues a whole run of capitalized words into one name,
# which is right for comparing headlines but too coarse to decide whether a
# claim is about a story: the cluster title "DMZ for Call of Duty: Modern
# Warfare 4 being released early, but Infinity Ward shoots down PvE hopes"
# produced the single entity "dmz for call of duty modern warfare 4", so a claim
# naming the game ("Release date of Call of Duty: Modern Warfare 4") matched
# none of it and was dropped as off-topic.
#
# :func:`story_entities` keeps each proper-name phrase but stops at the words
# and punctuation that separate two names ("for", "in", "on", "with", "and", a
# comma, a spaced dash) and preserves an internal colon, so a subtitle stays
# visible ("call of duty: modern warfare 4"). Each phrase is then split at its
# connectors into sub-entities ("call of duty", "modern warfare 4"), keeping the
# full phrase as well. A name known from ``data/entities.json`` is never split
# further, so "Call of Duty" and "Gears of War" stay whole when they are known.

DEFAULT_ENTITIES_PATH = PROJECT_ROOT / "data" / "entities.json"

#: Words that bind a multi-word proper name together ("Gears of War").
_STORY_GLUE_WORDS = frozenset({"of", "the", "a", "an", "vs", "de"})
#: Words that separate two names in one headline; they end a phrase.
_STORY_BREAK_WORDS = frozenset({"for", "in", "on", "with", "and"})
#: Role and title nouns that separate a person from the company they lead or a
#: product from its maker: a title-cased run "Xbox CEO Asha Sharma" hides both
#: "Xbox" and "Asha Sharma", so a capitalized role ends the phrase.
_ROLE_BREAK_WORDS = frozenset({
    "ceo", "cto", "coo", "cfo", "cio", "cro", "president", "founder",
    "co-founder", "cofounder", "director", "producer", "executive", "editor",
    "chairman", "chairwoman", "chair", "chief", "head", "boss", "lead",
    "manager", "spokesperson", "spokesman", "spokeswoman", "representative",
    "analyst", "reporter", "journalist", "author", "writer", "general",
    "owner", "creator", "designer", "developer", "vp", "svp", "evp",
})
#: Speech and attribution verbs (and contractions) a title-cased headline may
#: capitalize inside a run ("You'll", "Says"): they end the phrase so the names
#: on either side stay separate. Recovery of possessives is handled alongside.
_VERB_BREAK_WORDS = frozenset({
    "you'll", "we'll", "it'll", "they'll", "he'll", "she'll", "i'll",
    "don't", "won't", "can't", "isn't", "aren't", "doesn't", "didn't",
    "wasn't", "weren't", "hasn't", "haven't", "couldn't", "wouldn't",
    "shouldn't", "let's", "that's", "there's", "we've", "i've", "you've",
    "they've", "we're", "you're", "they're",
    "says", "said", "say", "tells", "tell", "told", "blames", "claims",
    "claimed", "confirms", "confirmed", "announces", "announced", "reveals",
    "revealed", "reports", "reported", "warns", "warned", "hopes", "asks",
    "asked", "wants", "wanted", "plans", "planned", "explains", "explained",
    "added", "adds", "notes", "noted",
})
#: A trailing possessive on a name token is not part of the name: the run
#: "Callisto Protocol's" is the entity "Callisto Protocol".
_NAME_POSSESSIVE_RE = re.compile(r"['\u2019]s$", re.IGNORECASE)
#: Punctuation kept inside a phrase (a subtitle colon); punctuation that ends a
#: phrase (a list comma, a spaced dash).
_STORY_INNER_PUNCT = frozenset({":"})
_STORY_BREAK_PUNCT = frozenset({",", "-", "\u2013", "\u2014"})
#: A name token (Unicode word chars, intra-word apostrophes/hyphens kept) or a
#: separating punctuation.
_STORY_TOKEN_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*|[:,\u2013\u2014-]")

_KNOWN_NAMES_CACHE: dict[str, frozenset[str]] = {}


def load_known_names(path: str | Path | None = None) -> frozenset[str]:
    """Lowercased canonical names and aliases from ``data/entities.json``.

    A missing or malformed file yields an empty set, so entity extraction still
    runs. The result is cached per resolved path.
    """
    resolved = str(Path(path).resolve()) if path else str(DEFAULT_ENTITIES_PATH.resolve())
    cached = _KNOWN_NAMES_CACHE.get(resolved)
    if cached is not None:
        return cached
    try:
        raw = json.loads(Path(resolved).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    entries = raw.get("entities", raw) if isinstance(raw, dict) else raw
    names: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        for value in [entry.get("canonical"), *(entry.get("aliases") or [])]:
            name = normalize_title(str(value)) if value else ""
            if name:
                names.add(name)
    result = frozenset(names)
    _KNOWN_NAMES_CACHE[resolved] = result
    return result


# --- entity stop-list --------------------------------------------------------
#
# Calendar and weekday words, bare ordinals and generic news phrasing look like
# proper nouns because headlines are title-cased, but they never identify a
# story. They are never treated as entities (``data/entities.json`` has no such
# names). The editorial words can be extended from
# ``facts.entity_stop_list`` in config.yaml; the calendar defaults stay in code
# so extraction still works when config.yaml is missing.

_DEFAULT_ENTITY_STOP = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
})

#: Bare ordinals ("first".."twelfth") are stop words, and a numeric ordinal is
#: matched by :data:`_ORDINAL_RE`.
_DEFAULT_ENTITY_STOP = _DEFAULT_ENTITY_STOP | frozenset({
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth",
})

_ORDINAL_RE = re.compile(r"^\d+(?:st|nd|rd|th)$")

_ENTITY_STOP_CACHE: dict[str, frozenset[str]] = {}


def entity_stop_list(path: str | Path | None = None) -> frozenset[str]:
    """Lowercased words and phrases that never name a story entity.

    Combines the built-in calendar/ordinal defaults with
    ``facts.entity_stop_list`` from config.yaml, so the owner can extend the
    list without touching code. A missing or malformed file yields the defaults.
    Cached per resolved path.
    """
    resolved = str(Path(path).resolve()) if path else str(DEFAULT_CONFIG_PATH.resolve())
    cached = _ENTITY_STOP_CACHE.get(resolved)
    if cached is not None:
        return cached
    stop = set(_DEFAULT_ENTITY_STOP)
    try:
        raw = yaml.safe_load(Path(resolved).read_text(encoding="utf-8")) or {}
        entries = (raw.get("facts") or {}).get("entity_stop_list") or []
    except (OSError, yaml.YAMLError):
        entries = []
    for entry in entries:
        text = re.sub(r"\s+", " ", strip_diacritics(str(entry)).lower()).strip()
        if text:
            stop.add(text)
    result = frozenset(stop)
    _ENTITY_STOP_CACHE[resolved] = result
    return result


def is_stop_entity(name: object, *, stop: Iterable[str] | None = None) -> bool:
    """True when ``name`` is only stop words and never a real entity.

    A phrase is a stop entity when it is listed verbatim, or when every one of
    its words is a stop word or a bare number/ordinal ("Year 1 DLC"). Names that
    merely contain a stop word ("New World") are kept.
    """
    stop_set = entity_stop_list() if stop is None else frozenset(stop)
    folded = re.sub(r"\s+", " ", strip_diacritics(str(name or "")).lower()).strip()
    if not folded:
        return True
    if folded in stop_set:
        return True
    words = re.findall(r"[^\W_]+", folded)
    if not words:
        return True
    return all(
        word in stop_set or word.isdigit() or _ORDINAL_RE.match(word)
        for word in words
    )


def _story_phrase_tokens(title: str) -> list[list[str]]:
    """Proper-name phrases as token lists, split at separating connectors."""
    phrases: list[list[str]] = []
    current: list[str] = []
    for token in _STORY_TOKEN_RE.findall(str(title or "")):
        lower = strip_diacritics(token).lower()
        if token in _STORY_INNER_PUNCT:
            if current:
                current.append(token)
        elif token in _STORY_BREAK_PUNCT or lower in _STORY_BREAK_WORDS:
            if current:
                phrases.append(current)
                current = []
        elif lower in _STORY_GLUE_WORDS:
            if current:
                current.append(lower)
        elif lower in _GENERIC_WORDS or lower in _ROLE_BREAK_WORDS or lower in _VERB_BREAK_WORDS:
            if current:
                phrases.append(current)
                current = []
        elif _is_name_token(token):
            if token.isdigit() and not current:
                continue  # numbers never start an entity
            stripped = _NAME_POSSESSIVE_RE.sub("", token)
            current.append(stripped or token)
        else:
            if current:
                phrases.append(current)
                current = []
    if current:
        phrases.append(current)
    return phrases


def _clean_story_phrase(tokens: list[str]) -> list[str]:
    """Strip leading/trailing glue words and stray punctuation from a phrase."""
    while tokens and (
        strip_diacritics(tokens[0]).lower() in _STORY_GLUE_WORDS
        or tokens[0] in _STORY_INNER_PUNCT
        or tokens[0] in _STORY_BREAK_PUNCT
    ):
        tokens = tokens[1:]
    while tokens and (
        strip_diacritics(tokens[-1]).lower() in _STORY_GLUE_WORDS
        or tokens[-1] in _STORY_INNER_PUNCT
        or tokens[-1] in _STORY_BREAK_PUNCT
    ):
        tokens = tokens[:-1]
    return tokens


def _format_story_phrase(tokens: list[str]) -> str:
    """Join phrase tokens, attaching an inner colon to the word before it."""
    words: list[str] = []
    for token in tokens:
        if token in _STORY_INNER_PUNCT and words:
            words[-1] = words[-1] + token
        else:
            words.append(token)
    return " ".join(words)


def story_phrases(title: str) -> list[str]:
    """Full proper-name phrases in ``title``, in order of first appearance.

    Words joined by a binding connector ("of", "the") stay one phrase; the
    separating words and punctuation end it. An internal colon is preserved, so
    "Call of Duty: Modern Warfare 4" is one phrase. Bare numbers, generic news
    words and lone leading words never start a phrase.
    """
    phrases: list[str] = []
    for tokens in _story_phrase_tokens(title):
        cleaned = _clean_story_phrase(tokens)
        if not cleaned or not any(char.isalpha() for char in "".join(cleaned)):
            continue
        text = _format_story_phrase(cleaned)
        if text and text not in phrases:
            phrases.append(text)
    return phrases


def sub_entities(name: str, *, known: Iterable[str] | None = None) -> list[str]:
    """A name phrase plus the sub-entities its connectors split it into.

    The full phrase is kept (lowercased, colon preserved) and each connector-
    separated part is added ("Call of Duty: Modern Warfare 4" yields "Call of
    Duty" and "Modern Warfare 4"). A phrase or part that is a known name from
    ``data/entities.json`` is never split further.
    """
    known_set = load_known_names() if known is None else frozenset(
        normalize_title(str(entry)) for entry in known
    )
    display = re.sub(r"\s+", " ", str(name or "").strip().lower())
    base = normalize_title(name)
    if not base:
        return []
    out: list[str] = [display] if display else []
    if base in known_set:
        return out
    current: list[str] = []
    segments: list[list[str]] = []
    for token in _STORY_TOKEN_RE.findall(str(name or "")):
        lower = strip_diacritics(token).lower()
        if token in _STORY_INNER_PUNCT or token in _STORY_BREAK_PUNCT or lower in _STORY_BREAK_WORDS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    for segment in segments:
        text = normalize_title(_format_story_phrase(segment))
        if text and text != base and any(char.isalpha() for char in text) and text not in out:
            out.append(text)
    for known_name in sorted(known_set, key=len, reverse=True):
        if (
            known_name not in out
            and contains_entity(base, known_name)
            and any(char.isalpha() for char in known_name)
        ):
            out.append(known_name)
    return out


def story_entities(
    title: str, *, known: Iterable[str] | None = None
) -> list[str]:
    """Every full name phrase in ``title`` plus its connector sub-entities."""
    entities: list[str] = []
    for phrase in story_phrases(title):
        for entity in sub_entities(phrase, known=known):
            if entity and entity not in entities:
                entities.append(entity)
    return entities


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


def event_words(title: str) -> set[str]:
    """Content words that describe *what happened* in a headline.

    Every word of the normalized title that is not part of a named entity
    (:func:`named_entities` with the leading word included) and not a generic
    news word (:data:`_GENERIC_WORDS`). These words form a story's event key:
    two titles that share the subject but not the event ("arc raiders" plus a
    PvE/PvP split, an anti-cheat stack, or a player-count milestone) leave no
    event words in common. Bare digits are kept ("patch 8" -> {"8"}) while
    single letters are dropped.
    """
    entity_words: set[str] = set()
    for entity in named_entities(title, include_leading=True):
        entity_words.update(normalize_title(entity).split())
    words: set[str] = set()
    for word in normalize_title(title).split():
        if word in _GENERIC_WORDS or word in entity_words:
            continue
        if len(word) == 1 and word.isalpha():
            continue
        words.add(word)
    return words


def event_overlap(a_title: str, b_title: str) -> int:
    """Number of shared event words between two headlines (:func:`event_words`)."""
    return len(event_words(a_title) & event_words(b_title))


def same_story(
    a: dict,
    b: dict,
    *,
    threshold: float = SIMILARITY_THRESHOLD,
    min_event_overlap: int = DEFAULT_EVENT_OVERLAP,
) -> bool:
    """True when two items describe the same story.

    Rules (regression cluster e3b478ab26ee mixed unrelated games; regression:
    three unrelated "arc raiders" headlines must stay separate):

    * Two equal titles are always the same story.
    * Otherwise they must share at least one named entity (generic words like
      "sold"/"copies"/"review" never count).
    * Reviews, sales figures and announcements are separate story types unless
      the titles are near-identical rewrites of one event.
    * A single shared entity is never enough on its own: the titles must also
      agree on the event. A lone shared entity merges only when it leads both
      headlines (see :func:`_single_subject_merge`) AND the titles share a
      specific story type or at least ``min_event_overlap`` event words
      (:func:`event_words`). Titles with several shared entities, or titles
      that are fuzzy-similar at or past ``threshold``, still merge.
    """
    title_a = str(a.get("title") or "")
    title_b = str(b.get("title") or "")
    normalized_a, normalized_b = normalize_title(title_a), normalize_title(title_b)
    if normalized_a and normalized_a == normalized_b:
        return True

    shared = shared_entities(title_a, title_b)
    if not shared:
        return False

    type_a, type_b = story_type(title_a), story_type(title_b)
    score = similarity(title_a, title_b)
    if type_a != type_b and score < SAME_EVENT_SIMILARITY:
        return False

    if score >= threshold:
        return True
    if len(shared) >= SHARED_ENTITIES_REQUIRED and score >= ENTITY_SIMILARITY_FLOOR:
        return True
    return _single_subject_merge(
        shared,
        title_a,
        title_b,
        same_type=type_a == type_b,
        story_type=type_a,
        min_event_overlap=min_event_overlap,
    )


def _single_subject_merge(
    shared: set[str],
    a_title: str,
    b_title: str,
    *,
    same_type: bool,
    story_type: str,
    min_event_overlap: int,
) -> bool:
    """A lone shared entity fuses stories only when it leads both headlines.

    The shared entity must be a real multi-word name (not a platform/store and
    not a bare company name) and appear as the subject, before any other entity,
    in both titles. This merges "Gears of War: E-Day sold 168,000 copies" and
    "Gears of War: E-Day - how's it selling?" without fusing unrelated stories
    that merely mention the same brand ("Microsoft workers sued" vs "Microsoft
    launches RTX Spark PCs").

    Leading both headlines is still only agreement on the *subject*. The titles
    must also agree on the event: either they share a specific story type
    (review, sales or delay), or they share at least ``min_event_overlap`` event
    words. Three unrelated "arc raiders" headlines (a PvE/PvP split, an
    anti-cheat stack, a player-count milestone) share the leading subject but
    none of these, so they stay separate.
    """
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
        if not (contains_entity(first_a, entity) and contains_entity(first_b, entity)):
            continue
        if same_type and story_type in _STORY_TYPES:
            return True
        return event_overlap(a_title, b_title) >= min_event_overlap
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


def record_unconfirmed(
    failed: dict,
    cluster_id: str,
    *,
    now: dt.datetime | None = None,
) -> dict:
    """Record a cluster that failed confirmation (``UnconfirmedStory``).

    Keyed by the membership-hash cluster id, so the entry only blocks the exact
    same source set: when the story gains or loses a member the id changes and
    the cluster is re-evaluated. Within the failed cooldown the unchanged
    cluster is skipped by :func:`cluster_items`; afterwards it is retried.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    entry = failed.setdefault("unconfirmed", {}).setdefault(str(cluster_id), {})
    entry["reason"] = "unconfirmed"
    entry["last_attempt"] = now.isoformat()
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    return failed


def clear_unconfirmed(failed: dict, cluster_id: str) -> dict:
    """Forget an unconfirmed entry (used once the story is covered)."""
    (failed.get("unconfirmed") or {}).pop(str(cluster_id), None)
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
    stop = entity_stop_list()
    collected: dict[str, str] = {}
    for item in items:
        for entity in named_entities(item.get("title", ""), include_leading=True):
            if is_stop_entity(entity, stop=stop):
                continue
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
    min_event_overlap: int = DEFAULT_EVENT_OVERLAP,
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
            if same_story(item, bucket["seed"], threshold=threshold, min_event_overlap=min_event_overlap):
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
        unconfirmed = failed.get("unconfirmed") or {}
        if str(cluster_id) in unconfirmed:
            last_attempt = _parse_time(unconfirmed[str(cluster_id)].get("last_attempt"))
            if last_attempt is not None and (now - last_attempt) < failed_cooldown:
                logger.info("skipping cluster %s: unconfirmed within cooldown", cluster_id)
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
                "category": category_of(bucket["seed"].get("title", ""), _entities(members)),
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


def confirmable(cluster: dict) -> bool:
    """A cluster can be confirmed, before any model call, when it has a tier-1
    source or sources from at least two different owners.

    This mirrors the facts stage's confirmation rules conservatively: two
    origins inside one article never confirm a story, and several articles
    from the same owner count as one voice, so a single non-tier-1 outlet
    cannot confirm on its own. Clusters that fail here are never gathered.
    """
    tiers = {int(tier) for tier in (cluster.get("tiers") or []) if tier not in (None, "")}
    if 1 in tiers:
        return True
    owners = {str(owner).strip().lower() for owner in (cluster.get("owners") or []) if str(owner).strip()}
    return len(owners) >= 2


def excluded_title_pattern(title: str, patterns: Iterable[str] | None = None) -> str | None:
    """The first format pattern a headline matches, else None.

    Patterns are matched case-insensitively. A pattern that does not compile
    as a regex is treated as a plain case-insensitive substring, so simple
    words and phrases (``podcast``, ``free weekend``) and real regexes work
    from the same editable list.
    """
    title = str(title or "").lower()
    if not title:
        return None
    for pattern in patterns or ():
        pattern = str(pattern or "")
        if not pattern:
            continue
        try:
            if re.search(pattern, title, re.IGNORECASE) is not None:
                return pattern
        except re.error:
            if pattern.lower() in title:
                return pattern
    return None


#: Ordered: the first category whose keyword matches wins. More specific before
#: the generic "gaming-news" fallback.
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("playstation", ("playstation", "ps5")),
    ("xbox", ("xbox", "game pass")),
    ("nintendo", ("nintendo", "switch")),
    ("hardware", ("gpu", "geforce", "rtx", "radeon", "ryzen", "intel core")),
    ("esports", ("esports",)),
]
DEFAULT_CATEGORY = "gaming-news"


def category_of(title: str, entities: Iterable[str] = ()) -> str:
    """Classify a cluster from content alone, never from its feed.

    Categories are assigned from ``title`` and ``entities`` by a fixed keyword
    map (ordered, first match wins): ``playstation``/``ps5`` -> playstation,
    ``xbox``/``game pass`` -> xbox, ``nintendo``/``switch`` -> nintendo,
    ``gpu``/``geforce``/``rtx``/``radeon``/``ryzen``/``intel core`` -> hardware,
    ``esports`` -> esports, anything else -> gaming-news. Keywords match whole
    words or phrases (word-boundary anchored) so a headline from any feed lands
    in the same category.
    """
    haystack = " ".join([str(title or ""), *[str(entity) for entity in entities or []]]).lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(re.search(rf"\b{re.escape(keyword)}\b", haystack) for keyword in keywords):
            return category
    return DEFAULT_CATEGORY


__all__ = [
    "DEFAULT_POOL_PATH",
    "DEFAULT_SEEN_PATH",
    "DEFAULT_FAILED_PATH",
    "DEFAULT_FAILED_COOLDOWN_HOURS",
    "DEFAULT_EVENT_OVERLAP",
    "SEEN_COOLDOWN",
    "FINGERPRINT_FORMAT",
    "cluster_id_for_urls",
    "cluster_items",
    "confirmable",
    "contains_entity",
    "event_overlap",
    "event_words",
    "excluded_title_pattern",
    "DEFAULT_ENTITIES_PATH",
    "load_known_names",
    "entity_stop_list",
    "is_stop_entity",
    "load_pool",
    "load_seen",
    "mark_seen",
    "story_entities",
    "story_phrases",
    "sub_entities",
    "merge_items",
    "named_entities",
    "normalize_title",
    "proper_nouns",
    "strip_diacritics",
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
    "record_unconfirmed",
    "clear_unconfirmed",
    "load_failed",
    "save_failed",
    "category_of",
    "DEFAULT_CATEGORY",
    "_story_key",
    "tokens",
]
