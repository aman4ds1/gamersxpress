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


class WriterInputError(Exception):
    """The writer was handed gathered source material instead of a facts sheet."""


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
    """Load the prompt template and substitute the facts sheet."""
    _reject_source_prose(facts, "facts")
    if template is None:
        template = load_template(template_path)
    if FACTS_SHEET_TOKEN not in template:
        raise ValueError(f"prompt template has no {FACTS_SHEET_TOKEN} placeholder: {template_path}")
    sheet = facts if isinstance(facts, str) else json.dumps(facts, indent=2, ensure_ascii=False)
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
    sheet = _numbered_facts(facts)
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
]