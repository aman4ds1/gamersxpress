"""Publish stage: write the finished article, cover, log and seen state.

On a clean pass the article Markdown goes to
``src/content/articles/<slug>.md`` and the cover to ``public/covers/<slug>.webp``;
the run is appended to ``data/published-log.json`` and the story is marked seen
in ``data/seen.json``. When a gate fails nothing is published -- the draft and
its cover are written under ``drafts/<slug>/`` instead, so a human can inspect
them.

The same files are written in both publish modes: ``PUBLISH_MODE`` only decides
whether the surrounding workflow commits to ``main`` or opens a pull request, so
this module records the mode and leaves git to the workflow (PLAN.md section 12).

``pubDate`` and the sources come from the facts sheet; the title, description,
category, tags and alt text come from the SEO stage; the cover fields come from
the cover stage. No model output is written verbatim into HTML meta tags.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTICLES_DIR = PROJECT_ROOT / "src" / "content" / "articles"
DEFAULT_COVERS_DIR = PROJECT_ROOT / "public" / "covers"
DEFAULT_DRAFTS_DIR = PROJECT_ROOT / "drafts"
DEFAULT_LOG_PATH = PROJECT_ROOT / "data" / "published-log.json"
DEFAULT_SEEN_PATH = PROJECT_ROOT / "data" / "seen.json"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "data" / "reports"
DEFAULT_LAST_RUN_PATH = DEFAULT_REPORTS_DIR / "last-run.json"
DEFAULT_ISSUE_PATH = DEFAULT_REPORTS_DIR / "issue.md"

PUBLISH_MODES = ("draft", "auto")

logger = logging.getLogger("gamersxpress.pipeline.publish")


def build_front_matter(
    facts: dict,
    seo: dict,
    cover: dict | None = None,
    *,
    pub_date: Any = None,
) -> dict:
    """Assemble the article front matter from facts + SEO + cover."""
    date = pub_date or _generated_date(facts)
    front: dict[str, Any] = {
        "title": seo["title"],
        "description": seo["description"],
        "pubDate": date,
        "category": seo.get("category"),
        "tags": list(seo.get("tags") or []),
        "entities": list(seo.get("entities") or []),
        "sources": [
            {"name": source.get("source_name", "?"), "url": source.get("link", "")}
            for source in facts.get("sources") or []
        ],
    }
    if cover and cover.get("image"):
        front["image"] = cover["image"]
        front["imageAlt"] = cover.get("imageAlt")
    return front


def _generated_date(facts: dict) -> dt.date:
    raw = facts.get("generated_at")
    if raw:
        try:
            return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return dt.datetime.now(dt.timezone.utc).date()


def _dump_front(front: dict) -> str:
    """Render ordered front-matter YAML with indented list items.

    Astro accepts either style, but the repo's own ``checks/check-articles.mjs``
    parser requires list items on indented lines (``  - name: ...``), so lists
    are indented explicitly here.
    """
    parts: list[str] = []
    for key, value in front.items():
        if isinstance(value, list):
            raw = yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True)
            head, _, body = raw.partition("\n")
            parts.append(head)
            parts.extend("  " + line for line in body.splitlines())
        else:
            parts.append(yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True).strip())
    return "\n".join(parts) + "\n"


def assemble_article(body: str, front: dict) -> str:
    """Render the final Markdown: YAML front matter followed by the body."""
    header = _dump_front(front)
    return f"---\n{header}---\n{body.lstrip(chr(10)).rstrip()}\n"


# --- published log -----------------------------------------------------------


def load_log(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_LOG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"articles": []}
    if not isinstance(data, dict):
        return {"articles": []}
    data.setdefault("articles", [])
    return data


def save_log(log: dict, path: str | Path | None = None) -> Path:
    path = Path(path) if path else DEFAULT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def count_today(log: dict, *, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    return sum(1 for entry in log.get("articles") or [] if str(entry.get("published_at") or "").startswith(today))


def append_log(
    entry: dict,
    *,
    path: str | Path | None = None,
    now: dt.datetime | None = None,
) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    log = load_log(path)
    log["articles"].append({"published_at": now.isoformat(), **entry})
    save_log(log, path)
    return log


# --- publishing --------------------------------------------------------------


def publish(
    article: str,
    *,
    slug: str,
    title: str,
    category: str,
    mode: str,
    cover_path: str | Path | None = None,
    cluster_id: str | None = None,
    cluster: dict | None = None,
    articles_dir: str | Path | None = None,
    covers_dir: str | Path | None = None,
    log_path: str | Path | None = None,
    seen_path: str | Path | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Write the article, cover, published-log entry and seen marker."""
    if mode not in PUBLISH_MODES:
        raise ValueError(f"publish mode must be one of {PUBLISH_MODES}, got {mode!r}")

    articles_dir = Path(articles_dir) if articles_dir else DEFAULT_ARTICLES_DIR
    covers_dir = Path(covers_dir) if covers_dir else DEFAULT_COVERS_DIR
    articles_dir.mkdir(parents=True, exist_ok=True)
    article_path = articles_dir / f"{slug}.md"
    article_path.write_text(article, encoding="utf-8")

    destination_cover = None
    if cover_path:
        covers_dir.mkdir(parents=True, exist_ok=True)
        destination_cover = covers_dir / f"{slug}.webp"
        shutil.copyfile(cover_path, destination_cover)

    append_log(
        {"slug": slug, "title": title, "category": category, "mode": mode, "cluster_id": cluster_id},
        path=log_path,
        now=now,
    )
    if cluster_id:
        _mark_seen(cluster_id, seen_path, now, cluster_data=cluster or {})

    logger.info("published slug=%s mode=%s -> %s", slug, mode, article_path)
    return {
        "published": True,
        "slug": slug,
        "article_path": str(article_path),
        "cover_path": str(destination_cover) if destination_cover else None,
        "mode": mode,
    }


def save_draft(
    article: str,
    *,
    slug: str,
    cover_path: str | Path | None = None,
    drafts_dir: str | Path | None = None,
    failures: list[str] | None = None,
) -> dict:
    """Write a blocked article and its cover under ``drafts/<slug>/``."""
    drafts_dir = Path(drafts_dir) if drafts_dir else DEFAULT_DRAFTS_DIR
    target = drafts_dir / slug
    target.mkdir(parents=True, exist_ok=True)
    article_path = target / "article.md"
    article_path.write_text(article, encoding="utf-8")
    if cover_path:
        shutil.copyfile(cover_path, target / f"{slug}.webp")
    if failures:
        (target / "failures.txt").write_text("\n".join(failures) + "\n", encoding="utf-8")
    logger.warning("gate failure: draft kept at %s (%d failure(s))", article_path, len(failures or []))
    return {"published": False, "slug": slug, "draft_path": str(article_path)}


def save_repair(
    original: str,
    repaired: str,
    *,
    slug: str,
    drafts_dir: str | Path | None = None,
    changed_clauses: list[str] | None = None,
) -> dict:
    """Keep the original and repaired drafts under ``drafts/<slug>/`` for review."""
    drafts_dir = Path(drafts_dir) if drafts_dir else DEFAULT_DRAFTS_DIR
    target = drafts_dir / slug
    target.mkdir(parents=True, exist_ok=True)
    original_path = target / "original.md"
    repaired_path = target / "repaired.md"
    original_path.write_text(original, encoding="utf-8")
    repaired_path.write_text(repaired, encoding="utf-8")
    if changed_clauses:
        (target / "repair-clauses.txt").write_text(
            "\n".join(changed_clauses) + "\n", encoding="utf-8"
        )
    logger.warning("repair drafts kept at %s and %s", original_path, repaired_path)
    return {"original_path": str(original_path), "repaired_path": str(repaired_path)}


def _mark_seen(cluster_id: str, seen_path: str | Path | None, now: dt.datetime | None, *, cluster_data: dict) -> None:
    import cluster

    seen = cluster.load_seen(seen_path)
    items = cluster_data.get("items") or []
    cluster.mark_seen(
        seen,
        cluster_id,
        now=now,
        member_urls=[str(item.get("link") or "") for item in items] if items else None,
        primary_entities=cluster_data.get("entities"),
        story_type=cluster_data.get("story_type"),
    )
    cluster.save_seen(seen, seen_path)


# --- reports -----------------------------------------------------------------


def write_last_run(report: dict, path: str | Path | None = None) -> Path:
    path = Path(path) if path else DEFAULT_LAST_RUN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return path


def write_issue(breaker: dict, path: str | Path | None = None, *, now: dt.datetime | None = None) -> Path:
    """Write the human-readable breaker issue listing the last failures."""
    path = Path(path) if path else DEFAULT_ISSUE_PATH
    now = now or dt.datetime.now(dt.timezone.utc)
    failures = list(breaker.get("recent_failures") or [])
    lines = [
        "# Pipeline paused: circuit breaker open",
        "",
        f"- Opened: {now.isoformat()}",
        f"- Consecutive failures: {breaker.get('consecutive_failures')}",
        f"- Last failure: {breaker.get('last_failure_date')}",
        "",
        "The pipeline stopped after repeated failures. Review the last failures below,",
        "fix the cause, then set `open` to `false` in `data/breaker.json` and reset",
        "`consecutive_failures` to `0`.",
        "",
        "## Recent failures",
        "",
    ]
    lines.extend(f"- {failure}" for failure in failures) if failures else lines.append("- (none recorded)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


__all__ = [
    "DEFAULT_ARTICLES_DIR",
    "DEFAULT_COVERS_DIR",
    "DEFAULT_DRAFTS_DIR",
    "DEFAULT_ISSUE_PATH",
    "DEFAULT_LAST_RUN_PATH",
    "DEFAULT_LOG_PATH",
    "DEFAULT_SEEN_PATH",
    "PUBLISH_MODES",
    "append_log",
    "assemble_article",
    "build_front_matter",
    "count_today",
    "load_log",
    "publish",
    "save_draft",
    "save_repair",
    "save_log",
    "write_issue",
    "write_last_run",
]
