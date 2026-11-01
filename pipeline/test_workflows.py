"""Safety rules for the GitHub workflows, checked on the YAML itself.

These cannot prove a workflow works (only a real run can), but they keep the
rules that protect the repository from being edited away by accident: one run at
a time, saved state never overwritten after a failed restore, secrets only where
they are needed, no script injection, no pull_request_target.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
NAMES = ["pipeline", "site-check", "deploy"]


def load(name):
    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8"))


def triggers(doc):
    # PyYAML reads the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True))


def steps_of(doc, job):
    return doc["jobs"][job]["steps"]


def step_named(doc, job, fragment):
    matches = [s for s in steps_of(doc, job) if fragment.lower() in str(s.get("name", "")).lower()]
    assert len(matches) == 1, f"expected one step matching {fragment!r}, found {len(matches)}"
    return matches[0]


def all_steps(doc):
    for job in doc["jobs"].values():
        yield from job["steps"]


@pytest.mark.parametrize("name", NAMES)
def test_the_workflow_file_parses_and_every_job_has_a_timeout(name):
    doc = load(name)
    assert doc["jobs"]
    for job_id, job in doc["jobs"].items():
        assert "timeout-minutes" in job, f"{name}/{job_id} has no timeout"


@pytest.mark.parametrize("name", NAMES)
def test_no_workflow_uses_pull_request_target(name):
    assert "pull_request_target" not in (triggers(load(name)) or {})


@pytest.mark.parametrize("name", NAMES)
def test_every_action_is_pinned_to_a_version(name):
    for step in all_steps(load(name)):
        uses = step.get("uses")
        if uses:
            assert "@" in uses and uses.rsplit("@", 1)[1] not in ("main", "master", "latest"), uses


@pytest.mark.parametrize("name", NAMES)
def test_permissions_are_declared_and_never_write_all(name):
    permissions = load(name).get("permissions")
    assert isinstance(permissions, dict)
    assert "write-all" not in str(permissions)


@pytest.mark.parametrize("name", NAMES)
def test_no_script_interpolates_an_expression_directly(name):
    # Anything that can carry model or user text (step outputs, event data) must
    # reach a script through an env var, never inside the script text.
    for step in all_steps(load(name)):
        run = step.get("run")
        if run:
            assert "${{" not in run, f"expression inside a run script: {run[:80]!r}"


# --- pipeline.yml -------------------------------------------------------------


def test_the_pipeline_runs_on_a_schedule_and_by_hand():
    on = triggers(load("pipeline"))
    assert on["schedule"][0]["cron"]
    assert len(on["schedule"][0]["cron"].split()) == 5
    assert "workflow_dispatch" in on


def test_only_one_pipeline_run_at_a_time_and_none_is_cancelled():
    concurrency = load("pipeline")["concurrency"]
    assert concurrency["group"] == "pipeline"
    assert concurrency["cancel-in-progress"] is False


def test_the_pipeline_refuses_any_mode_but_draft():
    doc = load("pipeline")
    guard = step_named(doc, "run", "draft mode")
    assert "draft" in guard["run"] and "exit 1" in guard["run"]
    # The guard comes before anything else runs.
    assert steps_of(doc, "run")[0] is guard


def test_the_kill_switch_variable_stops_the_job():
    assert "vars.PAUSED" in load("pipeline")["jobs"]["run"]["if"]


def test_state_is_saved_even_after_a_failed_run_but_never_after_a_failed_restore():
    doc = load("pipeline")
    save = step_named(doc, "run", "save state")
    assert "always()" in save["if"]
    assert "steps.restore.outcome == 'success'" in save["if"]
    assert "!inputs.dry_run" in save["if"]
    ids = [s.get("id") for s in steps_of(doc, "run")]
    assert ids.index("restore") < steps_of(doc, "run").index(save)


def test_a_dry_run_never_restores_saves_or_opens_a_pull_request():
    doc = load("pipeline")
    for fragment in ["restore saved state", "save state", "push the draft branch", "send the telegram alert"]:
        assert "!inputs.dry_run" in step_named(doc, "run", fragment)["if"], fragment


def test_the_run_step_is_time_limited_and_always_exits_zero_itself():
    step = step_named(load("pipeline"), "run", "run the pipeline")
    assert step["timeout-minutes"] <= 30
    assert "exit 0" in step["run"] and "exit_code=" in step["run"]


def test_artifacts_are_removed_within_a_week():
    upload = step_named(load("pipeline"), "run", "upload run files")
    assert upload["with"]["retention-days"] <= 7


def test_an_open_breaker_opens_one_issue_at_most():
    step = step_named(load("pipeline"), "run", "open an issue")
    assert "pipeline-breaker" in step["run"]
    assert "gh issue list" in step["run"]


def test_only_a_configuration_error_turns_the_job_red():
    step = step_named(load("pipeline"), "run", "configuration error")
    assert "exit_code == '1'" in step["if"]


def test_api_keys_reach_only_the_pipeline_step_and_alerts_only_the_alert_steps():
    doc = load("pipeline")
    holders = {}
    for job_id, job in doc["jobs"].items():
        assert "secrets." not in str(job.get("env", {})), "secret in job-level env"
        for step in job["steps"]:
            env = str(step.get("env", {}))
            for secret in ["GEMINI_API_KEY", "GROQ_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
                if f"secrets.{secret}" in env:
                    holders.setdefault(secret, set()).add(step["name"])
            assert "secrets." not in str(step.get("run", ""))
    assert holders["GEMINI_API_KEY"] == {"Run the pipeline"}
    assert holders["GROQ_API_KEY"] == {"Run the pipeline"}
    assert holders["TELEGRAM_BOT_TOKEN"] == {"Send the test message", "Send the Telegram alert"}
    assert holders["TELEGRAM_CHAT_ID"] == {"Send the test message", "Send the Telegram alert"}


def test_the_pull_request_step_gets_model_text_only_through_env():
    step = step_named(load("pipeline"), "run", "open the pull request")
    assert "steps.draft.outputs.title" in str(step["env"])
    assert "$TITLE" in step["run"]


# --- site-check.yml and deploy.yml --------------------------------------------


def test_site_check_needs_no_secrets_and_runs_on_pull_requests():
    doc = load("site-check")
    assert "secrets." not in (WORKFLOWS / "site-check.yml").read_text(encoding="utf-8")
    on = triggers(doc)
    assert "pull_request" in on and on["push"]["branches"] == ["main"]


def test_site_check_checks_the_dry_run_exit_codes():
    doc = load("site-check")
    step = step_named(doc, "pipeline", "dry runs")
    assert "pass:0" in step["run"] and "fail-verify:3" in step["run"] and "fail-gate:3" in step["run"]


def test_deploy_runs_only_from_main_or_by_hand_and_checks_first():
    doc = load("deploy")
    on = triggers(doc)
    assert set(on) == {"push", "workflow_dispatch"}
    assert on["push"]["branches"] == ["main"]
    runs = [s.get("run") for s in steps_of(doc, "deploy")]
    assert runs.index("npm run check") < runs.index("npm run deploy")


def test_cloudflare_secrets_reach_only_the_deploy_step():
    doc = load("deploy")
    holders = [s["name"] for s in steps_of(doc, "deploy") if "secrets.CLOUDFLARE" in str(s.get("env", ""))]
    assert holders == ["Build and deploy"]
    assert len(re.findall(r"secrets\.", (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8"))) == 2


def test_deploys_never_overlap_and_are_never_cancelled():
    concurrency = load("deploy")["concurrency"]
    assert concurrency["group"] == "deploy" and concurrency["cancel-in-progress"] is False
