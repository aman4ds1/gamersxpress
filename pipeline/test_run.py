import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

import dryrun
import providers
import publish
import run as run_module
import score

NOW = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)
STUB_CHECKS = lambda text, slug: (True, "stub")  # noqa: E731


def item(title, link, *, owner, tier=1, age_hours=1):
    """An ingest item in the same shape :func:`dryrun.sample_pool` emits."""
    return {
        "title": title,
        "link": link,
        "source_name": f"{owner} outlet",
        "tier": tier,
        "owner": owner,
        "region": "us",
        "published": (NOW - dt.timedelta(hours=age_hours)).isoformat(),
        "age_hours": float(age_hours),
    }


def ingest_fn_for(items):
    """Stub ingest stage that returns ``items`` instead of running feeds."""

    def ingest_fn(config_path=None, output_dir=None, cache_path=None, health_path=None):
        return {"items": items}

    return ingest_fn


def make_paths(tmp_path):
    return run_module.default_paths(dry_run=True, dry_run_dir=str(tmp_path / "state"))


def run_once(tmp_path, **overrides):
    kwargs = dict(
        paths=make_paths(tmp_path),
        now=NOW,
        env={},
        link_checker=lambda url: 200,
        site_checks=STUB_CHECKS,
    )
    kwargs.update(overrides)
    return run_module.run(**kwargs)


def test_pass_scenario_completes_and_publishes(tmp_path):
    result = run_once(tmp_path, dry_run=True, scenario="pass")
    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.run_state.writer_family == "google"
    assert result.run_state.verifier_family == "mistral"
    assert result.report["slug"] == dryrun.SAMPLE_SLUG

    state = tmp_path / "state"
    article_path = state / "articles" / f"{dryrun.SAMPLE_SLUG}.md"
    assert article_path.is_file()
    text = article_path.read_text(encoding="utf-8")
    assert dryrun.SAMPLE_TITLE.lower() in text.lower()
    assert "/covers/" in text

    log = publish.load_log(state / "published-log.json")
    assert len(log["articles"]) == 1
    assert log["articles"][0]["slug"] == dryrun.SAMPLE_SLUG

    seen = json.loads((state / "seen.json").read_text(encoding="utf-8"))
    assert result.report["cluster_id"] in seen["clusters"]

    breaker = json.loads((state / "breaker.json").read_text(encoding="utf-8"))
    assert breaker["consecutive_failures"] == 0
    assert breaker["open"] is False


def test_fail_verify_writes_draft_and_counts_failure(tmp_path):
    result = run_once(tmp_path, dry_run=True, scenario="fail-verify")
    assert result.status == "failed"
    assert result.exit_code == 3
    assert "article_rejected" in result.error  # dry-run verifier rejects, it does not fail to produce output
    assert result.report["error"] == result.error
    state = tmp_path / "state"
    drafts = list((state / "drafts").glob("*/article.md"))
    assert drafts, "expected a draft after verify failure"
    assert not list((state / "articles").glob("*.md"))
    breaker = json.loads((state / "breaker.json").read_text(encoding="utf-8"))
    assert breaker["consecutive_failures"] == 1
    assert any("article_rejected" in entry for entry in breaker["recent_failures"])


def test_fail_gate_exits_3_and_increments_breaker(tmp_path):
    result = run_once(tmp_path, dry_run=True, scenario="fail-gate")
    assert result.status == "failed"
    assert result.exit_code == 3
    assert "publish gates failed" in result.error
    state = tmp_path / "state"
    drafts = list((state / "drafts").glob("*/article.md"))
    assert drafts and len(drafts) == 1
    assert not list((state / "articles").glob("*.md"))
    breaker = json.loads((state / "breaker.json").read_text(encoding="utf-8"))
    assert breaker["consecutive_failures"] == 1


def test_breaker_opens_after_three_gate_failures_and_pauses(tmp_path):
    for index in range(3):
        # Advance past the 24h failed-story cooldown so the same story is retried.
        result = run_once(tmp_path, dry_run=True, scenario="fail-gate", now=NOW + dt.timedelta(hours=25 * index))
    assert result.status == "failed"
    assert result.exit_code == 3
    breaker = json.loads((tmp_path / "state" / "breaker.json").read_text(encoding="utf-8"))
    assert breaker["open"] is True
    assert breaker["consecutive_failures"] == 3
    issue = tmp_path / "state" / "reports" / "issue.md"
    assert issue.is_file()
    issue_text = issue.read_text(encoding="utf-8")
    assert "circuit breaker" in issue_text.lower()
    assert dryrun.SAMPLE_SLUG in issue_text

    paused = run_once(tmp_path, dry_run=True, scenario="fail-gate")
    assert paused.status == "paused"
    assert paused.exit_code == 0
    breaker = json.loads((tmp_path / "state" / "breaker.json").read_text(encoding="utf-8"))
    assert breaker["consecutive_failures"] == 3


def test_paused_env_stops_run_without_writing_state(tmp_path):
    result = run_once(tmp_path, dry_run=True, env={"PAUSED": "true"})
    assert result.status == "paused"
    assert result.exit_code == 0
    state = tmp_path / "state"
    assert not (state / "pool.json").exists()
    assert not (state / "breaker.json").exists()


def test_daily_cap_stops_run(tmp_path):
    paths = make_paths(tmp_path)
    today = NOW.date().isoformat()
    publish.save_log(
        {"articles": [
            {"published_at": f"{today}T08:00:00+00:00"},
            {"published_at": f"{today}T09:00:00+00:00"},
        ]},
        paths.log,
    )
    result = run_module.run(paths=paths, now=NOW, env={"DAILY_CAP": "2"}, dry_run=True,
                            link_checker=lambda url: 200, site_checks=STUB_CHECKS)
    assert result.status == "paused"
    assert "daily cap" in result.error


def test_invalid_publish_mode_is_config_error(tmp_path):
    result = run_once(tmp_path, dry_run=True, env={"PUBLISH_MODE": "live"})
    assert result.status == "failed"
    assert result.exit_code == 1
    assert "PUBLISH_MODE" in result.error


def test_breaker_resets_on_success(tmp_path):
    for index in range(2):
        # Advance past the 24h failed-story cooldown so the same story is retried.
        run_once(tmp_path, dry_run=True, scenario="fail-gate", now=NOW + dt.timedelta(hours=25 * index))
    result = run_once(tmp_path, dry_run=True, scenario="pass", now=NOW + dt.timedelta(hours=50))
    assert result.status == "completed"
    breaker = json.loads((tmp_path / "state" / "breaker.json").read_text(encoding="utf-8"))
    assert breaker["consecutive_failures"] == 0
    assert breaker["open"] is False


def test_rate_limit_skips_with_exit_2_and_leaves_breaker_unchanged(tmp_path):
    paths = make_paths(tmp_path)
    run_module.save_breaker(
        {"consecutive_failures": 2, "last_failure_date": None, "open": False, "recent_failures": []},
        paths.breaker,
    )

    def rate_limited(*_args, **_kwargs):
        raise run_module.SkipRun("provider rate limited")

    result = run_once(tmp_path, dry_run=True, generate=rate_limited)
    assert result.status == "skipped"
    assert result.exit_code == 2
    assert "provider rate limited" in result.error
    breaker = json.loads(paths.breaker.read_text(encoding="utf-8"))
    assert breaker["consecutive_failures"] == 2
    assert breaker["open"] is False
    assert not list((tmp_path / "state" / "articles").glob("*.md"))


def test_unknown_writer_family_exits_3_and_increments_breaker(tmp_path):
    paths = make_paths(tmp_path)
    run_module.save_breaker(
        {"consecutive_failures": 1, "last_failure_date": None, "open": False, "recent_failures": []},
        paths.breaker,
    )

    def unknown_family(*_args, **_kwargs):
        raise run_module.MissingWriterFamilyError("no writer family is recorded on RunState")

    result = run_once(tmp_path, dry_run=True, generate=unknown_family)
    assert result.status == "failed"
    assert result.exit_code == 3
    assert "no writer family" in result.error
    breaker = json.loads(paths.breaker.read_text(encoding="utf-8"))
    assert breaker["consecutive_failures"] == 2
    assert breaker["open"] is False


def test_nothing_to_cover_when_cluster_seen(tmp_path):
    paths = make_paths(tmp_path)
    cluster_id = run_module.cluster.cluster_id_for_urls(["https://a.example/1", "https://b.example/1"])
    run_module.cluster.save_seen(
        {
            "clusters": {
                cluster_id: {
                    "first_seen": NOW.isoformat(),
                    "last_seen": NOW.isoformat(),
                    "member_urls": ["https://a.example/1", "https://b.example/1"],
                }
            }
        },
        paths.seen,
    )
    result = run_module.run(paths=paths, now=NOW, env={}, dry_run=True,
                            link_checker=lambda url: 200, site_checks=STUB_CHECKS)
    assert result.status == "nothing"
    assert result.exit_code == 0
    assert not list((tmp_path / "state" / "articles").glob("*.md"))


def test_score_and_cluster_stages_write_pool(tmp_path):
    paths = make_paths(tmp_path)
    run_module.run(paths=paths, now=NOW, env={}, dry_run=True, scenario="pass",
                   link_checker=lambda url: 200, site_checks=STUB_CHECKS)
    pool = run_module.cluster.load_pool(paths.pool)
    assert len(pool["items"]) == 2
    assert pool["updated_at"] == NOW.isoformat()


def test_main_dry_run_cli_returns_zero():
    assert run_module.main(["--dry-run", "--dry-run-scenario", "pass"]) == 0


# --- failed-story tracking ---------------------------------------------------


def failed_entries(paths):
    if not paths.failed.exists():
        return {}
    return json.loads(paths.failed.read_text(encoding="utf-8"))["stories"]


def test_gate_failure_counts_one_attempt(tmp_path):
    paths = make_paths(tmp_path)
    run_module.run(paths=paths, now=NOW, env={}, dry_run=True, scenario="fail-gate",
                   link_checker=lambda url: 200, site_checks=STUB_CHECKS)
    entries = failed_entries(paths)
    assert len(entries) == 1
    entry = next(iter(entries.values()))
    assert entry["attempt_count"] == 1
    assert entry["failure_reason"] == "gate failure"
    assert entry["last_attempt"] == NOW.isoformat()


def test_verify_rejection_counts_article_rejected(tmp_path):
    paths = make_paths(tmp_path)
    run_module.run(paths=paths, now=NOW, env={}, dry_run=True, scenario="fail-verify",
                   link_checker=lambda url: 200, site_checks=STUB_CHECKS)
    entry = next(iter(failed_entries(paths).values()))
    assert entry["attempt_count"] == 1
    assert entry["failure_reason"] == "article_rejected"


def test_provider_outage_does_not_count_as_attempt(tmp_path):
    paths = make_paths(tmp_path)

    def outage(*_args, **_kwargs):
        raise run_module.SkipRun("provider outage")

    result = run_module.run(paths=paths, now=NOW, env={}, dry_run=True, generate=outage,
                            link_checker=lambda url: 200, site_checks=STUB_CHECKS)
    assert result.status == "skipped"
    assert failed_entries(paths) == {}


def test_dropped_story_does_not_count_as_attempt(tmp_path):
    paths = make_paths(tmp_path)

    def no_facts(*_args, **_kwargs):
        raise run_module.facts_stage.UnconfirmedStory("no confirmed facts")

    result = run_module.run(paths=paths, now=NOW, env={}, dry_run=True, facts_fn=no_facts,
                            link_checker=lambda url: 200, site_checks=STUB_CHECKS)
    assert result.status == "nothing"
    assert failed_entries(paths) == {}


def test_later_pass_removes_story_from_failed_list(tmp_path):
    paths = make_paths(tmp_path)
    run_module.run(paths=paths, now=NOW, env={}, dry_run=True, scenario="fail-gate",
                   link_checker=lambda url: 200, site_checks=STUB_CHECKS)
    assert failed_entries(paths)
    run_module.run(paths=paths, now=NOW, env={}, dry_run=True, scenario="pass",
                   link_checker=lambda url: 200, site_checks=STUB_CHECKS)
    assert failed_entries(paths) == {}


# --- candidate search ---------------------------------------------------------


def run_with_items(tmp_path, items, *, now=None, config_path=None, **overrides):
    kwargs = dict(
        paths=make_paths(tmp_path),
        now=now or NOW,
        env={},
        ingest_fn=ingest_fn_for(items),
        gather_fn=lambda chosen, output_dir=None, id=None: dryrun.sample_gathered(id, now or NOW),
        generate=dryrun.make_generate("pass"),
        link_checker=lambda url: 200,
        site_checks=STUB_CHECKS,
    )
    if config_path is not None:
        kwargs["config_path"] = str(config_path)
    kwargs.update(overrides)
    return run_module.run(**kwargs)


def latest_candidates_report(paths):
    files = sorted(paths.reports_dir.glob("candidates-*.json"))
    assert files, "expected a candidates report under data/reports"
    return json.loads(files[-1].read_text(encoding="utf-8"))


def test_unconfirmed_first_then_confirmed_publishes_second(tmp_path):
    paths = make_paths(tmp_path)
    first = [
        item("Call of Duty game returns to a modern campaign", "https://a.example/1", owner="owner-a"),
        item("Call of Duty game returns to a modern campaign", "https://a.example/2", owner="owner-b"),
    ]
    second = [
        # Older than the first story so the CoD cluster (fresh, itself two-tier-1)
        # is picked first and its failure is what the loop records; the sample is
        # still fresh enough to publish once the first candidate is dropped.
        item(dryrun.SAMPLE_TITLE, "https://b.example/1", owner="owner-c", age_hours=12),
        item(dryrun.SAMPLE_TITLE, "https://b.example/2", owner="owner-d", age_hours=12),
    ]

    def facts_fn(gathered, *, id, output_dir, generate, run_state, now, title, entities, **kwargs):
        if str(title).startswith("Call of Duty"):
            raise run_module.facts_stage.UnconfirmedStory(f"single voice: {title}")
        return run_module.facts_stage.facts(
            gathered, id=id, output_dir=output_dir, generate=generate,
            run_state=run_state, now=now, title=title, entities=entities,
        )

    result = run_with_items(
        tmp_path, first + second,
        facts_fn=facts_fn,
    )
    assert result.status == "completed"
    assert result.exit_code == 0
    assert list(paths.articles_dir.glob("*.md"))

    # The unconfirmed candidate is recorded keyed by its member-URL hash, not
    # as a writer/failed story attempt.
    failed = json.loads(paths.failed.read_text(encoding="utf-8"))
    assert failed["stories"] == {}
    first_id = run_module.cluster.cluster_id_for_urls(["https://a.example/1", "https://a.example/2"])
    assert first_id in failed["unconfirmed"]
    assert failed["unconfirmed"][first_id]["reason"] == "unconfirmed"
    assert failed["unconfirmed"][first_id]["last_attempt"] == NOW.isoformat()

    report = latest_candidates_report(paths)
    assert report["outcome"] == "story chosen"
    assert [c["title"] for c in report["candidates"]] == [
        first[0]["title"], second[0]["title"],
    ]
    assert report["candidates"][0]["confirmed"] is False
    assert report["candidates"][0]["reason"] == "unconfirmed"
    assert report["candidates"][1]["confirmed"] is True
    assert report["candidates"][1]["reason"] is None


def test_all_candidates_unconfirmed_is_nothing_without_breaker_change(tmp_path):
    paths = make_paths(tmp_path)
    items = [
        item("Gears of War game film casting announced", "https://c.example/1", owner="owner-c1", age_hours=1),
        item("Gears of War game film casting announced", "https://c.example/4", owner="owner-c4", age_hours=1),
        item("The Legend of Zelda game movie adds a director", "https://c.example/2", owner="owner-c2", age_hours=2),
        item("The Legend of Zelda game movie adds a director", "https://c.example/5", owner="owner-c5", age_hours=2),
        item("Rocket League introduces a new game mode", "https://c.example/3", owner="owner-c3", age_hours=3),
        item("Rocket League introduces a new game mode", "https://c.example/6", owner="owner-c6", age_hours=3),
    ]

    def always_unconfirmed(gathered, *, id=None, output_dir=None, generate=None,
                           run_state=None, now=None, title=None, entities=None, **kwargs):
        raise run_module.facts_stage.UnconfirmedStory(f"not confirmed: {title}")

    result = run_with_items(tmp_path, items, facts_fn=always_unconfirmed)
    assert result.status == "nothing"
    assert result.exit_code == 0
    assert not list(paths.articles_dir.glob("*.md"))

    # Nothing-to-cover never touches the breaker.
    if paths.breaker.exists():
        breaker = json.loads(paths.breaker.read_text(encoding="utf-8"))
        assert breaker["consecutive_failures"] == 0
        assert breaker["open"] is False

    failed = json.loads(paths.failed.read_text(encoding="utf-8"))
    assert len(failed["unconfirmed"]) == 3
    assert all(entry["reason"] == "unconfirmed" for entry in failed["unconfirmed"].values())
    assert failed["stories"] == {}

    report = latest_candidates_report(paths)
    assert report["outcome"] == "no confirmed story"
    assert len(report["candidates"]) == 3
    assert all(c["confirmed"] is False for c in report["candidates"])
    assert all(c["reason"] == "unconfirmed" for c in report["candidates"])


def test_unconfirmed_cluster_skipped_until_membership_changes(tmp_path):
    paths = make_paths(tmp_path)
    base = [
        item("Nintendo Switch 2 passes 20 million sold", "https://n.example/1", owner="owner-n1"),
        item("Nintendo Switch 2 passes 20 million sold", "https://n.example/2", owner="owner-n2"),
    ]
    calls = {"gather": 0, "facts": 0}

    def gather_fn(chosen, *, output_dir=None, id=None):
        calls["gather"] += 1
        return dryrun.sample_gathered(id, NOW)

    def always_unconfirmed(gathered, *, id=None, output_dir=None, generate=None,
                           run_state=None, now=None, title=None, entities=None, **kwargs):
        calls["facts"] += 1
        raise run_module.facts_stage.UnconfirmedStory(f"not confirmed: {title}")

    def run_ingest(items, now=NOW):
        return run_with_items(tmp_path, items, now=now, gather_fn=gather_fn,
                              facts_fn=always_unconfirmed)

    result = run_ingest(base)
    assert result.status == "nothing"
    first_id = run_module.cluster.cluster_id_for_urls(["https://n.example/1", "https://n.example/2"])
    failed = json.loads(paths.failed.read_text(encoding="utf-8"))
    assert first_id in failed["unconfirmed"]
    assert calls["gather"] == 1

    # Same membership within the failed cooldown: skipped, no model call.
    result = run_ingest(base, now=NOW + dt.timedelta(hours=1))
    assert result.status == "nothing"
    assert calls["gather"] == 1, "an unchanged unconfirmed cluster must not be retried"

    # A new member changes the membership hash, so the story is re-evaluated.
    grew = base + [item("Nintendo Switch 2 passes 20 million sold", "https://n.example/3", owner="owner-n3")]
    result = run_ingest(grew, now=NOW + dt.timedelta(hours=2))
    assert result.status == "nothing"
    assert calls["gather"] == 2
    second_id = run_module.cluster.cluster_id_for_urls(
        ["https://n.example/1", "https://n.example/2", "https://n.example/3"]
    )
    assert second_id != first_id
    failed = json.loads(paths.failed.read_text(encoding="utf-8"))
    assert first_id in failed["unconfirmed"]
    assert second_id in failed["unconfirmed"]


def test_model_call_budget_stops_candidate_attempts(tmp_path):
    paths = make_paths(tmp_path)
    config_path = tmp_path / "config.yaml"
    source_config = Path(run_module.__file__).resolve().parent / "config.yaml"
    config = yaml.safe_load(source_config.read_text(encoding="utf-8"))
    config.setdefault("score", {})["model_call_budget"] = 1
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    items = [
        item("Halo returns to Xbox Game Pass", "https://h.example/1", owner="owner-h1", age_hours=1),
        item("A new Titanfall game campaign is in development", "https://t.example/1", owner="owner-t1", age_hours=2),
        item("Halo returns to Xbox Game Pass", "https://h.example/2", owner="owner-h2", age_hours=1),
        item("A new Titanfall game campaign is in development", "https://t.example/2", owner="owner-t2", age_hours=2),
    ]
    calls = {"gather": 0}

    def gather_fn(chosen, *, output_dir=None, id=None):
        calls["gather"] += 1
        return dryrun.sample_gathered(id, NOW)

    def unconfirmed_after_one_call(gathered, *, id=None, output_dir=None, generate=None,
                                   run_state=None, now=None, title=None, entities=None, **kwargs):
        generation = providers.Generation("fast", "mock", "mock-fast", "google", "{}")
        (run_state or providers.RunState()).record(generation)
        raise run_module.facts_stage.UnconfirmedStory(f"not confirmed: {title}")

    result = run_with_items(
        tmp_path, items, config_path=config_path,
        gather_fn=gather_fn, facts_fn=unconfirmed_after_one_call,
    )
    assert result.status == "nothing"
    assert result.exit_code == 0
    assert calls["gather"] == 1, "the second candidate must not be gathered once the budget is spent"

    failed = json.loads(paths.failed.read_text(encoding="utf-8"))
    assert len(failed["unconfirmed"]) == 1, "only the first candidate was attempted"

    report = latest_candidates_report(paths)
    assert report["outcome"] == "no confirmed story"
    assert [c["reason"] for c in report["candidates"]] == ["unconfirmed", "model budget spent"]
    assert report["candidates"][0]["model_calls"] == 1
    assert report["candidates"][0]["confirmed"] is False
    assert report["candidates"][1]["confirmed"] is None
    assert report["candidates"][1]["model_calls"] == 1
    assert report["candidates"][1]["title"] == items[1]["title"]


def test_corroborated_cluster_below_min_score_is_tried(tmp_path):
    paths = make_paths(tmp_path)
    # Two tier-2 owners, 48h old: 15 + 2*10 (owners) + 10 (corroboration)
    # + 10 (two-day) = 55. That clears min_score_corroborated (55) but sits
    # below the 65 min_score a single-source story would need.
    items = [
        item("A quiet game story from two outlets", "https://p.example/1",
             owner="owner-p1", tier=2, age_hours=48),
        item("A quiet game story from two outlets", "https://p.example/2",
             owner="owner-p2", tier=2, age_hours=48),
    ]
    calls = {"gather": 0}

    def gather_fn(chosen, *, output_dir=None, id=None):
        calls["gather"] += 1
        return dryrun.sample_gathered(id, NOW)

    def always_unconfirmed(gathered, *, id=None, output_dir=None, generate=None,
                           run_state=None, now=None, title=None, entities=None, **kwargs):
        raise run_module.facts_stage.UnconfirmedStory(f"not confirmed: {title}")

    result = run_with_items(tmp_path, items, gather_fn=gather_fn, facts_fn=always_unconfirmed)

    assert result.status == "nothing"
    assert calls["gather"] == 1, "a corroborated cluster scoring 55 must be tried"
    report = latest_candidates_report(paths)
    assert [c["title"] for c in report["candidates"]] == [items[0]["title"]]
    assert report["candidates"][0]["score"] == 55
    assert report["candidates"][0]["owners"] == ["owner-p1", "owner-p2"]


def test_single_source_below_min_score_is_not_tried(tmp_path):
    paths = make_paths(tmp_path)
    # One tier-1 source, 50h old: 40 + 10 (owner) + 10 (corroboration) = 60,
    # below the 65 a single-source story needs, so it is never gathered.
    items = [
        item("A quiet game story from one outlet", "https://s.example/1",
             owner="owner-s1", age_hours=50),
    ]
    calls = {"gather": 0}

    def gather_fn(chosen, *, output_dir=None, id=None):
        calls["gather"] += 1
        return dryrun.sample_gathered(id, NOW)

    result = run_with_items(tmp_path, items, gather_fn=gather_fn)

    assert result.status == "nothing"
    assert result.exit_code == 0
    assert calls["gather"] == 0, "a single-source story scoring 60 must not be tried"
    assert not list(paths.reports_dir.glob("candidates-*.json"))


def test_only_below_threshold_clusters_is_nothing_to_cover(tmp_path):
    paths = make_paths(tmp_path)
    items = [
        item("A quiet game story from one outlet", "https://s.example/1",
             owner="owner-s1", age_hours=50),
        item("Rocket League introduces a new game mode", "https://s.example/2",
             owner="owner-s2", age_hours=52),
    ]
    calls = {"gather": 0}

    def gather_fn(chosen, *, output_dir=None, id=None):
        calls["gather"] += 1
        return dryrun.sample_gathered(id, NOW)

    result = run_with_items(tmp_path, items, gather_fn=gather_fn)

    assert result.status == "nothing"
    assert result.exit_code == 0
    assert result.error == "no story scored high enough"
    assert calls["gather"] == 0
    assert not list(paths.articles_dir.glob("*.md"))


# --- one repair attempt ------------------------------------------------------

UNSUPPORTED = "Nvidia will ship the card next month."
NEW_CLAIM = "The card includes a free game bundle."


def verdict_json(*unsupported):
    return json.dumps({
        "clauses": [
            {"clause": clause, "fact_ids": [], "match": "none", "difference": "not in the sheet"}
            for clause in unsupported
        ],
        "unsupported_claims": [],
        "rumors_stated_as_fact": [],
        "unsupported_regional": [],
        "costs_described_as_received": [],
        "invented_labels": [],
        "category_claims": [],
    })


def repair_generate(writer_outputs, verifier_outputs, calls):
    """Stub generate that returns the nth writer/verifier output, recording prompts."""

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        if role == "fast":
            properties = (json_schema or {}).get("properties", {})
            value = (
                dryrun._claims_json(dryrun._prompt_source_url(prompt))
                if "claims" in properties
                else dryrun._seo_json()
            )
            family = "google"
        elif role == "writer":
            value = writer_outputs[min(calls["writer"], len(writer_outputs) - 1)]
            calls["writer"] += 1
            calls["writer_prompts"].append(prompt)
            family = "google"
        elif role == "verifier":
            value = verifier_outputs[min(calls["verifier"], len(verifier_outputs) - 1)]
            calls["verifier"] += 1
            family = "mistral"
        else:
            raise AssertionError(f"unexpected role {role!r}")
        generation = providers.Generation(role, "mock", f"mock-{role}", family, value)
        if run_state is not None:
            run_state.record(generation)
        return generation

    return generate


def repair_calls():
    return {"writer": 0, "verifier": 0, "writer_prompts": []}


def verify_report_file(paths, result):
    return json.loads(
        (paths.reports_dir / f"verify-{result.report['cluster_id']}.json").read_text(encoding="utf-8")
    )


def test_repair_removing_flagged_clause_passes_and_is_recorded(tmp_path):
    paths = make_paths(tmp_path)
    original = dryrun.sample_body() + f"\n\n{UNSUPPORTED}\n"
    repaired = dryrun.sample_body()
    calls = repair_calls()
    generate = repair_generate([original, repaired], [verdict_json(UNSUPPORTED), verdict_json()], calls)

    result = run_module.run(paths=paths, now=NOW, env={}, dry_run=True, generate=generate,
                            link_checker=lambda url: 200, site_checks=STUB_CHECKS)

    assert result.status == "completed"
    assert result.exit_code == 0
    assert calls["writer"] == 2, "one original writer call plus exactly one repair"
    assert calls["verifier"] == 2

    slug = result.report["slug"]
    drafts = paths.drafts_dir / slug
    assert (drafts / "original.md").read_text(encoding="utf-8") == original
    assert (drafts / "repaired.md").read_text(encoding="utf-8") == repaired
    assert UNSUPPORTED in (drafts / "repair-clauses.txt").read_text(encoding="utf-8")

    report = verify_report_file(paths, result)
    assert report["repair"]["attempted"] is True
    assert UNSUPPORTED in report["repair"]["changed_clauses"]
    assert report["repair"]["original_chars"] == len(original)
    assert report["repair"]["repaired_chars"] == len(repaired)

    assert UNSUPPORTED in calls["writer_prompts"][1], "repair prompt names the flagged clause"
    assert repaired in calls["writer_prompts"][1], "repair prompt carries the draft article"


def test_repair_adding_new_unsupported_claim_still_fails(tmp_path):
    paths = make_paths(tmp_path)
    original = dryrun.sample_body() + f"\n\n{UNSUPPORTED}\n"
    repaired = dryrun.sample_body() + f"\n\n{NEW_CLAIM}\n"
    calls = repair_calls()
    generate = repair_generate(
        [original, repaired], [verdict_json(UNSUPPORTED), verdict_json(NEW_CLAIM)], calls,
    )

    result = run_module.run(paths=paths, now=NOW, env={}, dry_run=True, generate=generate,
                            link_checker=lambda url: 200, site_checks=STUB_CHECKS)

    assert result.status == "failed"
    assert result.exit_code == 3
    assert calls["writer"] == 2

    drafts = paths.drafts_dir / result.report["slug"]
    assert (drafts / "article.md").read_text(encoding="utf-8") == repaired
    assert NEW_CLAIM in result.report["failures"][0]


def test_no_second_repair_when_repaired_article_still_fails(tmp_path):
    paths = make_paths(tmp_path)
    original = dryrun.sample_body() + f"\n\n{UNSUPPORTED}\n"
    calls = repair_calls()
    generate = repair_generate(
        [original, original], [verdict_json(UNSUPPORTED), verdict_json(UNSUPPORTED)], calls,
    )

    result = run_module.run(paths=paths, now=NOW, env={}, dry_run=True, generate=generate,
                            link_checker=lambda url: 200, site_checks=STUB_CHECKS)

    assert result.status == "failed"
    assert calls["writer"] == 2, "a failing repair is never repaired a second time"
    assert calls["verifier"] == 2


def test_verifier_output_invalid_does_not_trigger_a_repair(tmp_path):
    paths = make_paths(tmp_path)
    calls = repair_calls()
    generate = repair_generate(
        [dryrun.sample_body() + f"\n\n{UNSUPPORTED}\n"], ["not json at all"], calls,
    )

    result = run_module.run(paths=paths, now=NOW, env={}, dry_run=True, generate=generate,
                            link_checker=lambda url: 200, site_checks=STUB_CHECKS)

    assert result.status == "failed"
    assert "verifier_output_invalid" in result.error
    assert calls["writer"] == 1
    assert calls["verifier"] == 1

    drafts = paths.drafts_dir / result.report["slug"]
    assert not (drafts / "original.md").exists()
    assert not (drafts / "repaired.md").exists()
    assert verify_report_file(paths, result)["repair"] is None


# --- readiness gate (enough contributing sources and usable claims) ----------


def test_confirmed_but_not_ready_tries_the_next_candidate_and_publishes_it(tmp_path):
    paths = make_paths(tmp_path)
    first = [
        item("Call of Duty game returns to a modern campaign", "https://a.example/1", owner="owner-a"),
        item("Call of Duty game returns to a modern campaign", "https://a.example/2", owner="owner-b"),
    ]
    second = [
        item(dryrun.SAMPLE_TITLE, "https://b.example/1", owner="owner-c", age_hours=12),
        item(dryrun.SAMPLE_TITLE, "https://b.example/2", owner="owner-d", age_hours=12),
    ]

    def facts_fn(gathered, *, id, output_dir, generate, run_state, now, title, entities, **kwargs):
        if str(title).startswith("Call of Duty"):
            # Confirmed (it does not raise) but only one contributing source, so
            # the writer must never see it and the search moves on.
            return {"sources": [{"link": "https://a.example/1"}], "claims": []}
        return run_module.facts_stage.facts(
            gathered, id=id, output_dir=output_dir, generate=generate,
            run_state=run_state, now=now, title=title, entities=entities,
        )

    result = run_with_items(tmp_path, first + second, facts_fn=facts_fn)
    assert result.status == "completed"
    assert result.exit_code == 0
    assert list(paths.articles_dir.glob("*.md"))

    first_id = run_module.cluster.cluster_id_for_urls(["https://a.example/1", "https://a.example/2"])
    failed = json.loads(paths.failed.read_text(encoding="utf-8"))
    assert failed["stories"] == {}
    assert failed["unconfirmed"][first_id]["reason"] == "insufficient sources"

    report = latest_candidates_report(paths)
    assert report["outcome"] == "story chosen"
    assert [c["title"] for c in report["candidates"]] == [first[0]["title"], second[0]["title"]]
    assert report["candidates"][0]["confirmed"] is True
    assert report["candidates"][0]["reason"] == "insufficient sources"
    assert report["candidates"][0]["outcome"] == "not ready"
    assert report["candidates"][1]["confirmed"] is True
    assert report["candidates"][1]["outcome"] == "selected"

    breaker = json.loads(paths.breaker.read_text(encoding="utf-8"))
    assert breaker["consecutive_failures"] == 0
    assert breaker["open"] is False


def test_not_ready_candidate_does_not_change_the_breaker(tmp_path):
    paths = make_paths(tmp_path)
    items = [
        item("Halo returns to Xbox Game Pass", "https://h.example/1", owner="owner-h1"),
        item("Halo returns to Xbox Game Pass", "https://h.example/2", owner="owner-h2"),
    ]

    def facts_fn(gathered, *, id, output_dir, generate, run_state, now, title, entities, **kwargs):
        return {"sources": [{"link": "https://h.example/1"}], "claims": []}

    result = run_with_items(tmp_path, items, facts_fn=facts_fn)
    assert result.status == "nothing"
    assert result.exit_code == 0
    assert not list(paths.articles_dir.glob("*.md"))

    if paths.breaker.exists():
        breaker = json.loads(paths.breaker.read_text(encoding="utf-8"))
        assert breaker["consecutive_failures"] == 0
        assert breaker["open"] is False

    failed = json.loads(paths.failed.read_text(encoding="utf-8"))
    assert len(failed["unconfirmed"]) == 1
    assert next(iter(failed["unconfirmed"].values()))["reason"] == "insufficient sources"

    report = latest_candidates_report(paths)
    assert report["outcome"] == "no confirmed story"
    assert report["candidates"][0]["confirmed"] is True
    assert report["candidates"][0]["reason"] == "insufficient sources"


def test_confirmed_but_too_few_usable_facts_is_not_ready(tmp_path):
    paths = make_paths(tmp_path)
    items = [
        item("Halo returns to Xbox Game Pass", "https://h.example/1", owner="owner-h1"),
        item("Halo returns to Xbox Game Pass", "https://h.example/2", owner="owner-h2"),
    ]

    def facts_fn(gathered, *, id, output_dir, generate, run_state, now, title, entities, **kwargs):
        # Two contributing sources but only one writer-ready claim (an opinion
        # never counts): not ready, so no article is written.
        return {
            "sources": [{"link": "https://h.example/1"}, {"link": "https://h.example/2"}],
            "claims": [{"kind": "confirmed"}, {"kind": "opinion"}],
        }

    result = run_with_items(tmp_path, items, facts_fn=facts_fn)
    assert result.status == "nothing"
    assert not list(paths.articles_dir.glob("*.md"))
    failed = json.loads(paths.failed.read_text(encoding="utf-8"))
    assert next(iter(failed["unconfirmed"].values()))["reason"] == "too few facts"
