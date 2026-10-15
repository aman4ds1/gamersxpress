"""Facts stage: extract claims from gathered source text with the 'fast' role.

Input is the output of the gather stage (``data/gathered/<id>.json``). The
"fast" model family extracts JSON claims from the source texts: claim, value,
source_url, confidence (0..1), origin (who produced the claim, or "unstated"),
and kind (confirmed / reported / estimate / rumor / opinion).

Extraction runs **once per source**, one model call per source, so a long
article cannot crowd out the story's other sources. Each call sees only its own
source and extracts every distinct fact about the story's subject (features,
modes, dates, prices, platforms, named quotes) plus the headline's central
claim, up to a per-source maximum (``facts.max_claims_per_source`` in
config.yaml, default :data:`MAX_CLAIMS_PER_SOURCE`). Claims from all sources
are merged into one sheet afterwards.

The extracted JSON is validated against :data:`CLAIM_SCHEMA` in code; anything
that does not match raises :class:`FactsError` and the story is dropped. Each
claim's ``kind`` is one of ``confirmed`` (stated by the company or an official
source), ``reported`` (stated by an outlet citing a named source), ``estimate``
(a third-party analyst or data estimate, never a confirmation), ``statement``
(a company or person's statement about a plan, possibility, intention or survey
result, never labeled ``confirmed``), ``rumor`` (a leak or unnamed source) or
``opinion``. ``origin`` names who produced the claim (the analyst or firm the
source cites, or the company); ``affiliation`` names the organization a person
speaks for when the article states one. When the source does not say who
produced the claim, the extractor stores ``"unstated"`` and the claim is
treated as at most ``reported``. In-game currency amounts must name their
currency (for example "100,000 in-game DMZ Cash") and are never stored as a
bare dollar figure.

A coherence check then keeps every claim honest to the story: each claim's
source must be about the cluster's primary entity (the named entity shared by
the most source headlines). Claims whose source is off-topic are dropped and
logged, so a mixed cluster cannot smuggle another game's figures into the sheet.

Duplicate claims across sources are then merged (:func:`merge_claims`): one
claim per fact, listing every source URL that states it in ``source_urls``,
taking the weakest ``kind`` when sources disagree, and keeping hedge words
(estimated, reportedly, more than, over) in the value.

Each claim's origin is then canonicalized -- case, possessives and leading
titles are stripped, and ``data/origins.json`` maps aliases to one canonical
name ("Alinea", "Alinea Analytics" and "Rhys Elliott of Alinea Analytics" all
become "alinea analytics", a person named alongside a firm counting as the
firm) -- and verified against the text of the claim's own source: an origin the
source never mentions is downgraded to ``"unstated"`` and logged. A person's
claim also keeps the organization they speak for (``affiliation``); that
organization is what the person counts as, so an executive producer and the
studio they work for are one origin, and two origins from the same single
article never count as two.

The story is confirmed through its central claim: the claim that restates the
cluster's seed headline. It is confirmed when a tier-1 source states it, or at
least two independent attributed origins support that same claim; only claims
of kind ``confirmed``/``reported``/``estimate`` count (a rumor, an opinion or a
statement never confirms a story), outlets repeating the same origin count as
one, origins supporting any other claim never count, two origins from the same
single article never count as two, and tier 3 never counts toward confirmation.
When one canonical origin is contained in another from the same set ("alinea
analytics" inside "alinea analytics ltd") they are treated as the same origin.
``allow_single_origin_attributed`` (config flag, default false) additionally
lets a single attributed origin confirm a story. Source text is consumed here
only; it is never handed to the writer.

Output is written to ``data/facts/<id>.json``:

    {
      "id": "<id>",
      "confirmation": {"tier1_sources": 1, "tier3_sources": 0, "origins": [...], "rule": "...", "confirmed": true},
      "sources": [{"source_name", "link", "title", "tier", "owner", "region"}],
      "claims": [{"claim", "value", "source_urls": [...], "origin", "affiliation", "confidence", "kind", "central"}],
      "coherence": {"checked": true, "primary_entities": [...], "canonical_entities": [...], "dropped_claims": [...]},
      "extractor": {"provider", "model", "family"},
      "fingerprint": {"format": 1, "value": "<hash of id + model chain + prompt settings>"}
    }

Only sources that contributed at least one claim appear in ``sources``; a
source that yielded no claim (or whose claims were all dropped) is left out of
the sheet so the writer and the published article never cite it.

When the file already exists and its stored fingerprint still matches the
current one (the cluster id, the enabled "fast" role chain from config.yaml,
the extraction prompt/schema, and the extraction tuning values), the cached
sheet is returned without calling the model; any mismatch -- including a sheet
written before fingerprints existed -- means claims are re-extracted and the
file overwritten, so stale facts are never reused. Bump :data:`PROMPT_VERSION`
when extraction output rules change so cached sheets invalidate automatically.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional, Iterable

import cluster
from ingest import normalize_url
from providers import (
    Generation,
    RunState,
    generate as default_generate,
    load_config,
    DEFAULT_CONFIG_PATH,
)
from json_schema import schema_errors

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "facts"

MAX_SOURCE_CHARS = 4000
# A claim's text and value must stay short: longer fields are almost always a
# verbatim passage copied from a source, so they are clipped before the sheet is
# saved (PLAN.md principle 3: facts, not source prose).
MAX_FIELD_CHARS = 300
# Extraction runs once per source; one long article may contribute at most this
# many claims, so it cannot crowd out the story's other sources. Overridable per
# story via `facts.max_claims_per_source` in config.yaml.
MAX_CLAIMS_PER_SOURCE = 15
# Cost control: extraction runs once per source, so a candidate with many
# sources could spend the whole model-call budget on one story. At most this
# many sources per candidate are extracted, chosen by tier first, then one per
# distinct owner, then by text length. Overridable via
# `score.max_sources_per_candidate` in config.yaml.
DEFAULT_MAX_SOURCES_PER_CANDIDATE = 3
# A source that carries more text than this yet produces fewer than
# WARN_MIN_CLAIMS claims is logged with a warning, because that strong of a gap
# usually means the extractor skipped most of what the source says.
WARN_LOW_SOURCE_CHARS = 1500
WARN_MIN_CLAIMS = 3
# Bump when the extraction prompt or the claim schema changes, so previously
# written facts sheets are treated as stale and re-extracted.
PROMPT_VERSION = "facts-v6"

# Claim kinds, weakest to strongest. Merging takes the weakest kind a fact's
# sources disagree on, so an estimate that one source calls an estimate is
# never upgraded to confirmed by another source's wording, and a plan stated as
# a possibility (``statement``) is never upgraded to a confirmed fact.
KIND_STRENGTH: dict[str, int] = {
    "confirmed": 6,
    "reported": 5,
    "estimate": 4,
    "statement": 3,
    "rumor": 2,
    "opinion": 1,
}
ALLOWED_KINDS = tuple(KIND_STRENGTH)

#: Origin used when a source does not say who produced the claim. A claim with
#: this origin is capped at ``reported`` (requirement: treat as at most reported).
UNSTATED_ORIGIN = "unstated"

#: Hedge words a merged fact must keep in its value. When sources state the same
#: fact with different values, the value carrying the most hedge words wins, so
#: "estimated 168,000" is never flattened to the unhedged "168,000", and a plan
#: hedged as "may", "could" or "not a promise" keeps that hedge.
HEDGE_WORDS = (
    "estimated", "estimate", "estimates", "reportedly", "more than", "over",
    "approximately", "about", "around", "roughly", "nearly", "up to", "at least",
    "could", "may", "might", "possibly", "hopes to", "not a promise",
)

#: Story-level confirmation is one tier-1 source OR at least two independent
#: attributed origins. Setting `facts.allow_single_origin_attributed` in
#: config.yaml lets a single attributed (named) origin confirm a story instead.
DEFAULT_ALLOW_SINGLE_ORIGIN_ATTRIBUTED = False

#: Origin strings are canonicalized through this small data file: each alias on
#: the left maps to one canonical name on the right ("alinea" -> "Alinea
#: Analytics"). A missing or malformed file is treated as an empty map.
DEFAULT_ORIGIN_ALIASES_PATH = PROJECT_ROOT / "data" / "origins.json"

#: Allowlist of well-known video game organizations (publishers, studios,
#: platform holders, chip makers) from ``data/orgs.json``. Used to keep a
#: company from being mistaken for a product/title and to recognise a
#: first-party announcement. Optional: a missing file yields an empty allowlist
#: and the corporate-suffix and announcement-subject rules still apply.
DEFAULT_ORGS_PATH = PROJECT_ROOT / "data" / "orgs.json"

#: Leading words stripped from an origin before matching, so an article or a
#: role ("the analyst Alinea Analytics") does not split one origin into two.
_ORIGIN_TITLE_WORDS = frozenset({"the", "a", "an", "analyst", "analysts"})

#: Claim kinds whose origins may count toward confirmation. A rumor, an opinion
#: or a statement never confirms a story, however many outlets repeat it.
CONFIRMING_KINDS = ("confirmed", "reported", "estimate")

#: Claim kinds a first-party announcement may rest on. Broader than
#: :data:`CONFIRMING_KINDS`: a company's own statement about its plan counts as
#: first party, while a rumor or an opinion never does.
FIRST_PARTY_KINDS = ("confirmed", "statement", "reported")

#: How many independent sources (distinct owners) must each attribute the
#: central claim to an organization before the first-party rule confirms it.
#: Overridable via `facts.min_first_party_sources` in config.yaml.
DEFAULT_MIN_FIRST_PARTY_SOURCES = 2

logger = logging.getLogger("gamersxpress.pipeline.facts")

#: Default vocabulary for the closed anonymous-descriptor rule (facts.py). A cue
#: whose origin is built only from these words names no real source -- "sources",
#: "unnamed sources", "people familiar with the matter", "four industry sources".
#: A phrase containing any token outside the vocabulary is a name ("Insider
#: Gaming", "Source Engine", "People Can Fly"), and a name listed in
#: ``data/origins.json`` always counts as named. Extendable through
#: ``facts.anonymous_vocabulary`` in config.yaml.
_DEFAULT_ANONYMOUS_VOCABULARY = frozenset({
    "sources", "source", "insiders", "insider", "officials", "official",
    "people", "person", "persons", "familiar", "matter", "close", "project",
    "plans", "studio", "industry", "unnamed", "anonymous", "unidentified",
    "several", "multiple", "some", "a", "an", "the", "with", "to", "of", "who",
    "requested", "anonymity", "knowledgeable", "aware", "involved", "inside",
    "within", "company", "developer", "staff", "employees", "employee",
    "former", "current", "one", "two", "three", "four", "five",
})

_ANONYMOUS_VOCABULARY_CACHE: tuple[tuple[int, int] | None, frozenset[str]] | None = None


def anonymous_vocabulary() -> frozenset[str]:
    """The anonymous-descriptor vocabulary: the code defaults plus config.

    A cue phrase is anonymous only when every one of its tokens is in this set,
    so extending ``facts.anonymous_vocabulary`` in config.yaml can teach the
    pipeline a new anonymous descriptor ("a well-placed source" -> "well",
    "placed"). Cached against the config file's size and mtime.
    """
    global _ANONYMOUS_VOCABULARY_CACHE
    resolved = DEFAULT_CONFIG_PATH
    try:
        stat = Path(resolved).stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = None
    if _ANONYMOUS_VOCABULARY_CACHE is not None and _ANONYMOUS_VOCABULARY_CACHE[0] == signature:
        return _ANONYMOUS_VOCABULARY_CACHE[1]
    entries = (load_config().get("facts") or {}).get("anonymous_vocabulary") or []
    words = set(_DEFAULT_ANONYMOUS_VOCABULARY)
    for entry in entries:
        for word in re.split(r"\s+", _strip_possessive(str(entry).lower())):
            token = re.sub(r"[^a-z0-9]+", "", word)
            if token:
                words.add(token)
    result = frozenset(words)
    _ANONYMOUS_VOCABULARY_CACHE = (signature, result)
    return result


CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "value": {"type": "string"},
                    "source_url": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "origin": {"type": "string"},
                    "affiliation": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KIND_STRENGTH)},
                },
                "required": [
                    "claim",
                    "value",
                    "source_url",
                    "confidence",
                    "origin",
                    "affiliation",
                    "kind",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTION = (
    "You are a news-room fact extractor, working one article at a time. From "
    "the single article text below, extract every distinct fact about the "
    "story's subject: the features, mechanics, modes, dates, prices, "
    "platforms, requirements and named quotes the text states. Do not stop "
    "early because the article is long: extract every distinct fact, up to the "
    "per-source limit given with the article. Never invent facts, values, or "
    "URLs; write only what the text actually says.\n"
    "Always extract the headline's central claim as one of the claims, even "
    "when the body states other facts first.\n"
    "Return each fact as an object with: claim (a short statement), value (the "
    "concrete value or a one-line summary), source_url (the exact URL given "
    "for this source), confidence (0 to 1), origin, affiliation, and kind.\n"
    "origin names who produced the claim: the company or official source, or "
    "the analyst or firm the article cites (for example 'Sony', 'Sensor "
    "Tower'). When the article does not say who produced the claim, set origin "
    "to 'unstated'.\n"
    "affiliation names the organization a person speaks for, when the article "
    "states one (for example 'Embark Studios' for that studio's executive "
    "producer). Always include the affiliation field in every claim: when the "
    "origin is a company itself, or the article names no organization, set "
    "affiliation to an empty string \"\" (never omit the key).\n"
    "kind is exactly one of: 'confirmed' (the company or an official source "
    "states it, with a named origin), 'reported' (an outlet citing a named "
    "source), 'estimate' (a third-party analyst or data estimate; never label "
    "an estimate 'confirmed'), 'statement' (a company or person's statement "
    "about a plan, possibility, intention or survey result; never label a "
    "statement 'confirmed'), 'rumor' (a leak or unnamed source), or 'opinion' "
    "(a writer's or source's judgement). origin 'unstated' caps a claim at "
    "'reported'.\n"
    "Keep hedge words (estimated, reportedly, more than, over, could, may, "
    "might, possibly, hopes to, not a promise) in the value. " 
    "For percentages and overlap or audience figures, state the exact "
    "relationship in words (for example 'overlap with players of X'), never a "
    "vague phrase. Any in-game currency amount must name the currency (for "
    "example '100,000 in-game DMZ Cash'), never appear as a bare dollar figure.\n"
    "Write every date as an absolute calendar date, never a relative time word: "
    "convert 'today', 'tonight', 'now', 'currently', 'out now', 'this week', "
    "'this weekend', 'yesterday', 'tomorrow', 'recently', 'soon' and 'next week' "
    "to the calendar date the article supports (using its publication date when "
    "given). Write each date in the form 'November 2, 2026' (month name, day, "
    "four-digit year), never as '2026-11-02' and never with a zero-padded day. "
    "A value must not claim more than the article states: keep the exact "
    "figures, names and dates the text gives, and never add words of your own "
    "such as 'full', 'complete', 'all', 'every', 'only' or 'official' unless the "
    "text uses them.\n"
    "A claim label must not add a contrast the article never states: do not write "
    "'and not', 'but not', 'rather than' or 'instead of' unless the article "
    "itself states that contrast.\n"
    "When the article is a company's own post and names a byline (for example "
    "'I'm [Name], [role] at [Company]'), attribute statements and explanations to "
    "that named person and use the company they name as the affiliation; never "
    "set the affiliation to the publishing outlet and never leave a bylined "
    "claim's origin 'unstated'.\n"
    "A named person describing a plan, possibility, intention, design decision or "
    "reason is a 'statement' attributed to that person (kind 'statement', origin "
    "the speaker), never 'confirmed'; a company's own post describing features, "
    "mechanics or a roster it is shipping is 'confirmed' and must carry the "
    "publisher or named speaker as its origin."
)


def _extraction_chain() -> list[dict]:
    """The enabled 'fast' role entries from config.yaml: they pick the extractor.

    This is what actually changes which model extracts claims, so a config edit
    must invalidate already-written facts sheets.
    """
    roles = load_config().get("roles") or {}
    return [
        {"provider": entry.get("provider"), "model": entry.get("model"), "family": entry.get("family")}
        for entry in (roles.get("fast") or [])
        if entry.get("enabled", True)
    ]


class FactsError(Exception):
    """The model output did not match the claim schema; the story is dropped."""


class UnconfirmedStory(Exception):
    """Not enough independent sources; the story must not be published."""


def build_prompt(
    id: str,
    source: dict,
    *,
    max_claims: int = MAX_CLAIMS_PER_SOURCE,
    published: object = None,
) -> str:
    """Build the extraction prompt for ONE source (one model call per source)."""
    parts = [SYSTEM_INSTRUCTION, "", f"Story ID: {id}", ""]
    parts.append(
        f"SOURCE: {source['source_name']} "
        f"(tier {source['tier']}, owner {source['owner']}, region {source['region']})"
    )
    parts.append(f"URL: {source['link']}")
    date = _as_date(published) or _source_published(source)
    if date:
        parts.append(f"PUBLISHED: {_MONTH_NAMES[date.month]} {date.day}, {date.year}")
    if source.get("title") and str(source["title"]).strip():
        parts.append(f"TITLE: {source['title']}")
    parts.append(
        f"Extract up to {max_claims} distinct facts from the text below, always "
        f"including the headline's central claim; every claim's source_url must "
        f"be exactly {source['link']}."
    )
    text = (source.get("text") or "").strip()
    if text:
        parts.append("TEXT:\n" + _truncate(text))
    else:
        parts.append("TEXT: (no text available)")
    return "\n".join(parts)


def _truncate(text: str, limit: int = MAX_SOURCE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [truncated]"


def extract_claims(
    id: str,
    source: dict,
    *,
    max_claims: int = MAX_CLAIMS_PER_SOURCE,
    generate: Callable[..., Generation] = default_generate,
    run_state: RunState | None = None,
    **kwargs: Any,
) -> Generation:
    """Run the 'fast' role on a single source, returning its Generation."""
    prompt = build_prompt(id, source, max_claims=max_claims)
    return generate("fast", prompt, json_schema=CLAIM_SCHEMA, run_state=run_state, **kwargs)


def _clip_field(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    """Keep claim text and values short so a sheet never stores a long passage."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _with_claim_defaults(data: Any) -> Any:
    """Return ``data`` with a missing ``affiliation`` filled in as ``""``.

    Strict JSON schemas (Groq, OpenAI) require every property to appear in
    ``required``, so the extraction prompt always returns ``affiliation`` and
    uses ``""`` when there is none. This keeps our own reader tolerant of an
    output that nevertheless omits the key: ``""`` and a missing key are the
    same. Anything not shaped like a claims payload is returned untouched and
    left to :func:`schema_errors` to reject.
    """
    if not isinstance(data, dict):
        return data
    claims = data.get("claims")
    if not isinstance(claims, list):
        return data
    normalized = [
        {**claim, "affiliation": claim.get("affiliation", "")}
        if isinstance(claim, dict)
        else claim
        for claim in claims
    ]
    return {**data, "claims": normalized}


def validate_claims(data: Any) -> list[dict]:
    """Validate the model output against CLAIM_SCHEMA and return the claims.

    ``claim`` and ``value`` are clipped to :data:`MAX_FIELD_CHARS` so a facts
    sheet never holds a long verbatim passage copied from a source article.
    ``origin`` is normalized (trimmed, lowercased; empty becomes
    :data:`UNSTATED_ORIGIN`), ``affiliation`` is normalized (trimmed,
    lowercased; empty stays empty), and a claim whose origin is ``unstated`` is
    capped at ``reported`` because an unattributed claim is never confirmed and
    never an estimate (requirement: treat an unstated origin as at most
    reported).

    A missing ``affiliation`` key is treated exactly like an empty string
    before validation, so a model that omits the (now strictly required) key
    still yields a valid claim.
    """
    data = _with_claim_defaults(data)
    errors = schema_errors(data, CLAIM_SCHEMA)
    if errors:
        raise FactsError("; ".join(errors))
    claims = data["claims"]
    clipped: list[dict] = []
    for index, claim in enumerate(claims):
        url = claim["source_url"]
        if not re.match(r"^https?://", url):
            raise FactsError(f"claims[{index}].source_url must be an http(s) URL: {url!r}")
        origin = _normalize_origin(claim.get("origin"))
        kind = claim["kind"]
        if origin == UNSTATED_ORIGIN and kind in ("confirmed", "estimate"):
            kind = "reported"
        clipped.append(
            {
                **claim,
                "claim": _clip_field(claim["claim"]),
                "value": _clip_field(claim["value"]),
                "origin": origin,
                "affiliation": _normalize_affiliation(claim.get("affiliation")),
                "kind": kind,
            }
        )
    return clipped


def _normalize_origin(origin: object) -> str:
    value = str(origin or "").strip().lower()
    return value or UNSTATED_ORIGIN


def _normalize_affiliation(affiliation: object) -> str:
    value = str(affiliation or "").strip().lower()
    return value if value != UNSTATED_ORIGIN else ""


# --- relative time: absolutize before a claim can be trusted ------------------

#: Relative-time words a claim must never keep: a page that says "runs today"
#: goes stale the moment it is read. Overridable via ``facts.relative_time_words``
#: in config.yaml; defaults keep the code usable without the config.
DEFAULT_RELATIVE_TIME_WORDS = (
    "today", "tonight", "now", "currently", "this week", "this weekend",
    "yesterday", "tomorrow", "recently", "soon", "next week", "out now",
)

_MONTH_NAMES = (
    "", "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)

#: ``published`` values are normally a date or an ISO datetime; parse either.
_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_LINK_DATE_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})(?:/|$)")


def _relative_time_words() -> tuple[str, ...]:
    """The configured relative-time words, longest first for greedy matching."""
    config = load_config().get("facts") or {}
    words = config.get("relative_time_words")
    if not isinstance(words, list) or not words:
        words = list(DEFAULT_RELATIVE_TIME_WORDS)
    cleaned = [str(word).strip().lower() for word in words if str(word).strip()]
    return tuple(sorted(set(cleaned), key=len, reverse=True))


def _as_date(value: object) -> Optional[dt.date]:
    """Parse a date or ISO datetime string into a :class:`datetime.date`."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    match = _DATE_PREFIX_RE.match(str(value or "").strip())
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _source_published(source: dict) -> Optional[dt.date]:
    """A source's publication date: its ``published`` field, else its URL date.

    Gathered sources carry ``published`` (the feed item's date, carried through
    the gather stage); a dated URL path (``/2026/10/07/``) is the fallback.
    When neither is present the claim has no date to absolutize from, and a
    feed-only figure cannot be grounded against it.
    """
    published = _as_date(source.get("published"))
    if published:
        return published
    match = _LINK_DATE_RE.search(str(source.get("link") or ""))
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


#: Words that already introduce a date: "from today" must become "from <date>",
#: never "from on <date>".
_DATE_PREPOSITIONS = (
    "as of", "from", "on", "until", "till", "through", "by", "after", "before",
)


def _follows_date_preposition(text: str, match: re.Match) -> bool:
    """True when the matched phrase follows a date preposition ("from today")."""
    prefix = text[: match.start()].rstrip().lower()
    return any(
        re.search(rf"(?<!\w){re.escape(prep)}(?!\w)\s*$", prefix)
        for prep in _DATE_PREPOSITIONS
    )


def absolutize_relative_time(value: object, published: object) -> Optional[str]:
    """Rewrite relative-time words in ``value`` to grammatical absolute dates.

    Every rewrite keeps the date attached to a preposition, so a sentence never
    ends up with a bare calendar date: "begins today" becomes "begins on October
    7, 2026", "is out now on PS5 and PC" becomes "is available on PS5 and PC as
    of October 7, 2026", and "now" or "currently" become "as of <date>". When a
    phrase already follows a date preposition ("from today") the duplicate "on"
    is dropped. "yesterday" and "tomorrow" shift by a day.

    Returns the rewritten value, the unchanged value when it holds no
    relative-time word, or ``None`` when it does but ``published`` gives no date
    to convert from (the caller drops that claim rather than store a stale one).
    """
    text = str(value or "")
    words = _relative_time_words()
    if not text or not words:
        return text
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(word) for word in words) + r")(?!\w)",
        re.IGNORECASE,
    )
    if not pattern.search(text):
        return text
    published_date = _as_date(published)
    if published_date is None:
        return None

    def _date(day: dt.date) -> str:
        return f"{_MONTH_NAMES[day.month]} {day.day}, {day.year}"

    had_out_now = False

    def replace(match: re.Match) -> str:
        nonlocal had_out_now
        word = match.group(0).lower()
        if word == "out now":
            # "is out now on PS5 and PC" rewrites to "is available on PS5 and
            # PC"; the "as of <date>" clause is appended once, at the end.
            had_out_now = True
            return "available"
        delta = {"yesterday": -1, "tomorrow": 1}.get(word, 0)
        day = published_date + dt.timedelta(days=delta)
        if word in ("now", "currently"):
            return f"as of {_date(day)}"
        prefix = "" if _follows_date_preposition(text, match) else "on "
        return prefix + _date(day)

    rewritten = pattern.sub(replace, text)
    if had_out_now:
        rewritten = f"{rewritten} as of {_date(published_date)}"
    return rewritten


def _absolutize_claims(
    claims: list[dict],
    sources: list[dict],
    *,
    story_id: str = "",
) -> tuple[list[dict], list[dict]]:
    """Replace relative-time values with dates; drop claims with no date."""
    by_url = _sources_by_url(sources)
    kept: list[dict] = []
    dropped: list[dict] = []
    for claim in claims:
        source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
        rewritten = absolutize_relative_time(claim.get("value"), _source_published(source))
        if rewritten is None:
            dropped.append(claim)
            logger.warning(
                "story %s: dropping claim with a relative time and no date: %r",
                story_id or "?",
                claim.get("value"),
            )
            continue
        kept.append({**claim, "value": rewritten})
    return kept, dropped


# --- grounding: a claim's value must stay inside its own source ---------------

_GROUNDING_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_GROUNDING_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")

#: Absolute qualifiers that only a source may state. A value that adds one on
#: its own ("full", "complete") claims more than the article does.
_ABSOLUTE_QUALIFIERS = (
    "full", "complete", "entire", "all", "every", "only",
    "first-ever", "official", "officially", "out now", "currently",
)

#: Contrast connectives that must be stated by the source before a claim label
#: may use them ("... and not a day-one fighter").
_CONTRAST_MARKERS = ("and not", "but not", "rather than", "instead of")


def _numbers_in(text: object) -> set[str]:
    """Digit figures in ``text`` with thousands separators removed."""
    return {
        match.replace(",", "").rstrip(".")
        for match in _GROUNDING_NUMBER_RE.findall(str(text or ""))
    }


def _capitalized_terms(text: object) -> set[str]:
    """Folded capitalized terms in a value, ignoring its first (sentence) word.

    Sentence-initial capitalization is not evidence, so "The" is skipped while
    real names and acronyms ("PvE", "Steam", "October") are checked against the
    source text. Folded for comparison, so "Tōkon" matches "Tokon".
    """
    raw = str(text or "")
    tokens = _GROUNDING_WORD_RE.findall(raw)
    terms: set[str] = set()
    for index, token in enumerate(tokens):
        if index == 0:
            continue
        if token[0].isupper() and any(char.isalpha() for char in token):
            terms.add(cluster.strip_diacritics(token).lower())
    return terms


def _word_in(text: object, phrase: str) -> bool:
    """True when ``phrase`` appears in ``text`` on word boundaries, folded."""
    haystack = cluster.strip_diacritics(text).lower()
    needle = cluster.strip_diacritics(phrase).lower()
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


#: Month name (full or abbreviated) -> calendar month number, for parsing
#: dates written as words.
_MONTH_NUMBERS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_WORD = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_MONTH_FIRST_DATE_RE = re.compile(
    rf"\b(?P<month>{_MONTH_WORD})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*,?\s*(?P<year>\d{{4}}))?(?!\d)",
    re.IGNORECASE,
)
_DAY_FIRST_DATE_RE = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>{_MONTH_WORD})\.?"
    rf"(?:\s*,?\s*(?P<year>\d{{4}}))?(?!\d)",
    re.IGNORECASE,
)

#: A calendar date normalized to ``(year or None, month, day)``.
DateTuple = tuple[Optional[int], int, int]


def _date_matches(text: object) -> list[tuple[DateTuple, set[str]]]:
    """Every date in ``text`` as ``((year|None, month, day), digits-used)``."""
    raw = str(text or "")
    found: list[tuple[DateTuple, set[str]]] = []
    for match in _ISO_DATE_RE.finditer(raw):
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            found.append(((year, month, day), {match.group(1), match.group(2), match.group(3)}))
    for pattern in (_MONTH_FIRST_DATE_RE, _DAY_FIRST_DATE_RE):
        for match in pattern.finditer(raw):
            month = _MONTH_NUMBERS.get(match.group("month").lower())
            if month is None:
                continue
            day = int(match.group("day"))
            if not 1 <= day <= 31:
                continue
            year = int(match.group("year")) if match.group("year") else None
            digits = {match.group("day")}
            if match.group("year"):
                digits.add(match.group("year"))
            found.append(((year, month, day), digits))
    return found


def _dates_in(text: object) -> set[DateTuple]:
    """Calendar dates in ``text``, in ISO, "November 2" or "2nd November" form."""
    return {date for date, _digits in _date_matches(text)}


def _date_number_tokens(text: object) -> set[str]:
    """Digit tokens that make up a date in ``text`` (so they are not read as figures)."""
    tokens: set[str] = set()
    for _date, digits in _date_matches(text):
        tokens |= digits
    return tokens


def _format_date(date: DateTuple) -> str:
    """Render a normalized date the way the pipeline writes dates."""
    year, month, day = date
    text = f"{_MONTH_NAMES[month]} {day}"
    return f"{text}, {year}" if year is not None else text


def _date_grounded(
    date: DateTuple,
    source_dates: set[DateTuple],
    anchor: Optional[dt.date],
) -> bool:
    """True when a value's date appears in the source, or is the anchor date.

    A date matches on month and day; a year given in the value must match too,
    but a source or anchor without a year still grounds the month and day
    ("2026-11-02" is grounded by a body that says only "November 2").
    """
    year, month, day = date
    for source_year, source_month, source_day in source_dates:
        if source_month == month and source_day == day:
            if year is None or source_year is None or source_year == year:
                return True
    if anchor is not None and anchor.month == month and anchor.day == day:
        if year is None or anchor.year == year:
            return True
    return False


def _value_month_words(dates: set[DateTuple]) -> set[str]:
    """Month words (full and abbreviated) used by a value's dates, folded."""
    words: set[str] = set()
    for _year, month, _day in dates:
        name = _MONTH_NAMES[month].lower()
        words.add(name)
        words.add(name[:3])
    return words


def _anchor_tokens(anchor: Optional[dt.date]) -> tuple[set[str], set[str]]:
    """The figures and month names an anchor publication date grounds.

    A claim may state the feed item's own publication date even when the body
    only gives it relatively ("out now", "today"): every component of
    ``anchor`` -- year, month and day, in any common format, the month's name
    and abbreviation included -- counts as grounded, because those tokens come
    from the feed/URL date rather than the article text.
    """
    if anchor is None:
        return set(), set()
    figures = {
        str(anchor.year),
        f"{anchor.year:02d}"[-2:],
        str(anchor.month),
        f"{anchor.month:02d}",
        str(anchor.day),
        f"{anchor.day:02d}",
    }
    terms: set[str] = set()
    if 1 <= anchor.month <= 12:
        name = _MONTH_NAMES[anchor.month].lower()
        terms.add(name)
        terms.add(name[:3])
    return figures, terms


def grounding_failure(
    claim: dict,
    source_text: object,
    *,
    anchor: Optional[dt.date] = None,
) -> Optional[str]:
    """Why a claim's value or label goes beyond its source, or ``None``.

    Checked against the claim's own source text, diacritics-insensitive: every
    number in the value must appear in the text, every capitalized name or
    acronym the value uses must appear in the text, an absolute qualifier
    ("full", "complete") is allowed only when the text uses it, and a contrast
    connective in the label ("and not", "rather than") is allowed only when the
    text states it. ``anchor`` is the source's publication date: figures equal
    to any component of it (year, month, day, in any common format, the month's
    name and abbreviation included) count as grounded too, because such tokens
    come from the feed/URL date rather than the article body.

    Dates are compared as calendar dates, not digit strings, so a value written
    as "2026-11-02" is grounded by a body that says "November 2", and a value's
    date is grounded when it matches the source's month and day or the anchor
    date. The digits a date is made of are not treated as loose figures.
    """
    value = str(claim.get("value") or "")
    label = str(claim.get("claim") or "")
    source = str(source_text or "")
    anchor_figures, anchor_terms = _anchor_tokens(anchor)
    value_dates = _dates_in(value)
    date_tokens = _date_number_tokens(value)
    missing_numbers = (
        _numbers_in(value) - _numbers_in(source) - anchor_figures - date_tokens
    )
    if missing_numbers:
        return f"value states {sorted(missing_numbers)} the source never gives"
    source_dates = _dates_in(source)
    month_words = _value_month_words(value_dates)
    for date in sorted(value_dates, key=lambda item: -1 if item[0] is None else item[0]):
        if not _date_grounded(date, source_dates, anchor):
            return f"value states the date {_format_date(date)} the source never gives"
    for term in _capitalized_terms(value):
        if term in anchor_terms or term in month_words:
            continue
        if not _word_in(source, term):
            return f"value names {term!r} the source never gives"
    for qualifier in _ABSOLUTE_QUALIFIERS:
        if _word_in(value, qualifier) and not _word_in(source, qualifier):
            return f"value uses {qualifier!r} the source never states"
    for marker in _CONTRAST_MARKERS:
        if _word_in(label, marker) and not _word_in(source, marker):
            return f"label adds the contrast {marker!r} the source never states"
    return None


def ground_claims_details(
    claims: list[dict],
    sources: list[dict],
    *,
    story_id: str = "",
) -> tuple[list[dict], list[dict]]:
    """Drop unsupported claims, returning ``(kept, dropped_details)``.

    Each entry in ``dropped_details`` is ``{"claim": <claim>, "reason": <str>}``
    so the caller can persist why a claim was discarded. Each claim is checked
    against its own source's text; the source's publication date (feed
    ``published``, else the URL date) is passed as the grounding anchor, so a
    feed-only date token is never treated as ungrounded.
    """
    by_url = _sources_by_url(sources)
    kept: list[dict] = []
    dropped: list[dict] = []
    for claim in claims:
        source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
        reason = grounding_failure(
            claim,
            source.get("text") or "",
            anchor=_source_published(source),
        )
        if reason is None:
            kept.append(claim)
        else:
            dropped.append({"claim": claim, "reason": reason})
            logger.warning(
                "story %s: dropping ungrounded claim from %s (%s): %r",
                story_id or "?",
                claim.get("source_url"),
                reason,
                claim.get("claim"),
            )
    return kept, dropped


def ground_claims(
    claims: list[dict],
    sources: list[dict],
    *,
    story_id: str = "",
) -> tuple[list[dict], list[dict]]:
    """Drop claims whose value or label is not supported by their own source."""
    kept, dropped_details = ground_claims_details(claims, sources, story_id=story_id)
    return kept, [entry["claim"] for entry in dropped_details]


# --- bylines: a company post's speaker is the person it names -----------------

#: A company's own post opens with its author. Three explicit forms are
#: trusted: a self-introduction ("I'm <Name>, <role> at <Company>"), a byline
#: ("by <Name>, <Role>, <Company>") and a byline line ("<Name>, <Role>,
#: <Company>"). Ordinary prose that merely mentions a name is never a byline.
_BYLINE_NAME = r"[^\W_][\w'’-]*(?:\s+[^\W_][\w'’-]*){0,3}?"
_BYLINE_ORG = r"[^\W_][\w'’&\-]*(?:\s+[^\W_][\w'’&\-]*){0,4}"
_BYLINE_RE = re.compile(
    r"\bI(?:['’]m| am)\s+"
    rf"({_BYLINE_NAME})\s*,\s*[^.\n]{{0,80}}?\b(?:at|of|for)\s+({_BYLINE_ORG})",
    re.IGNORECASE,
)
#: "by <Name>, <Role>, <Company>" — a byline line where the role is optional.
_BYLINE_BY_RE = re.compile(
    rf"\bby\s+({_BYLINE_NAME})\s*,\s*([^,.\n]{{0,60}}?),\s*({_BYLINE_ORG})",
    re.IGNORECASE,
)
#: "<Name>, <Role>, <Company>" opening a company post.
_BYLINE_ROLE_RE = re.compile(
    rf"^\s*({_BYLINE_NAME})\s*,\s*([^,.\n]{{0,60}}?),\s*({_BYLINE_ORG})",
    re.IGNORECASE,
)


def _extract_byline(text: object) -> tuple[str, str]:
    """The ``(name, organization)`` a post's byline states, or ``("", "")``."""
    raw = str(text or "")
    for match in _BYLINE_RE.finditer(raw):
        return match.group(1).strip(), match.group(2).strip()
    byline = _BYLINE_BY_RE.search(raw)
    if byline:
        return byline.group(1).strip(), byline.group(3).strip()
    role_line = _BYLINE_ROLE_RE.search(raw)
    if role_line:
        return role_line.group(1).strip(), role_line.group(3).strip()
    return "", ""


def _is_own_site(source: dict) -> bool:
    """True when the gathered source is a tier-1 organization's own site."""
    try:
        return int(source.get("tier") or 0) == 1
    except (TypeError, ValueError):
        return False


def _self_attribution(
    text: str, source: dict, byline_name: str, byline_org: str, aliases: dict[str, str]
) -> str:
    """The origin a company's own first-person post attributes to itself, or "".

    Only a tier-1 own-site post that carries a byline self-attributes. The
    organization the byline states wins ("Producer, Arc System Works"); else the
    named person; else the source's owner. An outlet article using "we" or "I"
    never reaches here, so its unattributed claims stay unstated.
    """
    if not _is_own_site(source) or not (byline_name or byline_org):
        return ""
    if byline_org:
        return canonicalize_origin(byline_org, aliases)
    if byline_name and _origin_in_text(byline_name, text, aliases):
        return canonicalize_origin(byline_name, aliases)
    owner = source.get("owner")
    if owner:
        return canonicalize_origin(owner, aliases)
    return ""


def _is_publication(name: object, source: dict) -> bool:
    """True when ``name`` is the publishing outlet itself, not a speaker's org.

    A publication ("PlayStation Blog") is not the organization a bylined person
    speaks for, so it must be replaced by the byline's own organization.
    """
    key = _origin_key(name)
    if not key:
        return False
    for field in ("source_name", "owner"):
        target = _origin_key(source.get(field))
        if target and (key == target or _contains_name(key, target)):
            return True
    return False


# --- origin canonicalization and verification --------------------------------


def _strip_possessive(text: str) -> str:
    return re.sub(r"['\u2019]s\b", "", text)


def _origin_key(origin: object) -> str:
    """Match key for an origin: punctuation-free, lowercased, titles stripped.

    Used for alias lookup, source-text verification and containment, so casing,
    possessives, leading titles and punctuation never split one origin into
    several spellings of the same name.
    """
    words = cluster.normalize_title(_strip_possessive(str(origin or "").lower())).split()
    while words and words[0] in _ORIGIN_TITLE_WORDS:
        words.pop(0)
    return " ".join(words)


def _origin_display(origin: object) -> str:
    """Canonical human-readable origin: lowercased, no possessive, no leading title."""
    text = re.sub(r"\s+", " ", _strip_possessive(str(origin or "").lower())).strip()
    text = text.strip(" \t.,;:!?")
    words = text.split()
    while words and words[0] in _ORIGIN_TITLE_WORDS:
        words.pop(0)
    return " ".join(words)


def _contains_name(haystack: str, needle: str) -> bool:
    """True when match-normalized ``needle`` appears in ``haystack`` on word boundaries."""
    if not needle:
        return False
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None


def load_origin_aliases(path: str | Path | None = None) -> dict[str, str]:
    """Alias -> canonical origin map from ``data/origins.json``.

    Optional: a missing, unreadable or malformed file yields ``{}`` so
    extraction still runs. Alias keys are stored under their match key
    (:func:`_origin_key`) and values under their canonical display name, so
    lookups ignore case, possessives, titles and punctuation.
    """
    path = Path(path) if path else DEFAULT_ORIGIN_ALIASES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, str] = {}
    for alias, canonical in raw.items():
        if not isinstance(canonical, str):
            continue
        key = _origin_key(alias)
        name = _origin_display(canonical)
        if key and name:
            aliases[key] = name
    return aliases


_ORGANIZATIONS_CACHE: tuple[tuple[int, int] | None, frozenset[str]] | None = None


def load_organizations(path: str | Path | None = None) -> frozenset[str]:
    """Known organization match keys from ``data/orgs.json``.

    The file is ``{"organizations": [...]}`` where each entry is either a name
    string or ``{"canonical": ..., "aliases": [...]}``. Keys are stored under
    :func:`_origin_key`, so casing, possessives and punctuation never matter.
    Cached by file mtime/size; a missing or malformed file yields an empty set
    so extraction still runs.
    """
    global _ORGANIZATIONS_CACHE
    path = Path(path) if path else DEFAULT_ORGS_PATH
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _ORGANIZATIONS_CACHE = (None, frozenset())
        return frozenset()
    if _ORGANIZATIONS_CACHE is not None and _ORGANIZATIONS_CACHE[0] == stamp:
        return _ORGANIZATIONS_CACHE[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _ORGANIZATIONS_CACHE = (stamp, frozenset())
        return frozenset()
    raw = data.get("organizations") if isinstance(data, dict) else data
    keys: set[str] = set()
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                key = _origin_key(entry)
                if key:
                    keys.add(key)
            elif isinstance(entry, dict):
                for name in [entry.get("canonical"), *(entry.get("aliases") or [])]:
                    key = _origin_key(name)
                    if key:
                        keys.add(key)
    frozen = frozenset(keys)
    _ORGANIZATIONS_CACHE = (stamp, frozen)
    return frozen


def canonicalize_origin(origin: object, aliases: dict[str, str] | None = None) -> str:
    """Resolve one origin string to its canonical name.

    An alias in :func:`load_origin_aliases` is mapped to its canonical name, and
    when a canonical name appears inside the origin ("Rhys Elliott of Alinea
    Analytics") the firm wins, so a person named alongside a firm counts as the
    firm. Empty or ``unstated`` input stays :data:`UNSTATED_ORIGIN`.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    key = _origin_key(origin)
    if not key or key == UNSTATED_ORIGIN:
        return UNSTATED_ORIGIN
    if key in aliases:
        return aliases[key]
    embedded = [
        canonical
        for canonical in set(aliases.values())
        if _contains_name(key, _origin_key(canonical))
    ]
    if embedded:
        return max(embedded, key=len)
    return _origin_display(origin) or UNSTATED_ORIGIN


def _origin_names(origin: object, aliases: dict[str, str]) -> set[str]:
    """Every match key that identifies an origin: its own form, its canonical
    name, and any alias that resolves to that canonical name."""
    canonical = canonicalize_origin(origin, aliases)
    if canonical == UNSTATED_ORIGIN:
        return set()
    canonical_key = _origin_key(canonical)
    names = {_origin_key(origin), canonical_key}
    names.update(
        alias for alias, target in aliases.items() if _origin_key(target) == canonical_key
    )
    return {name for name in names if name}


def _origin_in_text(origin: object, text: str, aliases: dict[str, str]) -> bool:
    """True when any spelling of ``origin`` appears in the source ``text``."""
    haystack = _origin_key(text)
    if not haystack:
        return False
    return any(_contains_name(haystack, name) for name in _origin_names(origin, aliases))


def _anonymous_tokens(folded: str) -> list[str]:
    """The word tokens of a normalized origin, possessive "s" already stripped."""
    return [token for token in re.split(r"[^a-z0-9]+", folded) if token]


def _is_known_origin_name(folded: str, aliases: dict[str, str]) -> bool:
    """True when ``folded`` matches a known origin name or alias in origins.json."""
    if not folded:
        return False
    if folded in aliases:
        return True
    return any(_origin_key(canonical) == folded for canonical in aliases.values())


#: Speech verbs: their subject is a person who said something, so that person
#: stays a speaker even when they are also a headline entity.
_ATTRIBUTION_SPEECH_VERBS = (
    "said", "says", "say", "told", "tells", "tell", "admitted", "admits",
    "blames", "blamed", "claims", "claimed", "wrote", "writes", "posted",
    "posts", "tweeted", "tweets", "stated", "states", "explains", "explained",
    "added", "adds", "noted", "notes", "warned", "warns",
)
#: Announcement and reporting verbs: their subject is an organization (a company
#: or studio) that put something out. They never make a game, character or
#: product a speaker, so a game that "reveals" a roster stays a subject.
_ATTRIBUTION_ANNOUNCEMENT_VERBS = (
    "announced", "announces", "revealed", "reveals", "confirmed", "confirms",
    "unveiled", "unveils", "detailed", "details", "shared", "shares",
    "reported", "reports", "estimated", "estimates", "suggested", "suggests",
    "alleged", "alleges",
)
_ATTRIBUTION_VERBS = tuple(
    dict.fromkeys(_ATTRIBUTION_SPEECH_VERBS + _ATTRIBUTION_ANNOUNCEMENT_VERBS)
)
_SPEECH_VERB_RE = "|".join(sorted(set(_ATTRIBUTION_SPEECH_VERBS), key=len, reverse=True))
_ATTRIBUTION_VERB_RE = "|".join(sorted(set(_ATTRIBUTION_VERBS), key=len, reverse=True))
_ATTRIBUTION_LEAD_RE = (
    r"in a statement from|in an interview with|in a statement|in an interview|"
    r"according to|data from|figures from|numbers from|estimates from|per|via|citing"
)
#: A capitalized origin chunk (up to four words) after a lead cue or before a
#: verb. Capitalization is the cheap way to find a name without an NLP parser.
_NAME_CHUNK = r"[A-Za-z][\w'’&\-\']*(?:\s+(?!has\b|have\b|had\b|is\b|was\b|were\b|will\b)[A-Za-z][\w'’&\-\']*){0,3}"
_LEAD_CUE_RE = re.compile(
    rf"\b(?:{_ATTRIBUTION_LEAD_RE})\s+(?:[a-z]+\s+){{0,4}}(?P<origin>{_NAME_CHUNK})",
    re.I,
)
_SUBJECT_CUE_RE = re.compile(
    rf"\b(?P<origin>{_NAME_CHUNK})(?:\s*,[^,.\n]{{0,40}},)?"
    rf"\s+(?:(?:has|have|had|is|was|were|will)\s+)?(?P<verb>{_ATTRIBUTION_VERB_RE})\b",
    re.I,
)
#: A capitalized name chunk (up to four words). Unlike :data:`_NAME_CHUNK` the
#: first word must be capitalized, for the inverted headline form where the
#: organization follows the verb ("..., confirms Epic Games").
_CAP_NAME_CHUNK = (
    r"[A-Z][\w'’&\-]*(?:\s+(?!has\b|have\b|had\b|is\b|was\b|were\b|will\b)"
    r"[A-Za-z][\w'’&\-]*){0,3}"
)
_ANNOUNCEMENT_VERB_RE = "|".join(
    sorted(set(_ATTRIBUTION_ANNOUNCEMENT_VERBS), key=len, reverse=True)
)
#: Inverted headline attribution: the organization follows an announcement verb
#: ("Fortnite is coming to Linux, confirms Epic Games"). Only announcement verbs
#: qualify, so "said Dan Houser" stays a speech cue handled by the speech path.
_POST_VERB_CUE_RE = re.compile(
    rf"(?:,\s*|\s+)(?i:(?P<verb>{_ANNOUNCEMENT_VERB_RE}))\s+(?P<origin>{_CAP_NAME_CHUNK})\b"
)


def _sentences(text: str) -> list[str]:
    """Split ``text`` into sentences on end punctuation kept simple."""
    return [part for part in re.split(r"(?<=[.!?])\s+", str(text or "")) if part.strip()]


def _sentences_with_positions(text: str) -> list[tuple[int, int, str]]:
    """Sentences tagged ``(paragraph_index, index_in_paragraph)``.

    Paragraphs are separated by a blank line; the attribution window is clamped
    to one paragraph so a cue in an unrelated section never vouches for a claim.
    """
    out: list[tuple[int, int, str]] = []
    for pi, para in enumerate(re.split(r"\n\s*\n", str(text or ""))):
        for si, sent in enumerate(_sentences(para)):
            out.append((pi, si, sent))
    return out


def _claim_key_terms(claim: dict) -> set[str]:
    """Stemmed content words of a claim's text and value, for sentence matching."""
    return _topic_words(claim.get("claim")) | _topic_words(claim.get("value"))


def _claim_attribution_window(
    text: str, claim: dict
) -> tuple[list[tuple[int, int, str]], list[tuple[int, int]]]:
    """The claim's attribution window and its strongest claim-hit positions.

    A sentence is in the window when it shares a paragraph with a sentence that
    carries the claim's key terms and sits within three sentences of it. The
    anchor hits are the sentence(s) with the most key terms in common, so a
    passing mention ("GTA Online") never outweighs the sentence that actually
    states the claim ("GTA 6 ... PC ... 2027"). When the claim shares no content
    word with the text (a paraphrase) the whole text is searched, so a genuine
    paraphrase is never dropped for lack of a local match.
    """
    sents = _sentences_with_positions(text)
    terms = _claim_key_terms(claim)
    scored = [
        (pi, si, len(_topic_words(sent) & terms))
        for pi, si, sent in sents
        if terms and (_topic_words(sent) & terms)
    ]
    if not terms or not scored:
        return sents, []
    all_hits = [(pi, si) for pi, si, _ in scored]
    best = max(score for _, _, score in scored)
    anchors = [(pi, si) for pi, si, score in scored if score == best]
    window = [
        (pi, si, sent)
        for pi, si, sent in sents
        if any(pi == hpi and abs(si - hsi) <= 3 for hpi, hsi in all_hits)
    ]
    return window, anchors


def _cue_occurrences(text: str, claim: dict) -> tuple[list[dict], list[tuple[int, int]]]:
    """Every attribution cue in the claim's window, with its verb kind.

    ``kind`` is ``"speech"`` (a person speaks), ``"announcement"`` (an
    organization puts something out) or ``"lead"`` (a reporting phrase such as
    "according to").
    """
    window, hits = _claim_attribution_window(text, claim)
    occurrences: list[dict] = []
    for pi, si, sent in window:
        for match in _LEAD_CUE_RE.finditer(sent):
            occurrences.append(
                {"origin": match.group("origin").strip(), "kind": "lead", "pi": pi, "si": si}
            )
        for match in _SUBJECT_CUE_RE.finditer(sent):
            verb = match.group("verb").lower()
            kind = "speech" if re.fullmatch(_SPEECH_VERB_RE, verb) else "announcement"
            occurrences.append(
                {"origin": match.group("origin").strip(), "kind": kind, "pi": pi, "si": si}
            )
    return occurrences, hits


def _cue_distance(occurrence: dict, hits: list[tuple[int, int]]) -> tuple[int, int, int]:
    """Sort key for a cue: the nearest preceding cue to the claim wins.

    A cue in the claim's own sentence (distance 0) sorts first, then the closest
    sentence before it, then the closest sentence after it. Without a claim-hit
    sentence every cue ties, so the earliest in the text wins.
    """
    if not hits:
        return (0, 0, 0)
    best: tuple[int, int, int] | None = None
    for hpi, hsi in hits:
        if occurrence["pi"] != hpi:
            continue
        delta = occurrence["si"] - hsi
        key = (0, -delta, abs(delta)) if delta <= 0 else (1, delta, abs(delta))
        if best is None or key < best:
            best = key
    return best if best is not None else (2, 0, 0)


def _is_anonymous_origin(origin: object, aliases: dict[str, str]) -> bool:
    """True when a cue's origin names no real source ("sources", "unnamed sources").

    Closed-vocabulary rule: a phrase is anonymous only when every one of its
    tokens (lowercased, possessive "s" stripped) is in
    :func:`anonymous_vocabulary`. Any token outside the vocabulary makes it a
    name ("Insider Gaming", "Source Engine", "People Can Fly"), and a name in
    ``data/origins.json`` is always named.
    """
    folded = _origin_key(origin)
    if not folded:
        return True
    if _is_known_origin_name(folded, aliases):
        return False
    tokens = _anonymous_tokens(folded)
    if not tokens:
        return True
    vocabulary = anonymous_vocabulary()
    return all(token in vocabulary for token in tokens)


def _attribution_winner(text: str, claim: dict, aliases: dict[str, str] | None = None) -> str:
    """The origin the nearest cue in the claim's window attributes it to, or "".

    "According to Bloomberg, A happened. Rockstar said B happened." attributes B
    to Rockstar: the later cue names a different source, so Bloomberg no longer
    carries forward, and with several cues the nearest preceding one wins.
    Anonymous cues ("according to sources") never outrank a named source, so a
    named outlet in the same sentence wins over a trailing "sources".
    """
    if aliases is None:
        aliases = load_origin_aliases()
    occurrences, hits = _cue_occurrences(text, claim)
    named = [occ for occ in occurrences if not _is_anonymous_origin(occ["origin"], aliases)]
    if not named:
        return ""
    ordered = sorted(
        enumerate(named),
        key=lambda pair: (_cue_distance(pair[1], hits), pair[0]),
    )
    return ordered[0][1]["origin"]


def _speech_subjects(text: str, claim: dict) -> list[str]:
    """Origins that are the subject of a speech verb in the claim's window."""
    occurrences, _ = _cue_occurrences(text, claim)
    return [occurrence["origin"] for occurrence in occurrences if occurrence["kind"] == "speech"]


def _announcement_subjects(text: str, claim: dict) -> list[str]:
    """Origins that are the subject of an announcement verb in the claim's window.

    "Epic Games announced ...", "Nintendo confirmed ...": the subject puts the
    news out, which is what makes it an organization (and a first party).
    """
    occurrences, _ = _cue_occurrences(text, claim)
    return [
        occurrence["origin"] for occurrence in occurrences if occurrence["kind"] == "announcement"
    ]


def _inverted_origins(text: str, claim: dict) -> list[str]:
    """Origins named *after* an announcement verb ("..., confirms Epic Games")."""
    window, _ = _claim_attribution_window(text, claim)
    origins: list[str] = []
    for _pi, _si, sent in window:
        for match in _POST_VERB_CUE_RE.finditer(sent):
            origins.append(match.group("origin").strip())
    return origins


def _origins_overlap(left: set[str], right: set[str]) -> bool:
    """True when two origin-name sets name the same source (containment allowed)."""
    for a in left:
        for b in right:
            if a == b or _contains_name(a, b) or _contains_name(b, a):
                return True
    return False


def _token_matches_any(token: object, candidates: Iterable[object], aliases: dict[str, str]) -> bool:
    """True when ``token`` names any origin in ``candidates``."""
    names = _origin_names(token, aliases)
    if not names:
        return False
    return any(_origins_overlap(names, _origin_names(candidate, aliases)) for candidate in candidates)


def _origin_attributed(origin: object, text: str, claim: dict, aliases: dict[str, str]) -> bool:
    """True when the claim's own window attributes it to ``origin``.

    Accepted forms are the nearest cue ("according to X", "X said/reports/..."),
    a first-person self-introduction ("I'm X") and "X data/figures/...". A bare
    mention of the name never counts, and when a later cue names a different
    source the earlier cue no longer carries forward.
    """
    names = _origin_names(origin, aliases)
    if not names:
        return False
    winner = _attribution_winner(text, claim, aliases)
    if winner and _origins_overlap(names, _origin_names(winner, aliases)):
        return True
    for inverted in _inverted_origins(text, claim):
        if _origins_overlap(names, _origin_names(inverted, aliases)):
            return True
    target = cluster.normalize_title(text)
    for name in names:
        escaped = re.escape(name)
        # A bylined first-person post attributes itself: "I'm Takeshi Yamanaka".
        if re.search(rf"\b(?:i m|i am|my name is)\s+{escaped}\b", target):
            return True
        if re.search(rf"\b{escaped}\s+(?:data|figures|numbers|estimates)\b", target):
            return True
    return False


def _is_product_origin(value: object, entities: list[str] | None) -> bool:
    """True when ``value`` is, or contains, a canonical entity of the story.

    Canonical entities are what a story is about -- game titles, characters,
    platforms -- so they name the subject, not a speaker. An origin equal to or
    containing one ("the Hollow Knight: Silksong game") is not an origin and is
    downgraded. The check is one-way: it does not reject a name that is merely
    part of an entity ("Nintendo" against the entity "Nintendo Switch 2"), and a
    company that makes the product is kept when the byline or affiliation names
    it (the caller passes that exception in).
    """
    key = _origin_key(value)
    if not key:
        return False
    for entity in entities or []:
        entity_key = _origin_key(entity)
        if entity_key and (key == entity_key or _contains_name(key, entity_key)):
            return True
    return False


#: Corporate suffixes: a name whose last word is one of these is an organization
#: even when it is not in ``data/orgs.json`` ("Some New Studio", "Foo Games").
_CORPORATE_SUFFIXES = frozenset({
    "games", "studios", "studio", "interactive", "entertainment", "software",
    "labs", "inc", "inc.", "ltd", "ltd.", "corp", "corp.", "corporation",
    "publishing",
})


def _matches_organization(value: object, organizations: frozenset[str]) -> bool:
    """True when ``value`` names an organization in the allowlist (containment allowed)."""
    key = _origin_key(value)
    if not key:
        return False
    for org in organizations:
        if key == org or _contains_name(key, org) or _contains_name(org, key):
            return True
    return False


def _has_corporate_suffix(value: object) -> bool:
    """True when the last word of ``value`` is a corporate suffix ("... Studios")."""
    words = _origin_key(value).split()
    if not words:
        return False
    return words[-1] in _CORPORATE_SUFFIXES


def _is_organization(
    value: object,
    text: str = "",
    claim: dict | None = None,
    aliases: dict[str, str] | None = None,
    entities: list[str] | None = None,
    organizations: frozenset[str] | None = None,
) -> bool:
    """True when ``value`` names an organization, not a product or title.

    An organization is a name in the allowlist, a name with a corporate suffix,
    or the grammatical subject of an announcement verb. When ``entities`` are
    given, a name that is a canonical product/title entity is not an
    organization -- unless the allowlist or suffix marks it as one -- so a game
    title the extractor treats as a speaker is still rejected.
    """
    if not _origin_key(value):
        return False
    orgs = load_organizations() if organizations is None else organizations
    if _matches_organization(value, orgs) or _has_corporate_suffix(value):
        return True
    if entities is not None and _is_product_origin(value, entities):
        return False
    if text and claim is not None:
        subjects = _announcement_subjects(text, claim)
        if subjects and _token_matches_any(
            value, subjects, aliases if aliases is not None else load_origin_aliases()
        ):
            return True
    return False


def _is_first_party_origin(
    origin: object,
    text: str,
    claim: dict,
    aliases: dict[str, str],
    entities: list[str] | None,
    organizations: frozenset[str],
) -> bool:
    """True when the source attributes the claim to an organization as its owner.

    First party means the organization itself put the news out: the source names
    it as the subject of an announcement verb ("Epic Games announced ...") and
    attributes the claim to it ("according to Epic Games").
    """
    if not _is_organization(origin, text, claim, aliases, entities, organizations):
        return False
    return _origin_attributed(origin, text, claim, aliases)


def _min_first_party_sources() -> int:
    """How many independent sources the first-party rule requires (configurable)."""
    value = load_config().get("facts", {}).get("min_first_party_sources")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_MIN_FIRST_PARTY_SOURCES
    return max(1, int(value))


def _first_party_origins(
    claims: list[dict],
    by_url: dict[str, dict],
    aliases: dict[str, str],
    entities: list[str] | None,
) -> tuple[list[str], dict[str, str], set[str]]:
    """First-party organizations among ``claims``, with their source owners.

    Returns ``(organizations, source_org, owners)``: the canonical organization
    names seen in :data:`FIRST_PARTY_KINDS` claims, the organization each source
    attributed its claim to, and the set of distinct owners backing them. A
    source must have its own text carrying the attribution, and an outlet that
    is merely reporting about someone else never counts.
    """
    organizations = load_organizations()
    orgs_seen: list[str] = []
    source_org: dict[str, str] = {}
    owners: set[str] = set()
    for claim in claims:
        if claim.get("kind") not in FIRST_PARTY_KINDS:
            continue
        url_key = _source_url_key(claim.get("source_url"))
        source = by_url.get(url_key) or {}
        text = str(source.get("text") or "")
        if not text:
            continue
        for token in str(claim.get("origin") or "").split(";"):
            token = token.strip()
            if not token or _origin_key(token) == UNSTATED_ORIGIN:
                continue
            if _is_publication(token, source):
                continue
            if not _is_first_party_origin(token, text, claim, aliases, entities, organizations):
                continue
            canonical = canonicalize_origin(token, aliases)
            if canonical == UNSTATED_ORIGIN:
                continue
            if canonical not in orgs_seen:
                orgs_seen.append(canonical)
            source_org[url_key] = canonical
            owner = _origin_key(source.get("owner")) or url_key
            owners.add(owner)
    return sorted(orgs_seen), source_org, owners


def _source_url_key(url: object) -> str:
    """Match key for a source URL: scheme/host lowercased, ``www.`` and fragment
    dropped, trailing slash ignored.

    A claim's ``source_url`` must find the gathered source it was extracted from
    even when the two differ only in casing, a trailing slash or a fragment;
    otherwise the origin is verified against no text and wrongly downgraded.
    """
    return normalize_url(str(url or "")).rstrip("/")


def _sources_by_url(sources: list[dict]) -> dict[str, dict]:
    """Gathered sources keyed by :func:`_source_url_key` for claim lookup."""
    by_url: dict[str, dict] = {}
    for source in sources:
        key = _source_url_key(source.get("link"))
        if key:
            by_url[key] = source
    return by_url


def _select_sources(sources: list[dict], max_sources: int | None) -> list[dict]:
    """The sources to extract from, capped and reduced to distinct owners.

    A candidate with many sources is capped so one story cannot spend the whole
    model-call budget. Sources are ranked by tier (tier-1 first), a single best
    source per distinct owner is taken, and any remaining slots go to the
    longest texts. The chosen sources are returned in their original order, so
    extraction and every downstream positional assumption stay stable.
    """
    if max_sources is None or max_sources <= 0 or len(sources) <= max_sources:
        return list(sources)

    def _tier(source: dict) -> int:
        try:
            return int(source.get("tier") or 99)
        except (TypeError, ValueError):
            return 99

    def _rank(pair: tuple[int, dict]) -> tuple[int, int, int]:
        index, source = pair
        return (_tier(source), -len(str(source.get("text") or "")), index)

    ordered = sorted(enumerate(sources), key=_rank)
    chosen: list[int] = []
    seen_owners: set[str] = set()
    for index, source in ordered:
        owner = _origin_key(source.get("owner")) or _source_url_key(source.get("link"))
        if owner in seen_owners:
            continue
        seen_owners.add(owner)
        chosen.append(index)
        if len(chosen) >= max_sources:
            break
    if len(chosen) < max_sources:
        for index, _source in ordered:
            if index in chosen:
                continue
            chosen.append(index)
            if len(chosen) >= max_sources:
                break
    return [sources[index] for index in sorted(chosen)]


def resolve_claim_origins(
    claims: list[dict],
    sources: list[dict],
    *,
    aliases: dict[str, str] | None = None,
    story_id: str = "",
    entities: list[str] | None = None,
    downgrades: list[dict] | None = None,
) -> list[dict]:
    """Canonicalize each claim's origin and drop any its source never names.

    Each ``;``-separated origin token is canonicalized, then checked against the
    text of the claim's own source. A token the text does not mention is removed
    and the downgrade is logged; a token the text mentions but does not attribute
    to the claim (its sentence carries no "according to X" / "X said" link) is
    removed too, so a speaker named only elsewhere in a long article never
    vouches for this claim. When no token survives, the origin becomes
    :data:`UNSTATED_ORIGIN`. A token that is a canonical entity of the story
    (``entities``: a game, character or platform) is a subject, not a speaker,
    and is downgraded the same way -- unless the byline or the claim's
    affiliation names it, which marks it as the company that makes the product.
    The claim's ``affiliation`` (the organization a person speaks for) is
    canonicalized the same way and dropped when its own source text never names
    it. No other claim field is touched.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    by_url = _sources_by_url(sources)
    resolved: list[dict] = []
    # Per-source tallies of why origins were dropped, so the run logs one summary
    # line per source instead of one warning per token. The individual verdicts
    # are still collected (``downgrades``) for the sheet, so nothing is lost.
    per_source: dict[str, dict] = {}
    for claim in claims:
        source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
        text = str(source.get("text") or "")
        claim_url = claim.get("source_url")
        source_key = _source_url_key(claim_url)
        byline_name, byline_org = _extract_byline(text)
        byline_org_key = _origin_key(byline_org)
        speaker_org_keys = {
            key
            for key in (byline_org_key, _origin_key(claim.get("affiliation")))
            if key
        }
        speech_subjects = _speech_subjects(text, claim)
        kept: list[str] = []

        def _record(token: str, reason: str) -> None:
            tally = per_source.setdefault(source_key, {"url": claim_url, "reasons": Counter()})
            tally["reasons"][reason] += 1
            if downgrades is not None:
                downgrades.append({"source_url": claim_url, "origin": token, "reason": reason})

        for token in str(claim.get("origin") or "").split(";"):
            token = token.strip()
            token_key = _origin_key(token)
            if not token_key or token_key == UNSTATED_ORIGIN:
                continue
            # A person who is the subject of a speech verb ("Dan Houser says…")
            # stays a speaker even when their name is also a headline entity;
            # an announcement verb never rescues a game, character or platform.
            speech_person = _token_matches_any(token, speech_subjects, aliases)
            reason = ""
            if _is_anonymous_origin(token, aliases):
                reason = "anonymous source"
            elif (
                _is_product_origin(token, entities)
                and token_key not in speaker_org_keys
                and not speech_person
                and not _is_organization(token, text, claim, aliases, entities)
            ):
                reason = "product or title"
            elif not _origin_in_text(token, text, aliases):
                reason = "not named in the source text"
            elif not _origin_attributed(token, text, claim, aliases):
                # The name appears in the article but not as the producer of
                # this claim (e.g. it speaks about a different fact). Crediting
                # it would conflate two statements, so it is downgraded.
                reason = "not attributed"
            if reason:
                # An outlet naming itself (usually the extractor mistaking the
                # publisher for the speaker) is a self-reference, not a genuine
                # attribution gap, so it is dropped quietly.
                if _is_publication(token, source):
                    reason = "outlet self-reference"
                _record(token, reason)
                continue
            canonical = canonicalize_origin(token, aliases)
            if canonical not in kept:
                kept.append(canonical)
        origin = "; ".join(kept) if kept else UNSTATED_ORIGIN
        # A company's own bylined post speaks for itself: a tier-1 own-site post
        # with a byline self-attributes ("I'm <Name>, <role> at <Company>"), so
        # its unattributed claims are not left unstated. An outlet that merely
        # uses "we"/"I" never self-attributes through this path.
        if origin == UNSTATED_ORIGIN:
            self_origin = _self_attribution(text, source, byline_name, byline_org, aliases)
            if self_origin and self_origin != UNSTATED_ORIGIN:
                origin = self_origin
        affiliation = _normalize_affiliation(claim.get("affiliation"))
        if affiliation and _is_publication(affiliation, source):
            # The extractor named the publishing outlet, not the speaker's
            # organization; the byline's own organization is the real one.
            affiliation = byline_org
        if (
            affiliation
            and _is_product_origin(affiliation, entities)
            and _origin_key(affiliation) != byline_org_key
            and not _is_organization(affiliation, text, claim, aliases, entities)
        ):
            # A game title can never be the organization a person speaks for.
            affiliation = ""
        if affiliation and _origin_in_text(affiliation, text, aliases):
            canonical_affiliation = canonicalize_origin(affiliation, aliases)
            if canonical_affiliation == UNSTATED_ORIGIN:
                canonical_affiliation = ""
        else:
            canonical_affiliation = ""
        resolved.append(
            {
                **claim,
                "origin": origin,
                "affiliation": canonical_affiliation,
            }
        )
    # One summary line per source. A source whose only drops are its own
    # self-references is logged at INFO; a genuine attribution gap (unattributed
    # or unnamed) stays a WARNING so it is not missed.
    for tally in per_source.values():
        reasons = tally["reasons"]
        quiet = all(reason == "outlet self-reference" for reason in reasons)
        parts = ", ".join(
            f"{count} {reason}" for reason, count in sorted(reasons.items())
        )
        logger.log(
            logging.INFO if quiet else logging.WARNING,
            "story %s: %d origin(s) downgraded for %s (%s)",
            story_id or "?",
            sum(reasons.values()),
            tally["url"],
            parts,
        )
    return resolved


# --- merging duplicate claims across sources ---------------------------------


def _claim_key(claim: dict) -> str:
    """Normalized claim text: the key that identifies one fact across sources.

    Sources word the same fact differently, but the extractor is told to state
    the same claim the same way; two claims whose normalized text matches are
    the same fact and must be merged into one.
    """
    text = re.sub(r"[\W_]+", " ", str(claim.get("claim", "")).lower()).strip()
    return re.sub(r"\s+", " ", text)


_MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})

_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "does",
    "for", "from", "in", "into", "is", "it", "its", "not", "of", "on", "or",
    "says", "said", "say", "that", "the", "this", "to", "was", "were", "will",
    "with",
})


def _word_tokens(text: str) -> set[str]:
    """Lowercased alphanumeric word tokens of ``text``."""
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _stem(word: str) -> str:
    """Very light suffix stemmer: testing/test/modes/mode all count together."""
    for suffix, length in (("ing", 3), ("ed", 2), ("es", 2), ("s", 1)):
        if len(word) > length and word.endswith(suffix):
            return word[:-length] or word
    return word


def _topic_words(text: str) -> set[str]:
    """Stemmed content words of a claim, excluding stop words, months and numbers."""
    words: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if token in _STOP_WORDS or token in _MONTHS or token.isdigit():
            continue
        if re.match(r"^\d", token):
            continue
        words.add(_stem(token))
    return words


def _value_key_tokens(value: str) -> set[str]:
    """Key tokens in a value: month names and numbers, ordinals stripped.

    "13th to 20th October" yields {13, 20, october}; "October 13" yields
    {october, 13}, so the two values share a figure and can merge, while
    "October 12" shares only the month with "October 13" and stays separate.
    """
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
        if token in _MONTHS:
            tokens.add(token)
            continue
        digits = re.match(r"^\d+", token)
        if digits:
            tokens.add(digits.group(0))
    return tokens


def _same_value_fact(a: dict, b: dict) -> bool:
    """True when two claims share a concrete date or figure and topic words.

    The values must share at least one number, or two key tokens (so "October
    12" and "October 13" never merge on the month alone), and the claims must
    share topic words, so two unrelated claims quoting the same date stay apart.
    """
    a_keys = _value_key_tokens(str(a.get("value") or ""))
    b_keys = _value_key_tokens(str(b.get("value") or ""))
    shared = a_keys & b_keys
    if not shared:
        return False
    if sum(1 for token in shared if token.isdigit()) < 1 and len(shared) < 2:
        return False
    return bool(_topic_words(str(a.get("claim") or "")) & _topic_words(str(b.get("claim") or "")))


def _same_fact(a: dict, b: dict) -> bool:
    """True when two claims describe one fact: same claim text or shared value."""
    if _claim_key(a) == _claim_key(b):
        return True
    return _same_value_fact(a, b)


def _claim_groups(claims: list[dict]) -> list[list[dict]]:
    """Group claims that describe one fact.

    Claims group when their normalized claim text matches, or when their values
    share a live date or figure and their claims share topic words: "The test
    runs from 13th to 20th October" and "October 13" are the same fact, while a
    different date ("October 12") stays its own fact.
    """
    groups: list[list[dict]] = []
    for claim in claims or []:
        for group in groups:
            if any(_same_fact(claim, other) for other in group):
                group.append(claim)
                break
        else:
            groups.append([claim])
    return groups


def _hedge_count(value: str) -> int:
    text = str(value or "").lower()
    return sum(1 for word in HEDGE_WORDS if word in text)


def _merge_score(claim: dict) -> tuple[int, int, int]:
    """Pick the value that keeps the story honest: most hedged, then weakest kind.

    A fact whose sources disagree (one wrote "estimated", another wrote a bare
    figure) must keep the hedged value, and the weakest kind, so an estimate is
    never flattened into a confirmation and a plan stated as a possibility is
    never upgraded to a fact.
    """
    return (-_hedge_count(str(claim.get("value", ""))), KIND_STRENGTH.get(claim["kind"], 0), -len(str(claim.get("value", ""))))


def _merge_origins(group: list[dict]) -> str:
    """One or more named origins, or 'unstated' when none is named."""
    named = sorted({claim["origin"] for claim in group if claim["origin"] != UNSTATED_ORIGIN})
    return "; ".join(named) if named else UNSTATED_ORIGIN


def _merge_affiliations(group: list[dict]) -> str:
    """One or more named affiliations, or '' when none is named."""
    named = sorted({
        str(claim.get("affiliation") or "").strip()
        for claim in group
        if str(claim.get("affiliation") or "").strip()
    })
    return "; ".join(named)


def _merge_group(group: list[dict], *, prefer_url: str | None = None) -> dict:
    """Collapse the claims of one fact (same fact) into one.

    ``prefer_url`` names the source whose wording should represent the merged
    fact: the central claim takes the tier-1 source's own value (so the source
    that confirms it is the source that wrote it), while the other sources'
    versions are kept as corroboration.
    """
    candidates = group
    if prefer_url is not None:
        preferred = [
            claim for claim in group
            if _source_url_key(claim.get("source_url")) == prefer_url
        ]
        if preferred:
            candidates = preferred
    best = min(candidates, key=_merge_score)
    return {
        "claim": str(best["claim"]),
        "value": str(best["value"]),
        "source_urls": sorted({claim["source_url"] for claim in group}),
        "origin": _merge_origins(group),
        "affiliation": _merge_affiliations(group),
        "confidence": min(float(claim["confidence"]) for claim in group),
        "kind": min((claim["kind"] for claim in group), key=KIND_STRENGTH.get),
        # Each source's own wording, so confirmation can require that a
        # confirming source actually states the merged value's specifics and a
        # tier-1 source never backs words only another source used.
        "versions": [
            {"source_url": claim["source_url"], "value": str(claim["value"])}
            for claim in group
        ],
    }


def _value_specifics(value: object) -> set[str]:
    """The concrete bits of a value: its figures and absolute qualifiers."""
    specifics = _numbers_in(value)
    for qualifier in _ABSOLUTE_QUALIFIERS:
        if _word_in(value, qualifier):
            specifics.add(qualifier)
    return specifics


def _version_supports(merged: dict, version_value: object) -> bool:
    """True when a source's own value carries every specific the merged value has.

    A merged fact (for example "full Year 1 DLC roster") must not be confirmed by
    a source whose own value omits a specific word ("Year 1 DLC roster"): the
    tier-1 source would be backing words it never wrote.
    """
    specifics = _value_specifics(merged.get("value"))
    if not specifics:
        return True
    return specifics <= _value_specifics(version_value)


def _source_supports(merged: dict, url_key: str) -> bool:
    """True when one source's own value backs the merged fact.

    A tier-1 source confirms only as long as one of its claims carries every
    specific of the merged value: it never confirms wording that only another
    source stated. When the source stated the fact more than once (for example
    splitting a date into its own claim), any of its versions may support it.
    """
    versions = [
        version for version in merged.get("versions") or []
        if _source_url_key(version.get("source_url")) == url_key
    ]
    return bool(versions) and any(
        _version_supports(merged, version.get("value")) for version in versions
    )


def merge_claims(claims: list[dict]) -> list[dict]:
    """Merge duplicate claims across sources: one claim per fact.

    A fact groups every claim that describes it (:func:`_claim_groups`) -- by
    shared claim text, or by a shared date/figure plus topic words -- and is
    stored once, listing every source URL that states it (``source_urls``),
    taking the weakest ``kind`` when sources disagree, keeping the hedge words
    in the value, joining the named origins, and keeping the lowest confidence.
    """
    return [_merge_group(group) for group in _claim_groups(claims)]


def _collapse_contained_origins(names: set[str]) -> list[str]:
    """Drop any origin whose match key contains another origin's from the set.

    "alinea analytics ltd" contains "alinea analytics", so only the shorter,
    more general name is kept; two spellings of one origin thus count once.
    """
    ordered = sorted(names, key=lambda name: (len(name.split()), len(name), name))
    kept: list[str] = []
    for name in ordered:
        key = _origin_key(name)
        if any(_contains_name(key, _origin_key(other)) for other in kept):
            continue
        kept.append(name)
    return sorted(kept)


def _claim_source_url_key(claim: dict) -> str:
    """Match key for the source that states a claim.

    Kept claims carry a single ``source_url``; merged claims carry
    ``source_urls``. Either form must resolve to the same gathered source.
    """
    urls = claim.get("source_urls")
    if isinstance(urls, list) and urls:
        return _source_url_key(next((url for url in urls if url), None))
    return _source_url_key(claim.get("source_url"))


def _confirmation_origins(
    claims: list[dict],
    by_url: dict[str, dict],
    *,
    aliases: dict[str, str] | None = None,
) -> list[str]:
    """Distinct attributed origins among the claims, tier- and kind-filtered.

    An origin counts only when its claim is of a confirming kind
    (:data:`CONFIRMING_KINDS`) and its source is not tier-3; a rumor, an opinion
    or a statement never counts toward confirmation. Each origin is canonicalized
    (case, possessives, titles, aliases). A claim whose ``affiliation`` names the
    organization a person speaks for counts as that organization, so an executive
    producer and the studio they work for are one origin. One article never
    counts as two independent origins: whatever canonicals a single source
    states, it contributes a single name (the longest of them), and when one
    canonical origin is contained in another they are treated as one, keeping the
    shorter name.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    per_source: dict[str, set[str]] = {}
    for claim in claims or []:
        if claim.get("kind") not in CONFIRMING_KINDS:
            continue
        source = by_url.get(_claim_source_url_key(claim))
        if source is None or source.get("tier") == 3:
            continue
        bucket = per_source.setdefault(_source_url_key(source.get("link")), set())
        affiliation = str(claim.get("affiliation") or "").strip().lower()
        if affiliation and affiliation != UNSTATED_ORIGIN:
            canonical = canonicalize_origin(affiliation, aliases)
            if canonical and canonical != UNSTATED_ORIGIN and not _is_anonymous_origin(canonical, aliases):
                bucket.add(canonical)
                continue
        for token in str(claim.get("origin") or "").split(";"):
            canonical = canonicalize_origin(token, aliases)
            if canonical and canonical != UNSTATED_ORIGIN and not _is_anonymous_origin(canonical, aliases):
                bucket.add(canonical)
    names = {max(bucket, key=len) for bucket in per_source.values() if bucket}
    return _collapse_contained_origins(names)


def confirmation_from_sources(
    sources: list[dict],
    *,
    claims: list[dict] | None = None,
    merged: dict | None = None,
    allow_single_origin_attributed: bool = False,
    aliases: dict[str, str] | None = None,
    entities: list[str] | None = None,
) -> dict:
    """Assess the confirmation rule against the cluster's sources and claims.

    Confirmed when at least one tier-1 source states a passed claim, OR the
    passed claims carry at least two independent attributed origins (outlets
    repeating the same origin count as one, and two origins from the same single
    article never count as two), OR the headline claim is an organization's own
    announcement independently attributed by enough sources (the "first-party"
    rule; see :func:`_first_party_origins`). Origins are canonicalized through
    ``aliases`` (:func:`load_origin_aliases`), so different spellings of one name
    collapse, and only claims of kind confirmed/reported/estimate count: a rumor,
    an opinion or a statement never confirms. When ``allow_single_origin_attributed``
    is set, a single attributed (named, non-"unstated") origin also confirms.
    Tier 3 never counts. ``claims`` are the pre-merge claims of the story's
    central fact (``source_url`` singular, or ``source_urls`` on merged claims);
    confirmation is about who produced that claim, so the origins of any other
    claim never count.

    ``merged`` is the merged central claim: when given, a tier-1 source counts
    only if its own value carries every specific of the merged value, so a tier-1
    source never confirms wording that only another source stated.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    by_url = _sources_by_url(sources)
    origins = _confirmation_origins(claims, by_url, aliases=aliases)
    if claims is not None:
        claim_urls = {_claim_source_url_key(claim) for claim in claims}
        tier1 = 0
        for source in sources:
            if source["tier"] != 1 or _source_url_key(source.get("link")) not in claim_urls:
                continue
            if merged is not None and not _source_supports(merged, _source_url_key(source.get("link"))):
                continue
            tier1 += 1
        tier3 = sum(
            1 for source in sources
            if source["tier"] == 3 and _source_url_key(source.get("link")) in claim_urls
        )
        first_party_orgs, _source_org, first_party_owners = _first_party_origins(
            claims, by_url, aliases, entities
        )
    else:
        tier1 = sum(1 for source in sources if source["tier"] == 1)
        tier3 = sum(1 for source in sources if source["tier"] == 3)
        first_party_orgs, first_party_owners = [], set()
    single_origin_attributed = len(origins) == 1
    first_party = (
        claims is not None and len(first_party_owners) >= _min_first_party_sources()
    )
    if merged is not None and str(merged.get("kind") or "") == "opinion":
        # A candidate whose central fact is a judgement is never confirmable:
        # an opinion can be repeated by every outlet and still be no fact.
        confirmed, rule, reason = False, "unconfirmed", "the central claim is an opinion"
    elif tier1 >= 1:
        confirmed, rule = True, "tier1"
        reason = "a tier-1 source states the headline claim"
    elif len(origins) >= 2:
        confirmed, rule = True, "two-independent-origins"
        reason = "two independent named origins support the headline claim"
    elif first_party:
        confirmed, rule = True, "first-party"
        organization = ", ".join(first_party_orgs) or "an organization"
        reason = (
            f"{len(first_party_owners)} independent sources attribute the "
            f"headline claim to {organization}"
        )
    elif allow_single_origin_attributed and single_origin_attributed:
        confirmed, rule = True, "single-origin-attributed"
        reason = (
            "a single attributed origin supports the headline claim and "
            "single-origin confirmation is enabled"
        )
    else:
        confirmed, rule = False, "unconfirmed"
        reason = "no tier-1 source and fewer than two independent named origins"
    return {
        "tier1_sources": tier1,
        "tier3_sources": tier3,
        "origins": origins,
        "first_party_organizations": first_party_orgs,
        "first_party_sources": len(first_party_owners),
        "single_origin_attributed": single_origin_attributed,
        "rule": rule,
        "confirmed": confirmed,
        "reason": reason,
    }


# --- the central claim -------------------------------------------------------


def _title_overlap_score(claim: dict, title_tokens: set[str]) -> int:
    """How many headline words the claim's text and value restate."""
    text = f"{str(claim.get('claim', ''))} {str(claim.get('value', ''))}"
    return len(title_tokens & _word_tokens(text))


#: A tier-1 source only sets the central claim when its own claim restates at
#: least this fraction of the seed headline's significant tokens. Overridable
#: through ``facts.min_central_overlap`` in config.yaml.
DEFAULT_MIN_CENTRAL_OVERLAP = 0.5


def _min_central_overlap() -> float:
    """The configured headline-overlap threshold for a tier-1 central claim."""
    config = load_config().get("facts") or {}
    try:
        value = float(config.get("min_central_overlap", DEFAULT_MIN_CENTRAL_OVERLAP))
    except (TypeError, ValueError):
        value = DEFAULT_MIN_CENTRAL_OVERLAP
    return min(max(value, 0.0), 1.0)


def _headline_significant_units(title: str, canonical: list[str]) -> list[tuple[str, str]]:
    """The headline's significant tokens: each entity, month and number once.

    "Significant token" means an entity, a date or a number, not each word of a
    long headline: a compound headline ("... but Xbox Game Pass ...") is not
    failed just because a claim about its main fact omits the other clause. A
    stop entity such as "Year 1 DLC" is no entity, but its bare number still
    counts here.
    """
    units: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str) -> None:
        key = (kind, value)
        if value and key not in seen:
            seen.add(key)
            units.append(key)

    for entity in canonical or []:
        add("entity", entity)
    for month in re.findall(r"[a-z]+", cluster.strip_diacritics(title).lower()):
        if month in _MONTHS:
            add("token", month)
    for token in _value_key_tokens(title):
        if token not in _MONTHS:
            add("token", token)
    return units


def _claim_names_entity(text: str, entity: str) -> bool:
    """True when the claim's text names the entity or any of its aliases."""
    if cluster.contains_entity(text, entity):
        return True
    return any(cluster.contains_entity(text, alias) for alias in entity_aliases(entity))


def _claim_overlap_fraction(claim: dict, units: list[tuple[str, str]]) -> float:
    """The share of the headline's significant units the claim restates."""
    if not units:
        return 1.0
    text = f"{str(claim.get('claim', ''))} {str(claim.get('value', ''))}"
    tokens = _word_tokens(cluster.strip_diacritics(text))
    shared = 0
    for kind, value in units:
        if kind == "token":
            shared += value in tokens
        else:
            shared += _claim_names_entity(text, value)
    return shared / len(units)


def central_claim(claims: list[dict], *, title: str = "") -> Optional[dict]:
    """Pick the claim that restates the cluster's seed headline.

    The cluster seed is its newest source; the seed headline becomes the cluster
    title, passed to ``facts()`` as ``title``. The central claim is the one
    whose text and value share the most words with that headline -- the claim
    the headline states is the claim this story must confirm. Returns None when
    no headline is given or no extracted claim overlaps it; ties break to the
    longest text.
    """
    title = str(title or "").strip()
    if not title:
        return None
    title_tokens = _word_tokens(title)
    best: Optional[dict] = None
    best_key = (0, 0, "")
    for claim in claims or []:
        score = _title_overlap_score(claim, title_tokens)
        key = (
            score,
            len(str(claim.get("claim", ""))) + len(str(claim.get("value", ""))),
            str(claim.get("claim", "")),
        )
        if score > 0 and key > best_key:
            best, best_key = claim, key
    return best


def _preferred_central_index(
    groups: list[list[dict]],
    sources: list[dict],
    title: str,
    *,
    canonical: list[str] | None = None,
    min_overlap: float | None = None,
) -> Optional[tuple[int, str]]:
    """The group to center on, preferring a tier-1 source's own claims.

    A cluster that has a tier-1 source (a company's or platform's own post)
    takes its central claim from that source's claims: the headline-overlap
    pick is restricted to claims the tier-1 source itself wrote, so the fact the
    headline states is the fact the tier-1 source restates, and the other
    outlets' versions corroborate it rather than driving the central wording. A
    tier-1 claim must also restate at least ``min_overlap`` (default
    ``facts.min_central_overlap``) of the seed headline's significant tokens: a
    passing mention is not the headline's fact. Returns ``(group_index,
    url_key)``, or None when no headline is given, no tier-1 claim reaches the
    threshold, or no tier-1 source exists (the plain headline overlap rule
    applies to all claims then).
    """
    title = str(title or "").strip()
    if not title:
        return None
    if canonical is None:
        canonical = canonical_entities(title=title)
    threshold = _min_central_overlap() if min_overlap is None else min_overlap
    units = _headline_significant_units(title, canonical)
    title_tokens = _word_tokens(title)
    tier1_urls = {
        _source_url_key(source.get("link"))
        for source in sources
        if source.get("tier") == 1
    }
    best: Optional[tuple[int, str]] = None
    best_key = (0, 0, "")
    if tier1_urls:
        for index, group in enumerate(groups):
            for claim in group:
                if _source_url_key(claim.get("source_url")) not in tier1_urls:
                    continue
                if _claim_overlap_fraction(claim, units) < threshold:
                    continue
                score = _title_overlap_score(claim, title_tokens)
                key = (
                    score,
                    len(str(claim.get("claim", ""))) + len(str(claim.get("value", ""))),
                    str(claim.get("claim", "")),
                )
                if score > 0 and key > best_key:
                    best, best_key = (
                        (index, _source_url_key(claim.get("source_url"))),
                        key,
                    )
    return best


def _fold_entity(entity: object) -> str:
    """Case- and diacritics-fold an entity so spelling variants count together."""
    return cluster.strip_diacritics(str(entity or "")).lower()


def _accented_char_count(text: str) -> int:
    """Count base characters that carry diacritics ("ō" in "Tōkon")."""
    return sum(
        1
        for char in text
        if unicodedata.combining(char) == 0 and len(unicodedata.normalize("NFKD", char)) > 1
    )


def _best_display_entity(variants: list[str], exact_counts: dict[str, int]) -> str:
    """The most reliably spelled display form of one folded entity.

    Prefer proper casing and diacritics over ALL-CAPS ASCII forms, then the
    most frequent spelling, then the longest explicit form.
    """
    def quality(name: str):
        words = [word for word in name.split() if word]
        title_case = bool(words) and all(
            word[0].isupper() and (len(word) == 1 or word[1:].islower())
            for word in words
        )
        return (
            _accented_char_count(name),
            title_case,
            exact_counts.get(name, 0),
            len(name),
        )

    return max(variants, key=quality)


def _chosen_display_entities(
    title: str | None,
    source_titles: list[str] | None,
) -> list[str]:
    """One best-display phrase per folded entity the headlines support.

    Counts each full name phrase (:func:`cluster.story_phrases`) across the
    cluster title and the source headlines, folding case and diacritics so
    spelling variants of one name ("Marvel Tōkon", "MARVEL Tōkon") merge into a
    single count. Keeps every entity whose folded count is at least half of the
    highest count and that more than one headline carries, plus every entity the
    seed headline itself names (so an entity the headline mentions only once is
    still kept); the best display form (preferring proper casing and diacritics)
    survives. The result is one phrase per folded entity, in folded-name order.
    """
    folded: dict[str, int] = {}
    exact_counts: dict[str, int] = {}
    displays: dict[str, list[str]] = {}
    for candidate in [title, *(source_titles or [])]:
        if not candidate:
            continue
        for entity in cluster.story_phrases(candidate):
            exact_counts[entity] = exact_counts.get(entity, 0) + 1
            key = _fold_entity(entity)
            folded[key] = folded.get(key, 0) + 1
            displays.setdefault(key, [])
            if entity not in displays[key]:
                displays[key].append(entity)
    if not folded:
        return []
    top = max(folded.values())
    floor_kept = {
        key
        for key, count in folded.items()
        if count >= 2 and count * 2 >= top
    }
    headline_kept = (
        {_fold_entity(entity) for entity in cluster.story_phrases(title) or []}
        if title else set()
    )
    kept = floor_kept | headline_kept
    return [_best_display_entity(displays[key], exact_counts) for key in sorted(kept)]


def primary_entities(
    *,
    title: str | None = None,
    source_titles: list[str] | None = None,
    entities: list[str] | None = None,
) -> list[str]:
    """The entities that define the story's subject, with their sub-entities.

    The supported, best-display name phrases (:func:`_chosen_display_entities`,
    never a stop word such as a month, a weekday or "Year 1 DLC") are each
    expanded into themselves plus the sub-entities their connectors split them
    into (:func:`cluster.sub_entities`), so a claim that names only part of a
    long phrase ("Call of Duty" inside "Call of Duty: Modern Warfare 4") is still
    recognized as on-topic. Generic entities ("PC", "AI", "IPs") are dropped: a
    category word never makes a claim on-topic on its own. Falls back to the
    cluster's reported entities when no headline parses.
    """
    chosen = _chosen_display_entities(title, source_titles)
    if not chosen:
        chosen = [str(entity).strip() for entity in (entities or []) if str(entity).strip()]
    stop = cluster.entity_stop_list()
    chosen = [entity for entity in chosen if not cluster.is_stop_entity(entity, stop=stop)]
    return [
        entity for entity in _expand_entities(chosen)
        if not cluster.is_generic_entity(entity)
    ]


def canonical_entities(
    *,
    title: str | None = None,
    source_titles: list[str] | None = None,
    entities: list[str] | None = None,
) -> list[str]:
    """The story's canonical entities, one per concept, for SEO and hubs.

    The same supported, best-display phrases as :func:`primary_entities` but
    without the sub-entity expansion: a colon/subtitle phrase stays one canonical
    entity ("Marvel Tōkon: Fighting Souls") and its parts are aliases used only
    for matching (:func:`entity_aliases`), never listed here. Stop words are
    dropped and folded duplicates collapse, so the SEO entities list can never
    contain an alias-only entry.
    """
    chosen = _chosen_display_entities(title, source_titles)
    if not chosen:
        chosen = [str(entity).strip() for entity in (entities or []) if str(entity).strip()]
    stop = cluster.entity_stop_list()
    seen: set[str] = set()
    canonical: list[str] = []
    for entity in chosen:
        if cluster.is_stop_entity(entity, stop=stop):
            continue
        key = _fold_entity(entity)
        if key in seen:
            continue
        seen.add(key)
        canonical.append(entity)
    return sorted(canonical)


def entity_aliases(canonical: str) -> list[str]:
    """The matching aliases of one canonical entity: its subtitle/connector parts.

    "Marvel Tōkon: Fighting Souls" yields "marvel tōkon" and "fighting souls";
    "Call of Duty: Modern Warfare 4" yields "call of duty" and "modern warfare
    4". Aliases are lowercased but keep their diacritics, are never listed as
    entities themselves, and stop words or a part equal to the whole are dropped.
    """
    whole = _fold_entity(canonical)
    stop = cluster.entity_stop_list()
    aliases: list[str] = []
    for segment in re.split(r"[:]", str(canonical or "")):
        part = re.sub(r"\s+", " ", segment).strip().lower()
        if not part:
            continue
        key = _fold_entity(part)
        if key == whole or key in {_fold_entity(alias) for alias in aliases}:
            continue
        if cluster.is_stop_entity(part, stop=stop):
            continue
        aliases.append(part)
    return aliases


def _expand_entities(entities: list[str]) -> list[str]:
    """Each entity followed by the sub-entities its connectors split it into."""
    expanded: list[str] = []
    for entity in entities:
        for sub in cluster.sub_entities(entity):
            if sub and sub not in expanded:
                expanded.append(sub)
    return sorted(expanded)


def _claim_on_topic(claim: dict, source: dict, entities: list[str]) -> bool:
    """True when a claim or its source headline names any entity or sub-entity.

    Either the claim's own text/value or the source article's title is enough;
    matching is case-insensitive and on whole words or phrases.
    """
    text = f"{claim.get('claim', '')} {claim.get('value', '')}"
    if any(cluster.contains_entity(text, entity) for entity in entities):
        return True
    source_title = str(source.get("title") or "")
    if source_title:
        return any(cluster.contains_entity(source_title, entity) for entity in entities)
    return False


def coherence_check(
    claims: list[dict],
    sources: list[dict],
    *,
    title: str | None = None,
    entities: list[str] | None = None,
    story_id: str = "",
) -> tuple[list[dict], list[dict], list[str]]:
    """Drop claims whose claim text and source both miss the story's entities.

    Returns ``(kept, dropped, primary)``. A claim is kept when its claim text or
    value names any primary entity or sub-entity, or when the title of its source
    article does. When no primary entity can be determined the check is skipped
    and every claim is kept. When more than half of a single source's claims are
    dropped, a warning naming that source and the entities is logged.
    """
    primary = primary_entities(
        title=title,
        source_titles=[str(source.get("title") or "") for source in sources],
        entities=entities,
    )
    if not primary:
        logger.warning("story %s: no primary entity found; skipping coherence check", story_id or "?")
        return list(claims), [], []
    by_url = _sources_by_url(sources)
    kept: list[dict] = []
    dropped: list[dict] = []
    totals: Counter[str] = Counter()
    dropped_by_source: Counter[str] = Counter()
    for claim in claims:
        key = _source_url_key(claim.get("source_url"))
        source = by_url.get(key) or {}
        totals[key] += 1
        if _claim_on_topic(claim, source, primary):
            kept.append(claim)
        else:
            dropped.append(claim)
            dropped_by_source[key] += 1
            logger.warning(
                "story %s: dropping off-topic claim from %s (mentions none of %s): %r",
                story_id or "?",
                claim.get("source_url"),
                primary,
                claim.get("claim"),
            )
    for key, dropped_count in dropped_by_source.items():
        total = totals.get(key, 0)
        if total and dropped_count * 2 > total:
            source = by_url.get(key) or {}
            label = str(
                source.get("source_name") or source.get("title") or key or "unknown source"
            )
            logger.warning(
                "story %s: dropped %d of %d claims from %s (%s) as off-topic; "
                "entities checked: %s",
                story_id or "?",
                dropped_count,
                total,
                label,
                key or "unknown url",
                primary,
            )
    return kept, dropped, primary


def _coherence_dropped_details(dropped: list[dict], primary: list[str]) -> list[dict]:
    """The coherence-dropped claims, each with the reason it was dropped.

    A claim is dropped off-topic when neither its own text/value nor its source
    article's title names any primary entity; the reason records the entities
    that were checked, so a saved sheet shows exactly why the claim was cut.
    """
    entities = ", ".join(primary) if primary else "the story's primary entities"
    reason = f"claim and its source headline name none of: {entities}"
    return [
        {
            "claim": claim.get("claim"),
            "source_url": claim.get("source_url"),
            "reason": reason,
        }
        for claim in dropped
    ]


def _confirmation_reason(confirmation: dict) -> str:
    """Plain-text reason for a confirmation verdict, for the saved sheet."""
    reason = str(confirmation.get("reason") or "").strip()
    if reason:
        return reason
    if confirmation.get("confirmed"):
        return "confirmed"
    return "no tier-1 source and fewer than two independent named origins"


def _write_unconfirmed_sheet(
    output_dir: Path,
    name: str,
    now: dt.datetime,
    confirmation: dict,
    central: Optional[dict],
    kept: list[dict],
    claims: list[dict],
    sheet_sources: list[dict],
    dropped: list[dict],
    *,
    primary: list[str] | None = None,
    canonical: list[str] | None = None,
    grounding_details: list[dict] | None = None,
    origin_downgrades: list[dict] | None = None,
) -> Optional[Path]:
    """Persist a rejected story's sheet under ``<output_dir>/unconfirmed/``.

    Unconfirmed stories are dropped from the run, but saving the sheet lets the
    owner inspect exactly what the extractor saw and why it was not confirmed.
    The sheet carries the same diagnostics as a confirmed one -- the full
    coherence block (checked, primary/canonical entities and the reason each
    dropped claim was cut), the grounding block (each ungrounded claim and its
    reason), and a plain-text ``confirmation_reason`` -- so a rejection can be
    diagnosed without rerunning the pipeline. Failures here are logged, never
    raised -- the story still raises :class:`UnconfirmedStory`.
    """
    primary = list(primary or [])
    canonical = list(canonical or [])
    payload = {
        "id": name,
        "generated_at": now.isoformat(),
        "confirmation": confirmation,
        "confirmation_reason": _confirmation_reason(confirmation),
        "central_claim": central,
        "kinds": sorted({str(claim.get("kind")) for claim in claims if claim.get("kind")}),
        "origins": list(confirmation.get("origins") or []),
        "sources": sheet_sources,
        "claims": claims,
        "grounding": [
            {"claim": entry["claim"].get("claim"), "reason": entry["reason"]}
            for entry in (grounding_details or [])
        ],
        "origins_downgraded": list(origin_downgrades or []),
        "coherence": {
            "checked": bool(primary),
            "primary_entities": primary,
            "canonical_entities": canonical,
            "dropped_claims": _coherence_dropped_details(dropped, primary),
        },
    }
    directory = Path(output_dir) / "unconfirmed"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        logger.info("story %s: wrote unconfirmed sheet -> %s", name, path)
        return path
    except OSError as exc:  # noqa: BLE001 - never block the raise on a write failure
        logger.warning("story %s: could not write unconfirmed sheet (%s)", name, exc)
        return None


def facts(
    gathered: dict,
    *,
    id: str | None = None,
    title: str | None = None,
    entities: list[str] | None = None,
    output_dir: str | Path | None = None,
    now: dt.datetime | None = None,
    generate: Callable[..., Generation] = default_generate,
    run_state: RunState | None = None,
    max_claims_per_source: int | None = None,
    allow_single_origin_attributed: bool | None = None,
    max_sources: int | None = None,
    model_call_budget: int | None = None,
    **kwargs: Any,
) -> dict:
    """Extract claims from gathered text, enforce confirmation, save the sheet.

    Confirmation is assessed after extraction because it now depends on the
    claims' origins (who produced each claim), not just on the outlets. A story
    with no tier-1 source is not rejected before the model runs: a single source
    can still carry two independent origins. Only the claim that restates the
    seed headline (``title``) is confirmed; other claims' origins never count.

    Extraction runs once per source, so ``max_sources`` caps how many sources a
    single candidate may use (chosen by tier, then one per distinct owner, then
    by text length); ``model_call_budget`` stops extraction before it would
    exceed the run's model-call budget. Both default to ``score`` settings in
    config.yaml.
    """
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    name = re.sub(r"[^0-9A-Za-z._-]", "-", str(id or gathered.get("id") or "unnamed"))
    now = now or dt.datetime.now(dt.timezone.utc)
    sources = gathered.get("sources") or []

    config = load_config()
    config_facts = config.get("facts") or {}
    config_max = int(config_facts.get("max_claims_per_source", MAX_CLAIMS_PER_SOURCE))
    max_claims = max_claims_per_source if max_claims_per_source is not None else config_max
    if allow_single_origin_attributed is None:
        allow_single = bool(
            config_facts.get(
                "allow_single_origin_attributed", DEFAULT_ALLOW_SINGLE_ORIGIN_ATTRIBUTED
            )
        )
    else:
        allow_single = bool(allow_single_origin_attributed)
    if max_sources is None:
        config_score = config.get("score") or {}
        try:
            max_sources = int(
                config_score.get("max_sources_per_candidate", DEFAULT_MAX_SOURCES_PER_CANDIDATE)
            )
        except (TypeError, ValueError):
            max_sources = DEFAULT_MAX_SOURCES_PER_CANDIDATE

    aliases = load_origin_aliases()
    fingerprint = cluster.stage_fingerprint(
        name, "facts", PROMPT_VERSION, SYSTEM_INSTRUCTION, CLAIM_SCHEMA,
        _extraction_chain(), MAX_SOURCE_CHARS, MAX_FIELD_CHARS, max_claims,
        allow_single, HEDGE_WORDS, sorted(aliases.items()), _relative_time_words(),
        max_sources,
    )
    path = output_dir / f"{name}.json"
    if cluster.stored_fingerprint(path) == fingerprint:
        payload = json.loads(path.read_text(encoding="utf-8"))
        logger.info("facts reused cached sheet %s (fingerprint match)", path)
        return payload

    extracted: list[dict] = []
    generation = None
    for source in _select_sources(sources, max_sources):
        text = str(source.get("text") or "")
        if not text.strip():
            logger.info("story %s: source %s has no text; nothing to extract", name, source.get("link"))
            continue
        if (
            run_state is not None
            and model_call_budget is not None
            and run_state.calls >= model_call_budget
        ):
            logger.warning(
                "story %s: model-call budget of %d reached; skipping remaining sources",
                name, model_call_budget,
            )
            break
        generation = extract_claims(
            name, source, max_claims=max_claims, generate=generate, run_state=run_state, **kwargs
        )
        part = validate_claims(generation.value)
        if len(part) > max_claims:
            logger.warning(
                "story %s: source %s returned %d claim(s), keeping the first %d",
                name, source.get("link"), len(part), max_claims,
            )
            part = part[:max_claims]
        logger.info("story %s: source %s extracted %d claim(s)", name, source.get("link"), len(part))
        if len(text) > WARN_LOW_SOURCE_CHARS and len(part) < WARN_MIN_CLAIMS:
            logger.warning(
                "story %s: source %s has %d chars but yielded only %d claim(s); "
                "most of what it says may be missing",
                name, source.get("link"), len(text), len(part),
            )
        extracted.extend(part)
    if generation is None:
        raise FactsError(f"story {name}: none of the sources had extractable text")

    kept, dropped, primary = coherence_check(
        extracted,
        sources,
        title=title,
        entities=entities,
        story_id=name,
    )
    if dropped:
        logger.warning("story %s: dropped %d off-topic claim(s) from off-topic sources", name, len(dropped))

    # A claim's value must stay inside its own source text: figures, names and
    # absolute qualifiers the source never states are dropped before merging.
    kept, ungrounded_details = ground_claims_details(kept, sources, story_id=name)
    ungrounded = [entry["claim"] for entry in ungrounded_details]
    if ungrounded:
        logger.warning("story %s: dropped %d ungrounded claim(s)", name, len(ungrounded))

    # Relative-time values are converted to absolute dates using each source's
    # publication date; a relative claim with no date to convert from is dropped.
    kept = _absolutize_claims(kept, sources, story_id=name)[0]

    # One canonical entity per concept, for SEO and hub pages; the flat
    # primary-entity set is the matching surface used by the coherence check.
    # Computed before origins so a game or character named as an origin is
    # recognized as the story's subject, not a speaker.
    canonical = canonical_entities(
        title=title,
        source_titles=[str(source.get("title") or "") for source in sources],
        entities=entities,
    )

    # Origins are canonicalized and verified against each claim's own source
    # text before anything is counted or stored; a product/title is not an origin.
    origin_downgrades: list[dict] = []
    kept = resolve_claim_origins(
        kept,
        sources,
        aliases=aliases,
        story_id=name,
        entities=canonical,
        downgrades=origin_downgrades,
    )

    tier1_present = any(int(source.get("tier") or 0) == 1 for source in sources)

    # Same facts merge (by shared claim text, or by a shared date/figure plus
    # topic words) before the central claim is picked. The merged central
    # claim of a cluster with a tier-1 source takes that source's own wording.
    groups = _claim_groups(kept)
    preferred_central = _preferred_central_index(
        groups, sources, title, canonical=canonical
    )
    if preferred_central is not None:
        prefer_index, prefer_url = preferred_central
        claims = [
            _merge_group(group, prefer_url=prefer_url)
            if index == prefer_index
            else _merge_group(group)
            for index, group in enumerate(groups)
        ]
        central = claims[prefer_index]
    else:
        claims = [_merge_group(group) for group in groups]
        central = central_claim(claims, title=title)

    # The cluster seed headline (the cluster title) is restated by one claim;
    # mark that claim central and confirm only it, so origins supporting any
    # other claim never confirm the story.
    for claim in claims:
        claim["central"] = claim is central
    central_group = groups[claims.index(central)] if central is not None else None
    if title and central is None:
        logger.warning(
            "story %s: no extracted claim overlaps the seed headline %r; "
            "confirmation falls back to all claims",
            name, title,
        )

    # Confirmation uses the pre-merge claims of the central fact only: it is
    # about who produced that claim, so three outlets repeating one origin still
    # collapse to one origin, two origins from the same article never count as
    # two, and other claims' origins never count.
    confirmation = confirmation_from_sources(
        sources,
        claims=central_group if central_group is not None else kept,
        merged=central,
        allow_single_origin_attributed=allow_single,
        aliases=aliases,
        entities=canonical,
    )
    # A tier-1 source that only mentions the topic in passing does not confirm
    # the story: it is the headline's fact the story must establish, not any
    # tier-1 sentence. Two outlets' agreement never stands in for it.
    if tier1_present and title and preferred_central is None:
        confirmation = dict(confirmation)
        confirmation.update(
            confirmed=False,
            rule="unconfirmed",
            reason="tier-1 source does not state the headline claim",
        )
    # Only sources that contributed at least one claim are listed in the sheet;
    # a source that yielded no claim is never cited by the article.
    claimed_urls = {_source_url_key(url) for claim in claims for url in claim["source_urls"]}
    sheet_sources = [
        {
            key: source.get(key)
            for key in ("source_name", "link", "title", "tier", "owner", "region")
        }
        for source in sources
        if _source_url_key(source.get("link")) in claimed_urls
    ]

    if not confirmation["confirmed"]:
        message = (
            f"story {name}: needs one tier-1 source stating the headline claim "
            f"or two independent origins supporting it; got {confirmation}"
        )
        logger.warning("%s", message)
        _write_unconfirmed_sheet(
            output_dir, name, now, confirmation, central, kept, claims, sheet_sources, dropped,
            primary=primary, canonical=canonical, grounding_details=ungrounded_details,
            origin_downgrades=origin_downgrades,
        )
        raise UnconfirmedStory(message)

    payload = {
        "id": name,
        "generated_at": now.isoformat(),
        "confirmation": confirmation,
        "confirmation_reason": _confirmation_reason(confirmation),
        "sources": sheet_sources,
        "claims": claims,
        "grounding": [
            {"claim": entry["claim"].get("claim"), "reason": entry["reason"]}
            for entry in ungrounded_details
        ],
        "origins_downgraded": origin_downgrades,
        "coherence": {
            "checked": bool(primary),
            "primary_entities": primary,
            "canonical_entities": canonical,
            "dropped_claims": _coherence_dropped_details(dropped, primary),
        },
        "extractor": {
            "provider": generation.provider,
            "model": generation.model,
            "family": generation.family,
        },
        "fingerprint": {"format": cluster.FINGERPRINT_FORMAT, "value": fingerprint},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info(
        "facts complete: %d claim(s), %d source(s) -> %s",
        len(claims), len(sheet_sources), path,
    )
    return payload


__all__ = [
    "ALLOWED_KINDS",
    "CLAIM_SCHEMA",
    "CONFIRMING_KINDS",
    "DEFAULT_ORIGIN_ALIASES_PATH",
    "DEFAULT_ORGS_PATH",
    "DEFAULT_RELATIVE_TIME_WORDS",
    "FIRST_PARTY_KINDS",
    "HEDGE_WORDS",
    "KIND_STRENGTH",
    "MAX_FIELD_CHARS",
    "MAX_CLAIMS_PER_SOURCE",
    "PROMPT_VERSION",
    "UNSTATED_ORIGIN",
    "FactsError",
    "UnconfirmedStory",
    "absolutize_relative_time",
    "build_prompt",
    "canonicalize_origin",
    "central_claim",
    "coherence_check",
    "canonical_entities",
    "entity_aliases",
    "extract_claims",
    "ground_claims",
    "ground_claims_details",
    "grounding_failure",
    "load_origin_aliases",
    "load_organizations",
    "merge_claims",
    "primary_entities",
    "resolve_claim_origins",
    "validate_claims",
    "confirmation_from_sources",
    "facts",
]