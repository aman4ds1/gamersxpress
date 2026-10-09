"""Score stage: rank clusters and pick at most one story (PLAN.md section 7.4).

Scoring is code-only (no model): a story earns points for a tier-1 source, for
each distinct outlet owner, and for recency, plus an optional per-category boost
from ``data/topic-performance.json`` when that file exists. Only the top story
is covered, and only when it reaches ``score.min_score`` from ``config.yaml``.
:func:`guess_category` maps the story's title and entities to one of the site's
categories so the writer, SEO and cover stages agree on a single category.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PERFORMANCE_PATH = PROJECT_ROOT / "data" / "topic-performance.json"

DEFAULT_MIN_SCORE = 50
DEFAULT_WEIGHTS = {
    "tier1": 40,
    "tier2": 15,
    "owner": 10,
    "max_owners": 4,
    "fresh_hours": 6,
    "fresh_bonus": 30,
    "day_hours": 24,
    "day_bonus": 20,
    "two_day_hours": 48,
    "two_day_bonus": 10,
}

# Ordered: the first category whose keywords match wins. More specific before
# the generic "gaming-news" fallback.
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("playstation", ("playstation", "ps5", "ps4", "sony", "naughty dog", "insomniac", "god of war", "spider-man", "horizon")),
    ("xbox", ("xbox", "microsoft", "game pass", "bethesda", "halo", "forza", "activision", "blizzard")),
    ("nintendo", ("nintendo", "switch", "mario", "zelda", "pokemon", "splatoon", "metroid")),
    ("hardware", ("nvidia", "geforce", "rtx", "radeon", "amd", "ryzen", "intel", "arc", "gpu", "cpu", "snapdragon", "ssd", "steam deck", "graphics card")),
    ("esports", ("esports", "tournament", "championship", "major", "worlds", "val", "esl", "evo")),
    ("pc", ("steam", "epic games", "valve", "cd projekt", "riot games", "league of legends", "dota", "world of warcraft", "valorant", "pc gaming")),
    ("india", ("india", "indian", "rupee", "ivg", "gaming in india")),
    ("tech", ("openai", "chatgpt", "apple", "meta", "android", "windows", "ai model", "artificial intelligence")),
]
DEFAULT_CATEGORY = "gaming-news"

logger = logging.getLogger("gamersxpress.pipeline.score")


def load_performance(path: str | Path | None = None) -> dict:
    """Optional per-category performance boosts; a missing file means none."""
    path = Path(path) if path else DEFAULT_PERFORMANCE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}
    return {}


def guess_category(cluster: dict) -> str:
    haystack = " ".join([str(cluster.get("title") or ""), *[str(e) for e in cluster.get("entities") or []]]).lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return category
    return DEFAULT_CATEGORY


def score_cluster(cluster: dict, *, weights: dict | None = None, performance: dict | None = None) -> float:
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    tiers = cluster.get("tiers") or []
    score = 0.0
    if 1 in tiers:
        score += weights["tier1"]
    elif 2 in tiers:
        score += weights["tier2"]

    owners = cluster.get("owners") or []
    score += min(len(owners), weights["max_owners"]) * weights["owner"]

    age = cluster.get("age_hours")
    if age is not None:
        if age <= weights["fresh_hours"]:
            score += weights["fresh_bonus"]
        elif age <= weights["day_hours"]:
            score += weights["day_bonus"]
        elif age <= weights["two_day_hours"]:
            score += weights["two_day_bonus"]

    if performance:
        boost = performance.get(guess_category(cluster), 0.0)
        score += max(0.0, min(float(boost), 20.0))
    return float(score)


def rank(
    clusters: Iterable[dict],
    *,
    weights: dict | None = None,
    performance: dict | None = None,
) -> list[dict]:
    scored = [
        {**cluster, "category": guess_category(cluster), "score": score_cluster(cluster, weights=weights, performance=performance)}
        for cluster in clusters
    ]
    scored.sort(key=lambda cluster: cluster["score"], reverse=True)
    return scored


def pick(
    clusters: Iterable[dict],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    weights: dict | None = None,
    performance: dict | None = None,
) -> dict | None:
    """Return the highest-scoring cluster at or above ``min_score``, else None."""
    ranked = rank(clusters, weights=weights, performance=performance)
    if not ranked or ranked[0]["score"] < min_score:
        if ranked:
            logger.info("top story scored %.0f, below min_score %.0f; nothing to cover", ranked[0]["score"], min_score)
        return None
    logger.info("selected story %s (score=%.0f, category=%s)", ranked[0]["id"], ranked[0]["score"], ranked[0]["category"])
    return ranked[0]


__all__ = [
    "CATEGORY_KEYWORDS",
    "DEFAULT_CATEGORY",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_WEIGHTS",
    "guess_category",
    "load_performance",
    "pick",
    "rank",
    "score_cluster",
]
