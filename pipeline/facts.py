"""Facts stage: extract claims from gathered source text with the 'fast' role.

Input is the output of the gather stage (``data/gathered/<id>.json``). The
"fast" model family extracts JSON claims from the source texts: claim, value,
source_url, confidence (0..1), and kind (confirmed / rumor / opinion).

Extraction runs **once per source**, one model call per source, so a long
article cannot crowd out the story's other sources. Each call sees only its own
source and extracts every distinct fact about the story's subject (features,
modes, dates, prices, platforms, named quotes), up to a per-source maximum
(``facts.max_claims_per_source`` in config.yaml, default
:data:`MAX_CLAIMS_PER_SOURCE`). Claims from all sources are merged into one
sheet afterwards.

The extracted JSON is validated against :data:`CLAIM_SCHEMA` in code; anything
that does not match raises :class:`FactsError` and the story is dropped. Each
claim's ``kind`` is one of ``confirmed``, ``rumor`` or ``opinion``. In-game
currency amounts must name their currency (for example "100,000 in-game DMZ
Cash") and are never stored as a bare dollar figure.

A coherence check then keeps every claim honest to the story: each claim's
source must be about the cluster's primary entity (the named entity shared by
the most source headlines). Claims whose source is off-topic are dropped and
logged, so a mixed cluster cannot smuggle another game's figures into the sheet.

The story is rejected with :class:`UnconfirmedStory` unless it has at least one
tier-1 source OR two tier-2 sources with different owners. Tier 3 never counts
toward confirmation. Source text is consumed here only; it is never handed to
the writer.

Output is written to ``data/facts/<id>.json``:

    {
      "id": "<id>",
      "confirmation": {"tier1_sources": 1, "tier2_owners": [...], "confirmed": true},
      "sources": [{"source_name", "link", "title", "tier", "owner", "region"}],
      "claims": [{"claim", "value", "source_url", "confidence", "kind"}],
      "coherence": {"checked": true, "primary_entities": [...], "dropped_claims": [...]},
      "extractor": {"provider", "model", "family"},
      "fingerprint": {"format": 1, "value": "<hash of id + model chain + prompt settings>"}
    }

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
PROMPT_VERSION = "facts-v2"

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
                    "kind": {"type": "string", "enum": ["confirmed", "rumor", "opinion"]},
                },
                "required": ["claim", "value", "source_url", "confidence", "kind"],
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
    "Return each fact as an object with: claim (a short statement), value (the "
    "concrete value or a one-line summary), source_url (the exact URL given for "
    "this source), confidence (0 to 1), and kind (one of confirmed, rumor, or "
    "opinion). kind is 'confirmed' when the text states the fact, 'rumor' only "
    "when the text itself marks it as speculative, unconfirmed, or a leak, and "
    "'opinion' for a writer's or a source's judgement. Any in-game currency "
    "amount must name the currency (for example '100,000 in-game DMZ Cash'), "
    "never appear as a bare dollar figure."
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
    parts.append(
        f"Extract up to {max_claims} distinct facts from the text below; every "
        f"claim's source_url must be exactly {source['link']}."
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
        clipped.append(
            {
                **claim,
                "claim": _clip_field(claim["claim"]),
                "value": _clip_field(claim["value"]),
            }
        )
    return clipped


def confirmation_from_sources(sources: list[dict]) -> dict:
    """Assess the confirmation rule against the cluster's sources.

    Confirmed when there is at least one tier-1 source, or at least two tier-2
    sources with different owners. Tier 3 never counts.
    """
    tier1 = sum(1 for source in sources if source["tier"] == 1)
    tier2_owners = sorted({source["owner"] for source in sources if source["tier"] == 2})
    tier3 = sum(1 for source in sources if source["tier"] == 3)
    return {
        "tier1_sources": tier1,
        "tier2_owners": tier2_owners,
        "tier3_sources": tier3,
        "confirmed": tier1 >= 1 or len(tier2_owners) >= 2,
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
    by_url = {source.get("link"): source for source in sources}
    kept: list[dict] = []
    dropped: list[dict] = []
    for claim in claims:
        source = by_url.get(claim.get("source_url")) or {}
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
    **kwargs: Any,
) -> dict:
    """Extract claims from gathered text, enforce confirmation, save the sheet."""
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    name = re.sub(r"[^0-9A-Za-z._-]", "-", str(id or gathered.get("id") or "unnamed"))
    now = now or dt.datetime.now(dt.timezone.utc)
    sources = gathered.get("sources") or []

    confirmation = confirmation_from_sources(sources)
    if not confirmation["confirmed"]:
        message = (
            f"story {name}: needs one tier-1 source or two tier-2 sources with "
            f"different owners; got {confirmation}"
        )
        logger.warning("%s", message)
        raise UnconfirmedStory(message)

    config_max = int(
        (load_config().get("facts") or {}).get("max_claims_per_source", MAX_CLAIMS_PER_SOURCE)
    )
    max_claims = max_claims_per_source if max_claims_per_source is not None else config_max
    fingerprint = cluster.stage_fingerprint(
        name, "facts", PROMPT_VERSION, SYSTEM_INSTRUCTION, CLAIM_SCHEMA,
        _extraction_chain(), MAX_SOURCE_CHARS, MAX_FIELD_CHARS, max_claims,
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
    claims = kept

    payload = {
        "id": name,
        "generated_at": now.isoformat(),
        "confirmation": confirmation,
        "sources": [
            {
                key: source.get(key)
                for key in ("source_name", "link", "title", "tier", "owner", "region")
            }
            for source in sources
        ],
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
    logger.info("facts complete: %d claim(s), %d source(s) -> %s", len(claims), len(sources), path)
    return payload


__all__ = [
    "CLAIM_SCHEMA",
    "MAX_FIELD_CHARS",
    "MAX_CLAIMS_PER_SOURCE",
    "PROMPT_VERSION",
    "FactsError",
    "UnconfirmedStory",
    "build_prompt",
    "coherence_check",
    "extract_claims",
    "primary_entities",
    "validate_claims",
    "confirmation_from_sources",
    "facts",
]