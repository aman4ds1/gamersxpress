"""Writer stage: turn a verified facts sheet into an article draft.

Calls the ``writer`` role via :func:`providers.generate`. ``run_state`` is
required so the writer's actual model family is recorded on the shared
:class:`RunState` for the verifier's fail-closed guard (PLAN.md principle 4).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from providers import Generation, RunState, generate as default_generate

SYSTEM_INSTRUCTION = (
    "You are a games journalist. Write an original news article using ONLY the "
    "verified facts sheet provided. Never state a rumor as fact. Do not invent "
    "claims, numbers, prices, dates or specs that are not in the facts sheet."
)


def build_prompt(facts: Any) -> str:
    facts_text = facts if isinstance(facts, str) else json.dumps(facts, indent=2, ensure_ascii=False)
    return f"{SYSTEM_INSTRUCTION}\n\nVerified facts sheet:\n{facts_text}\n\nWrite the article."


def write(
    facts: Any,
    *,
    run_state: RunState,
    prompt: str | None = None,
    generate: Callable[..., Generation] = default_generate,
    **kwargs: Any,
) -> Generation:
    """Generate the draft and record the writer's family on ``run_state``."""
    prompt = prompt if prompt is not None else build_prompt(facts)
    return generate("writer", prompt, run_state=run_state, **kwargs)
