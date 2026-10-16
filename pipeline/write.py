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

from gates import find_leaks
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
    lines = [line for line in (_clause_line(entry) for entry in unsupported_clauses) if line]
    clauses = "\n".join(lines) or "- (none)"
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
    """
    original = str(original or "")
    repaired = str(repaired or "")
    problems: list[str] = list(find_leaks(_body_of_article(repaired)))

    original_sentences = _sentences(original)
    repaired_sentences = _sentences(repaired)
    original_norm = [_norm(sentence) for sentence in original_sentences]
    repaired_norm = [_norm(sentence) for sentence in repaired_sentences]
    original_folded = [_fold(sentence) for sentence in original_sentences]
    flagged: list[str] = []
    for clause in flagged_clauses:
        folded = _fold(_clause_parts(clause)[0])
        if folded:
            flagged.append(folded)
    is_flagged = [any(clause in sentence for clause in flagged) for sentence in original_folded]
    original_set = set(original_norm)

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