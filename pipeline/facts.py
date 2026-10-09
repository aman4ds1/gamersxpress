"""Facts stage: extract claims from gathered source text with the 'fast' role.

Input is the output of the gather stage (``data/gathered/<id>.json``). The
"fast" model family extracts every distinct factual claim from the source
texts as JSON: claim, value, source_url, confidence (0..1), is_rumor.

The extracted JSON is validated against :data:`CLAIM_SCHEMA` in code; anything
that does not match raises :class:`FactsError` and the story is dropped.

The story is rejected with :class:`UnconfirmedStory` unless it has at least one
tier-1 source OR two tier-2 sources with different owners. Tier 3 never counts
toward confirmation. Source text is consumed here only; it is never handed to
the writer.

Output is written to ``data/facts/<id>.json``:

    {
      "id": "<id>",
      "confirmation": {"tier1_sources": 1, "tier2_owners": [...], "confirmed": true},
      "sources": [{"source_name", "link", "tier", "owner", "region"}],
      "claims": [{"claim", "value", "source_url", "confidence", "is_rumor"}],
      "extractor": {"provider", "model", "family"}
    }
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional

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


def facts(
    gathered: dict,
    *,
    id: str | None = None,
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

    payload = {
        "id": name,
        "generated_at": now.isoformat(),
        "confirmation": confirmation,
        "sources": [
            {key: source[key] for key in ("source_name", "link", "tier", "owner", "region")}
            for source in sources
        ],
        "claims": claims,
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
    "extract_claims",
    "validate_claims",
    "confirmation_from_sources",
    "facts",
]