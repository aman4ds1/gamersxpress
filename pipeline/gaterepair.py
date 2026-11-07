"""Turn publish-gate failures into sentence-level repair requests.

The publish gates (``gates.py``) run after the article is written, checked and
given its SEO fields, and a failure used to end the run. Most failures name a
sentence or can be traced to some: a restated sentence, a date said a third
time, source wording copied over, a banned phrase. Those can be fixed by
deleting or rewording just those sentences, which is exactly what the repair
pass (``write.repair``) does.

:func:`repair_requests` maps each failure to the sentences behind it. When
*every* failure maps, the caller runs one repair and re-checks; when any
failure does not (a thin section, a missing source, a front-matter field), the
draft is rejected as before, because a partial fix could not make the gates pass.
"""

from __future__ import annotations

import re
from typing import Any

import gates
import verify as verify_stage

#: Why a sentence is flagged when a gate named it only by a quoted fragment.
GATE_REASON = "fails a publish check ({check}); rewrite it so the check passes, keeping every fact, date and name"

_QUOTED_RE = re.compile(r"'([^'\n]{8,})'|\"([^\"\n]{8,})\"")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _plain(text: str) -> str:
    """Lowercase, markdown links reduced to their text, whitespace collapsed."""
    return " ".join(_LINK_RE.sub(r"\1", str(text or "")).lower().split())


def _quoted_sentences(message: str, sentences: list[str]) -> list[str]:
    """The sentences containing a quoted fragment of a failure message."""
    snippets = [next(group for group in match.groups() if group) for match in _QUOTED_RE.finditer(message)]
    found: list[str] = []
    for snippet in snippets:
        wanted = _plain(snippet)
        for sentence in sentences:
            shown = _plain(sentence)
            if (wanted in shown or (len(shown) >= 20 and shown in wanted)) and sentence not in found:
                found.append(sentence)
    return found


def repair_requests(
    failures: list[str],
    body: str,
    *,
    source_texts: list[str] | None = None,
    name_text: str = "",
    settings: dict[str, Any] | None = None,
) -> tuple[list[dict], list[str]]:
    """``(requests, unfixable)`` for a list of gate failure messages.

    ``requests`` are ``{"clause", "reason"}`` for the repair pass, one per
    sentence; ``unfixable`` are the failures no sentence could be found for.
    """
    settings = settings if settings is not None else gates.load_gates_config()
    defaults = gates.DEFAULT_GATES
    requests: dict[str, str] = {}
    unfixable: list[str] = []
    sentences = gates._sentences(body)
    for failure in failures:
        message = str(failure)
        found: list[tuple[str, str]] = []
        if message.startswith("repeated fact:"):
            limit = int(settings.get("max_repeat_tokens", defaults["max_repeat_tokens"]))
            found = [
                (item["sentence"], verify_stage.REPEATED_FACT_REASON.format(fact=item["fact"]))
                for item in gates.repeated_fact_sentences(body, limit, name_text=name_text)
            ]
        elif message.startswith("near-duplicate sentences"):
            threshold = float(settings.get("near_duplicate_threshold", defaults["near_duplicate_threshold"]))
            found = [
                (item["sentence"], verify_stage.NEAR_DUPLICATE_REASON)
                for item in gates.near_duplicate_sentences(body, threshold)
            ]
        elif message.startswith("article reuses") and source_texts:
            overlap = settings.get("overlap") or defaults["overlap"]
            ngram, limit = int(overlap["ngram"]), float(overlap["max_ratio"])
            found = [
                (item["sentence"], verify_stage.OVERLAP_REASON.format(count=item["copied"], ngram=ngram))
                for item in gates.overlap_sentences(body, source_texts, ngram, limit * verify_stage.OVERLAP_MARGIN)
            ]
        else:
            found = [(sentence, GATE_REASON.format(check=message)) for sentence in _quoted_sentences(message, sentences)]
        if not found:
            unfixable.append(message)
        for sentence, reason in found:
            requests.setdefault(sentence, reason)
    return [{"clause": sentence, "reason": reason} for sentence, reason in requests.items()], unfixable
