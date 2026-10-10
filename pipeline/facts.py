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
(a third-party analyst or data estimate, never a confirmation), ``rumor`` (a
leak or unnamed source) or ``opinion``. ``origin`` names who produced the claim
(the analyst or firm the source cites, or the company); when the source does
not say, the extractor stores ``"unstated"`` and the claim is treated as at
most ``reported``. In-game currency amounts must name their currency (for
example "100,000 in-game DMZ Cash") and are never stored as a bare dollar
figure.

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
source never mentions is downgraded to ``"unstated"`` and logged.

The story is rejected with :class:`UnconfirmedStory` unless it has at least one
tier-1 source OR at least two independent attributed origins among the kept
claims; only claims of kind ``confirmed``/``reported``/``estimate`` count (a
rumor or an opinion never confirms a story), outlets repeating the same origin
count as one, and tier 3 never counts toward confirmation. When one canonical
origin is contained in another from the same cluster ("alinea analytics" inside
"alinea analytics ltd") they are treated as the same origin.
``allow_single_origin_attributed`` (config flag, default false) additionally
lets a single attributed origin confirm a story. Source text is consumed here
only; it is never handed to the writer.

Output is written to ``data/facts/<id>.json``:

    {
      "id": "<id>",
      "confirmation": {"tier1_sources": 1, "tier3_sources": 0, "origins": [...], "rule": "...", "confirmed": true},
      "sources": [{"source_name", "link", "title", "tier", "owner", "region"}],
      "claims": [{"claim", "value", "source_urls": [...], "origin", "confidence", "kind"}],
      "coherence": {"checked": true, "primary_entities": [...], "dropped_claims": [...]},
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
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

import cluster
from ingest import normalize_url
from providers import Generation, RunState, generate as default_generate, load_config
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
# A source that carries more text than this yet produces fewer than
# WARN_MIN_CLAIMS claims is logged with a warning, because that strong of a gap
# usually means the extractor skipped most of what the source says.
WARN_LOW_SOURCE_CHARS = 1500
WARN_MIN_CLAIMS = 3
# Bump when the extraction prompt or the claim schema changes, so previously
# written facts sheets are treated as stale and re-extracted.
PROMPT_VERSION = "facts-v4"

# Claim kinds, weakest to strongest. Merging takes the weakest kind a fact's
# sources disagree on, so an estimate that one source calls an estimate is
# never upgraded to confirmed by another source's wording.
KIND_STRENGTH: dict[str, int] = {
    "confirmed": 5,
    "reported": 4,
    "estimate": 3,
    "rumor": 2,
    "opinion": 1,
}
ALLOWED_KINDS = tuple(KIND_STRENGTH)

#: Origin used when a source does not say who produced the claim. A claim with
#: this origin is capped at ``reported`` (requirement: treat as at most reported).
UNSTATED_ORIGIN = "unstated"

#: Hedge words a merged fact must keep in its value. When sources state the same
#: fact with different values, the value carrying the most hedge words wins, so
#: "estimated 168,000" is never flattened to the unhedged "168,000".
HEDGE_WORDS = (
    "estimated", "estimate", "estimates", "reportedly", "more than", "over",
    "approximately", "about", "around", "roughly", "nearly", "up to", "at least",
)

#: Story-level confirmation is one tier-1 source OR at least two independent
#: attributed origins. Setting `facts.allow_single_origin_attributed` in
#: config.yaml lets a single attributed (named) origin confirm a story instead.
DEFAULT_ALLOW_SINGLE_ORIGIN_ATTRIBUTED = False

#: Origin strings are canonicalized through this small data file: each alias on
#: the left maps to one canonical name on the right ("alinea" -> "Alinea
#: Analytics"). A missing or malformed file is treated as an empty map.
DEFAULT_ORIGIN_ALIASES_PATH = PROJECT_ROOT / "data" / "origins.json"

#: Leading words stripped from an origin before matching, so an article or a
#: role ("the analyst Alinea Analytics") does not split one origin into two.
_ORIGIN_TITLE_WORDS = frozenset({"the", "a", "an", "analyst", "analysts"})

#: Claim kinds whose origins may count toward confirmation. A rumor or an
#: opinion never confirms a story, however many outlets repeat it.
CONFIRMING_KINDS = ("confirmed", "reported", "estimate")

logger = logging.getLogger("gamersxpress.pipeline.facts")

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
                    "kind": {"type": "string", "enum": list(KIND_STRENGTH)},
                },
                "required": ["claim", "value", "source_url", "confidence", "origin", "kind"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
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
    "for this source), confidence (0 to 1), origin, and kind.\n"
    "origin names who produced the claim: the company or official source, or "
    "the analyst or firm the article cites (for example 'Sony', 'Sensor "
    "Tower'). When the article does not say who produced the claim, set origin "
    "to 'unstated'.\n"
    "kind is exactly one of: 'confirmed' (the company or an official source "
    "states it, with a named origin), 'reported' (an outlet citing a named "
    "source), 'estimate' (a third-party analyst or data estimate; never label "
    "an estimate 'confirmed'), 'rumor' (a leak or unnamed source), or 'opinion' "
    "(a writer's or source's judgement). origin 'unstated' caps a claim at "
    "'reported'.\n"
    "Keep hedge words (estimated, reportedly, more than, over) in the value. "
    "For percentages and overlap or audience figures, state the exact "
    "relationship in words (for example 'overlap with players of X'), never a "
    "vague phrase. Any in-game currency amount must name the currency (for "
    "example '100,000 in-game DMZ Cash'), never appear as a bare dollar figure."
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


def build_prompt(id: str, source: dict, *, max_claims: int = MAX_CLAIMS_PER_SOURCE) -> str:
    """Build the extraction prompt for ONE source (one model call per source)."""
    parts = [SYSTEM_INSTRUCTION, "", f"Story ID: {id}", ""]
    parts.append(
        f"SOURCE: {source['source_name']} "
        f"(tier {source['tier']}, owner {source['owner']}, region {source['region']})"
    )
    parts.append(f"URL: {source['link']}")
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


def validate_claims(data: Any) -> list[dict]:
    """Validate the model output against CLAIM_SCHEMA and return the claims.

    ``claim`` and ``value`` are clipped to :data:`MAX_FIELD_CHARS` so a facts
    sheet never holds a long verbatim passage copied from a source article.
    ``origin`` is normalized (trimmed, lowercased; empty becomes
    :data:`UNSTATED_ORIGIN`), and a claim whose origin is ``unstated`` is capped
    at ``reported`` because an unattributed claim is never confirmed and never
    an estimate (requirement: treat an unstated origin as at most reported).
    """
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
                "kind": kind,
            }
        )
    return clipped


def _normalize_origin(origin: object) -> str:
    value = str(origin or "").strip().lower()
    return value or UNSTATED_ORIGIN


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


def resolve_claim_origins(
    claims: list[dict],
    sources: list[dict],
    *,
    aliases: dict[str, str] | None = None,
    story_id: str = "",
) -> list[dict]:
    """Canonicalize each claim's origin and drop any its source never names.

    Each ``;``-separated origin token is canonicalized, then checked against the
    text of the claim's own source. A token the text does not mention is removed
    and the downgrade is logged; when no token survives, the origin becomes
    :data:`UNSTATED_ORIGIN`. No other claim field is touched.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    by_url = _sources_by_url(sources)
    resolved: list[dict] = []
    for claim in claims:
        source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
        text = str(source.get("text") or "")
        kept: list[str] = []
        for token in str(claim.get("origin") or "").split(";"):
            token = token.strip()
            if not token or _origin_key(token) == UNSTATED_ORIGIN:
                continue
            if _origin_in_text(token, text, aliases):
                canonical = canonicalize_origin(token, aliases)
                if canonical not in kept:
                    kept.append(canonical)
            else:
                logger.warning(
                    "story %s: origin %r is not named in the source text for %s; "
                    "downgrading to %s",
                    story_id or "?",
                    token,
                    claim.get("source_url"),
                    UNSTATED_ORIGIN,
                )
        resolved.append({**claim, "origin": "; ".join(kept) if kept else UNSTATED_ORIGIN})
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


def _hedge_count(value: str) -> int:
    text = str(value or "").lower()
    return sum(1 for word in HEDGE_WORDS if word in text)


def _merge_score(claim: dict) -> tuple[int, int, int]:
    """Pick the value that keeps the story honest: most hedged, then weakest kind.

    A fact whose sources disagree (one wrote "estimated", another wrote a bare
    figure) must keep the hedged value, and the weakest kind, so an estimate is
    never flattened into a confirmation.
    """
    return (-_hedge_count(str(claim.get("value", ""))), KIND_STRENGTH.get(claim["kind"], 0), -len(str(claim.get("value", ""))))


def _merge_origins(group: list[dict]) -> str:
    """One or more named origins, or 'unstated' when none is named."""
    named = sorted({claim["origin"] for claim in group if claim["origin"] != UNSTATED_ORIGIN})
    return "; ".join(named) if named else UNSTATED_ORIGIN


def _merge_group(group: list[dict]) -> dict:
    """Collapse the claims of one fact (same normalized claim text) into one."""
    best = min(group, key=_merge_score)
    return {
        "claim": str(best["claim"]),
        "value": str(best["value"]),
        "source_urls": sorted({claim["source_url"] for claim in group}),
        "origin": _merge_origins(group),
        "confidence": min(float(claim["confidence"]) for claim in group),
        "kind": min((claim["kind"] for claim in group), key=KIND_STRENGTH.get),
    }


def merge_claims(claims: list[dict]) -> list[dict]:
    """Merge duplicate claims across sources: one claim per fact.

    A fact groups all claims that share the same normalized claim text and is
    stored once, listing every source URL that states it (``source_urls``),
    taking the weakest ``kind`` when sources disagree, keeping the hedge words
    in the value, joining the named origins, and keeping the lowest confidence.
    """
    groups: dict[str, list[dict]] = {}
    for claim in claims:
        groups.setdefault(_claim_key(claim), []).append(claim)
    return [_merge_group(group) for group in groups.values()]


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


def _confirmation_origins(
    claims: list[dict],
    by_url: dict[str, dict],
    *,
    aliases: dict[str, str] | None = None,
) -> list[str]:
    """Distinct attributed origins among the claims, tier- and kind-filtered.

    An origin counts only when its claim is of a confirming kind
    (:data:`CONFIRMING_KINDS`) and its source is not tier-3; a rumor or an
    opinion never counts toward confirmation. Each origin is canonicalized
    (case, possessives, titles, aliases) and ``;``-separated tokens count
    separately. When one canonical origin is contained in another from the same
    set they are treated as one, keeping the shorter name.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    names: set[str] = set()
    for claim in claims or []:
        if claim.get("kind") not in CONFIRMING_KINDS:
            continue
        source = by_url.get(_source_url_key(claim.get("source_url")))
        if source is None or source.get("tier") == 3:
            continue
        for token in str(claim.get("origin") or "").split(";"):
            canonical = canonicalize_origin(token, aliases)
            if canonical and canonical != UNSTATED_ORIGIN:
                names.add(canonical)
    return _collapse_contained_origins(names)


def confirmation_from_sources(
    sources: list[dict],
    *,
    claims: list[dict] | None = None,
    allow_single_origin_attributed: bool = False,
    aliases: dict[str, str] | None = None,
) -> dict:
    """Assess the confirmation rule against the cluster's sources and claims.

    Confirmed when there is at least one tier-1 source, OR the extracted claims
    carry at least two independent attributed origins (outlets repeating the
    same origin count as one). Origins are canonicalized through ``aliases``
    (:func:`load_origin_aliases`), so different spellings of one name collapse,
    and only claims of kind confirmed/reported/estimate count: a rumor or an
    opinion never confirms. When ``allow_single_origin_attributed`` is set, a
    single attributed (named, non-"unstated") origin also confirms. Tier 3 never
    counts. ``claims`` are the kept, pre-merge claims (``source_url`` still
    singular); confirmation is about who produced each claim, so duplicates of
    the same fact across outlets still collapse to one origin.
    """
    by_url = _sources_by_url(sources)
    origins = _confirmation_origins(claims, by_url, aliases=aliases)
    tier1 = sum(1 for source in sources if source["tier"] == 1)
    tier3 = sum(1 for source in sources if source["tier"] == 3)
    single_origin_attributed = len(origins) == 1
    if tier1 >= 1:
        confirmed, rule = True, "tier1"
    elif len(origins) >= 2:
        confirmed, rule = True, "two-independent-origins"
    elif allow_single_origin_attributed and single_origin_attributed:
        confirmed, rule = True, "single-origin-attributed"
    else:
        confirmed, rule = False, "unconfirmed"
    return {
        "tier1_sources": tier1,
        "tier3_sources": tier3,
        "origins": origins,
        "single_origin_attributed": single_origin_attributed,
        "rule": rule,
        "confirmed": confirmed,
    }


def primary_entities(
    *,
    title: str | None = None,
    source_titles: list[str] | None = None,
    entities: list[str] | None = None,
) -> list[str]:
    """The entity (or tied entities) that define the story's subject.

    Counts each named entity across the cluster title and the source headlines
    and keeps those with the highest support. Falls back to the cluster's
    reported entities when no headline parses.
    """
    counts: Counter[str] = Counter()
    for candidate in [title, *(source_titles or [])]:
        if candidate:
            for entity in cluster.named_entities(candidate, include_leading=True):
                counts[entity] += 1
    if counts:
        top = max(counts.values())
        return sorted(entity for entity, count in counts.items() if count == top)
    return sorted({str(entity).strip().lower() for entity in (entities or []) if str(entity).strip()})


def _claim_on_topic(claim: dict, source: dict, primary: list[str]) -> bool:
    source_title = str(source.get("title") or "")
    if source_title:
        return any(cluster.contains_entity(source_title, entity) for entity in primary)
    text = f"{claim.get('claim', '')} {claim.get('value', '')}"
    return any(cluster.contains_entity(text, entity) for entity in primary)


def coherence_check(
    claims: list[dict],
    sources: list[dict],
    *,
    title: str | None = None,
    entities: list[str] | None = None,
    story_id: str = "",
) -> tuple[list[dict], list[dict], list[str]]:
    """Drop claims whose source is not about the cluster's primary entity.

    Returns ``(kept, dropped, primary)``. When no primary entity can be
    determined the check is skipped and every claim is kept.
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
    for claim in claims:
        source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
        if _claim_on_topic(claim, source, primary):
            kept.append(claim)
        else:
            logger.warning(
                "story %s: dropping off-topic claim from %s (mentions none of %s): %r",
                story_id or "?",
                claim.get("source_url"),
                primary,
                claim.get("claim"),
            )
            dropped.append(claim)
    return kept, dropped, primary


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
    **kwargs: Any,
) -> dict:
    """Extract claims from gathered text, enforce confirmation, save the sheet.

    Confirmation is assessed after extraction because it now depends on the
    claims' origins (who produced each claim), not just on the outlets. A story
    with no tier-1 source is not rejected before the model runs: a single source
    can still carry two independent origins.
    """
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    name = re.sub(r"[^0-9A-Za-z._-]", "-", str(id or gathered.get("id") or "unnamed"))
    now = now or dt.datetime.now(dt.timezone.utc)
    sources = gathered.get("sources") or []

    config_facts = load_config().get("facts") or {}
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

    aliases = load_origin_aliases()
    fingerprint = cluster.stage_fingerprint(
        name, "facts", PROMPT_VERSION, SYSTEM_INSTRUCTION, CLAIM_SCHEMA,
        _extraction_chain(), MAX_SOURCE_CHARS, MAX_FIELD_CHARS, max_claims,
        allow_single, HEDGE_WORDS, sorted(aliases.items()),
    )
    path = output_dir / f"{name}.json"
    if cluster.stored_fingerprint(path) == fingerprint:
        payload = json.loads(path.read_text(encoding="utf-8"))
        logger.info("facts reused cached sheet %s (fingerprint match)", path)
        return payload

    extracted: list[dict] = []
    generation = None
    for source in sources:
        text = str(source.get("text") or "")
        if not text.strip():
            logger.info("story %s: source %s has no text; nothing to extract", name, source.get("link"))
            continue
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

    # Origins are canonicalized and verified against each claim's own source
    # text before anything is counted or stored.
    kept = resolve_claim_origins(kept, sources, aliases=aliases, story_id=name)

    # Confirmation uses the kept, pre-merge claims: it is about who produced each
    # claim, so three outlets repeating one origin still collapse to one origin.
    confirmation = confirmation_from_sources(
        sources, claims=kept, allow_single_origin_attributed=allow_single, aliases=aliases
    )
    if not confirmation["confirmed"]:
        message = (
            f"story {name}: needs one tier-1 source or two independent origins; "
            f"got {confirmation}"
        )
        logger.warning("%s", message)
        raise UnconfirmedStory(message)

    claims = merge_claims(kept)

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

    payload = {
        "id": name,
        "generated_at": now.isoformat(),
        "confirmation": confirmation,
        "sources": sheet_sources,
        "claims": claims,
        "coherence": {
            "checked": bool(primary),
            "primary_entities": primary,
            "dropped_claims": [
                {"claim": claim.get("claim"), "source_url": claim.get("source_url")}
                for claim in dropped
            ],
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
    "HEDGE_WORDS",
    "KIND_STRENGTH",
    "MAX_FIELD_CHARS",
    "MAX_CLAIMS_PER_SOURCE",
    "PROMPT_VERSION",
    "UNSTATED_ORIGIN",
    "FactsError",
    "UnconfirmedStory",
    "build_prompt",
    "canonicalize_origin",
    "coherence_check",
    "extract_claims",
    "load_origin_aliases",
    "merge_claims",
    "primary_entities",
    "resolve_claim_origins",
    "validate_claims",
    "confirmation_from_sources",
    "facts",
]