"""Re-run the facts stage, or a full draft, on a saved story.

A story that has left the feeds is normally impossible to re-check: its cluster
never forms again, so ``pipeline.run`` cannot reach the facts stage for it. This
module replays that one stage on what a past run already saved, so a fix to the
extraction, grounding, coherence, grouping, evidence or confirmation code can be
tested on real text after the story is gone.

Usage::

    python -m pipeline.replay <cluster_id>
    python -m pipeline.replay <cluster_id> --through draft

It reads ``data/gathered/<id>.json`` (the saved source text) and the story title
and entity list from ``data/facts/unconfirmed/<id>.json`` or
``data/facts/<id>.json``, whichever exists, falling back to the newest
``candidates-*.json`` report entry with that cluster id.

It calls the same :func:`facts.facts` function ``run.py`` calls, with the same
model roles, fallbacks and config, but writes everything to a temp directory
only. It never writes to ``data/facts``, ``data/failed.json``,
``data/breaker.json``, ``data/reports``, ``src``, ``public``, ``drafts`` or any
other tracked file, and never touches the circuit breaker or publishes.

``--through draft`` continues only when the confirmation verdict is confirmed.
It then runs write, verify, one repair when needed, SEO, cover and the publish
gates with the real stage functions and model roles, still writing every file
under the temp directory. The gate site check runs against a temp content
directory, exactly as a dry run does. The whole replay is capped at
:data:`MAX_MODEL_CALLS` model calls; exceeding it stops the replay with a clear
message. An unconfirmed sheet stops after facts and prints
``not confirmed: <rule> <reason>``.

It prints the facts report as before, then the verify result (passed or failed,
the unsupported clauses), whether a repair ran and its outcome, the SEO fields,
the gate results, the full article text and the temp article/folder paths.

The report has to survive a Windows console. :func:`render_report` emits ASCII
punctuation only (:data:`ASCII_PUNCT`), stdout is reconfigured to replace any
character the console code page cannot encode, and the complete report is also
written as UTF-8 next to the sheet. The last printed line names both files, and
a failure while reporting says so instead of hiding a finished run.
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

import cover as cover_stage
import facts as facts_stage
import gates as gates_stage
import providers
import publish as publish_stage
import score as score_stage
import seo as seo_stage
import verify as verify_stage
import write as write_stage
from providers import PROJECT_ROOT, RunState, generate as default_generate

logger = logging.getLogger("gamersxpress.replay")

#: Root of the saved pipeline state. Tests monkeypatch this to redirect every
#: read at a temporary data tree.
DATA_DIR = PROJECT_ROOT / "data"

#: Most model calls one replay may make across every stage it runs.
MAX_MODEL_CALLS = 12

#: Read-only inputs shared with the real pipeline (never written by a replay).
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"
PAGE_TEMPLATE = PROJECT_ROOT / "src" / "pages" / "news" / "[id].astro"

#: How many claims the printed report shows.
CLAIM_REPORT_LIMIT = 8

#: How many words of an attribution quote the printed report keeps.
ATTRIBUTION_QUOTE_WORDS = 15

#: Fixed note at the end of every report, printed and saved.
REPORT_NOTE = (
    "notes: gates read the real data/entities.json; "
    "the duplicate-article check sees an empty temp content folder"
)

#: Typographic characters converted to plain ASCII before printing, so a
#: Windows console on an ANSI code page (cp932, cp1252, ...) can encode them.
#: Letters are never touched: a Japanese label stays Japanese and the console
#: replaces it instead of raising UnicodeEncodeError.
ASCII_PUNCT = {
    # dashes and hyphens
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-", "\ufe58": "-", "\uff0d": "-", "\u2043": "-",
    # single quotes
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2032": "'", "\u00b4": "'", "\uff07": "'", "\u02b9": "'",
    # double quotes
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u00ab": '"', "\u00bb": '"', "\uff02": '"',
    # ellipsis
    "\u2026": "...",
    # spaces and invisible characters
    "\u00a0": " ", "\u3000": " ", "\u2002": " ", "\u2003": " ", "\u2009": " ",
    "\u200b": "", "\ufeff": "", "\u00ad": "",
    # arrows
    "\u2190": "<-", "\u2192": "->", "\u2194": "<->", "\u2191": "^", "\u2193": "v",
    # bullets
    "\u2022": "-", "\u00b7": "-", "\u30fb": "-", "\u25cf": "-",
    # fullwidth and CJK punctuation
    "\u3001": ",", "\u3002": ".", "\uff01": "!", "\uff1f": "?", "\uff1a": ":",
    "\uff1b": ";", "\uff08": "(", "\uff09": ")", "\u300c": '"', "\u300d": '"',
    "\u300e": '"', "\u300f": '"', "\u3010": "[", "\u3011": "]", "\uff3b": "[",
    "\uff3d": "]", "\uff5b": "{", "\uff5d": "}", "\uff5e": "~", "\uff05": "%",
    "\uff0a": "*", "\uff0b": "+", "\uff1d": "=", "\uff20": "@", "\uff03": "#",
    "\uff06": "&", "\uff0f": "/", "\uff5c": "|",
}

#: The translation table built from :data:`ASCII_PUNCT`.
ASCII_PUNCT_TABLE = str.maketrans(ASCII_PUNCT)


class ReplayError(Exception):
    """A replay cannot start or the facts stage could not produce a sheet."""


class BudgetExceeded(Exception):
    """The replay would need more model calls than its fixed budget allows."""


def _budget_generate(
    generate: Callable[..., Any], run_state: RunState, limit: int
) -> Callable[..., Any]:
    """Wrap ``generate`` so a replay can never exceed ``limit`` model calls.

    The check happens before the call, so the cap is a hard ceiling even when a
    stage retries (SEO validates twice) or repairs a draft.
    """

    def wrapped(role: str, prompt: str, *args: Any, **kwargs: Any) -> Any:
        if run_state.calls >= limit:
            raise BudgetExceeded(
                f"model call budget exceeded ({run_state.calls} of {limit})"
            )
        return generate(role, prompt, *args, **kwargs)

    return wrapped


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


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


def load_saved_sheet(path: str | Path) -> tuple[dict, Path]:
    """Read a saved facts sheet for ``--use-sheet``; refuse anything unusable.

    The file is only ever read. A usable sheet is a JSON object with a
    non-empty ``claims`` list and a ``confirmation`` object, which is what the
    write stage and the report read.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise ReplayError(f"saved sheet not found: {source}")
    try:
        sheet = _read_json(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"could not read saved sheet {source}: {exc}") from exc
    if not isinstance(sheet, dict):
        raise ReplayError(f"saved sheet {source} is not a JSON object")
    claims = sheet.get("claims")
    if not isinstance(claims, list) or not any(isinstance(item, dict) for item in claims):
        raise ReplayError(f"saved sheet {source} has no claims")
    if not isinstance(sheet.get("confirmation"), dict):
        raise ReplayError(f"saved sheet {source} has no confirmation block")
    return sheet, source


def _place_saved_sheet(source: Path, sheet: dict, output_dir: Path, cluster_id: str) -> Path:
    """Copy the saved sheet into ``output_dir`` as a facts run would have left it.

    A confirmed sheet goes to ``<id>.json`` and an unconfirmed one to
    ``unconfirmed/<id>.json``, so the rest of the replay finds it where it
    finds a fresh sheet. The bytes are copied exactly; the original is never
    modified.
    """
    name = _sanitize_name(cluster_id)
    confirmed = bool((sheet.get("confirmation") or {}).get("confirmed"))
    target = output_dir / (f"{name}.json" if confirmed else f"unconfirmed/{name}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.resolve() != source.resolve():
        target.write_bytes(source.read_bytes())
    return target


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


def _ascii_punct(text: object) -> str:
    """``text`` with every typographic character replaced by ASCII."""
    return str(text or "").translate(ASCII_PUNCT_TABLE)



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
    output_dir: Path | None = None,
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
        "output_dir": str(output_dir) if output_dir is not None else None,
    }


def render_report(report: dict, *, limit: int | None = CLAIM_REPORT_LIMIT) -> str:
    """Render a replay report as the short block the CLI prints.

    Typographic punctuation is converted to ASCII (see :data:`ASCII_PUNCT`), so
    what is emitted never trips a console code page. Letters are kept as they
    are; the CLI's stdout replaces any of those the console cannot encode.
    ``limit`` caps the claims shown (:data:`CLAIM_REPORT_LIMIT` by default,
    every claim when None).
    """
    title = _ascii_punct(report.get("title") or "(no title)")
    lines = [f"replay: {report.get('cluster_id')} - {title}"]
    lines.append(f"model calls: {report.get('model_calls')}")
    if report.get("reused_sheet"):
        lines.append(
            f"sheet reused from: {report.get('reused_sheet')} "
            "(extraction and grouping skipped)"
        )
    confirmation = report.get("confirmation") or {}
    lines.append(
        "confirmation: "
        f"rule={confirmation.get('rule')} "
        f"confirmed={confirmation.get('confirmed')} "
        f"reason={_ascii_punct(confirmation.get('reason'))}"
    )
    fact_groups = report.get("fact_groups") or {}
    lines.append(
        f"fact_groups: accepted={fact_groups.get('accepted')} "
        f"dropped={fact_groups.get('dropped')}"
    )
    claims = list(report.get("claims") or [])
    shown = claims if limit is None else claims[:limit]
    lines.append(f"claims: showing {len(shown)} of {report.get('claims_total')}")
    for index, claim in enumerate(shown, start=1):
        lines.append(
            f"claim {index}: label={_ascii_punct(claim.get('label'))!r} "
            f"kind={claim.get('kind')} central={claim.get('central')}"
        )
        evidence = claim.get("evidence") or []
        if not evidence:
            lines.append("  evidence: none")
        for entry in evidence:
            lines.append(
                "  evidence: "
                f"ok={entry.get('ok')} reason={_ascii_punct(entry.get('reason'))} "
                f"speaker={_ascii_punct(entry.get('speaker'))} "
                f"attribution_mode={entry.get('attribution_mode')} "
                f"attribution_quote={_ascii_punct(entry.get('attribution_quote'))!r}"
            )
    lines.append(f"sheet: {report.get('sheet_path')}")

    if report.get("not_confirmed"):
        verdict = report.get("confirmation") or {}
        lines.append(
            f"not confirmed: {verdict.get('rule')} {_ascii_punct(verdict.get('reason'))}"
        )
    if report.get("through"):
        _render_draft_report(lines, report)
    lines.append(REPORT_NOTE)
    return _ascii_punct("\n".join(lines))


def _render_draft_report(lines: list[str], report: dict) -> None:
    """Append the write/verify/repair/SEO/cover/gates part of a replay report."""
    lines.append(f"through: {report.get('through')} status={report.get('status')}")
    if report.get("error"):
        lines.append(f"error: {_ascii_punct(report.get('error'))}")

    verify = report.get("verify") or {}
    if verify:
        unsupported = list(verify.get("unsupported") or [])
        lines.append(
            f"verify: {'passed' if verify.get('passed') else 'failed'} "
            f"unsupported_clauses={len(unsupported)} "
            f"failure_reason={verify.get('failure_reason')}"
        )
        for clause in unsupported:
            lines.append(f"  unsupported: {_ascii_punct(clause)!r}")
        for number in verify.get("unmatched_numbers") or []:
            lines.append(f"  unmatched number: {_ascii_punct(number)!r}")

    repair = report.get("repair") or {}
    if repair:
        if repair.get("attempted"):
            lines.append(
                f"repair: ran outcome={repair.get('outcome')} "
                f"problems={len(repair.get('problems') or [])}"
            )
            for problem in repair.get("problems") or []:
                lines.append(f"  repair problem: {_ascii_punct(problem)}")
        else:
            lines.append("repair: not needed")

    seo = report.get("seo") or {}
    if seo:
        lines.append(
            f"seo: title={_ascii_punct(seo.get('title'))!r} "
            f"description={_ascii_punct(seo.get('description'))!r} "
            f"slug={seo.get('slug')} "
            f"tags={seo.get('tags')} entities={seo.get('entities')}"
        )

    cover = report.get("cover") or {}
    if cover:
        lines.append(f"cover: {cover.get('path')}")

    gates = report.get("gates") or {}
    if gates:
        lines.append(
            f"gates: {'passed' if gates.get('passed') else 'failed'} "
            f"failures={len(gates.get('failures') or [])} "
            f"warnings={len(gates.get('warnings') or [])}"
        )
        for failure in gates.get("failures") or []:
            lines.append(f"  gate failure: {_ascii_punct(failure)}")
        for warning in gates.get("warnings") or []:
            lines.append(f"  gate warning: {_ascii_punct(warning)}")

    article = report.get("article")
    if article is not None:
        lines.append("article:")
        lines.append(_ascii_punct(article))
    if report.get("article_path"):
        lines.append(f"temp article: {report.get('article_path')}")
    if report.get("temp_dir"):
        lines.append(f"temp folder: {report.get('temp_dir')}")



def _resolve_output_dir(output_dir: str | Path) -> Path:
    """Resolve an explicit output dir and refuse one inside the repository."""
    path = Path(output_dir).resolve()
    root = PROJECT_ROOT.resolve()
    if path == root or root in path.parents:
        raise ReplayError("output dir must be outside the repository")
    return path


def run(
    cluster_id: str,
    *,
    data_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    generate: Callable[..., Any] | None = None,
    now: dt.datetime | None = None,
    run_state: RunState | None = None,
    config: dict | None = None,
    through: str | None = None,
    max_model_calls: int | None = None,
    site_checks: Callable[[str, str], tuple[bool, str]] | None = None,
    link_checker: Callable[[str], Any] | None = None,
    use_sheet: str | Path | None = None,
) -> dict:
    """Replay the facts stage (or a full draft) for ``cluster_id``.

    The gathered file must exist (a :class:`ReplayError` naming the missing path
    otherwise). The sheet is written to ``output_dir`` (a fresh temp directory
    when none is given) and never into the data tree. An explicit ``output_dir``
    that resolves to the repository or a path under it is refused. ``generate``
    defaults to the real ``providers.generate``; tests pass a stub so no live
    call is made.

    ``use_sheet`` names a saved facts sheet to reuse: extraction, grounding and
    grouping are skipped (no model call), the sheet is copied into
    ``output_dir`` unchanged, and the replay continues from it. A draft replay
    run on one saved sheet is then comparable from run to run, because the
    writer and verifier always see the same facts. The saved file is only read.

    ``through="draft"`` continues after a confirmed facts sheet through write,
    verify, one repair, SEO, cover and the publish gates, all inside
    ``output_dir``. ``max_model_calls`` (default :data:`MAX_MODEL_CALLS`) caps
    the whole replay.
    """
    if through not in (None, "draft"):
        raise ReplayError(f"unknown replay target {through!r}; expected 'draft'")

    if output_dir is not None:
        output_dir = _resolve_output_dir(output_dir)

    saved_sheet: dict | None = None
    saved_source: Path | None = None
    if use_sheet is not None:
        saved_sheet, saved_source = load_saved_sheet(use_sheet)

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
    if saved_sheet is not None:
        if not title and saved_sheet.get("title"):
            title = str(saved_sheet["title"])
        if not entities:
            coherence = saved_sheet.get("coherence") or {}
            entities = [
                str(entity)
                for entity in (
                    coherence.get("canonical_entities") or coherence.get("primary_entities") or []
                )
            ]

    if output_dir is None:
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

    model_limit = MAX_MODEL_CALLS if max_model_calls is None else int(max_model_calls)
    if through == "draft":
        generate = _budget_generate(generate, run_state, model_limit)
        model_call_budget = min(model_call_budget, model_limit)

    sheet: dict | None = None
    sheet_path: Path | None = None
    if saved_sheet is not None and saved_source is not None:
        sheet = saved_sheet
        sheet_path = _place_saved_sheet(saved_source, saved_sheet, output_dir, cluster_id)
        logger.info(
            "replay: reusing saved sheet %s (extraction and grouping skipped)", saved_source
        )
    else:
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
        except BudgetExceeded as exc:
            raise ReplayError(str(exc)) from exc

        if sheet is None:
            sheet, sheet_path = _locate_sheet(output_dir, cluster_id)
        else:
            sheet_path = output_dir / f"{_sanitize_name(cluster_id)}.json"
        if sheet is None:
            raise ReplayError(
                f"facts stage produced no sheet for cluster {cluster_id!r} in {output_dir}"
            )

    report = build_report(
        sheet,
        cluster_id=cluster_id,
        title=title,
        entities=entities,
        model_calls=run_state.calls,
        sheet_path=sheet_path,
        output_dir=output_dir,
    )
    if saved_source is not None:
        report["reused_sheet"] = str(saved_source)
    confirmation = sheet.get("confirmation") or {}
    if not confirmation.get("confirmed"):
        if through == "draft":
            report["through"] = through
            report["status"] = "not_confirmed"
            report["not_confirmed"] = True
        return report
    if through != "draft":
        return report

    return _run_draft_stages(
        report,
        sheet,
        gathered=gathered,
        cluster_id=cluster_id,
        data_dir=data_dir,
        output_dir=output_dir,
        generate=generate,
        run_state=run_state,
        now=now,
        verify_config=verify_stage.load_verify_config(),
        gates_config=gates_stage.load_gates_config(),
        site_checks=site_checks,
        link_checker=link_checker,
    )


def _run_draft_stages(
    report: dict,
    sheet: dict,
    *,
    gathered: dict,
    cluster_id: str,
    data_dir: Path,
    output_dir: Path,
    generate: Callable[..., Any],
    run_state: RunState,
    now: dt.datetime,
    verify_config: dict,
    gates_config: dict,
    site_checks: Callable[[str, str], tuple[bool, str]] | None,
    link_checker: Callable[[str], Any] | None,
) -> dict:
    """Run write, verify, repair, SEO, cover and gates inside ``output_dir``."""
    reports_dir = output_dir / "reports"
    drafts_dir = output_dir / "drafts"
    content_dir = output_dir / "content"
    covers_dir = output_dir / "covers"
    article_dir = output_dir / "article"
    for directory in (reports_dir, drafts_dir, content_dir, covers_dir, article_dir):
        directory.mkdir(parents=True, exist_ok=True)

    title = report.get("title") or cluster_id
    provisional_slug = seo_stage.slugify(title)
    draft = ""
    article = ""
    article_path: Path | None = None
    verify_report: verify_stage.VerifyReport | None = None
    repair_info: dict = {"attempted": False}
    seo: dict = {}
    cover: dict = {}
    gate_result: gates_stage.GateResult | None = None
    status = "completed"
    error: str | None = None

    report["through"] = "draft"
    report["temp_dir"] = str(output_dir)
    report["repair"] = repair_info

    def finish() -> dict:
        nonlocal article_path
        report["status"] = status
        report["error"] = error
        report["model_calls"] = run_state.calls
        if verify_report is not None:
            report["verify"] = {
                "passed": verify_report.passed,
                "unsupported": list(verify_report.unsupported),
                "unmatched_numbers": list(verify_report.unmatched_numbers),
                "failure_reason": verify_report.failure_reason,
            }
        if seo:
            report["seo"] = {
                "title": seo.get("title"),
                "description": seo.get("description"),
                "slug": seo.get("slug"),
                "tags": list(seo.get("tags") or []),
                "entities": list(seo.get("entities") or []),
                "category": seo.get("category"),
            }
        if cover:
            report["cover"] = {
                "path": cover.get("path"),
                "image": cover.get("image"),
                "bytes": cover.get("bytes"),
            }
        if gate_result is not None:
            report["gates"] = gate_result.to_dict()
        text = article or draft
        if text and article_path is None:
            article_path = _write_text(
                article_dir / f"{provisional_slug}.md", text
            )
        report["article"] = text
        if article_path is not None:
            report["article_path"] = str(article_path)
        return report

    try:
        writer = write_stage.write(sheet, run_state=run_state, generate=generate)
        draft = writer.value if isinstance(writer.value, str) else str(writer.value)

        verify_report = verify_stage.verify(
            draft,
            sheet,
            run_state=run_state,
            generate=generate,
            report_dir=reports_dir,
            id=cluster_id,
            verify_config=verify_config,
        )

        if verify_report.failure_reason == "article_rejected" and verify_report.unsupported:
            changed_clauses = list(verify_report.unsupported)
            reasons = {
                str(sentence.get("clause") or ""): str(sentence.get("difference") or "").strip()
                for sentence in verify_report.model_sentences or []
                if isinstance(sentence, dict)
            }
            flagged = [
                {
                    "clause": clause,
                    "reason": reasons.get(clause) or "flagged as unsupported by the facts",
                }
                for clause in changed_clauses
            ]
            repaired = write_stage.repair(
                draft, flagged, sheet, run_state=run_state, generate=generate
            )
            repair_info.update(
                attempted=True,
                changed_clauses=changed_clauses,
                original_chars=len(draft),
                repaired_chars=len(repaired.value),
                paths=publish_stage.save_repair(
                    draft,
                    repaired.value,
                    slug=provisional_slug,
                    drafts_dir=drafts_dir,
                    changed_clauses=changed_clauses,
                ),
            )
            problems = write_stage.validate_repair(draft, repaired.value, changed_clauses)
            repair_info["problems"] = problems
            if problems:
                repair_info["accepted"] = False
                repair_info["outcome"] = "rejected"
                logger.warning(
                    "repair rejected (the original draft is kept): %s",
                    "; ".join(problems),
                )
            else:
                repair_info["accepted"] = True
                repair_info["outcome"] = "accepted"
                draft = repaired.value
                verify_report = verify_stage.verify(
                    draft,
                    sheet,
                    run_state=run_state,
                    generate=generate,
                    report_dir=reports_dir,
                    id=cluster_id,
                    verify_config=verify_config,
                    repair={
                        "attempted": True,
                        "changed_clauses": changed_clauses,
                        "original_chars": len(writer.value),
                        "repaired_chars": len(repaired.value),
                    },
                )
                repair_info["verify_passed_after"] = verify_report.passed

        if not verify_report.passed:
            status = "failed"
            error = f"verification failed ({verify_report.failure_reason or 'unknown'})"
            return finish()

        entities_path = output_dir / "entities.json"
        if not entities_path.exists():
            source_entities = data_dir / "entities.json"
            if source_entities.is_file():
                try:
                    entities_path.write_text(
                        source_entities.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                except OSError:
                    pass

        seo = seo_stage.generate_seo(
            draft,
            sheet,
            run_state=run_state,
            generate=generate,
            entities_path=entities_path,
            articles_dir=content_dir,
            drafts_dir=drafts_dir,
            config=verify_config,
        )
        slug = str(seo["slug"])
        cover = cover_stage.generate_cover(
            slug,
            seo["title"],
            seo["category"],
            output_dir=covers_dir,
            fonts_dir=FONTS_DIR,
        )
        front = publish_stage.build_front_matter(sheet, seo, cover)
        body = gates_stage.parse_front_matter(draft)[1]
        article = publish_stage.assemble_article(body, front)
        article_path = _write_text(article_dir / f"{slug}.md", article)

        gate_result = gates_stage.run_gates(
            article,
            slug=slug,
            facts=sheet,
            cluster_id=cluster_id,
            gathered=gathered,
            verify_report=verify_report.to_dict(),
            run_state=run_state,
            config=gates_config,
            articles_dir=content_dir,
            page_template=PAGE_TEMPLATE,
            now=now,
            link_checker=link_checker or (lambda url: 200),
            site_checks=site_checks,
        )
        if not gate_result.passed:
            status = "failed"
            error = "publish gates failed"
        return finish()
    except BudgetExceeded as exc:
        status = "budget_exceeded"
        error = str(exc)
        return finish()
    except (
        providers.SkipRun,
        providers.MissingWriterFamilyError,
        seo_stage.SeoError,
        cover_stage.CoverError,
        write_stage.WriterInputError,
    ) as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        return finish()
    except Exception as exc:  # noqa: BLE001 - keep the temp tree and report it
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        return finish()


def _report_path(report: dict) -> Path:
    """The UTF-8 report file's path: next to the sheet, inside the temp dir."""
    sheet_path = report.get("sheet_path")
    if sheet_path:
        sheet = Path(sheet_path)
        return sheet.parent / f"{sheet.stem}-report.txt"
    output_dir = report.get("output_dir")
    return (Path(output_dir) if output_dir else Path(".")) / "report.txt"


def _make_stdout_encodable() -> None:
    """Ask stdout to replace characters it cannot encode instead of raising."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(errors="replace")
    except (OSError, ValueError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.replay",
        description="Re-run the facts stage (or a full draft) on a saved story.",
    )
    parser.add_argument("cluster_id", help="cluster id whose saved story to replay")
    parser.add_argument("--data-dir", default=None, help="override the data directory")
    parser.add_argument("--output-dir", default=None, help="directory for the replay files")
    parser.add_argument(
        "--through",
        choices=("draft",),
        default=None,
        help="continue through write, verify, SEO, cover and gates after confirmed facts",
    )
    parser.add_argument(
        "--use-sheet",
        default=None,
        metavar="PATH",
        help=(
            "reuse this saved facts sheet instead of extracting again: no extraction "
            "or grouping calls, so a draft replay always starts from the same facts"
        ),
    )
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
            through=args.through,
            use_sheet=args.use_sheet,
        )
    except ReplayError as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 2

    _make_stdout_encodable()
    try:
        report_path = _report_path(report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(report, limit=None) + "\n", encoding="utf-8")
        print(render_report(report))
        print(f"report: {report_path} sheet: {report.get('sheet_path')}")
    except Exception as exc:
        print(f"report failed: {exc}", file=sys.stderr)
        print(f"sheet: {report.get('sheet_path')}", file=sys.stderr)
        return 3
    if report.get("status") in ("failed", "budget_exceeded"):
        return 3
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
