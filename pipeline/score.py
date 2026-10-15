"""Score stage: rank clusters and pick at most one story (PLAN.md section 7.4).

Scoring is code-only (no model): a story earns points for a tier-1 source, for
each distinct outlet owner, and for recency, plus an optional per-category boost
from ``data/topic-performance.json`` when that file exists and a bonus for a
seed headline that carries a news signal (``score.news_verbs`` as a whole word,
or a date such as "November 18"). Only one top
story is considered for coverage, and only when it reaches its score threshold:
``score.min_score_corroborated`` (default 55) for a corroborated cluster with at
least two distinct owners, else ``score.min_score`` (default 65), both from
``config.yaml``. Before scoring, clusters must pass an eligibility gate (no
model call): they need a tier-1 source or at least two distinct owners
(:func:`cluster.confirmable`), their headline must not match a configured
format-exclusion pattern (``score.exclude_title_patterns``), and they must be
topically relevant. A cluster is relevant when any member comes from a source
whose ``topic`` is "gaming"; otherwise the seed title or the cluster's named
entities must contain a ``relevance.keywords`` phrase as a whole word. Ineligible
clusters are annotated and skipped, never gathered or sent to a model.
:func:`guess_category` maps the story's title and entities to one of the site's
categories so the writer, SEO and cover stages agree on a single category.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

import re

import cluster
from gates import load_content_limits
from providers import load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PERFORMANCE_PATH = PROJECT_ROOT / "data" / "topic-performance.json"

DEFAULT_MIN_SCORE = 65
DEFAULT_MIN_SCORE_CORROBORATED = 55
DEFAULT_MAX_CANDIDATES_PER_RUN = 5
#: Candidate diversity: at most this many candidates may share one lead entity
#: per run, so three GTA 6 clusters cannot fill the whole run. The lead entity is
#: the cluster's first non-umbrella primary entity (``score.umbrella_entities``).
DEFAULT_MAX_CANDIDATES_PER_ENTITY = 2
DEFAULT_MODEL_CALL_BUDGET = 12
#: Minimum distinct source URLs a cluster needs to be eligible. config.yaml's
#: ``content_limits.min_sources`` is the single source of truth (shared with the
#: publish gates); this default keeps the pipeline running with a partial config.
DEFAULT_MIN_SOURCES = 2

#: Minimum distinct owners a cluster's countable sources must have to be
#: eligible, from ``score.min_distinct_owners``. Two posts from one publisher
#: are one voice, so a story needs independent owners (see
#: :func:`cluster.distinct_owners`). The score-stage eligibility gate and the
#: run's post-gather readiness check share this value.
DEFAULT_MIN_DISTINCT_OWNERS = 2

DEFAULT_WEIGHTS = {
    "tier1": 40,
    "tier2": 15,
    "owner": 10,
    "max_owners": 4,
    "corroboration": 10,
    "fresh_hours": 6,
    "fresh_bonus": 30,
    "day_hours": 24,
    "day_bonus": 20,
    "two_day_hours": 48,
    "two_day_bonus": 10,
}

#: Format/promotional headlines that are never worth covering, regardless of
#: how confirmable the cluster is. Every entry is a case-insensitive regex.
#: Most use word-boundary anchors so they match whole words or phrases, not
#: substrings: "\bbest deals\b" catches the exact phrase while a bare "deals"
#: is allowed, and "\d+\s?% off" / "up to \d+%" match discounts like "40% off"
#: and "up to 28%". Editable through ``score.exclude_title_patterns`` in
#: config.yaml.
DEFAULT_EXCLUDE_TITLE_PATTERNS = [
    r"\bpodcast\b",
    r"\bshare of the week\b",
    r"\bnext week on xbox\b",
    r"\bthis week on xbox\b",
    r"\binto the omniverse\b",
    r"\bwalkthrough\b",
    r"\bgiveaway\b",
    r"\bfree this weekend\b",
    r"\bfree to play this weekend\b",
    r"\bfree play days\b",
    r"\bindie selects\b",
    r"\bgfn thursday\b",
    r"\bcoming to xbox game pass\b",
    r"\bcoming to game pass\b",
    r"\bto report (?:first|second|third|fourth|fiscal)\b",
    r"\bconference call\b",
    r"\bsale ends\b",
    r"\bbest deals\b",
    r"\d+\s?% off",
    r"up to \d+%",
    r"\bprice drop\b",
    r"\bon sale\b",
    r"\bdiscount code\b",
    r"\bon geforce now\b",
    r"\bplayers' choice\b",
    r"\bpre-order bonus\b",
    r"\bpre-order bonuses\b",
    r"\bblueprint\b",
    r"\bbundle\b",
    r"\bbattle pass\b",
    r"\btwitch drops\b",
    r"\boperator skin\b",
    r"\bweapon skin\b",
    r"\bcall of duty: mobile\b",
    # Affiliate/listicle headlines: "one of these 12 ...", "you'll be needing",
    # gift/buying guides and "best ... upgrades|accessories|deals" roundups are
    # never original news, however confirmable the cluster they join.
    r"\bone of these \d+\b",
    r"\byou['\u2019]ll be needing\b",
    r"\bgift guide\b",
    r"\bbuying guide\b",
    r"\bbest .{0,40}(?:upgrades|accessories|deals)\b",
]

#: News-signal words a seed headline may carry. A match earns
#: ``score.news_bonus`` points, pushing a borderline single-source story over
#: ``score.min_score`` while a plain story stays below.
DEFAULT_NEWS_BONUS = 10
DEFAULT_NEWS_VERBS: tuple[str, ...] = (
    "launches",
    "release date",
    "price",
    "announces",
    "unveils",
    "acquires",
    "appoints",
    "leadership",
    "delay",
    "delayed",
    "cancels",
    "cancelled",
    "reveals",
    "confirms",
    "launching",
    "arrives",
    "arriving",
    "heading to",
    "coming to",
    "joins",
    "joining",
    "release",
    "released",
    "revealed",
    "officially",
    "update",
    "patch",
)

#: Month names (full or three-letter) used for the date signal. "sept" is
#: included as a common four-letter abbreviation.
_MONTH_NAME = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

#: Date signal: a month name (full or three-letter) followed by a day number,
#: or a day number followed by a month name. It earns the same bonus as a
#: ``news_verbs`` match, and the bonus is applied once however many signals hit.
NEWS_DATE_SIGNAL_PATTERN = rf"\b{_MONTH_NAME}\b\.?\s+\d{{1,2}}\b|\b\d{{1,2}}\s+{_MONTH_NAME}\b\.?"
_NEWS_DATE_SIGNAL_RE = re.compile(NEWS_DATE_SIGNAL_PATTERN, re.IGNORECASE)

#: Topical relevance phrases (whole word or phrase matches). A cluster whose
#: items come only from "mixed" or unknown sources must contain one of these in
#: its seed title or named entities, or it is off-topic. Editable through the
#: ``relevance`` block in config.yaml.
DEFAULT_RELEVANCE_KEYWORDS: tuple[str, ...] = (
    "game",
    "games",
    "gaming",
    "gamer",
    "console",
    "playstation",
    "ps5",
    "xbox",
    "nintendo",
    "switch",
    "steam",
    "pc",
    "gpu",
    "geforce",
    "rtx",
    "radeon",
    "ryzen",
    "handheld",
    "vr",
    "esports",
    "dlss",
    "fsr",
    "graphics card",
    "graphics cards",
    "video card",
    "cpu",
    "processor",
    "vram",
    "gddr6",
)

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
    """Category for ``cluster``: its derived category, else the keyword map.

    Clusters produced by :func:`cluster.cluster_items` carry a ``category``
    derived from the seed title and entities alone (never from the feed); raw
    ad-hoc cluster dicts fall back to the keyword scanner below.
    """
    derived = cluster.get("category")
    if derived:
        return str(derived)
    haystack = " ".join([str(cluster.get("title") or ""), *[str(e) for e in cluster.get("entities") or []]]).lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return category
    return DEFAULT_CATEGORY


def load_exclude_title_patterns(config: dict | None = None) -> list[str]:
    """Format-exclusion patterns from ``score.exclude_title_patterns``.

    Reads the scoring section of ``config.yaml`` so the list is editable
    without code changes; a missing or malformed entry falls back to
    :data:`DEFAULT_EXCLUDE_TITLE_PATTERNS`.
    """
    if config is None:
        config = load_config()
    patterns = (config.get("score") or {}).get("exclude_title_patterns")
    if isinstance(patterns, list) and patterns:
        return [str(pattern) for pattern in patterns]
    return list(DEFAULT_EXCLUDE_TITLE_PATTERNS)


def load_relevance_keywords(config: dict | None = None) -> list[str]:
    """Topical relevance phrases from the ``relevance`` block of config.yaml."""
    if config is None:
        config = load_config()
    keywords = (config.get("relevance") or {}).get("keywords")
    if isinstance(keywords, list) and keywords:
        return [str(keyword) for keyword in keywords]
    return list(DEFAULT_RELEVANCE_KEYWORDS)


def load_source_topics(config: dict | None = None) -> dict[str, str]:
    """Map each configured source name to its ``topic`` ("gaming"/"mixed")."""
    if config is None:
        config = load_config()
    topics: dict[str, str] = {}
    for source in config.get("sources") or []:
        if isinstance(source, dict) and source.get("name"):
            topics[str(source["name"])] = str(source.get("topic") or "")
    return topics


def load_news_settings(config: dict | None = None) -> tuple[float, list[str]]:
    """News bonus points and signal phrases from ``score.news_*`` in config.yaml."""
    if config is None:
        config = load_config()
    section = config.get("score") or {}
    bonus = section.get("news_bonus", DEFAULT_NEWS_BONUS)
    if not isinstance(bonus, (int, float)):
        bonus = DEFAULT_NEWS_BONUS
    verbs = section.get("news_verbs")
    if isinstance(verbs, list) and verbs:
        return float(bonus), [str(verb) for verb in verbs]
    return float(bonus), list(DEFAULT_NEWS_VERBS)


def load_min_score_corroborated(config: dict | None = None) -> float:
    """Score threshold for corroborated clusters from ``score.min_score_corroborated``.

    A cluster with at least two distinct owners clears this lower bar; every
    other cluster uses ``score.min_score``. Falls back to
    :data:`DEFAULT_MIN_SCORE_CORROBORATED` when the key is missing or malformed.
    """
    if config is None:
        config = load_config()
    value = (config.get("score") or {}).get("min_score_corroborated")
    if isinstance(value, (int, float)):
        return float(value)
    return float(DEFAULT_MIN_SCORE_CORROBORATED)


def load_min_sources(config: dict | None = None) -> int:
    """Minimum distinct source URLs for eligibility, from ``content_limits``.

    Shared with the publish gates (``pipeline/gates.py``) through
    ``content_limits.min_sources`` in config.yaml, so the score-stage gate and
    the publish gate can never drift apart. Falls back to
    :data:`DEFAULT_MIN_SOURCES` when the value is missing or malformed.
    """
    if config is None:
        return int(load_content_limits().get("min_sources", DEFAULT_MIN_SOURCES))
    section = config.get("content_limits") or {}
    value = section.get("min_sources", DEFAULT_MIN_SOURCES)
    if isinstance(value, (int, float)):
        return int(value)
    return int(DEFAULT_MIN_SOURCES)


def load_min_distinct_owners(config: dict | None = None) -> int:
    """Minimum distinct owners for eligibility, from ``score.min_distinct_owners``.

    Falls back to :data:`DEFAULT_MIN_DISTINCT_OWNERS` when the key is missing or
    malformed. The same value gates the run's post-gather readiness check.
    """
    if config is None:
        return int(DEFAULT_MIN_DISTINCT_OWNERS)
    section = config.get("score") or {}
    value = section.get("min_distinct_owners", DEFAULT_MIN_DISTINCT_OWNERS)
    if isinstance(value, (int, float)):
        return int(value)
    return int(DEFAULT_MIN_DISTINCT_OWNERS)


def _countable_items(cluster_dict: dict) -> list[dict]:
    """Member items that count toward the source floor.

    Items whose feed disabled fetching (``fetchable: false``) and items
    re-attributed to an unconfigured external domain (tier 3) cannot support a
    story, so they never satisfy ``min_sources``.
    """
    countable: list[dict] = []
    for item in cluster_dict.get("items") or []:
        if item.get("fetchable") is False:
            continue
        tier = item.get("tier")
        if tier is not None and str(tier) != "":
            try:
                if int(tier) == 3:
                    continue
            except (TypeError, ValueError):
                pass
        countable.append(item)
    return countable


def source_urls(cluster_dict: dict) -> list[str]:
    """The cluster's distinct, normalized source URLs that count toward eligibility."""
    links = [str(item.get("link") or "") for item in _countable_items(cluster_dict)]
    return cluster._normalized_urls(links)


def source_count(cluster_dict: dict) -> int:
    """How many distinct countable source URLs a cluster has (one-source stories are not eligible)."""
    return len(source_urls(cluster_dict))


def owner_count(cluster_dict: dict) -> int:
    """How many distinct countable owners a cluster has.

    Delegates to :func:`cluster.distinct_owners` so eligibility and the run's
    post-gather readiness check count owners the same way (several posts from one
    publisher count once, and ``fetchable: false``/tier-3 items are ignored).
    """
    return len(cluster.distinct_owners(_countable_items(cluster_dict)))


def filter_excluded_items(
    items: list[dict] | None,
    patterns: Iterable[str] | None = None,
    *,
    on_excluded: Any = None,
) -> list[dict]:
    """Drop feed items whose headline matches a format-exclusion pattern.

    Applied before clustering so a promo headline can never join a story and
    inflate its source count. Logs one line per matched pattern and, when
    ``on_excluded`` is given, calls ``on_excluded(pattern, count)`` for each.
    """
    if patterns is None:
        patterns = load_exclude_title_patterns()
    patterns = list(patterns)
    kept: list[dict] = []
    counts: dict[str, int] = {}
    for item in items or []:
        pattern = cluster.excluded_title_pattern(str(item.get("title") or ""), patterns)
        if pattern is None:
            kept.append(item)
        else:
            counts[pattern] = counts.get(pattern, 0) + 1
    for pattern, count in counts.items():
        logger.info("item exclusion: dropped %d item(s) matching %r", count, pattern)
        if on_excluded is not None:
            on_excluded(pattern, count)
    return kept


def min_score_for(
    cluster_dict: dict,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    min_score_corroborated: float = DEFAULT_MIN_SCORE_CORROBORATED,
) -> float:
    """The score threshold a cluster must reach to be covered.

    A corroborated cluster (two or more distinct owners) uses the lower
    ``min_score_corroborated``; a single-owner or single-source cluster uses
    ``min_score``.
    """
    owners = cluster_dict.get("owners") or []
    if len(set(owners)) >= 2:
        return float(min_score_corroborated)
    return float(min_score)


def _matches_any(text: str, phrases: Iterable[str]) -> bool:
    """True when ``text`` contains a ``phrases`` entry as a whole word/phrase."""
    haystack = str(text or "").lower()
    for phrase in phrases:
        phrase = str(phrase or "").lower().strip()
        if not phrase:
            continue
        if re.search(rf"\b{re.escape(phrase)}\b", haystack):
            return True
    return False


def has_news_signal(text: str, phrases: Iterable[str] | None = None) -> bool:
    """True when a seed headline carries a news signal.

    A signal is a ``phrases`` entry (``score.news_verbs``) as a whole word, or a
    date like "November 18" / "18 Nov" (see :data:`NEWS_DATE_SIGNAL_PATTERN`).
    Several matches still count as one signal, so the bonus is added once.
    """
    if phrases is None:
        _, phrases = load_news_settings()
    if _matches_any(text, phrases or ()):
        return True
    return _NEWS_DATE_SIGNAL_RE.search(str(text or "")) is not None


def relevant_cluster(
    cluster_dict: dict,
    *,
    keywords: list[str] | None = None,
    source_topics: dict[str, str] | None = None,
) -> bool:
    """A cluster is on-topic when a member source is "gaming", or when its seed
    title or named entities contain a relevance keyword as a whole word.

    ``keywords`` and ``source_topics`` default to the current config values.
    An empty keyword list disables the keyword half of the gate (so a missing
    ``relevance`` block never blocks every non-gaming story).
    """
    topics = load_source_topics() if source_topics is None else source_topics
    member_topics: set[str] = set()
    for item in cluster_dict.get("items") or []:
        topic = topics.get(str(item.get("source_name") or ""))
        if topic:
            member_topics.add(topic)
    if "gaming" in member_topics:
        return True
    if keywords is None:
        keywords = load_relevance_keywords()
    if not keywords:
        return True
    title = str(cluster_dict.get("title") or "")
    entities = [str(entity) for entity in (cluster_dict.get("entities") or [])]
    return _matches_any(" ".join([title, *entities]), keywords)


def ineligible_reason(
    cluster_dict: dict,
    exclude_patterns: list[str] | None = None,
    min_sources: int | None = None,
    min_distinct_owners: int | None = None,
) -> str | None:
    """Why a cluster cannot be gathered, or None when it is eligible.

    A cluster is eligible only when it is confirmable (:func:`cluster.confirmable`),
    has at least ``min_sources`` distinct source URLs, is backed by at least
    ``min_distinct_owners`` distinct owners, its headline matches no
    format-exclusion pattern, and it is topically relevant
    (:func:`relevant_cluster`). Reasons are reported verbatim:
    ``"not confirmable"``, ``"too few sources"``, ``"too few independent owners"``,
    ``"excluded pattern: <pattern>"``, or ``"off-topic"``.
    """
    patterns = load_exclude_title_patterns() if exclude_patterns is None else list(exclude_patterns)
    if min_sources is None:
        min_sources = load_min_sources()
    if min_distinct_owners is None:
        min_distinct_owners = load_min_distinct_owners()
    if not cluster.confirmable(cluster_dict):
        return "not confirmable"
    if source_count(cluster_dict) < int(min_sources):
        return "too few sources"
    if owner_count(cluster_dict) < int(min_distinct_owners):
        return "too few independent owners"
    pattern = cluster.excluded_title_pattern(str(cluster_dict.get("title") or ""), patterns)
    if pattern is not None:
        return f"excluded pattern: {pattern!r}"
    if not relevant_cluster(cluster_dict):
        return "off-topic"
    return None


def score_cluster(
    cluster_dict: dict,
    *,
    weights: dict | None = None,
    performance: dict | None = None,
    news_bonus: float | None = None,
    news_verbs: list[str] | None = None,
) -> float:
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    tiers = cluster_dict.get("tiers") or []
    score = 0.0
    if 1 in tiers:
        score += weights["tier1"]
    elif 2 in tiers:
        score += weights["tier2"]

    owners = cluster_dict.get("owners") or []
    score += min(len(owners), weights["max_owners"]) * weights["owner"]

    if cluster.confirmable(cluster_dict):
        score += weights["corroboration"]

    age = cluster_dict.get("age_hours")
    if age is not None:
        if age <= weights["fresh_hours"]:
            score += weights["fresh_bonus"]
        elif age <= weights["day_hours"]:
            score += weights["day_bonus"]
        elif age <= weights["two_day_hours"]:
            score += weights["two_day_bonus"]

    if performance:
        boost = performance.get(guess_category(cluster_dict), 0.0)
        score += max(0.0, min(float(boost), 20.0))

    if news_bonus is None or news_verbs is None:
        default_bonus, default_verbs = load_news_settings()
        if news_bonus is None:
            news_bonus = default_bonus
        if news_verbs is None:
            news_verbs = default_verbs
    if has_news_signal(str(cluster_dict.get("title") or ""), news_verbs or ()):
        score += float(news_bonus or 0.0)
    return float(score)


def rank(
    clusters: Iterable[dict],
    *,
    weights: dict | None = None,
    performance: dict | None = None,
    exclude_patterns: list[str] | None = None,
    relevance_keywords: list[str] | None = None,
    source_topics: dict[str, str] | None = None,
    news_bonus: float | None = None,
    news_verbs: list[str] | None = None,
    min_sources: int | None = None,
    min_distinct_owners: int | None = None,
) -> list[dict]:
    patterns = load_exclude_title_patterns() if exclude_patterns is None else list(exclude_patterns)
    if relevance_keywords is None:
        relevance_keywords = load_relevance_keywords()
    if source_topics is None:
        source_topics = load_source_topics()
    if min_sources is None:
        min_sources = load_min_sources()
    if min_distinct_owners is None:
        min_distinct_owners = load_min_distinct_owners()
    # Resolve the news bonus/phrases once for the whole ranking instead of once
    # per cluster, so score_cluster never re-reads config.yaml inside the loop.
    if news_bonus is None or news_verbs is None:
        default_bonus, default_verbs = load_news_settings()
        if news_bonus is None:
            news_bonus = default_bonus
        if news_verbs is None:
            news_verbs = default_verbs
    scored = []
    for cluster_dict in clusters:
        confirmable = cluster.confirmable(cluster_dict)
        sources = source_count(cluster_dict)
        owners = owner_count(cluster_dict)
        excluded_by = cluster.excluded_title_pattern(
            str(cluster_dict.get("title") or ""), patterns
        )
        relevant = relevant_cluster(
            cluster_dict, keywords=relevance_keywords, source_topics=source_topics
        )
        scored.append(
            {
                **cluster_dict,
                "category": guess_category(cluster_dict),
                "score": score_cluster(
                    cluster_dict,
                    weights=weights,
                    performance=performance,
                    news_bonus=news_bonus,
                    news_verbs=news_verbs,
                ),
                "confirmable": confirmable,
                "source_count": sources,
                "owner_count": owners,
                "excluded_by": excluded_by,
                "relevant": relevant,
                "eligible": (
                    confirmable and sources >= int(min_sources)
                    and owners >= int(min_distinct_owners)
                    and excluded_by is None and relevant
                ),
            }
        )
    scored.sort(key=lambda cluster_dict: cluster_dict["score"], reverse=True)
    return scored


def pick(
    clusters: Iterable[dict],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    min_score_corroborated: float | None = None,
    weights: dict | None = None,
    performance: dict | None = None,
    exclude_patterns: list[str] | None = None,
    relevance_keywords: list[str] | None = None,
    source_topics: dict[str, str] | None = None,
    news_bonus: float | None = None,
    news_verbs: list[str] | None = None,
    min_sources: int | None = None,
    min_distinct_owners: int | None = None,
) -> dict | None:
    """Return the highest-scoring eligible cluster that reaches its threshold.

    Each eligible cluster is checked against :func:`min_score_for`: a
    corroborated cluster (two or more distinct owners) needs only
    ``min_score_corroborated`` (``score.min_score_corroborated``, default 55),
    while a single-owner cluster needs ``min_score`` (default 65). Clusters that
    are not confirmable (no tier-1 source and fewer than two distinct owners),
    have fewer than ``content_limits.min_sources`` distinct source URLs, whose
    headline matches a format-exclusion pattern, or that are off-topic are never
    considered, so ineligible stories are never gathered or sent to a model.
    """
    if min_score_corroborated is None:
        min_score_corroborated = load_min_score_corroborated()
    ranked = rank(
        clusters,
        weights=weights,
        performance=performance,
        exclude_patterns=exclude_patterns,
        relevance_keywords=relevance_keywords,
        source_topics=source_topics,
        news_bonus=news_bonus,
        news_verbs=news_verbs,
        min_sources=min_sources,
        min_distinct_owners=min_distinct_owners,
    )
    eligible = [cluster for cluster in ranked if cluster.get("eligible")]
    if not eligible:
        logger.info("no eligible (confirmable, sourced, relevant) story among %d cluster(s)", len(ranked))
        return None
    for best in eligible:
        threshold = min_score_for(
            best, min_score=min_score, min_score_corroborated=min_score_corroborated
        )
        if best["score"] >= threshold:
            logger.info(
                "selected story %s (score=%.0f, threshold=%.0f, category=%s)",
                best["id"], best["score"], threshold, best["category"],
            )
            return best
    top = eligible[0]
    logger.info(
        "top story scored %.0f, below its threshold; nothing to cover", top["score"]
    )
    return None


def pick_candidates(
    clusters: Iterable[dict],
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES_PER_RUN,
    max_candidates_per_entity: int | None = DEFAULT_MAX_CANDIDATES_PER_ENTITY,
    min_score: float = DEFAULT_MIN_SCORE,
    min_score_corroborated: float | None = None,
    weights: dict | None = None,
    performance: dict | None = None,
    exclude_patterns: list[str] | None = None,
    relevance_keywords: list[str] | None = None,
    source_topics: dict[str, str] | None = None,
    news_bonus: float | None = None,
    news_verbs: list[str] | None = None,
    min_sources: int | None = None,
    min_distinct_owners: int | None = None,
) -> list[dict]:
    """Rank once, then walk the ranked list for up to ``max_candidates`` stories.

    Equivalent to calling :func:`pick` repeatedly on the shrinking set — eligible
    clusters are visited in descending score order and each must reach its own
    :func:`min_score_for` threshold — but :func:`rank` runs a single time for the
    whole run instead of once per candidate.

    Diversity: at most ``max_candidates_per_entity`` candidates may share one
    lead entity (:func:`cluster.lead_entity`, the first non-umbrella primary
    entity), so three GTA 6 clusters fill only two slots. Clusters with no lead
    entity are never capped against each other. Order is otherwise unchanged.
    """
    if min_score_corroborated is None:
        min_score_corroborated = load_min_score_corroborated()
    ranked = rank(
        clusters,
        weights=weights,
        performance=performance,
        exclude_patterns=exclude_patterns,
        relevance_keywords=relevance_keywords,
        source_topics=source_topics,
        news_bonus=news_bonus,
        news_verbs=news_verbs,
        min_sources=min_sources,
        min_distinct_owners=min_distinct_owners,
    )
    eligible = [cluster_dict for cluster_dict in ranked if cluster_dict.get("eligible")]
    if not eligible:
        logger.info(
            "no eligible (confirmable, sourced, relevant) story among %d cluster(s)",
            len(ranked),
        )
        return []
    per_entity_cap = (
        None
        if max_candidates_per_entity is None
        else max(1, int(max_candidates_per_entity))
    )
    picked: list[dict] = []
    entity_counts: dict[str, int] = {}
    for best in eligible:
        if len(picked) >= int(max_candidates):
            break
        key = _lead_entity_key(best)
        if per_entity_cap is not None and key and entity_counts.get(key, 0) >= per_entity_cap:
            logger.info(
                "skipping story %s: lead entity %r already has %d candidate(s)",
                best["id"],
                key,
                entity_counts[key],
            )
            continue
        threshold = min_score_for(
            best, min_score=min_score, min_score_corroborated=min_score_corroborated
        )
        if best["score"] >= threshold:
            logger.info(
                "selected story %s (score=%.0f, threshold=%.0f, category=%s)",
                best["id"], best["score"], threshold, best["category"],
            )
            picked.append(best)
            if key:
                entity_counts[key] = entity_counts.get(key, 0) + 1
    if not picked:
        top = eligible[0]
        logger.info(
            "top story scored %.0f, below its threshold; nothing to cover", top["score"]
        )
    return picked


def _lead_entity_key(cluster_dict: dict) -> str:
    """The diversity key for one cluster: its lead (non-umbrella) entity.

    Prefers the seed title, so a GTA 6 story keys on "gta 6" while an Xbox Labs
    story keys on "xbox labs" and an Asha Sharma story on "asha sharma". Falls
    back to the cluster's own primary entities when there is no title.
    """
    title = str(cluster_dict.get("title") or "")
    if title:
        lead = cluster.lead_entity(title)
        if lead:
            return lead
    umbrella = cluster.umbrella_entities()
    for entity in cluster_dict.get("entities") or []:
        key = cluster.normalize_title(str(entity))
        if key and key not in umbrella:
            return key
    return ""


__all__ = [
    "CATEGORY_KEYWORDS",
    "DEFAULT_CATEGORY",
    "DEFAULT_EXCLUDE_TITLE_PATTERNS",
    "DEFAULT_MAX_CANDIDATES_PER_RUN",
    "DEFAULT_MAX_CANDIDATES_PER_ENTITY",
    "DEFAULT_MIN_DISTINCT_OWNERS",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_MIN_SCORE_CORROBORATED",
    "DEFAULT_MIN_SOURCES",
    "DEFAULT_MODEL_CALL_BUDGET",
    "DEFAULT_NEWS_BONUS",
    "DEFAULT_NEWS_VERBS",
    "DEFAULT_RELEVANCE_KEYWORDS",
    "DEFAULT_WEIGHTS",
    "NEWS_DATE_SIGNAL_PATTERN",
    "filter_excluded_items",
    "guess_category",
    "has_news_signal",
    "ineligible_reason",
    "load_exclude_title_patterns",
    "load_min_distinct_owners",
    "load_min_score_corroborated",
    "load_min_sources",
    "load_news_settings",
    "load_performance",
    "load_relevance_keywords",
    "load_source_topics",
    "min_score_for",
    "owner_count",
    "pick",
    "pick_candidates",
    "rank",
    "relevant_cluster",
    "score_cluster",
    "source_count",
    "source_urls",
]
