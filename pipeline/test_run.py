import datetime as dt
import json

import pytest

import dryrun
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
    state = tmp_path / "state"
    drafts = list((state / "drafts").glob("*/article.md"))
    assert drafts, "expected a draft after verify failure"
    assert not list((state / "articles").glob("*.md"))
    breaker = json.loads((state / "breaker.json").read_text(encoding="utf-8"))
    assert breaker["consecutive_failures"] == 1


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
    for _ in range(3):
        result = run_once(tmp_path, dry_run=True, scenario="fail-gate")
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
    for _ in range(2):
        run_once(tmp_path, dry_run=True, scenario="fail-gate")
    result = run_once(tmp_path, dry_run=True, scenario="pass")
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