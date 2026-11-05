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
import logging
import re
from pathlib import Path
from typing import Any, Callable

from gates import find_leaks
from providers import (
    Generation,
    RunState,
    estimate_prompt_tokens,
    generate as default_generate,
)

logger = logging.getLogger(__name__)

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


def _outlet_names(sources: Any) -> set[str]:
    """Folded display names of the sheet's sources (the outlets)."""
    names: set[str] = set()
    for source in sources if isinstance(sources, list) else []:
        if isinstance(source, dict):
            name = source.get("source_name") or source.get("name")
            if isinstance(name, str) and name.strip():
                names.add(name.strip().lower())
    return names


def _origin_is_outlet(claim: dict, outlets: set[str]) -> bool:
    """True when a claim's origin is a news outlet, not a person or company."""
    origin = str(claim.get("origin") or "").strip().lower()
    return str(claim.get("speaker_type") or "").lower() == "outlet" or origin in outlets


def _writer_usable(claim: Any, outlets: set[str]) -> bool:
    """False for a claim the writer could only state by naming an outlet.

    The site writes in its own voice and never names an outlet in the text, so an
    opinion or statement that belongs to an outlet, or an opinion with no named
    origin, has no speaker to credit and is left out of the writer's sheet.
    """
    if not isinstance(claim, dict):
        return True
    kind = str(claim.get("kind") or "").lower()
    origin = str(claim.get("origin") or "").strip().lower()
    if kind == "opinion" and (not origin or origin == "unstated" or _origin_is_outlet(claim, outlets)):
        return False
    return not (kind == "statement" and _origin_is_outlet(claim, outlets))


def _writer_claim(claim: Any, outlets: set[str] = frozenset()) -> Any:
    """Reduce one claim to the fields the writer uses."""
    if not isinstance(claim, dict):
        return claim
    reduced: dict[str, Any] = {field: claim.get(field, "") for field in _CLAIM_FIELDS}
    if _origin_is_outlet(claim, outlets):
        # An outlet is not a speaker: the claim is stated directly, with no origin to name.
        reduced["origin"] = "unstated"
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
    No claim lists the outlets that reported it: the writer states facts in the
    site's own voice and never names an outlet in the text. Claims it could only
    state by naming an outlet (see :func:`_writer_usable`) are left out.
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
        outlets = _outlet_names(sources)
        view["claims"] = [
            _writer_claim(claim, outlets) for claim in claims if _writer_usable(claim, outlets)
        ]
    note = single_origin_note(facts)
    if note:
        view["single_origin_note"] = note
    return view


def single_origin_note(facts: dict) -> str:
    """The instruction a single-origin story adds to the writer's facts, or "".

    One named origin is reported by several outlets; nothing independent backs
    it. The note rides in the facts view, not the static prompt, so the prompt
    keeps its Groq size budget and only these stories pay for the extra tokens.
    """
    confirmation = facts.get("confirmation") if isinstance(facts, dict) else None
    if not isinstance(confirmation, dict) or confirmation.get("rule") != "single-origin":
        return ""
    origin = confirmation.get("single_origin") or {}
    speaker = str(origin.get("speaker") or "").strip()
    if not speaker:
        return ""
    return (
        f"This story rests on one named source, {speaker}, and nothing independent confirms it. "
        f"Write every claim as {speaker}'s statement, in the same sentence "
        f"(\"{speaker} said ...\", \"according to {speaker}\"); never state a claim in the "
        f"publication's own voice. Open by naming {speaker}. Do not name any outlet. "
        "Do not call it confirmed."
    )


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


#: The trimmed writer prompt aims under this many tokens, leaving the Groq
#: fallback room for its output minimum inside the provider's TPM limit.
WRITER_FALLBACK_TOKEN_BUDGET = 4300

#: Which claims the trimmed prompt keeps first. Central claims never drop.
_FALLBACK_KIND_PRIORITY = {
    "confirmed": 4,
    "statement": 4,
    "reported": 3,
    "estimate": 2,
    "opinion": 1,
    "rumor": 1,
}


def _fallback_claim_score(claim: dict, entities: set[str]) -> int:
    """How important ``claim`` is to the trimmed writer prompt."""
    score = _FALLBACK_KIND_PRIORITY.get(str(claim.get("kind") or "").lower(), 2)
    if claim.get("central"):
        score += 10
    text = _fold(f"{claim.get('claim') or ''} {claim.get('value') or ''}")
    if any(entity and entity in text for entity in entities):
        score += 2
    return score


def build_fallback_prompt(
    facts: Any,
    *,
    primary: str | None = None,
    template: str | None = None,
    template_path: str | Path = DEFAULT_PROMPT_PATH,
    budget: float = WRITER_FALLBACK_TOKEN_BUDGET,
) -> str:
    """The writer prompt with low-priority claims dropped to fit ``budget``.

    Only used when the primary prompt is too large for a provider's TPM limit.
    The primary prompt itself is never changed. Identical values are deduped
    first, then claims are ranked central > gate-needed entities >
    confirmed/statement > reported > estimate > opinion/rumor. The source list
    and single-origin note stay; central claims are never dropped.
    """
    primary = primary if primary is not None else build_prompt(
        facts, template=template, template_path=template_path
    )
    if estimate_prompt_tokens(primary) <= budget:
        return primary
    if not isinstance(facts, dict) or not isinstance(facts.get("claims"), list):
        return primary
    claims = [claim for claim in facts["claims"] if isinstance(claim, dict)]
    if not claims:
        return primary
    entities = {
        _fold(str(entity)) for entity in facts.get("entities") or [] if str(entity).strip()
    }
    deduped: dict[str, tuple[int, int, dict]] = {}
    for index, claim in enumerate(claims):
        key = _fold(str(claim.get("value") or claim.get("claim") or "")) or f"__{index}"
        score = _fallback_claim_score(claim, entities)
        if key not in deduped or score > deduped[key][0]:
            deduped[key] = (score, index, claim)
    kept = [
        claim
        for _, _, claim in sorted(
            deduped.values(), key=lambda item: (-item[0], item[1])
        )
    ]
    removed = 0
    text = primary
    while True:
        text = build_prompt({**facts, "claims": kept}, template=template, template_path=template_path)
        if estimate_prompt_tokens(text) <= budget:
            break
        droppable = [index for index, claim in enumerate(kept) if not claim.get("central")]
        if not droppable:
            break
        kept.pop(droppable[-1])
        removed += 1
    if removed:
        logger.info(
            "writer prompt trimmed: %d claim(s) dropped to fit %d tokens",
            removed, budget,
        )
    return text


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


def _clause_parts(entry: Any) -> tuple[str, str]:
    """Split a flagged-clause entry into ``(clause, reason)``; both may be empty."""
    if isinstance(entry, dict):
        return str(entry.get("clause") or "").strip(), str(entry.get("reason") or "").strip()
    if isinstance(entry, (tuple, list)) and len(entry) >= 2:
        return str(entry[0]).strip(), str(entry[1]).strip()
    return str(entry or "").strip(), ""


def _clause_line(entry: Any) -> str:
    """One ``- <clause>`` line for the repair prompt, with its reason when given."""
    clause, reason = _clause_parts(entry)
    if not clause:
        return ""
    return f"- {clause}" + (f" (flagged because: {reason})" if reason else "")


def _clause_lines_for_repair(article: Any, unsupported_clauses: list[Any]) -> list[str]:
    """The ``- <clause>`` lines for the repair prompt, dependents included.

    The verifier flags sentences; a sentence right after a flagged one that
    leans on it ("Beside this prediction, …") is unsupported too once the
    flagged sentence goes, so it is added to the flagged set here and shown to
    the writer as ``rewrite or delete``. A dependent already listed is not
    repeated.
    """
    clauses = list(unsupported_clauses or [])
    entries: list[Any] = list(clauses)
    article_text = article if isinstance(article, str) else ""
    if article_text:
        sentences = _sentences(article_text)
        is_flagged = _flagged_flags(sentences, clauses)
        listed = {
            _fold(_clause_parts(entry)[0])
            for entry in clauses
            if _clause_parts(entry)[0]
        }
        for sentence in _dependent_sentences(article_text, is_flagged):
            if _fold(sentence) not in listed:
                entries.append({"clause": sentence, "reason": DEPENDENT_CLAUSE_REASON})
    return [line for line in (_clause_line(entry) for entry in entries) if line]


def build_repair_prompt(
    article: Any,
    unsupported_clauses: list[Any],
    facts: Any,
    *,
    template: str | None = None,
    template_path: str | Path = DEFAULT_REPAIR_PROMPT_PATH,
) -> str:
    """Load the repair template and substitute the article, clauses and facts sheet.

    Each flagged clause is a string, or a mapping with ``clause`` and ``reason``
    when the verifier recorded why it was flagged; the reason is shown to the
    writer so it knows what to fix rather than guessing.
    """
    _reject_source_prose(facts, "facts")
    if template is None:
        template = load_template(template_path)
    for token in (UNSUPPORTED_CLAUSES_TOKEN, ARTICLE_TOKEN, FACTS_SHEET_TOKEN):
        if token not in template:
            raise ValueError(f"repair prompt template has no {token} placeholder: {template_path}")
    clauses = "\n".join(_clause_lines_for_repair(article, unsupported_clauses)) or "- (none)"
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
    if prompt is None:
        prompt = build_prompt(facts, template_path=prompt_path)
        fallback = build_fallback_prompt(
            facts, primary=prompt, template_path=prompt_path
        )
        if fallback != prompt:
            kwargs["fallback_prompt"] = fallback
    return generate("writer", prompt, run_state=run_state, **kwargs)


def repair(
    article: Any,
    unsupported_clauses: list[Any],
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


# --- surgical repair validation ----------------------------------------------
#
# The repair pass may delete or re-ground the clauses the verifier flagged and
# nothing else. ``validate_repair`` checks that promise on the text itself, so a
# repair that rewrites an untouched sentence, leaves a fragment or echoes the
# prompt's facts sheet is rejected before it costs a second verification call.

#: An added sentence must be a real sentence: at least this many words, opening
#: with a capital letter and closing with a terminal mark. This is what turns a
#: repair's leftover fragment into a rejection instead of a silent publish.
REPAIR_MIN_NEW_SENTENCE_WORDS = 6

#: The words a repair adds may not exceed the words it removes by more than
#: this, so a repair cannot smuggle an extra section into the article.
REPAIR_MAX_NET_ADDED_WORDS = 40

#: A source name: capitalized words with short connectors, so organization names
#: like "ASUS Republic of Gamers" or "Xbox Wire" are recognized as one source.
_NAME_WORD = r"[A-Z][\w.'\u2019&-]*"
_NAME_CONNECTOR = r"of|and|the|for|in|on|at|de|van"
_NAME = rf"{_NAME_WORD}(?:\s+(?:{_NAME_WORD}|{_NAME_CONNECTOR}))*"
#: "according to <Source>" and "<Source> states/says/claims/announced/..."
_ATTRIBUTION_LEAD_RE = re.compile(rf"(?i:according)\s+to\s+({_NAME})")
_ATTRIBUTION_VERB_RE = re.compile(
    rf"\b({_NAME})\s+(?:states|stated|state|says|said|say|claims|claimed|claim|"
    rf"announces|announced|announce|confirms|confirmed|confirm|reports|reported|"
    rf"report|reveals|revealed|reveal|adds|added|add|notes|noted|note)\b"
)
_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_LIST_ITEM_LINE_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+")

#: Anaphoric openers, longest first so a multi-word marker is matched whole. A
#: sentence that opens with one of these leans on the sentence just before it,
#: so it cannot stand on its own once that sentence is removed.
ANAPHORIC_MARKERS = (
    "in addition to this", "beside this", "besides this", "following this",
    "in contrast", "as mentioned", "as noted", "the above", "additionally",
    "however", "this", "that", "these", "those", "such", "also",
)

#: The reason shown to the writer for each dependent sentence.
DEPENDENT_CLAUSE_REASON = (
    "follows a flagged sentence and refers back to it; rewrite or delete"
)


def _norm(text: str) -> str:
    """Whitespace-normalized text: the only tolerance a repair comparison allows."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _fold(text: str) -> str:
    """Lowercased, quote- and dash-unified text for tolerant clause matching."""
    folded = str(text or "").casefold().replace("\u2019", "'").replace("\u2018", "'")
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014"):
        folded = folded.replace(dash, "-")
    return _norm(folded)


def _sentences(text: str) -> list[str]:
    """Sentences of ``text``; front-matter lines count as sentences of their own."""
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text or "") if part.strip()]


def _paragraph_sentence_groups(text: str) -> list[list[str]]:
    """``text``'s sentences grouped by Markdown paragraph (blank-line separated).

    Flattening the groups reproduces :func:`_sentences`, but the group boundary
    is what lets :func:`_dependent_sentences` see which sentences share a
    paragraph, so a dependent sentence is only paired with its neighbour.
    """
    groups: list[list[str]] = []
    for chunk in re.split(r"\n\s*\n", str(text or "")):
        sentences = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+|\n+", chunk)
            if part.strip()
        ]
        if sentences:
            groups.append(sentences)
    return groups


def _starts_with_anaphoric(sentence: str) -> bool:
    """True when ``sentence`` opens with one of :data:`ANAPHORIC_MARKERS`."""
    folded = _fold(sentence)
    if not folded:
        return False
    for marker in ANAPHORIC_MARKERS:
        if folded == marker:
            return True
        if folded.startswith(marker) and not folded[len(marker)].isalnum():
            return True
    return False


#: A flagged clause needs at least this many tokens to match a sentence fuzzily.
REPAIR_MIN_CLAUSE_TOKENS = 4
#: Share of a flagged clause's tokens a sentence must contain to be its source.
REPAIR_CLAUSE_OVERLAP = 0.8


def _clause_tokens(text: str) -> tuple[str, ...]:
    """Lowercased, punctuation-free tokens of a clause or sentence."""
    return tuple(re.findall(r"[a-z0-9]+", _fold(text)))


def _flagged_flags(sentences: list[str], flagged_clauses: list[Any]) -> list[bool]:
    """Which of ``sentences`` carry a flagged clause.

    An exact folded match wins first. The verifier usually returns a paraphrase
    or a trimmed clause, so a clause that is not contained verbatim is mapped to
    the one sentence holding at least :data:`REPAIR_CLAUSE_OVERLAP` of its
    tokens (a clause needs :data:`REPAIR_MIN_CLAUSE_TOKENS` tokens to qualify).
    """
    folded_sentences = [_fold(sentence) for sentence in sentences]
    sentence_tokens = [set(_clause_tokens(sentence)) for sentence in sentences]
    is_flagged = [False] * len(sentences)
    for entry in flagged_clauses:
        folded = _fold(_clause_parts(entry)[0])
        if not folded:
            continue
        exact = [
            index for index, sentence in enumerate(folded_sentences) if folded in sentence
        ]
        if exact:
            for index in exact:
                is_flagged[index] = True
            continue
        tokens = set(_clause_tokens(folded))
        if len(tokens) < REPAIR_MIN_CLAUSE_TOKENS:
            continue
        best_index = 0
        best_score = 0.0
        for index, candidate in enumerate(sentence_tokens):
            if not candidate:
                continue
            score = len(tokens & candidate) / len(tokens)
            if score > best_score:
                best_score = score
                best_index = index
        if sentences and best_score >= REPAIR_CLAUSE_OVERLAP:
            is_flagged[best_index] = True
    return is_flagged


def _dependent_sentences(text: str, is_flagged: list[bool]) -> list[str]:
    """Sentences, in order, that lean on a flagged sentence just before them.

    A sentence directly after a flagged one in the same paragraph that opens
    with an anaphoric marker ("This…", "Beside this…", "However…") cannot stand
    on its own once the flagged sentence goes, so it must be rewritten or
    deleted too. ``is_flagged`` is indexed like the flattened paragraph groups.
    """
    dependents: list[str] = []
    flat = 0
    for group in _paragraph_sentence_groups(text):
        for position in range(len(group) - 1):
            if is_flagged[flat + position] and _starts_with_anaphoric(group[position + 1]):
                dependents.append(group[position + 1])
        flat += len(group)
    return dependents


def _dependent_indices(text: str, is_flagged: list[bool]) -> set[int]:
    """Flat indices of :func:`_dependent_sentences`, for repair validation."""
    indices: set[int] = set()
    flat = 0
    for group in _paragraph_sentence_groups(text):
        for position in range(len(group) - 1):
            if is_flagged[flat + position] and _starts_with_anaphoric(group[position + 1]):
                indices.add(flat + position + 1)
        flat += len(group)
    return indices


def _removable_heading_indices(
    text: str, is_flagged: list[bool], dependents: set[int]
) -> set[int]:
    """Headings a repair may delete because their whole section is gone.

    A heading is removable only when every sentence of its section (until the
    next heading) is flagged or a dependent: the section would otherwise be
    left with no paragraph, so removing the heading keeps the article coherent.
    """
    groups = _paragraph_sentence_groups(text)
    starts: list[int] = []
    flat = 0
    for group in groups:
        starts.append(flat)
        flat += len(group)

    def is_heading(index: int) -> bool:
        group = groups[index]
        return len(group) == 1 and bool(
            _HEADING_LINE_RE.match(group[0]) or _LIST_ITEM_LINE_RE.match(group[0])
        )

    removable: set[int] = set()
    for index in range(len(groups)):
        if not is_heading(index):
            continue
        content: list[int] = []
        for following in range(index + 1, len(groups)):
            if is_heading(following):
                break
            content.extend(range(starts[following], starts[following] + len(groups[following])))
        if content and all(is_flagged[i] or i in dependents for i in content):
            removable.add(starts[index])
    return removable


def _body_of_article(text: str) -> str:
    """The Markdown body of ``text`` with its front matter removed."""
    match = re.match(r"^---\r?\n.*?\r?\n---\r?\n?(.*)$", text or "", re.DOTALL)
    return match.group(1) if match else (text or "")


def _attributions(sentence: str) -> set[str]:
    """Folded source names that ``sentence`` attributes its statement to."""
    found: set[str] = set()
    for pattern in (_ATTRIBUTION_LEAD_RE, _ATTRIBUTION_VERB_RE):
        for match in pattern.finditer(sentence):
            name = _fold(match.group(1).strip(" \t.,:;"))
            if name:
                found.add(name)
    return found


def _check_added_sentence(
    sentence: str,
    pending_sources: list[tuple[str, set[str]]],
    problems: list[str],
) -> None:
    """A sentence the original did not have must be a complete, attributed one."""
    text = _norm(sentence)
    preview = text[:120]
    words = text.split()
    if len(words) < REPAIR_MIN_NEW_SENTENCE_WORDS:
        problems.append(
            f"added sentence has fewer than {REPAIR_MIN_NEW_SENTENCE_WORDS} words: {preview!r}"
        )
    elif not words[0][0].isupper():
        problems.append(f"added sentence does not start with a capital letter: {preview!r}")
    elif not text.rstrip("\"')\u2019\u201d]").endswith((".", "!", "?")):
        problems.append(f"added sentence does not end with sentence punctuation: {preview!r}")
    if pending_sources:
        _removed, sources = pending_sources.pop(0)
        if not (sources & _attributions(sentence)):
            problems.append(
                "replacement drops the attribution to "
                + ", ".join(sorted(sources))
                + f": {preview!r}"
            )


def validate_repair(original: str, repaired: str, flagged_clauses: list[str]) -> list[str]:
    """Check a repaired draft against the draft it came from.

    Returns every rule the repair broke; an empty list means the repair may be
    verified. The repair pass may delete or re-ground the flagged clauses and
    nothing else, so:

    1. the body may not contain pipeline detail from the repair prompt (the
       facts-sheet heading, ``F1:`` lines, JSON keys, raw JSON);
    2. every unflagged sentence of ``original`` appears unchanged and in order,
       which also pins the front matter;
    3. every sentence the repair added is a complete sentence (``F1:`` scraps
       and clause fragments are rejected), and a replacement for an attributed
       flagged sentence still attributes the same source;
    4. no heading or list item is added;
    5. the word count does not grow by more than
       :data:`REPAIR_MAX_NET_ADDED_WORDS` net words.

    A sentence that directly follows a flagged one in the same paragraph and
    opens with an anaphoric marker ("This…", "Beside this…", "However…") is a
    dependent: it must be rewritten or deleted too, and a heading whose whole
    section disappears with it may be removed. Such a paragraph or section
    being emptied and removed is accepted.
    """
    original = str(original or "")
    repaired = str(repaired or "")
    problems: list[str] = list(find_leaks(_body_of_article(repaired)))

    original_sentences = _sentences(original)
    repaired_sentences = _sentences(repaired)
    original_norm = [_norm(sentence) for sentence in original_sentences]
    repaired_norm = [_norm(sentence) for sentence in repaired_sentences]
    is_flagged = _flagged_flags(original_sentences, flagged_clauses)
    original_set = set(original_norm)

    # A sentence that leans on a flagged sentence may not survive its removal:
    # treat it as flagged (so dropping it is allowed) and require it to change.
    # A heading whose whole section disappears goes with it.
    dependents = _dependent_indices(original, is_flagged)
    if dependents:
        for index in _removable_heading_indices(original, is_flagged, dependents):
            is_flagged[index] = True
        for index in dependents:
            is_flagged[index] = True

    # Rule 2: an unflagged sentence keeps its exact wording and its position.
    cursor = 0
    for index, sentence in enumerate(original_norm):
        if is_flagged[index]:
            continue
        found = next(
            (position for position in range(cursor, len(repaired_norm)) if repaired_norm[position] == sentence),
            None,
        )
        if found is None:
            problems.append(f"unflagged sentence was changed or dropped: {original_sentences[index]!r}")
        else:
            cursor = found + 1

    # Rules 3 and 4: walk both texts, pairing each removal with the additions
    # that follow it, so a replacement is checked against the sentence it stands
    # in for.
    pending_sources: list[tuple[str, set[str]]] = []
    original_pos = 0
    repaired_pos = 0
    while original_pos < len(original_norm) and repaired_pos < len(repaired_norm):
        if original_norm[original_pos] == repaired_norm[repaired_pos]:
            original_pos += 1
            repaired_pos += 1
            pending_sources = []
            continue
        if original_norm[original_pos] not in repaired_norm[repaired_pos:]:
            sources = _attributions(original_sentences[original_pos])
            pending_sources = (
                [(original_sentences[original_pos], sources)]
                if is_flagged[original_pos] and sources
                else []
            )
            original_pos += 1
            continue
        if repaired_norm[repaired_pos] not in original_set:
            _check_added_sentence(repaired_sentences[repaired_pos], pending_sources, problems)
        repaired_pos += 1
    while repaired_pos < len(repaired_norm):
        if repaired_norm[repaired_pos] not in original_set:
            _check_added_sentence(repaired_sentences[repaired_pos], pending_sources, problems)
        repaired_pos += 1

    original_headings = {
        _norm(sentence)
        for sentence in original_sentences
        if _HEADING_LINE_RE.match(sentence) or _LIST_ITEM_LINE_RE.match(sentence)
    }
    for sentence in repaired_sentences:
        if _HEADING_LINE_RE.match(sentence) or _LIST_ITEM_LINE_RE.match(sentence):
            if _norm(sentence) not in original_headings:
                problems.append(f"repair added a heading or list item: {sentence[:120]!r}")

    for index in sorted(dependents):
        if original_norm[index] in repaired_norm:
            problems.append(
                "dependent sentence must be rewritten or deleted: "
                f"{original_sentences[index]!r}"
            )

    net_words = len(repaired.split()) - len(original.split())
    if net_words > REPAIR_MAX_NET_ADDED_WORDS:
        problems.append(
            f"repair grows the article by {net_words} words "
            f"(max {REPAIR_MAX_NET_ADDED_WORDS} more than it removes)"
        )
    return problems


__all__ = [
    "DEFAULT_PROMPT_PATH",
    "DEFAULT_REPAIR_PROMPT_PATH",
    "FACTS_SHEET_TOKEN",
    "ARTICLE_TOKEN",
    "REPAIR_MAX_NET_ADDED_WORDS",
    "REPAIR_MIN_NEW_SENTENCE_WORDS",
    "UNSUPPORTED_CLAUSES_TOKEN",
    "WriterInputError",
    "build_prompt",
    "build_repair_prompt",
    "load_template",
    "repair",
    "validate_repair",
    "write",
    "writer_view",
]