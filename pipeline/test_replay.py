import datetime as dt
import io
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

import dryrun
import providers
import publish
import replay
import run as run_module
from providers import Generation

NOW = dt.datetime(2026, 10, 19, 12, tzinfo=dt.timezone.utc)
CLUSTER_ID = "b939ef7c7f26"

#: Twelve distinct words, one per synthetic claim, so no two claims merge.
WORDS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
    "golf", "hotel", "india", "juliet", "kilo", "lima",
]


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """A test that writes a temporary config must not reuse a cached parse."""
    providers.clear_config_cache()
    yield
    providers.clear_config_cache()


def listing(root: Path) -> set[str]:
    """Every path under ``root``, relative to it, for an unchanged-listing check."""
    root = Path(root)
    if not root.is_dir():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def write_gathered(data_dir: Path, cluster_id: str = CLUSTER_ID, gathered: dict | None = None) -> Path:
    directory = data_dir / "gathered"
    directory.mkdir(parents=True, exist_ok=True)
    payload = gathered if gathered is not None else dryrun.sample_gathered(cluster_id, NOW)
    path = directory / f"{cluster_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_sheet(
    data_dir: Path,
    cluster_id: str = CLUSTER_ID,
    title: str = dryrun.SAMPLE_TITLE,
    entities: tuple[str, ...] = ("Nvidia",),
) -> Path:
    directory = data_dir / "facts" / "unconfirmed"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": cluster_id,
        "title": title,
        "coherence": {
            "checked": True,
            "primary_entities": [entity.lower() for entity in entities],
            "canonical_entities": list(entities),
        },
        "confirmation": {"rule": "tier1", "confirmed": True, "reason": "confirmed"},
    }
    path = directory / f"{cluster_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def is_grouping_call(json_schema) -> bool:
    return isinstance(json_schema, dict) and "groups" in (json_schema.get("properties") or {})


def many_claim_gathered(cluster_id: str = CLUSTER_ID) -> dict:
    """One source whose body holds every word the synthetic claims cite."""
    text = "nvidia " + " ".join(WORDS) + "."
    return {
        "id": cluster_id,
        "gathered_at": NOW.isoformat(),
        "sources": [
            {
                "link": "https://a.example/1",
                "title": "Nvidia update roundup",
                "source_name": "Alpha",
                "tier": 2,
                "owner": "owner-a",
                "region": "us",
                "status": "ok",
                "text": text,
                "error": None,
            }
        ],
    }


def many_claims_generate(count: int = len(WORDS)):
    """A stub model returning ``count`` distinct grounded claims."""
    payload = {
        "claims": [
            {
                "claim": f"nvidia reported {word} developments",
                "value": f"the {word} development",
                "source_url": "https://a.example/1",
                "confidence": 0.5,
                "origin": "unstated",
                "affiliation": "",
                "kind": "reported",
                "quote_ids": [],
                "attribution_ids": [],
                "speaker": "",
                "speaker_type": "none",
            }
            for word in WORDS[:count]
        ]
    }

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        value = {"groups": []} if is_grouping_call(json_schema) else payload
        generation = Generation(role=role, provider="mock", model="m", family="f", value=value)
        if run_state is not None:
            run_state.record(generation)
        return generation

    return generate


def report_dict(
    sheet_path: Path | None = None,
    output_dir: Path | None = None,
    *,
    cluster_id: str = "abc",
    title: str = "A title",
    label: str = "Nvidia confirmed it",
    quote: str = "says nvidia",
) -> dict:
    """A finished replay report, as ``run`` would return it."""
    return {
        "cluster_id": cluster_id,
        "title": title,
        "entities": ["Nvidia"],
        "model_calls": 3,
        "confirmation": {"rule": "tier1", "confirmed": True, "reason": "confirmed"},
        "fact_groups": {"accepted": 2, "dropped": 1},
        "claims": [
            {
                "label": label,
                "kind": "confirmed",
                "central": True,
                "evidence": [
                    {
                        "ok": True,
                        "reason": None,
                        "speaker": "Nvidia",
                        "attribution_mode": "explicit",
                        "attribution_quote": quote,
                    }
                ],
            }
        ],
        "claims_total": 1,
        "sheet_path": str(sheet_path) if sheet_path is not None else None,
        "output_dir": str(output_dir) if output_dir is not None else None,
    }



# --- replay report -----------------------------------------------------------


def test_replay_loads_gathered_and_sheet_title_and_prints_fields(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    write_sheet(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    report = replay.run(CLUSTER_ID, generate=dryrun.make_generate("pass"), now=NOW)

    assert report["cluster_id"] == CLUSTER_ID
    assert report["title"] == dryrun.SAMPLE_TITLE
    assert report["entities"] == ["Nvidia"]
    assert report["model_calls"] >= 2

    confirmation = report["confirmation"]
    assert set(confirmation) == {"rule", "confirmed", "reason"}
    assert confirmation["confirmed"] is True
    assert isinstance(confirmation["rule"], str) and confirmation["rule"]

    assert report["fact_groups"]["accepted"] >= 1
    assert report["fact_groups"]["dropped"] >= 0

    assert report["claims"]
    first = report["claims"][0]
    assert set(first) >= {"label", "kind", "central", "evidence"}

    printed = replay.render_report(report)
    assert dryrun.SAMPLE_TITLE in printed
    assert "model calls:" in printed
    assert "confirmation:" in printed
    assert "fact_groups: accepted=" in printed
    assert "claim 1:" in printed


def test_replay_main_prints_the_report(tmp_path, monkeypatch, capsys):
    sheet_path = tmp_path / "abc.json"
    monkeypatch.setattr(replay, "run", lambda *args, **kwargs: report_dict(sheet_path))

    code = replay.main(["abc"])
    captured = capsys.readouterr()

    assert code == 0
    assert "model calls: 3" in captured.out
    assert "confirmation: rule=tier1 confirmed=True reason=confirmed" in captured.out
    assert "fact_groups: accepted=2 dropped=1" in captured.out
    assert "claim 1:" in captured.out
    assert "attribution_mode=explicit" in captured.out
    assert captured.out.strip().splitlines()[-1] == (
        f"report: {sheet_path.parent / (sheet_path.stem + '-report.txt')} sheet: {sheet_path}"
    )



# --- isolation: no writes to the data tree, no breaker -----------------------


def test_replay_writes_nothing_under_the_data_folder(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    write_sheet(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    real_data = providers.PROJECT_ROOT / "data"
    real_before = listing(real_data)
    data_before = listing(data_dir)

    replay.run(
        CLUSTER_ID,
        generate=dryrun.make_generate("pass"),
        now=NOW,
        output_dir=tmp_path / "out",
    )

    assert listing(real_data) == real_before
    assert listing(data_dir) == data_before


def test_replay_never_touches_the_breaker(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    write_sheet(data_dir)
    breaker = data_dir / "breaker.json"
    breaker.write_text('{"consecutive_failures": 2, "open": false}\n', encoding="utf-8")
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    real_breaker = providers.PROJECT_ROOT / "data" / "breaker.json"
    real_before = real_breaker.read_bytes() if real_breaker.is_file() else None
    before = breaker.read_bytes()

    output_dir = tmp_path / "out"
    replay.run(
        CLUSTER_ID,
        generate=dryrun.make_generate("pass"),
        now=NOW,
        output_dir=output_dir,
    )

    assert breaker.read_bytes() == before
    assert not (output_dir / "breaker.json").exists()
    real_after = real_breaker.read_bytes() if real_breaker.is_file() else None
    assert real_after == real_before


# --- clear failures ----------------------------------------------------------


def test_replay_missing_cluster_id_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "DATA_DIR", tmp_path / "data")

    with pytest.raises(replay.ReplayError) as excinfo:
        replay.run("does-not-exist", generate=dryrun.make_generate("pass"), now=NOW)

    message = str(excinfo.value)
    assert "does-not-exist" in message
    assert "gathered" in message


def test_replay_missing_gathered_file_fails(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_sheet(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    with pytest.raises(replay.ReplayError) as excinfo:
        replay.run(CLUSTER_ID, generate=dryrun.make_generate("pass"), now=NOW)

    message = str(excinfo.value)
    assert CLUSTER_ID in message
    assert "gathered" in message


def test_replay_main_reports_a_missing_gathered_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(replay, "DATA_DIR", tmp_path / "data")

    code = replay.main([CLUSTER_ID])
    captured = capsys.readouterr()

    assert code != 0
    assert "replay failed" in captured.err
    assert "gathered" in captured.err


# --- claim limit -------------------------------------------------------------


def test_replay_prints_at_most_eight_claims(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    cluster_id = "many-claims"
    write_gathered(data_dir, cluster_id=cluster_id, gathered=many_claim_gathered(cluster_id))
    write_sheet(
        data_dir,
        cluster_id=cluster_id,
        title="Nvidia update roundup",
        entities=("Nvidia",),
    )
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    report = replay.run(cluster_id, generate=many_claims_generate(), now=NOW)

    assert report["claims_total"] >= 9
    assert len(report["claims"]) == replay.CLAIM_REPORT_LIMIT == 8

    printed = replay.render_report(report)
    assert "claim 8:" in printed
    assert "claim 9:" not in printed


# --- console safety: a Windows code page must not kill a finished run --------


def cp932_console():
    """A stdout that encodes to cp932 strictly, like a Japanese console."""
    buffer = io.BytesIO()
    return buffer, io.TextIOWrapper(buffer, encoding="cp932", errors="strict")


def test_replay_prints_em_dash_and_japanese_on_a_cp932_console(tmp_path, monkeypatch):
    sheet_path = tmp_path / "unconfirmed" / "abc.json"
    report = report_dict(
        sheet_path,
        tmp_path,
        title="Nvidia \u2014 GPU launch confirmed \U0001f3ae",
        label="Nvidia \u306f\u767a\u8868 \u2014 launch \U0001f3ae",
        quote="\u300c\u767a\u8868\u3059\u308b\u3001\u3053\u306e\u5834\u5408\u3002\u300d",
    )
    monkeypatch.setattr(replay, "run", lambda *args, **kwargs: report)

    buffer, console = cp932_console()
    monkeypatch.setattr(sys, "stdout", console)

    code = replay.main(["abc"])
    console.flush()
    printed = buffer.getvalue().decode("cp932")

    assert code == 0
    assert "model calls: 3" in printed
    assert "\u2014" not in printed
    assert str(sheet_path) in printed
    assert printed.strip().splitlines()[-1].startswith("report:")


def test_replay_writes_the_report_file_as_utf8(tmp_path, monkeypatch, capsys):
    sheet_path = tmp_path / "unconfirmed" / "abc.json"
    monkeypatch.setattr(
        replay,
        "run",
        lambda *args, **kwargs: report_dict(sheet_path, tmp_path, title="Nvidia \u767a\u8868"),
    )

    code = replay.main(["abc"])

    report_path = tmp_path / "unconfirmed" / "abc-report.txt"
    assert code == 0
    assert report_path.is_file()
    text = report_path.read_text(encoding="utf-8")
    assert "Nvidia \u767a\u8868" in text
    assert "claim 1:" in text
    assert "sheet: " in text


def test_report_note_is_printed_and_saved(tmp_path, monkeypatch, capsys):
    sheet_path = tmp_path / "unconfirmed" / "abc.json"
    monkeypatch.setattr(replay, "run", lambda *args, **kwargs: report_dict(sheet_path, tmp_path))

    code = replay.main(["abc"])
    captured = capsys.readouterr()
    report_path = tmp_path / "unconfirmed" / "abc-report.txt"

    assert code == 0
    assert replay.REPORT_NOTE in captured.out
    assert captured.out.strip().splitlines()[-2] == replay.REPORT_NOTE
    assert report_path.read_text(encoding="utf-8").splitlines()[-1] == replay.REPORT_NOTE


def test_replay_last_printed_line_names_the_sheet_path(tmp_path, monkeypatch, capsys):
    sheet_path = tmp_path / "unconfirmed" / "abc.json"
    monkeypatch.setattr(replay, "run", lambda *args, **kwargs: report_dict(sheet_path, tmp_path))

    code = replay.main(["abc"])
    lines = capsys.readouterr().out.strip().splitlines()

    assert code == 0
    assert lines[-1] == (
        f"report: {tmp_path / 'unconfirmed' / 'abc-report.txt'} sheet: {sheet_path}"
    )


def stage_calls():
    return {"writer": 0, "verifier": 0}


def verdict_json(*unsupported):
    return json.dumps({
        "clauses": [
            {
                "clause": clause,
                "fact_ids": [],
                "match": "none",
                "exceeds_facts": False,
                "difference": "not in the sheet",
            }
            for clause in unsupported
        ],
        "unsupported_claims": [],
        "rumors_stated_as_fact": [],
        "unsupported_regional": [],
        "costs_described_as_received": [],
        "invented_labels": [],
        "category_claims": [],
    })


def stage_generate(writer_outputs, verifier_outputs, calls):
    """Stub every model call for a full draft replay, recording roles."""

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        properties = (json_schema or {}).get("properties") or {}
        if role == "verifier" and "groups" in properties:
            value, family = {"groups": []}, "mistral"
        elif role == "fast":
            value = (
                dryrun._claims_json(dryrun._prompt_source_url(prompt))
                if "claims" in properties
                else dryrun._seo_json()
            )
            family = "google"
        elif role == "writer":
            value = writer_outputs[min(calls["writer"], len(writer_outputs) - 1)]
            calls["writer"] += 1
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


def unconfirmed_generate(roles):
    """A stub whose only claim is a rumor, so the story never confirms."""

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        roles.append(role)
        properties = (json_schema or {}).get("properties") or {}
        if role == "verifier" and "groups" in properties:
            value = {"groups": []}
            family = "mistral"
        elif role == "fast" and "claims" in properties:
            value = {
                "claims": [
                    {
                        "claim": "Nvidia plans a briefing with partners",
                        "value": "plans a briefing with partners",
                        "source_url": dryrun._prompt_source_url(prompt),
                        "confidence": 0.5,
                        "origin": "unstated",
                        "affiliation": "",
                        "kind": "rumor",
                        "quote_ids": [],
                        "attribution_ids": [],
                        "speaker": "",
                        "speaker_type": "none",
                    }
                ]
            }
            family = "google"
        else:
            raise AssertionError(f"stage {role!r} must not run for an unconfirmed story")
        generation = providers.Generation(role, "mock", f"mock-{role}", family, value)
        if run_state is not None:
            run_state.record(generation)
        return generation

    return generate


def replay_through_draft(tmp_path, monkeypatch, generate, **kwargs):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    write_sheet(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)
    options = {
        "through": "draft",
        "generate": generate,
        "now": NOW,
        "output_dir": tmp_path / "out",
        "site_checks": lambda text, slug: (True, "stub site check"),
    }
    options.update(kwargs)
    return replay.run(CLUSTER_ID, **options)


def install_tripwires(monkeypatch) -> list[str]:
    """Make every publish/seen/draft/last-run/issue/breaker writer fail loudly."""
    calls: list[str] = []
    targets = {
        publish: (
            "publish",
            "save_draft",
            "_mark_seen",
            "write_last_run",
            "write_issue",
        ),
        run_module: (
            "load_breaker",
            "save_breaker",
            "record_failure",
            "reset_breaker",
        ),
    }
    for module, names in targets.items():
        for name in names:
            def tripwire(*args, __name=name, **kwargs):
                calls.append(__name)
                raise AssertionError(f"{__name} must not be called during a replay")

            monkeypatch.setattr(module, name, tripwire)
    return calls


# --- output dir: never inside the repository ---------------------------------


def test_output_dir_inside_repo_is_refused_before_any_stage(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)
    stages: list[str] = []
    monkeypatch.setattr(replay.facts_stage, "facts", lambda *args, **kwargs: stages.append("facts"))

    output_dir = providers.PROJECT_ROOT / "data" / f"replay-out-{uuid.uuid4().hex}"
    code = replay.main([CLUSTER_ID, "--output-dir", str(output_dir)])
    captured = capsys.readouterr()

    assert code != 0
    assert "output dir must be outside the repository" in captured.err
    assert stages == []
    assert not output_dir.exists()


def test_output_dir_equal_to_repo_root_is_refused(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        replay.facts_stage, "facts", lambda *args, **kwargs: pytest.fail("facts stage ran")
    )

    with pytest.raises(replay.ReplayError, match="output dir must be outside the repository"):
        replay.run(
            CLUSTER_ID,
            generate=dryrun.make_generate("pass"),
            now=NOW,
            output_dir=providers.PROJECT_ROOT,
        )


def test_relative_output_dir_into_repo_is_refused(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        replay.facts_stage, "facts", lambda *args, **kwargs: pytest.fail("facts stage ran")
    )

    target = providers.PROJECT_ROOT / "data" / f"replay-out-{uuid.uuid4().hex}"
    relative = os.path.relpath(target, Path.cwd())

    with pytest.raises(replay.ReplayError, match="output dir must be outside the repository"):
        replay.run(
            CLUSTER_ID,
            generate=dryrun.make_generate("pass"),
            now=NOW,
            output_dir=relative,
        )

    assert not target.exists()


def test_output_dir_outside_repo_is_accepted(tmp_path, monkeypatch):
    output_dir = tmp_path / "sibling"
    assert not output_dir.is_symlink()

    report = replay_through_draft(
        tmp_path,
        monkeypatch,
        dryrun.make_generate("pass"),
        output_dir=output_dir,
    )

    assert report["status"] == "completed"
    assert Path(report["temp_dir"]) == output_dir.resolve()
    assert output_dir.is_dir()


# --- --through draft: every stage in a temp tree -----------------------------


def test_through_draft_confirmed_runs_all_stages_and_prints_article(tmp_path, monkeypatch):
    report = replay_through_draft(tmp_path, monkeypatch, dryrun.make_generate("pass"))

    assert report["status"] == "completed"
    assert report["verify"]["passed"] is True
    assert report["verify"]["unsupported"] == []
    assert report["gates"]["passed"] is True
    assert report["seo"]["title"] == dryrun.SAMPLE_TITLE
    assert report["seo"]["slug"] == dryrun.SAMPLE_SLUG
    assert report["seo"]["tags"] and report["seo"]["entities"] == ["Nvidia"]

    assert "## Overview" in report["article"]
    article_path = Path(report["article_path"])
    assert article_path.is_file()
    assert article_path.read_text(encoding="utf-8") == report["article"]
    assert Path(report["temp_dir"]) == tmp_path / "out"
    assert article_path.parent == tmp_path / "out" / "article"

    printed = replay.render_report(report)
    assert "verify: passed" in printed
    assert "gates: passed" in printed
    assert "seo: title=" in printed
    assert "article:" in printed
    assert "## Overview" in printed
    assert str(article_path) in printed
    assert str(tmp_path / "out") in printed


def test_through_draft_unconfirmed_stops_after_facts_and_says_so(tmp_path, monkeypatch):
    roles: list[str] = []
    report = replay_through_draft(
        tmp_path, monkeypatch, unconfirmed_generate(roles)
    )

    assert report["status"] == "not_confirmed"
    assert report["not_confirmed"] is True
    assert report["confirmation"]["confirmed"] is False
    assert report.get("verify") is None
    assert report.get("article_path") is None
    assert "writer" not in roles

    printed = replay.render_report(report)
    assert (
        f"not confirmed: {report['confirmation']['rule']} "
        f"{report['confirmation']['reason']}" in printed
    )
    assert "verify:" not in printed


def test_through_draft_writes_nothing_under_the_real_tree(tmp_path, monkeypatch):
    real_root = providers.PROJECT_ROOT
    real_dirs = {
        name: real_root / name for name in ("src", "public", "drafts", "data")
    }
    before = {name: listing(path) for name, path in real_dirs.items()}
    breaker = real_root / "data" / "breaker.json"
    breaker_before = breaker.read_bytes() if breaker.is_file() else None

    report = replay_through_draft(tmp_path, monkeypatch, dryrun.make_generate("pass"))

    assert report["status"] == "completed"
    after = {name: listing(path) for name, path in real_dirs.items()}
    assert after == before
    breaker_after = breaker.read_bytes() if breaker.is_file() else None
    assert breaker_after == breaker_before
    assert not (tmp_path / "out" / "breaker.json").exists()
    assert not (tmp_path / "out" / "seen.json").exists()
    assert not (tmp_path / "out" / "published-log.json").exists()


def test_through_draft_stops_at_the_model_call_cap(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    gathered = dryrun.sample_gathered(CLUSTER_ID, NOW)
    gathered["sources"] = gathered["sources"][:1]
    write_gathered(data_dir, gathered=gathered)
    write_sheet(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    report = replay.run(
        CLUSTER_ID,
        through="draft",
        generate=dryrun.make_generate("pass"),
        now=NOW,
        output_dir=tmp_path / "out",
        max_model_calls=1,
        site_checks=lambda text, slug: (True, "stub site check"),
    )

    assert report["status"] == "budget_exceeded"
    assert "model call budget exceeded" in report["error"]
    assert (tmp_path / "out").is_dir()
    assert "budget_exceeded" in replay.render_report(report)
    assert "model call budget exceeded" in replay.render_report(report)


def test_through_draft_failed_verify_prints_clauses_and_keeps_temp(tmp_path, monkeypatch):
    unsupported = "Nvidia will ship the card next month."
    original = dryrun.sample_body() + f"\n\n{unsupported}\n"
    calls = stage_calls()
    generate = stage_generate(
        [original, original],
        [verdict_json(unsupported), verdict_json(unsupported)],
        calls,
    )

    report = replay_through_draft(tmp_path, monkeypatch, generate)

    assert report["status"] == "failed"
    assert report["verify"]["passed"] is False
    assert unsupported in report["verify"]["unsupported"]
    assert "verification failed" in report["error"]
    assert (tmp_path / "out").is_dir()
    assert Path(report["article_path"]).is_file()
    assert "reports" in listing(tmp_path / "out")

    printed = replay.render_report(report)
    assert "verify: failed" in printed
    assert "unsupported_clauses=1" in printed
    assert unsupported in printed


def test_through_draft_confirmed_never_publishes_or_touches_breaker(tmp_path, monkeypatch):
    calls = install_tripwires(monkeypatch)

    report = replay_through_draft(tmp_path, monkeypatch, dryrun.make_generate("pass"))

    assert report["status"] == "completed"
    assert calls == []


def test_through_draft_unconfirmed_never_publishes_or_touches_breaker(tmp_path, monkeypatch):
    calls = install_tripwires(monkeypatch)

    report = replay_through_draft(
        tmp_path, monkeypatch, unconfirmed_generate([])
    )

    assert report["status"] == "not_confirmed"
    assert calls == []


def test_through_draft_failed_verify_never_publishes_or_touches_breaker(tmp_path, monkeypatch):
    unsupported = "Nvidia will ship the card next month."
    original = dryrun.sample_body() + f"\n\n{unsupported}\n"
    calls = stage_calls()
    generate = stage_generate(
        [original, original],
        [verdict_json(unsupported), verdict_json(unsupported)],
        calls,
    )
    tripwires = install_tripwires(monkeypatch)

    report = replay_through_draft(tmp_path, monkeypatch, generate)

    assert report["status"] == "failed"
    assert report["verify"]["passed"] is False
    assert tripwires == []


def test_through_draft_reports_a_repair_that_passes(tmp_path, monkeypatch):
    unsupported = "Nvidia will ship the card next month."
    original = dryrun.sample_body() + f"\n\n{unsupported}\n"
    repaired = dryrun.sample_body()
    calls = stage_calls()
    generate = stage_generate(
        [original, repaired], [verdict_json(unsupported), verdict_json()], calls
    )

    report = replay_through_draft(tmp_path, monkeypatch, generate)

    assert report["status"] == "completed"
    assert report["repair"]["attempted"] is True
    assert report["repair"]["outcome"] == "accepted"
    assert report["repair"]["problems"] == []
    assert report["repair"]["verify_passed_after"] is True
    assert report["verify"]["passed"] is True
    assert report["gates"]["passed"] is True
    assert calls["writer"] == 2
    assert calls["verifier"] == 2

    drafts = tmp_path / "out" / "drafts" / replay.seo_stage.slugify(dryrun.SAMPLE_TITLE)
    assert (drafts / "original.md").read_text(encoding="utf-8") == original
    assert (drafts / "repaired.md").read_text(encoding="utf-8") == repaired
    assert unsupported in (drafts / "repair-clauses.txt").read_text(encoding="utf-8")

    printed = replay.render_report(report)
    assert "repair: ran outcome=accepted" in printed
    assert "verify: passed" in printed


def test_replay_print_failure_reports_and_keeps_the_temp_folder(tmp_path, monkeypatch, capsys):
    temp_dir = Path(tempfile.mkdtemp(prefix="gx-replay-", dir=tmp_path))
    sheet_path = temp_dir / "unconfirmed" / "abc.json"
    monkeypatch.setattr(replay, "run", lambda *args, **kwargs: report_dict(sheet_path, temp_dir))

    class BrokenConsole:
        def write(self, text):
            raise OSError("console is broken")

        def flush(self):
            raise OSError("console is broken")

    monkeypatch.setattr(sys, "stdout", BrokenConsole())

    code = replay.main(["abc"])
    captured = capsys.readouterr()

    assert code != 0
    assert "report failed: console is broken" in captured.err
    assert str(sheet_path) in captured.err
    assert temp_dir.exists()


# --- --use-sheet: reuse a saved sheet, no extraction ---------------------------


def no_extraction(generate):
    """Wrap a stub so any extraction or grouping call fails the test."""

    def wrapped(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        properties = (json_schema or {}).get("properties") or {}
        assert "claims" not in properties, "extraction must not run with --use-sheet"
        assert "groups" not in properties, "grouping must not run with --use-sheet"
        return generate(role, prompt, json_schema, run_state=run_state, **kwargs)

    return wrapped


def make_saved_sheet(tmp_path, monkeypatch, name="saved") -> Path:
    """A real sheet from a first replay, saved outside the output folder."""
    replay_through_draft(
        tmp_path, monkeypatch, dryrun.make_generate("pass"), output_dir=tmp_path / "first"
    )
    saved = tmp_path / f"{name}.json"
    saved.write_bytes((tmp_path / "first" / f"{CLUSTER_ID}.json").read_bytes())
    return saved


def test_use_sheet_skips_extraction_and_grouping_and_makes_no_model_call(tmp_path, monkeypatch):
    saved = make_saved_sheet(tmp_path, monkeypatch)
    calls: list[str] = []
    inner = dryrun.make_generate("pass")

    def recording(role, prompt, json_schema=None, **kwargs):
        calls.append(role)
        return inner(role, prompt, json_schema, **kwargs)

    monkeypatch.setattr(
        replay.facts_stage, "facts",
        lambda *args, **kwargs: pytest.fail("facts.facts must not run with --use-sheet"),
    )
    report = replay_through_draft(
        tmp_path, monkeypatch, no_extraction(recording),
        use_sheet=saved, output_dir=tmp_path / "second",
    )
    assert report["status"] == "completed"
    assert report["reused_sheet"] == str(saved)
    assert report["confirmation"]["confirmed"] is True
    assert "writer" in calls and "verifier" in calls


def test_use_sheet_never_modifies_the_saved_file(tmp_path, monkeypatch):
    saved = make_saved_sheet(tmp_path, monkeypatch)
    before = saved.read_bytes()
    replay_through_draft(
        tmp_path, monkeypatch, no_extraction(dryrun.make_generate("pass")),
        use_sheet=saved, output_dir=tmp_path / "second",
    )
    assert saved.read_bytes() == before


def test_use_sheet_copies_the_sheet_into_the_output_dir_unchanged(tmp_path, monkeypatch):
    saved = make_saved_sheet(tmp_path, monkeypatch)
    report = replay_through_draft(
        tmp_path, monkeypatch, no_extraction(dryrun.make_generate("pass")),
        use_sheet=saved, output_dir=tmp_path / "second",
    )
    copy = tmp_path / "second" / f"{CLUSTER_ID}.json"
    assert Path(report["sheet_path"]) == copy
    assert copy.read_bytes() == saved.read_bytes()


def test_use_sheet_gives_the_same_facts_on_every_run(tmp_path, monkeypatch):
    saved = make_saved_sheet(tmp_path, monkeypatch)
    first = replay_through_draft(
        tmp_path, monkeypatch, no_extraction(dryrun.make_generate("pass")),
        use_sheet=saved, output_dir=tmp_path / "run-a",
    )
    second = replay_through_draft(
        tmp_path, monkeypatch, no_extraction(dryrun.make_generate("pass")),
        use_sheet=saved, output_dir=tmp_path / "run-b",
    )
    assert first["claims"] == second["claims"]
    assert first["confirmation"] == second["confirmation"]
    assert first["fact_groups"] == second["fact_groups"]


def test_use_sheet_with_an_unconfirmed_sheet_stops_after_the_facts(tmp_path, monkeypatch):
    saved = make_saved_sheet(tmp_path, monkeypatch)
    sheet = json.loads(saved.read_text(encoding="utf-8"))
    sheet["confirmation"] = {"rule": "unconfirmed", "confirmed": False, "reason": "not enough evidence"}
    saved.write_text(json.dumps(sheet), encoding="utf-8")
    roles: list[str] = []

    def generate(role, prompt, json_schema=None, **kwargs):
        roles.append(role)
        raise AssertionError("no model call may run for an unconfirmed saved sheet")

    report = replay_through_draft(
        tmp_path, monkeypatch, generate, use_sheet=saved, output_dir=tmp_path / "second"
    )
    assert report["status"] == "not_confirmed"
    assert roles == []
    assert (tmp_path / "second" / "unconfirmed" / f"{CLUSTER_ID}.json").is_file()


def test_use_sheet_without_through_prints_the_report_and_calls_no_model(tmp_path, monkeypatch):
    saved = make_saved_sheet(tmp_path, monkeypatch)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    def generate(*args, **kwargs):
        raise AssertionError("no model call may run")

    report = replay.run(
        CLUSTER_ID, generate=generate, now=NOW,
        use_sheet=saved, output_dir=tmp_path / "second",
    )
    assert report["model_calls"] == 0
    assert "sheet reused from:" in replay.render_report(report)
    assert "extraction and grouping skipped" in replay.render_report(report)


@pytest.mark.parametrize("content, message", [
    (None, "saved sheet not found"),
    ("not json", "could not read saved sheet"),
    ("[1, 2]", "is not a JSON object"),
    ('{"claims": [], "confirmation": {}}', "has no claims"),
    ('{"claims": [{"claim": "x"}]}', "has no confirmation block"),
])
def test_use_sheet_refuses_an_unusable_file(tmp_path, monkeypatch, content, message):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    write_sheet(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)
    path = tmp_path / "bad.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(replay.ReplayError, match=message):
        replay.run(CLUSTER_ID, use_sheet=path, now=NOW, output_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_use_sheet_still_refuses_an_output_dir_inside_the_repo(tmp_path, monkeypatch):
    saved = make_saved_sheet(tmp_path, monkeypatch)
    inside = providers.PROJECT_ROOT / "data" / f"replay-out-{uuid.uuid4().hex}"
    with pytest.raises(replay.ReplayError, match="output dir must be outside the repository"):
        replay.run(CLUSTER_ID, use_sheet=saved, output_dir=inside, now=NOW)
    assert not inside.exists()


def test_use_sheet_never_publishes_or_touches_the_breaker(tmp_path, monkeypatch):
    saved = make_saved_sheet(tmp_path, monkeypatch)
    calls = install_tripwires(monkeypatch)
    report = replay_through_draft(
        tmp_path, monkeypatch, no_extraction(dryrun.make_generate("pass")),
        use_sheet=saved, output_dir=tmp_path / "second",
    )
    assert report["status"] == "completed"
    assert calls == []


def test_main_accepts_use_sheet_and_prints_it(tmp_path, monkeypatch, capsys):
    saved = make_saved_sheet(tmp_path, monkeypatch)
    seen = {}

    def fake_run(cluster_id, **kwargs):
        seen.update(kwargs)
        return replay.build_report(
            json.loads(saved.read_text(encoding="utf-8")),
            cluster_id=cluster_id, title="t", entities=[], model_calls=0,
            sheet_path=saved, output_dir=tmp_path,
        ) | {"reused_sheet": str(saved)}

    monkeypatch.setattr(replay, "run", fake_run)
    code = replay.main([CLUSTER_ID, "--use-sheet", str(saved), "--output-dir", str(tmp_path / "o")])
    assert code == 0
    assert seen["use_sheet"] == str(saved)
    assert "sheet reused from:" in capsys.readouterr().out
