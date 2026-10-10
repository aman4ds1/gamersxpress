"""Offline fixtures and a stub generator for ``--dry-run`` (PLAN.md section 8).

A dry run must exercise the whole pipeline with no network and no API keys, and
must not touch real state. This module supplies one confirmed sample story and a
``generate`` stand-in that answers each stage's role/schema with realistic,
schema-valid output. :func:`make_generate` is scenario-aware:

* ``pass`` -- a valid article and a passing verifier report.
* ``fail-verify`` -- the verifier returns an unsupported claim, so the run fails
  at the verification step.
* ``fail-gate`` -- the verifier passes but the writer's article is too short, so
  the run fails at the publish gates.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

from providers import Generation, RunState

SAMPLE_TITLE = "Nvidia confirms next graphics card launch window"
SAMPLE_DESCRIPTION = (
    "Nvidia has confirmed the launch window for its next graphics card line "
    "during its latest briefing."
)
SAMPLE_SLUG = "nvidia-confirms-next-graphics-card-launch-window"

SCENARIOS = ("pass", "fail-verify", "fail-gate")

_FILLER_WORDS = (
    "studio players update confirmed release hardware lineup community report "
    "details feature support platform title launch window roadmap roadmap update"
).split()

_SOURCE_PROSE = (
    "According to people familiar with the matter, the company outlined its "
    "plans during a briefing with regional partners. Representatives declined "
    "to comment on unannounced products, while analysts noted that supply and "
    "retail availability remain uncertain across several markets. The full "
    "announcement is expected alongside the company's next scheduled event, and "
    "retailers have reportedly begun preparing internal listings ahead of time."
)


def _clock(now: dt.datetime | None = None) -> dt.datetime:
    return now or dt.datetime.now(dt.timezone.utc)


def sample_pool(now: dt.datetime | None = None) -> dict:
    """Two tier-1 items that cluster into one confirmed story."""
    now = _clock(now)
    items = [
        {
            "title": SAMPLE_TITLE,
            "link": "https://a.example/1",
            "source_name": "Tier One A",
            "tier": 1,
            "owner": "owner-a",
            "region": "us",
            "published": (now - dt.timedelta(hours=2)).isoformat(),
            "age_hours": 2.0,
        },
        {
            "title": SAMPLE_TITLE,
            "link": "https://b.example/1",
            "source_name": "Tier One B",
            "tier": 1,
            "owner": "owner-b",
            "region": "us",
            "published": (now - dt.timedelta(hours=1)).isoformat(),
            "age_hours": 1.0,
        },
    ]
    return {"updated_at": now.isoformat(), "items": items}


def sample_gathered(cluster_id: str, now: dt.datetime | None = None) -> dict:
    now = _clock(now)
    return {
        "id": cluster_id,
        "gathered_at": now.isoformat(),
        "sources": [
            {
                "link": "https://a.example/1",
                "source_name": "Tier One A",
                "tier": 1,
                "owner": "owner-a",
                "region": "us",
                "status": "ok",
                "text": _SOURCE_PROSE,
                "error": None,
            },
            {
                "link": "https://b.example/1",
                "source_name": "Tier One B",
                "tier": 1,
                "owner": "owner-b",
                "region": "us",
                "status": "ok",
                "text": _SOURCE_PROSE,
                "error": None,
            },
        ],
    }


def _filler(count: int) -> str:
    return " ".join(_FILLER_WORDS[index % len(_FILLER_WORDS)] for index in range(count))


def sample_body(*, short: bool = False) -> str:
    if short:
        return "## Overview\n\n" + _filler(60)
    return (
        "## Overview\n\n"
        + _filler(220)
        + "\n\n## What it means\n\n"
        + _filler(140)
    )


def _claims_json() -> dict:
    return {
        "claims": [
            {
                "claim": "Nvidia confirmed the launch window for its next graphics card line",
                "value": "the launch window was confirmed for the next graphics card line",
                "source_url": "https://a.example/1",
                "confidence": 0.9,
                "kind": "confirmed",
            }
        ]
    }


def _seo_json() -> dict:
    return {
        "title": "Nvidia confirms next graphics card launch window",
        "description": SAMPLE_DESCRIPTION,
        "slug": SAMPLE_SLUG,
        "category": "hardware",
        "tags": ["nvidia", "graphics-card", "hardware"],
        "entities": ["Nvidia"],
        "imageAlt": "Cover image for the Nvidia graphics card story with the Hardware label.",
    }


def _verifier_json(scenario: str) -> str:
    unsupported = ["Nvidia will ship next month"] if scenario == "fail-verify" else []
    payload = {
        "clauses": [
            {
                "clause": "Nvidia confirmed the launch window for its next graphics card line.",
                "supported": scenario != "fail-verify",
                "fact_id": "F1",
            }
        ],
        "unsupported_claims": unsupported,
        "rumors_stated_as_fact": [],
        "unsupported_regional": [],
        "costs_described_as_received": [],
        "invented_labels": [],
        "category_claims": [],
    }
    return json.dumps(payload)


def make_generate(scenario: str = "pass") -> Callable[..., Generation]:
    """Return a stub ``generate`` that answers every stage for ``scenario``."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown dry-run scenario {scenario!r}; expected one of {SCENARIOS}")

    def generate(
        role: str,
        prompt: str,
        json_schema: dict | None = None,
        *,
        run_state: RunState | None = None,
        **kwargs: Any,
    ) -> Generation:
        if role == "fast":
            properties = (json_schema or {}).get("properties", {})
            if "claims" in properties:
                value = _claims_json()
            else:
                value = _seo_json()
            generation = Generation("fast", "mock", "mock-fast", "google", value)
        elif role == "writer":
            body = sample_body(short=(scenario == "fail-gate"))
            generation = Generation("writer", "mock", "mock-writer", "google", body)
        elif role == "verifier":
            generation = Generation("verifier", "mock", "mock-verifier", "mistral", _verifier_json(scenario))
        else:
            raise AssertionError(f"unexpected role in dry run: {role!r}")
        if run_state is not None:
            run_state.record(generation)
        return generation

    return generate


__all__ = [
    "SCENARIOS",
    "SAMPLE_DESCRIPTION",
    "SAMPLE_SLUG",
    "SAMPLE_TITLE",
    "make_generate",
    "sample_body",
    "sample_gathered",
    "sample_pool",
]
