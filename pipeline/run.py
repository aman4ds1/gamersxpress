"""End-to-end pipeline orchestration.

One run does, in order: pause/cap checks, ingest (merged into
``data/pool.json``), cluster, score, gather, facts, write, verify, SEO, internal
links, cover, publish gates and publish. At most one story is covered per run.

A single :class:`~providers.RunState` is created per run and threaded through
every stage that calls ``providers.generate`` (facts, writer, verifier, SEO), so
the writer's model family is recorded once and the verifier's fail-closed guard
can force a different family.

Safety (PLAN.md section 11):

* ``PAUSED=true`` or an open ``data/breaker.json`` stops the run (exit 0) until a
  human resets it.
* ``DAILY_CAP`` (environment, else ``safety.daily_cap``) stops the run once that
  many articles have been published today.
* The circuit breaker opens after ``safety.breaker_threshold`` consecutive
  failures; ``data/reports/issue.md`` records the last failures. Skips and
  "nothing to cover" runs do not count.

Exit codes: ``0`` published / nothing-to-cover / paused, ``1`` configuration
error, ``2`` skipped (provider rate limit/outage, or no different-family
verifier available), ``3`` failed run (publish gate, verification, or write/SEO
failure). The breaker increments on ``3`` only, resets to 0 on a successful
publish, and is untouched by skips and "nothing to cover" runs.

``python -m pipeline.run`` runs the real pipeline; ``python -m pipeline.run
--dry-run [--dry-run-scenario pass|fail-verify|fail-gate]`` runs entirely on
sample data and mock providers inside a temp directory, never touching ``src/``
or the real ``data/`` state.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Support `python -m pipeline.run` (namespace package): make sibling modules
# importable by their top-level names before any of them are imported.
_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import cluster
import cover as cover_stage
import dryrun as dryrun_stage
import facts as facts_stage
import gates as gates_stage
import gather as gather_stage
import ingest as ingest_stage
import link as link_stage
import publish as publish_stage
import score as score_stage
import seo as seo_stage
import verify as verify_stage
import write as write_stage
from providers import (
    PROJECT_ROOT,
    Generation,
    MissingWriterFamilyError,
    RunState,
    SkipRun,
    generate as default_generate,
    load_config,
    load_env,
)

logger = logging.getLogger("gamersxpress.pipeline")

CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_BREAKER_PATH = PROJECT_ROOT / "data" / "breaker.json"
DEFAULT_LAST_RUN_PATH = PROJECT_ROOT / "data" / "reports" / "last-run.json"

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_SKIP = 2
EXIT_FAIL = 3

_STATUS_EXIT = {
    "completed": EXIT_OK,
    "nothing": EXIT_OK,
    "paused": EXIT_OK,
    "skipped": EXIT_SKIP,
}


@dataclass
class Paths:
    """Every file and directory a run reads or writes (redirected for dry runs)."""

    pool: Path
    seen: Path
    breaker: Path
    log: Path
    reports_dir: Path
    facts_dir: Path
    gathered_dir: Path
    articles_dir: Path
    covers_dir: Path
    drafts_dir: Path
    entities: Path
    fonts_dir: Path
    page_template: Path
    checks_dir: Path
    performance: Path
    candidates_dir: Path
    feed_cache: Path
    feed_health: Path

    @property
    def last_run(self) -> Path:
        return self.reports_dir / "last-run.json"

    @property
    def issue(self) -> Path:
        return self.reports_dir / "issue.md"


@dataclass
class RunResult:
    status: str
    run_state: RunState
    error: str | None = None
    report: dict = field(default_factory=dict)
    exit_code: int = EXIT_OK


def default_paths(dry_run: bool = False, dry_run_dir: str | Path | None = None) -> Paths:
    """Real project paths, or an isolated temp tree for a dry run."""
    if not dry_run:
        data = PROJECT_ROOT / "data"
        return Paths(
            pool=data / "pool.json",
            seen=data / "seen.json",
            breaker=data / "breaker.json",
            log=data / "published-log.json",
            reports_dir=data / "reports",
            facts_dir=data / "facts",
            gathered_dir=data / "gathered",
            articles_dir=PROJECT_ROOT / "src" / "content" / "articles",
            covers_dir=PROJECT_ROOT / "public" / "covers",
            drafts_dir=PROJECT_ROOT / "drafts",
            entities=data / "entities.json",
            fonts_dir=PROJECT_ROOT / "assets" / "fonts",
            page_template=PROJECT_ROOT / "src" / "pages" / "news" / "[id].astro",
            checks_dir=PROJECT_ROOT / "checks",
            performance=data / "topic-performance.json",
            candidates_dir=data / "candidates",
            feed_cache=data / "feed-cache.json",
            feed_health=data / "reports" / "feed-health.json",
        )

    base = Path(dry_run_dir) if dry_run_dir else Path(tempfile.mkdtemp(prefix="gx-dryrun-"))
    base.mkdir(parents=True, exist_ok=True)
    entities = base / "entities.json"
    if not entities.exists():
        source = PROJECT_ROOT / "data" / "entities.json"
        if source.is_file():
            shutil.copyfile(source, entities)
    return Paths(
        pool=base / "pool.json",
        seen=base / "seen.json",
        breaker=base / "breaker.json",
        log=base / "published-log.json",
        reports_dir=base / "reports",
        facts_dir=base / "facts",
        gathered_dir=base / "gathered",
        articles_dir=base / "articles",
        covers_dir=base / "covers",
        drafts_dir=base / "drafts",
        entities=entities,
        fonts_dir=PROJECT_ROOT / "assets" / "fonts",
        page_template=PROJECT_ROOT / "src" / "pages" / "news" / "[id].astro",
        checks_dir=PROJECT_ROOT / "checks",
        performance=base / "topic-performance.json",
        candidates_dir=base / "candidates",
        feed_cache=base / "feed-cache.json",
        feed_health=base / "reports" / "feed-health.json",
    )


# --- breaker -----------------------------------------------------------------


def load_breaker(path: str | Path = DEFAULT_BREAKER_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("consecutive_failures", 0)
    data.setdefault("last_failure_date", None)
    data.setdefault("open", False)
    data.setdefault("recent_failures", [])
    return data


def save_breaker(breaker: dict, path: str | Path = DEFAULT_BREAKER_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(breaker, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def record_failure(breaker: dict, message: str, *, now: dt.datetime, threshold: int) -> dict:
    breaker["consecutive_failures"] = int(breaker.get("consecutive_failures", 0)) + 1
    breaker["last_failure_date"] = now.date().isoformat()
    recent = list(breaker.get("recent_failures") or [])
    recent.append(message)
    breaker["recent_failures"] = recent[-3:]
    if breaker["consecutive_failures"] >= threshold:
        breaker["open"] = True
    return breaker


def reset_breaker(breaker: dict) -> dict:
    breaker["consecutive_failures"] = 0
    breaker["open"] = False
    breaker["recent_failures"] = []
    return breaker


# --- helpers -----------------------------------------------------------------


def _env_flag(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_yaml(path: str | Path | None) -> dict:
    import yaml

    target = Path(path) if path else _PIPELINE_DIR / "config.yaml"
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _body_of(article: str) -> str:
    _front, body = gates_stage.parse_front_matter(article)
    return body


def _article_records(articles_dir: Path) -> list[dict]:
    records: list[dict] = []
    if not articles_dir.is_dir():
        return records
    for path in sorted(articles_dir.glob("*.md")):
        front, _body = gates_stage.parse_front_matter(path.read_text(encoding="utf-8"))
        records.append(
            {
                "id": path.stem,
                "title": str(front.get("title") or ""),
                "category": str(front.get("category") or ""),
                "pubDate": front.get("pubDate"),
                "entities": [str(e) for e in front.get("entities") or []],
            }
        )
    return records


# --- the run -----------------------------------------------------------------


def run(
    *,
    config_path: str | Path | None = None,
    paths: Paths | None = None,
    now: dt.datetime | None = None,
    env: dict | None = None,
    dry_run: bool = False,
    scenario: str = "pass",
    dry_run_dir: str | Path | None = None,
    generate: Callable[..., Generation] | None = None,
    ingest_fn: Callable[..., dict] | None = None,
    gather_fn: Callable[..., dict] | None = None,
    facts_fn: Callable[..., dict] | None = None,
    write_fn: Callable[..., Generation] | None = None,
    verify_fn: Callable[..., Any] | None = None,
    seo_fn: Callable[..., dict] | None = None,
    link_fn: Callable[..., Any] | None = None,
    cover_fn: Callable[..., dict] | None = None,
    gates_fn: Callable[..., Any] | None = None,
    performance: dict | None = None,
    link_checker: Callable[[str], Any] | None = None,
    site_checks: Callable[[str, str], tuple[bool, str]] | None = None,
) -> RunResult:
    """Run the whole pipeline once and return a :class:`RunResult`."""
    now = now or dt.datetime.now(dt.timezone.utc)
    env = env if env is not None else load_env()
    raw_config = _load_yaml(config_path)
    safety = raw_config.get("safety") or {}
    score_section = raw_config.get("score") or {}
    threshold = int(safety.get("breaker_threshold", CIRCUIT_BREAKER_THRESHOLD))
    daily_cap = int(env.get("DAILY_CAP") or safety.get("daily_cap", 4))
    modes = tuple(safety.get("publish_modes") or publish_stage.PUBLISH_MODES)
    publish_mode = str(env.get("PUBLISH_MODE", "draft"))
    gates_config = gates_stage.load_gates_config(config_path)
    verify_config = verify_stage.load_verify_config(config_path)

    paths = paths or default_paths(dry_run, dry_run_dir)
    run_state = RunState()
    breaker = load_breaker(paths.breaker)
    stale_after = None
    if not dry_run:
        _windows, stale_after = ingest_stage.load_freshness(config_path)

    if generate is None:
        generate = dryrun_stage.make_generate(scenario) if dry_run else default_generate
    if dry_run and link_checker is None:
        link_checker = lambda url: 200  # offline dry runs never touch the network
    ingest_fn = ingest_fn or ingest_stage.run
    gather_fn = gather_fn or gather_stage.gather
    facts_fn = facts_fn or facts_stage.facts
    write_fn = write_fn or write_stage.write
    verify_fn = verify_fn or verify_stage.verify
    seo_fn = seo_fn or seo_stage.generate_seo
    link_fn = link_fn or link_stage.add_links
    cover_fn = cover_fn or cover_stage.generate_cover
    gates_fn = gates_fn or gates_stage.run_gates
    if performance is None:
        performance = score_stage.load_performance(paths.performance)

    def finish(status: str, error: str | None, *, exit_code: int | None = None, **extra: Any) -> RunResult:
        code = _STATUS_EXIT.get(status, EXIT_CONFIG) if exit_code is None else exit_code
        report = {
            "status": status,
            "error": error,
            "exit_code": code,
            "generated_at": now.isoformat(),
            "breaker": dict(breaker),
            **run_state.report(),
            **extra,
        }
        publish_stage.write_last_run(report, paths.last_run)
        return RunResult(status, run_state, error, report, code)

    if publish_mode not in modes:
        logger.error("PUBLISH_MODE must be one of %s, got %r", modes, publish_mode)
        return finish("failed", f"PUBLISH_MODE must be one of {modes}, got {publish_mode!r}", exit_code=EXIT_CONFIG)

    if _env_flag(env.get("PAUSED")) or breaker.get("open"):
        logger.warning("run paused (PAUSED=%s, breaker.open=%s)", env.get("PAUSED"), breaker.get("open"))
        return finish("paused", "paused by PAUSED or an open circuit breaker")

    published_today = publish_stage.count_today(publish_stage.load_log(paths.log), now=now)
    if published_today >= daily_cap:
        logger.warning("daily cap reached (%d/%d)", published_today, daily_cap)
        return finish("paused", f"daily cap reached ({published_today}/{daily_cap})")

    try:
        if dry_run:
            items = dryrun_stage.sample_pool(now).get("items") or []
        else:
            ingested = ingest_fn(
                config_path,
                output_dir=paths.candidates_dir,
                cache_path=paths.feed_cache,
                health_path=paths.feed_health,
            )
            items = ingested.get("items") or []

        pool = cluster.load_pool(paths.pool)
        cluster.merge_items(pool, items, now=now, max_age=stale_after or cluster.DEFAULT_STALE_AFTER)
        cluster.save_pool(pool, now=now, path=paths.pool)

        seen = cluster.load_seen(paths.seen)
        clusters = cluster.cluster_items(pool.get("items"), now=now, seen=seen)
        if not clusters:
            return finish("nothing", "no new stories to cover")
        chosen = score_stage.pick(
            clusters,
            min_score=float(score_section.get("min_score", score_stage.DEFAULT_MIN_SCORE)),
            weights=score_section.get("weights"),
            performance=performance,
        )
        if chosen is None:
            return finish("nothing", "no story scored high enough")
        cluster_id = chosen["id"]

        if dry_run:
            gathered = dryrun_stage.sample_gathered(cluster_id, now)
        else:
            gathered = gather_fn(chosen, output_dir=paths.gathered_dir, id=cluster_id)

        facts = facts_fn(
            gathered, id=cluster_id, output_dir=paths.facts_dir,
            generate=generate, run_state=run_state, now=now,
        )

        writer = write_fn(facts, run_state=run_state, generate=generate)

        verify_report = verify_fn(
            writer.value, facts, run_state=run_state, generate=generate,
            report_dir=paths.reports_dir, id=cluster_id,
        )

        provisional_slug = seo_stage.slugify(chosen.get("title") or cluster_id)
        if not verify_report.passed:
            return _fail(
                self_finish=finish, breaker=breaker, paths=paths, threshold=threshold, now=now,
                status="failed", exit_code=EXIT_FAIL,
                error="verification failed",
                slug=provisional_slug, cluster_id=cluster_id, chosen=chosen,
                article=writer.value, cover_path=None,
                failures=list(verify_report.unsupported) + list(verify_report.unmatched_numbers) or ["verify failed"],
            )

        seo = seo_fn(
            writer.value, facts, run_state=run_state, generate=generate,
            entities_path=paths.entities, articles_dir=paths.articles_dir,
            drafts_dir=paths.drafts_dir, config=verify_config,
        )
        slug = seo["slug"]

        cover_staging = Path(tempfile.mkdtemp(prefix="gx-cover-"))
        try:
            cover = cover_fn(
                slug, seo["title"], seo["category"],
                output_dir=cover_staging, fonts_dir=paths.fonts_dir,
            )

            index = link_stage.build_index(_article_records(paths.articles_dir))
            linked = link_fn(writer.value, index, self_id=slug, now=now)
            body = _body_of(linked.article)
            front = publish_stage.build_front_matter(facts, seo, cover)
            article = publish_stage.assemble_article(body, front)

            gate_result = gates_fn(
                article, slug=slug, facts=facts, cluster_id=cluster_id, gathered=gathered,
                verify_report=verify_report.to_dict(), run_state=run_state, config=gates_config,
                articles_dir=paths.articles_dir, page_template=paths.page_template, now=now,
                link_checker=link_checker, site_checks=site_checks,
            )
            if not gate_result.passed:
                return _fail(
                    self_finish=finish, breaker=breaker, paths=paths, threshold=threshold, now=now,
                    status="failed", exit_code=EXIT_FAIL, error="publish gates failed",
                    slug=slug, cluster_id=cluster_id, chosen=chosen,
                    article=article, cover_path=cover.get("path"), failures=gate_result.failures,
                    gates=gate_result.to_dict(),
                )

            result = publish_stage.publish(
                article, slug=slug, title=seo["title"], category=seo["category"], mode=publish_mode,
                cover_path=cover.get("path"), cluster_id=cluster_id,
                articles_dir=paths.articles_dir, covers_dir=paths.covers_dir,
                log_path=paths.log, seen_path=paths.seen, now=now,
            )
        finally:
            shutil.rmtree(cover_staging, ignore_errors=True)

        reset_breaker(breaker)
        save_breaker(breaker, paths.breaker)
        logger.info("run completed: slug=%s mode=%s", slug, publish_mode)
        return finish(
            "completed", None, slug=slug, cluster_id=cluster_id, category=seo["category"],
            mode=publish_mode, article_path=result["article_path"], score=chosen.get("score"),
            gates=gate_result.to_dict(),
        )

    except SkipRun as exc:
        save_breaker(breaker, paths.breaker)
        logger.warning("run skipped: %s", exc)
        return finish("skipped", str(exc))
    except (facts_stage.UnconfirmedStory, facts_stage.FactsError) as exc:
        save_breaker(breaker, paths.breaker)
        logger.warning("story dropped: %s", exc)
        return finish("nothing", str(exc))
    except MissingWriterFamilyError as exc:
        return _fail(
            self_finish=finish, breaker=breaker, paths=paths, threshold=threshold, now=now,
            status="failed", exit_code=EXIT_FAIL, error=str(exc),
        )
    except (seo_stage.SeoError, write_stage.WriterInputError) as exc:
        return _fail(
            self_finish=finish, breaker=breaker, paths=paths, threshold=threshold, now=now,
            status="failed", exit_code=EXIT_FAIL, error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - record and fail closed
        logger.exception("unexpected pipeline failure")
        return _fail(
            self_finish=finish, breaker=breaker, paths=paths, threshold=threshold, now=now,
            status="failed", exit_code=EXIT_FAIL, error=f"{type(exc).__name__}: {exc}",
        )


def _fail(
    *,
    self_finish: Callable[..., RunResult],
    breaker: dict,
    paths: Paths,
    threshold: int,
    now: dt.datetime,
    status: str,
    exit_code: int,
    error: str,
    slug: str | None = None,
    cluster_id: str | None = None,
    chosen: dict | None = None,
    article: str | None = None,
    cover_path: str | Path | None = None,
    failures: list[str] | None = None,
    **extra: Any,
) -> RunResult:
    if article is not None and slug:
        publish_stage.save_draft(
            article, slug=slug, cover_path=cover_path, drafts_dir=paths.drafts_dir,
            failures=failures,
        )
    record_failure(breaker, f"{error}: {slug or cluster_id or 'run'}", now=now, threshold=threshold)
    save_breaker(breaker, paths.breaker)
    if breaker.get("open"):
        publish_stage.write_issue(breaker, paths.issue, now=now)
    return self_finish(
        status, error, exit_code=exit_code,
        slug=slug, cluster_id=cluster_id, category=(chosen or {}).get("category"),
        failures=failures or [], **extra,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.run", description="Run the GamersXpress pipeline once.")
    parser.add_argument("--dry-run", action="store_true", help="use sample data and mock providers")
    parser.add_argument("--dry-run-scenario", choices=dryrun_stage.SCENARIOS, default="pass")
    parser.add_argument("--dry-run-dir", default=None, help="directory for dry-run state (default: a temp dir)")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    result = run(
        config_path=args.config,
        dry_run=args.dry_run,
        scenario=args.dry_run_scenario,
        dry_run_dir=args.dry_run_dir,
    )
    print(f"run {result.status} (exit {result.exit_code})" + (f": {result.error}" if result.error else ""))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
