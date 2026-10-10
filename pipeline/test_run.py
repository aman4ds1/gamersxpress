import datetime as dt
import json

import pytest

import dryrun
import providers
import publish
import run as run_module
import score

NOW = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)
STUB_CHECKS = lambda text, slug: (True, "stub")  # noqa: E731


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


# --- one repair attempt ------------------------------------------------------

UNSUPPORTED = "Nvidia will ship the card next month."
NEW_CLAIM = "The card includes a free game bundle."


def verdict_json(*unsupported):
    return json.dumps({
        "clauses": [{"clause": clause, "supported": False} for clause in unsupported],
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
            value = dryrun._claims_json() if "claims" in properties else dryrun._seo_json()
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
