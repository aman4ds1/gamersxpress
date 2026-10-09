import json

import pytest

import providers
import run as run_module
import verify as verify_module


def test_run_passes_same_run_state_from_writer_to_verifier(tmp_path):
    seen = {}

    def fake_write(facts, *, run_state, **kwargs):
        seen["writer_state"] = run_state
        generation = providers.Generation(
            role="writer", provider="gemini", model="g", family="google", value="ARTICLE",
        )
        run_state.record(generation)
        return generation

    def fake_verify(draft, facts, *, run_state, **kwargs):
        seen["verifier_state"] = run_state
        assert run_state.writer_family == "google"
        generation = providers.Generation(
            role="verifier", provider="mistral", model="m", family="mistral", value="OK",
        )
        run_state.record(generation)
        return generation

    result = run_module.run(
        {}, write_fn=fake_write, verify_fn=fake_verify,
        report_path=tmp_path / "report.json", circuit_path=tmp_path / "circuit.json",
    )

    assert result.status == "completed"
    assert seen["writer_state"] is seen["verifier_state"]
    assert result.run_state is seen["writer_state"]
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["writer_family"] == "google"
    assert report["verifier_family"] == "mistral"


def test_verify_requires_run_state_argument():
    with pytest.raises(TypeError):
        verify_module.verify("draft")


def test_missing_writer_family_is_failed_run_and_counts_toward_circuit(tmp_path):
    def fake_write(facts, *, run_state, **kwargs):
        generation = providers.Generation("writer", "gemini", "g", "google", "ARTICLE")
        run_state.record(generation)
        return generation

    def fake_verify(draft, facts, *, run_state, **kwargs):
        raise providers.MissingWriterFamilyError("no writer family recorded")

    circuit_path = tmp_path / "circuit.json"
    result = run_module.run(
        {}, write_fn=fake_write, verify_fn=fake_verify,
        report_path=tmp_path / "report.json", circuit_path=circuit_path,
    )

    assert result.status == "failed"
    assert "no writer family recorded" in result.error
    circuit = json.loads(circuit_path.read_text(encoding="utf-8"))
    assert circuit["consecutive_failures"] == 1
    assert circuit["paused"] is False
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["error"] == "no writer family recorded"


def test_circuit_breaker_pauses_after_three_failed_runs(tmp_path):
    def fake_write(facts, *, run_state, **kwargs):
        generation = providers.Generation("writer", "gemini", "g", "google", "A")
        run_state.record(generation)
        return generation

    def fake_verify(draft, facts, *, run_state, **kwargs):
        raise providers.MissingWriterFamilyError("boom")

    circuit_path = tmp_path / "circuit.json"
    for _ in range(3):
        result = run_module.run(
            {}, write_fn=fake_write, verify_fn=fake_verify,
            report_path=tmp_path / "report.json", circuit_path=circuit_path,
        )
    assert result.status == "failed"
    assert json.loads(circuit_path.read_text(encoding="utf-8"))["paused"] is True


def test_skip_run_writes_report_and_does_not_count_as_failure(tmp_path):
    def fake_write(facts, *, run_state, **kwargs):
        generation = providers.Generation("writer", "gemini", "g", "google", "A")
        run_state.record(generation)
        return generation

    def fake_verify(draft, facts, *, run_state, **kwargs):
        raise providers.SkipRun("all providers unavailable")

    circuit_path = tmp_path / "circuit.json"
    result = run_module.run(
        {}, write_fn=fake_write, verify_fn=fake_verify,
        report_path=tmp_path / "report.json", circuit_path=circuit_path,
    )
    assert result.status == "skipped"
    circuit = json.loads(circuit_path.read_text(encoding="utf-8"))
    assert circuit["consecutive_failures"] == 0


def test_success_resets_circuit_breaker(tmp_path):
    def fake_write(facts, *, run_state, **kwargs):
        generation = providers.Generation("writer", "gemini", "g", "google", "A")
        run_state.record(generation)
        return generation

    def failing_verify(draft, facts, *, run_state, **kwargs):
        raise providers.MissingWriterFamilyError("boom")

    def ok_verify(draft, facts, *, run_state, **kwargs):
        generation = providers.Generation("verifier", "mistral", "m", "mistral", "OK")
        run_state.record(generation)
        return generation

    circuit_path = tmp_path / "circuit.json"
    for _ in range(2):
        run_module.run(
            {}, write_fn=fake_write, verify_fn=failing_verify,
            report_path=tmp_path / "report.json", circuit_path=circuit_path,
        )
    run_module.run(
        {}, write_fn=fake_write, verify_fn=ok_verify,
        report_path=tmp_path / "report.json", circuit_path=circuit_path,
    )
    circuit = json.loads(circuit_path.read_text(encoding="utf-8"))
    assert circuit["consecutive_failures"] == 0
    assert circuit["paused"] is False


def test_run_dry_run_end_to_end(tmp_path):
    result = run_module.run(
        {"claim": "example"}, dry_run=True,
        report_path=tmp_path / "report.json", circuit_path=tmp_path / "circuit.json",
    )
    assert result.status == "completed"
    assert result.run_state.writer_family == "google"
    assert result.run_state.verifier_family == "mistral"
