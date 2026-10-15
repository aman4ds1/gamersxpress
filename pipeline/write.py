"""Writer stage: turn a verified facts sheet into an article draft.

PLAN.md principle 3 ("facts first"): the writer sees only the verified facts
sheet, never the source articles' prose. The prompt lives in
``prompts/writer.md`` with a ``{{facts_sheet}}`` placeholder; this module
substitutes the sheet and calls the ``writer`` role via
:func:`providers.generate`. ``run_state`` is required so the writer's actual
model family is recorded on the shared :class:`RunState` for the verifier's
fail-closed guard (PLAN.md principle 4).

Anything that looks like gathered source material (a ``text`` or
``gathered_at`` field) is rejected with :class:`WriterInputError` instead of
being sent to the model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from providers import Generation, RunState, generate as default_generate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "writer.md"
DEFAULT_REPAIR_PROMPT_PATH = PROJECT_ROOT / "prompts" / "writer-repair.md"
FACTS_SHEET_TOKEN = "{{facts_sheet}}"
ARTICLE_TOKEN = "{{article}}"
UNSUPPORTED_CLAUSES_TOKEN = "{{unsupported_clauses}}"

# Fields that only exist on gathered source material. The writer must never
# receive them (PLAN.md principle 3).
SOURCE_PROSE_KEYS = frozenset({"text", "gathered_at"})

#: Absolute calendar dates the writer may copy verbatim: ISO (2026-10-14) and
#: month-name forms ("October 14, 2026"). The facts stage absolutizes relative
#: time before a claim is kept, so anything found here is already a calendar date.
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_MONTH_DATE_RE = re.compile(
    r"\b(?:" + "|".join(_MONTH_NAMES) + r")\s+\d{1,2},\s+\d{4}\b"
)

#: The only per-claim fields the writer needs. Everything else on a claim
#: (``source_urls``, ``confidence``, ``versions``) is pipeline detail: the merged
#: ``value`` already carries every source's wording, rule 15 forbids printing a
#: confidence, and the front matter sources are copied from ``sources`` below.
_CLAIM_FIELDS = ("claim", "value", "kind", "origin", "affiliation")


class WriterInputError(Exception):
    """The writer was handed gathered source material instead of a facts sheet."""


def _absolute_dates(text: Any) -> list[str]:
    """Calendar dates named in ``text``, in order, without duplicates."""
    raw = str(text or "")
    found: list[str] = []
    for match in _ISO_DATE_RE.finditer(raw):
        found.append(match.group(0))
    for match in _MONTH_DATE_RE.finditer(raw):
        found.append(match.group(0))
    seen: set[str] = set()
    unique: list[str] = []
    for date in found:
        if date not in seen:
            seen.add(date)
            unique.append(date)
    return unique


def _source_url_names(sources: Any) -> dict[str, str]:
    """Map each source URL to its display name for per-claim attribution."""
    mapping: dict[str, str] = {}
    if not isinstance(sources, list):
        return mapping
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = source.get("link") or source.get("url")
        name = source.get("source_name") or source.get("name")
        if isinstance(url, str) and url and isinstance(name, str) and name:
            mapping[url] = name
    return mapping


def _writer_claim(claim: Any, url_names: dict[str, str]) -> Any:
    """Reduce one claim to the fields the writer uses."""
    if not isinstance(claim, dict):
        return claim
    reduced: dict[str, Any] = {field: claim.get(field, "") for field in _CLAIM_FIELDS}
    urls = claim.get("source_urls")
    if not isinstance(urls, list):
        single = claim.get("source_url")
        urls = [single] if single else []
    names = sorted({url_names[url] for url in urls if url in url_names})
    if names:
        reduced["sources"] = names
    # The central flag is what the writer leads with, so it is always present.
    reduced["central"] = bool(claim.get("central"))
    dates = _absolute_dates(claim.get("value")) or _absolute_dates(claim.get("claim"))
    if dates:
        reduced["dates"] = dates
    return reduced


def writer_view(facts: Any) -> Any:
    """A compact, writer-only view of a facts sheet.

    Keeps what the writer needs: ``generated_at`` (rule 64: the ``pubDate``), the
    front-matter ``sources`` (name + url, rule 68) and each claim's text, value,
    kind, origin, affiliation, the central flag and any absolute dates it names.
    Drops pipeline detail the writer never uses: coherence, grounding,
    confirmation internals, ``origins_downgraded`` diagnostics, ``extractor``,
    ``fingerprint``, ``confidence`` and the per-source ``versions``/``source_urls``.
    A non-dict sheet (or a pre-rendered string) is returned unchanged.
    """
    if not isinstance(facts, dict):
        return facts
    view: dict[str, Any] = {}
    if facts.get("generated_at"):
        view["generated_at"] = facts["generated_at"]
    sources = facts.get("sources")
    if isinstance(sources, list):
        view["sources"] = [
            {
                "name": source.get("source_name") or source.get("name"),
                "url": source.get("link") or source.get("url"),
            }
            for source in sources
            if isinstance(source, dict)
        ]
    claims = facts.get("claims")
    if isinstance(claims, list):
        url_names = _source_url_names(sources)
        view["claims"] = [_writer_claim(claim, url_names) for claim in claims]
    return view


def load_template(path: str | Path = DEFAULT_PROMPT_PATH) -> str:
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"writer prompt not found: {path}") from exc


def _reject_source_prose(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in SOURCE_PROSE_KEYS:
                raise WriterInputError(
                    f"refusing to build the writer prompt: {where}.{key} looks like "
                    "gathered source material. The writer receives only a facts "
                    "sheet (PLAN.md principle 3)."
                )
            _reject_source_prose(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_source_prose(item, f"{where}[{index}]")


def build_prompt(
    facts: Any,
    *,
    template: str | None = None,
    template_path: str | Path = DEFAULT_PROMPT_PATH,
) -> str:
    """Load the prompt template and substitute the compact facts-sheet view."""
    _reject_source_prose(facts, "facts")
    if template is None:
        template = load_template(template_path)
    if FACTS_SHEET_TOKEN not in template:
        raise ValueError(f"prompt template has no {FACTS_SHEET_TOKEN} placeholder: {template_path}")
    sheet = (
        facts
        if isinstance(facts, str)
        else json.dumps(writer_view(facts), indent=2, ensure_ascii=False)
    )
    return template.replace(FACTS_SHEET_TOKEN, sheet)


def _numbered_facts(facts: Any) -> str:
    """Render a facts sheet with claims numbered F1, F2, ... for the repair prompt."""
    if not isinstance(facts, dict):
        return facts if isinstance(facts, str) else json.dumps(facts, indent=2, ensure_ascii=False)
    claims = facts.get("claims")
    if not isinstance(claims, list) or not claims:
        return json.dumps(facts, indent=2, ensure_ascii=False)
    lines: list[str] = []
    for index, claim in enumerate(claims, start=1):
        text = str(claim.get("claim", "")).strip()
        value = str(claim.get("value", "")).strip()
        lines.append(f"F{index}: {text!r}" + (f" (value: {value!r})" if value else ""))
    return "\n".join(lines)


def build_repair_prompt(
    article: Any,
    unsupported_clauses: list[str],
    facts: Any,
    *,
    template: str | None = None,
    template_path: str | Path = DEFAULT_REPAIR_PROMPT_PATH,
) -> str:
    """Load the repair template and substitute the article, clauses and facts sheet."""
    _reject_source_prose(facts, "facts")
    if template is None:
        template = load_template(template_path)
    for token in (UNSUPPORTED_CLAUSES_TOKEN, ARTICLE_TOKEN, FACTS_SHEET_TOKEN):
        if token not in template:
            raise ValueError(f"repair prompt template has no {token} placeholder: {template_path}")
    clauses = "\n".join(f"- {clause}" for clause in unsupported_clauses) or "- (none)"
    sheet = _numbered_facts(writer_view(facts))
    # Substitute the article last so its own text is never rescanned for tokens.
    text = template.replace(UNSUPPORTED_CLAUSES_TOKEN, clauses).replace(FACTS_SHEET_TOKEN, sheet)
    article_text = article if isinstance(article, str) else json.dumps(article, indent=2, ensure_ascii=False)
    return text.replace(ARTICLE_TOKEN, article_text)


def write(
    facts: Any,
    *,
    run_state: RunState,
    prompt: str | None = None,
    generate: Callable[..., Generation] = default_generate,
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    **kwargs: Any,
) -> Generation:
    """Generate the draft and record the writer's family on ``run_state``."""
    prompt = prompt if prompt is not None else build_prompt(facts, template_path=prompt_path)
    return generate("writer", prompt, run_state=run_state, **kwargs)


def repair(
    article: Any,
    unsupported_clauses: list[str],
    facts: Any,
    *,
    run_state: RunState,
    prompt: str | None = None,
    generate: Callable[..., Generation] = default_generate,
    prompt_path: str | Path = DEFAULT_REPAIR_PROMPT_PATH,
    **kwargs: Any,
) -> Generation:
    """Ask the writer once to delete or re-ground the verifier's unsupported clauses."""
    if prompt is None:
        prompt = build_repair_prompt(article, unsupported_clauses, facts, template_path=prompt_path)
    return generate("writer", prompt, run_state=run_state, **kwargs)


__all__ = [
    "DEFAULT_PROMPT_PATH",
    "DEFAULT_REPAIR_PROMPT_PATH",
    "FACTS_SHEET_TOKEN",
    "ARTICLE_TOKEN",
    "UNSUPPORTED_CLAUSES_TOKEN",
    "WriterInputError",
    "build_prompt",
    "build_repair_prompt",
    "load_template",
    "repair",
    "write",
    "writer_view",
]