"""Facts stage: extract claims from gathered source text with the 'fast' role.

Input is the output of the gather stage (``data/gathered/<id>.json``). The
"fast" model family extracts every distinct factual claim from the source
texts as JSON: claim, value, source_url, confidence (0..1), is_rumor.

The extracted JSON is validated against :data:`CLAIM_SCHEMA` in code; anything
that does not match raises :class:`FactsError` and the story is dropped.

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
      "claims": [{"claim", "value", "source_url", "confidence", "is_rumor"}],
      "coherence": {"checked": true, "primary_entities": [...], "dropped_claims": [...]},
      "extractor": {"provider", "model", "family"}
    }
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
from providers import Generation, RunState, generate as default_generate
from json_schema import schema_errors

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "facts"

MAX_SOURCE_CHARS = 4000
# A claim's text and value must stay short: longer fields are almost always a
# verbatim passage copied from a source, so they are clipped before the sheet is
# saved (PLAN.md principle 3: facts, not source prose).
MAX_FIELD_CHARS = 300

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
                    "is_rumor": {"type": "boolean"},
                },
                "required": ["claim", "value", "source_url", "confidence", "is_rumor"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
}

SYSTEM_INSTRUCTION = (
    "You are a news-room fact extractor. From the article text below, extract "
    "every distinct factual claim: what happened, key figures, prices, dates, "
    "specs, and attributed quotes. Return each claim as an object with: claim "
    "(short statement), value (the concrete value or a one-line summary), "
    "source_url (the exact URL that supports it, from the list given), "
    "confidence (0 to 1), and is_rumor (true only when the text itself marks it "
    "as speculative, unconfirmed or a rumor). Extract only what the text says; "
    "never infer, fill gaps, or invent URLs."
)


class FactsError(Exception):
    """The model output did not match the claim schema; the story is dropped."""


class UnconfirmedStory(Exception):
    """Not enough independent sources; the story must not be published."""


def build_prompt(id: str, sources: list[dict]) -> str:
    parts = [SYSTEM_INSTRUCTION, "", f"Story ID: {id}", ""]
    for index, source in enumerate(sources, start=1):
        parts.append(
            f"SOURCE {index}: {source['source_name']} "
            f"(tier {source['tier']}, owner {source['owner']}, region {source['region']})"
        )
        parts.append(f"URL: {source['link']}")
        text = (source.get("text") or "").strip()
        if text:
            parts.append("TEXT:\n" + _truncate(text))
        else:
            parts.append("TEXT: (no text available)")
        parts.append("")
    return "\n".join(parts)


def _truncate(text: str, limit: int = MAX_SOURCE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [truncated]"


def extract_claims(
    gathered: dict,
    *,
    prompt: str | None = None,
    generate: Callable[..., Generation] = default_generate,
    run_state: RunState | None = None,
    **kwargs: Any,
) -> Generation:
    """Run the 'fast' role to extract claims, returning its Generation."""
    prompt = prompt if prompt is not None else build_prompt(
        gathered.get("id", ""), gathered.get("sources") or []
    )
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

    generation = extract_claims(gathered, generate=generate, run_state=run_state, **kwargs)
    claims = validate_claims(generation.value)

    kept, dropped, primary = coherence_check(
        claims,
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
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("facts complete: %d claim(s), %d source(s) -> %s", len(claims), len(sources), path)
    return payload


__all__ = [
    "CLAIM_SCHEMA",
    "MAX_FIELD_CHARS",
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