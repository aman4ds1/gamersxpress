"""Minimal pipeline run orchestration.

Creates exactly one :class:`RunState` per run and threads it through every stage
that calls :func:`providers.generate` (currently ``write`` and ``verify``). The
writer records its actual model family on the shared state; the verifier's
fail-closed guard reads it and runs on a different family.

Outcomes:

- ``MissingWriterFamilyError`` -> failed run. Nothing is published, a report is
  written, and the circuit breaker counter advances (pausing after three
  consecutive failures).
- ``SkipRun`` -> skipped run (provider outage/rate limit). Nothing is published
  and the counter is left unchanged.
- success -> completed run; the counter resets.

The publish gate and the remaining PLAN.md stages are added later; this module
currently wires the writer -> verifier slice.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from providers import (
    PROJECT_ROOT,
    Generation,
    MissingWriterFamilyError,
    RunState,
    SkipRun,
)

import verify as verify_stage
import write as write_stage

logger = logging.getLogger("gamersxpress.pipeline")

CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_REPORT_PATH = PROJECT_ROOT / "data" / "run-report.json"
DEFAULT_CIRCUIT_PATH = PROJECT_ROOT / "data" / "circuit-breaker.json"


@dataclass
class RunResult:
    status: str
    run_state: RunState
    error: str | None = None
    report: dict = field(default_factory=dict)


def _load_json(path: str | Path, default: dict) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)


def _write_json(path: str | Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _build_report(status: str, run_state: RunState, error: str | None, circuit: dict) -> dict:
    report = {"status": status, "error": error, "circuit_breaker": dict(circuit)}
    report.update(run_state.report())
    return report


def run(
    facts: Any,
    *,
    write_fn: Callable[..., Generation] | None = None,
    verify_fn: Callable[..., Generation] | None = None,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    circuit_path: str | Path = DEFAULT_CIRCUIT_PATH,
    stage_kwargs: dict | None = None,
    **generate_kwargs: Any,
) -> RunResult:
    """Run the writer and verifier with one shared :class:`RunState`."""
    write_fn = write_fn or write_stage.write
    verify_fn = verify_fn or verify_stage.verify
    kwargs = dict(stage_kwargs or {})
    kwargs.update(generate_kwargs)

    run_state = RunState()
    circuit = _load_json(circuit_path, {"consecutive_failures": 0, "paused": False})

    try:
        writer = write_fn(facts, run_state=run_state, **kwargs)
        verify_fn(writer.value, facts, run_state=run_state, **kwargs)
    except MissingWriterFamilyError as exc:
        circuit["consecutive_failures"] = int(circuit.get("consecutive_failures", 0)) + 1
        circuit["paused"] = circuit["consecutive_failures"] >= CIRCUIT_BREAKER_THRESHOLD
        _write_json(circuit_path, circuit)
        logger.error(
            "run failed: %s (consecutive_failures=%s, paused=%s)",
            exc, circuit["consecutive_failures"], circuit["paused"],
        )
        result = RunResult("failed", run_state, str(exc),
                           _build_report("failed", run_state, str(exc), circuit))
        _write_json(report_path, result.report)
        return result
    except SkipRun as exc:
        _write_json(circuit_path, circuit)
        logger.warning("run skipped: %s", exc)
        result = RunResult("skipped", run_state, str(exc),
                           _build_report("skipped", run_state, str(exc), circuit))
        _write_json(report_path, result.report)
        return result

    circuit["consecutive_failures"] = 0
    circuit["paused"] = False
    _write_json(circuit_path, circuit)
    logger.info(
        "run completed: writer_family=%s verifier_family=%s",
        run_state.writer_family, run_state.verifier_family,
    )
    result = RunResult("completed", run_state, None,
                       _build_report("completed", run_state, None, circuit))
    _write_json(report_path, result.report)
    return result


__all__ = ["RunResult", "run", "CIRCUIT_BREAKER_THRESHOLD"]
