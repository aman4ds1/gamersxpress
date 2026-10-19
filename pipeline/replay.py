"""Re-run the facts stage on a saved story (no ingest, clustering or gather).

A story that has left the feeds is normally impossible to re-check: its cluster
never forms again, so ``pipeline.run`` cannot reach the facts stage for it. This
module replays that one stage on what a past run already saved, so a fix to the
extraction, grounding, coherence, grouping, evidence or confirmation code can be
tested on real text after the story is gone.

Usage::

    python -m pipeline.replay <cluster_id>

It reads ``data/gathered/<id>.json`` (the saved source text) and the story title
and entity list from ``data/facts/unconfirmed/<id>.json`` or
``data/facts/<id>.json``, whichever exists, falling back to the newest
``candidates-*.json`` report entry with that cluster id.

It calls the same :func:`facts.facts` function ``run.py`` calls, with the same
model roles, fallbacks and config, but writes the resulting sheet to a temp
directory only. It never writes to ``data/facts``, ``data/failed.json``,
``data/breaker.json``, ``data/reports`` or any other tracked file, and never
touches the circuit breaker.

It prints a short report: how many model calls were used, the confirmation
verdict (rule, confirmed, reason), the accepted and dropped fact-group counts,
and -- for the first :data:`CLAIM_REPORT_LIMIT` claims -- the label, kind,
central flag and, per evidence entry, ok, reason, speaker, attribution mode and
the attribution quote truncated to :data:`ATTRIBUTION_QUOTE_WORDS` words.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

# Support `python -m pipeline.replay` (namespace package): make sibling modules
# importable by their top-level names before any of them are imported. This must
# run before the imports below, exactly as run.py does it.
_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import facts as facts_stage
import score as score_stage
from providers import PROJECT_ROOT, RunState, generate as default_generate

logger = logging.getLogger("gamersxpress.replay")

#: Root of the saved pipeline state. Tests monkeypatch this to redirect every
#: read at a temporary data tree.
DATA_DIR = PROJECT_ROOT / "data"

#: How many claims the printed report shows.
CLAIM_REPORT_LIMIT = 8

#: How many words of an attribution quote the printed report keeps.
ATTRIBUTION_QUOTE_WORDS = 15


class ReplayError(Exception):
    """A replay cannot start or the facts stage could not produce a sheet."""


def _sanitize_name(value: object) -> str:
    """The sheet file stem facts.py uses for ``value`` (same substitution)."""
    return re.sub(r"[^0-9A-Za-z._-]", "-", str(value))


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _candidate_files(data_dir: Path) -> list[Path]:
    """Candidate report files, newest first.

    The run writes one ``candidates-<timestamp>.json`` report per run under
    ``data/reports``; the ingest stage writes per-run item lists under
    ``data/candidates``. Timestamped names sort chronologically, so reversing
    the sorted list yields the newest first.
    """
    files: list[Path] = []
    reports = data_dir / "reports"
    if reports.is_dir():
        files.extend(sorted(reports.glob("candidates-*.json")))
    candidates = data_dir / "candidates"
    if candidates.is_dir():
        files.extend(sorted(candidates.glob("*.json")))
    return list(reversed(files))


def _newest_candidate_entry(cluster_id: str, *, data_dir: Path) -> dict | None:
    """The newest candidate-report entry whose ``cluster_id`` matches, or None."""
    target = str(cluster_id)
    for path in _candidate_files(data_dir):
        try:
            payload = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            entries = payload.get("candidates") or payload.get("items") or []
        elif isinstance(payload, list):
            entries = payload
        else:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("cluster_id") or entry.get("id") or "") == target:
                return entry
    return None


def load_story_context(cluster_id: str, *, data_dir: Path) -> tuple[str | None, list[str]]:
    """The story title and entity list a prior run used for ``cluster_id``.

    Prefers the saved facts sheet (``data/facts/unconfirmed/<id>.json`` then
    ``data/facts/<id>.json``): the entity list is its canonical (else primary)
    entities. The sheet does not store the seed headline, so when it has no
    ``title`` the newest candidates report entry supplies it. Falls back to that
    entry for both when no sheet exists. Either value may be absent.
    """
    title: str | None = None
    entities: list[str] = []
    for candidate in (
        data_dir / "facts" / "unconfirmed" / f"{cluster_id}.json",
        data_dir / "facts" / f"{cluster_id}.json",
    ):
        if not candidate.is_file():
            continue
        try:
            payload = _read_json(candidate)
        except (OSError, json.JSONDecodeError):
            break
        coherence = payload.get("coherence") or {}
        entities = [
            str(entity)
            for entity in (coherence.get("canonical_entities") or [])
        ] or [
            str(entity)
            for entity in (coherence.get("primary_entities") or [])
        ]
        raw_title = payload.get("title")
        title = str(raw_title) if raw_title else None
        break
    if title is None:
        entry = _newest_candidate_entry(cluster_id, data_dir=data_dir)
        if entry is not None:
            raw_title = entry.get("title")
            title = str(raw_title) if raw_title else None
            if not entities:
                entities = [str(entity) for entity in (entry.get("entities") or [])]
    return title, entities


def _locate_sheet(output_dir: Path, cluster_id: str) -> tuple[dict | None, Path | None]:
    """The sheet facts.py just wrote under ``output_dir`` and its path."""
    name = _sanitize_name(cluster_id)
    for path in (output_dir / "unconfirmed" / f"{name}.json", output_dir / f"{name}.json"):
        if path.is_file():
            try:
                return _read_json(path), path
            except (OSError, json.JSONDecodeError):
                return None, path
    return None, None


def _truncate_words(text: object, limit: int) -> str:
    words = str(text or "").split()
    return " ".join(words[:limit])


def _claim_report(claim: dict) -> dict:
    """One claim's report entry: label, kind, central flag and its evidence."""
    evidence = claim.get("evidence") or []
    if isinstance(evidence, dict):
        evidence = [evidence]
    return {
        "label": str(claim.get("claim") or ""),
        "kind": str(claim.get("kind") or ""),
        "central": bool(claim.get("central")),
        "evidence": [
            {
                "ok": bool(entry.get("ok")),
                "reason": entry.get("reason"),
                "speaker": entry.get("speaker"),
                "attribution_mode": entry.get("attribution_mode"),
                "attribution_quote": _truncate_words(
                    entry.get("attribution_quote"), ATTRIBUTION_QUOTE_WORDS
                ),
            }
            for entry in evidence
            if isinstance(entry, dict)
        ],
    }


def build_report(
    sheet: dict,
    *,
    cluster_id: str,
    title: str | None,
    entities: list[str],
    model_calls: int,
    sheet_path: Path | None,
) -> dict:
    """Summarize a facts sheet for the printed replay report."""
    confirmation = sheet.get("confirmation") or {}
    fact_groups = sheet.get("fact_groups") or {}
    claims = [claim for claim in (sheet.get("claims") or []) if isinstance(claim, dict)]
    return {
        "cluster_id": cluster_id,
        "title": title,
        "entities": list(entities),
        "model_calls": model_calls,
        "confirmation": {
            "rule": confirmation.get("rule"),
            "confirmed": bool(confirmation.get("confirmed")),
            "reason": confirmation.get("reason"),
        },
        "fact_groups": {
            "accepted": len(fact_groups.get("accepted") or []),
            "dropped": len(fact_groups.get("dropped") or []),
        },
        "claims": [_claim_report(claim) for claim in claims[:CLAIM_REPORT_LIMIT]],
        "claims_total": len(claims),
        "sheet_path": str(sheet_path) if sheet_path is not None else None,
    }


def render_report(report: dict) -> str:
    """Render a replay report as the short block the CLI prints."""
    title = report.get("title") or "(no title)"
    lines = [f"replay: {report.get('cluster_id')} - {title}"]
    lines.append(f"model calls: {report.get('model_calls')}")
    confirmation = report.get("confirmation") or {}
    lines.append(
        "confirmation: "
        f"rule={confirmation.get('rule')} "
        f"confirmed={confirmation.get('confirmed')} "
        f"reason={confirmation.get('reason')}"
    )
    fact_groups = report.get("fact_groups") or {}
    lines.append(
        f"fact_groups: accepted={fact_groups.get('accepted')} "
        f"dropped={fact_groups.get('dropped')}"
    )
    claims = report.get("claims") or []
    lines.append(f"claims: showing {len(claims)} of {report.get('claims_total')}")
    for index, claim in enumerate(claims, start=1):
        lines.append(
            f"claim {index}: label={claim.get('label')!r} "
            f"kind={claim.get('kind')} central={claim.get('central')}"
        )
        evidence = claim.get("evidence") or []
        if not evidence:
            lines.append("  evidence: none")
        for entry in evidence:
            lines.append(
                "  evidence: "
                f"ok={entry.get('ok')} reason={entry.get('reason')} "
                f"speaker={entry.get('speaker')} "
                f"attribution_mode={entry.get('attribution_mode')} "
                f"attribution_quote={entry.get('attribution_quote')!r}"
            )
    lines.append(f"sheet: {report.get('sheet_path')}")
    return "\n".join(lines)


def run(
    cluster_id: str,
    *,
    data_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    generate: Callable[..., Any] | None = None,
    now: dt.datetime | None = None,
    run_state: RunState | None = None,
    config: dict | None = None,
) -> dict:
    """Replay the facts stage for ``cluster_id`` and return its report.

    The gathered file must exist (a :class:`ReplayError` naming the missing path
    otherwise). The sheet is written to ``output_dir`` (a fresh temp directory
    when none is given) and never into the data tree. ``generate`` defaults to
    the real ``providers.generate``; tests pass a stub so no live call is made.
    """
    data_dir = Path(data_dir) if data_dir is not None else Path(DATA_DIR)
    gathered_path = data_dir / "gathered" / f"{cluster_id}.json"
    if not gathered_path.is_file():
        raise ReplayError(
            f"no gathered file for cluster {cluster_id!r}: expected {gathered_path}"
        )
    try:
        gathered = _read_json(gathered_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError(f"could not read gathered file {gathered_path}: {exc}") from exc

    title, entities = load_story_context(cluster_id, data_dir=data_dir)

    if output_dir is not None:
        output_dir = Path(output_dir)
    else:
        output_dir = Path(tempfile.mkdtemp(prefix="gx-replay-"))

    config = config if config is not None else facts_stage.load_config()
    score_section = config.get("score") or {}
    max_sources = int(
        score_section.get(
            "max_sources_per_candidate", facts_stage.DEFAULT_MAX_SOURCES_PER_CANDIDATE
        )
    )
    model_call_budget = int(
        score_section.get("model_call_budget", score_stage.DEFAULT_MODEL_CALL_BUDGET)
    )

    run_state = run_state if run_state is not None else RunState()
    now = now or dt.datetime.now(dt.timezone.utc)
    if generate is None:
        generate = default_generate

    sheet: dict | None = None
    try:
        sheet = facts_stage.facts(
            gathered,
            id=cluster_id,
            title=title,
            entities=entities,
            output_dir=output_dir,
            now=now,
            generate=generate,
            run_state=run_state,
            max_sources=max_sources,
            model_call_budget=model_call_budget,
        )
    except facts_stage.UnconfirmedStory:
        # An unconfirmed story still wrote its sheet under output_dir/unconfirmed.
        sheet = None
    except facts_stage.FactsError as exc:
        raise ReplayError(f"facts stage failed for cluster {cluster_id!r}: {exc}") from exc

    if sheet is None:
        sheet, sheet_path = _locate_sheet(output_dir, cluster_id)
    else:
        sheet_path = output_dir / f"{_sanitize_name(cluster_id)}.json"
    if sheet is None:
        raise ReplayError(
            f"facts stage produced no sheet for cluster {cluster_id!r} in {output_dir}"
        )

    return build_report(
        sheet,
        cluster_id=cluster_id,
        title=title,
        entities=entities,
        model_calls=run_state.calls,
        sheet_path=sheet_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.replay",
        description="Re-run the facts stage on a saved story.",
    )
    parser.add_argument("cluster_id", help="cluster id whose saved story to replay")
    parser.add_argument("--data-dir", default=None, help="override the data directory")
    parser.add_argument("--output-dir", default=None, help="directory for the replay sheet")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        report = run(
            args.cluster_id,
            data_dir=args.data_dir,
            output_dir=args.output_dir,
        )
    except ReplayError as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 2
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
