"""Verifier stage: independent claim check by a different model family.

``run_state`` is required and must already carry the writer's family. If it
does not, :func:`providers.generate` fails closed with
:class:`MissingWriterFamilyError` before calling any provider, because the
verifier must never share the writer's model family (PLAN.md principle 4).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from providers import Generation, RunState, generate as default_generate

SYSTEM_INSTRUCTION = (
    "You are an independent fact checker. Compare every claim, number, price, "
    "date and spec in the draft against the facts sheet and report anything "
    "unsupported or contradicted. Do not assume facts that are not listed."
)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


def build_prompt(draft: Any, facts: Any) -> str:
    return (
        f"{SYSTEM_INSTRUCTION}\n\nDraft:\n{_as_text(draft)}"
        f"\n\nFacts sheet:\n{_as_text(facts)}"
    )


def verify(
    draft: Any,
    facts: Any = None,
    *,
    run_state: RunState,
    prompt: str | None = None,
    generate: Callable[..., Generation] = default_generate,
    **kwargs: Any,
) -> Generation:
    """Generate the independent check and record the verifier's family."""
    prompt = prompt if prompt is not None else build_prompt(draft, facts)
    return generate("verifier", prompt, run_state=run_state, **kwargs)
