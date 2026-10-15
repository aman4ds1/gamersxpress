"""Verifier stage: no article passes without independent verification.

Two independent checks must both pass, and the whole stage runs behind the
family guard from PLAN.md principle 4:

1. **Model check.** The ``verifier`` role (a different model family than the
   writer) receives the article and the facts sheet (claims numbered F1, F2, ...)
   and must return JSON saying, for every factual clause, which sheet claim(s)
   support it (``fact_ids``), how it matches (``match``: ``exact``,
   ``paraphrase``, ``partial`` or ``none``) and, for a partial or absent match, a
   short ``difference`` note. A clause is a pass only when its ``match`` is
   ``exact`` or ``paraphrase``, it cites the supporting fact id(s) and its
   ``difference`` is empty. A ``partial`` or ``none`` match, any non-empty
   ``difference``, a clause resting on a ``statement`` fact whose speaker the
   clause does not name, and a relative-time word outside a quoted name are all
   unsupported. The same reply also lists any unsupported claim, any rumor
   stated as fact, any region-specific price or availability missing from the
   sheet, any cost described as something the player receives, any label the
   article invents, and any category claim the sheet does not state. Missing,
   malformed, schema-invalid or truncated output is a FAIL, never a pass.

   ``generate`` is called *without* a ``json_schema`` on purpose: with one, a
   parse error inside the provider layer becomes ``ProviderError`` retries and
   finally ``SkipRun`` (a skipped run), which would let malformed verifier
   output stop the pipeline instead of failing it. Parsing here keeps a parse
   error a hard FAIL. A ``finish_reason`` of ``"length"`` (truncated JSON) is
   never parsed; the provider retries once with a larger budget, and if it is
   still truncated the run fails as ``verifier_output_invalid``.

2. **Code check (independent of the model).** Every number, price, percentage,
   date, time, version and spec value in the article body must appear in the
   facts sheet after normalization (``1,299`` = ``1299``, ``$1.3k`` = ``1300``,
   dates in any of several formats). A small, explicit config whitelist covers
   harmless numbers (list markers, a calendar year in a heading).

3. **Family guard.** The verifier call goes through the guard in
   :func:`providers.generate`: no recorded writer family raises
   :class:`MissingWriterFamilyError` without calling a provider, and no
   different-family model raises :class:`SkipRun`. Mock providers bypass the
   missing-state error only in tests and ``--dry-run``.

Any failure means the article must not be published. The result is written to
``data/reports/verify-<id>.json``; every failed report also saves the provider's
raw response, ``finish_reason`` and token usage, and a distinct
``failure_reason``: ``verifier_output_invalid`` (the model produced no usable
verdict) or ``article_rejected`` (the verdict was valid but the article failed).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import yaml

from providers import (
    PROJECT_ROOT,
    Generation,
    RunState,
    generate as default_generate,
)
from json_schema import schema_errors

DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config.yaml")
DEFAULT_REPORT_DIR = PROJECT_ROOT / "data" / "reports"
DEFAULT_LIST_MARKERS = r"^\s*(?:[-*+]|\d{1,3}[.)])\s+"

logger = logging.getLogger("gamersxpress.pipeline.verify")

VERIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause": {"type": "string"},
                    "fact_ids": {"type": "array", "items": {"type": "string"}},
                    "match": {
                        "type": "string",
                        "enum": ["exact", "paraphrase", "partial", "none"],
                    },
                    "difference": {"type": "string"},
                },
                "required": ["clause", "fact_ids", "match", "difference"],
                "additionalProperties": False,
            },
        },
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "rumors_stated_as_fact": {"type": "array", "items": {"type": "string"}},
        "unsupported_regional": {"type": "array", "items": {"type": "string"}},
        "costs_described_as_received": {"type": "array", "items": {"type": "string"}},
        "invented_labels": {"type": "array", "items": {"type": "string"}},
        "category_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "clauses",
        "unsupported_claims",
        "rumors_stated_as_fact",
        "unsupported_regional",
        "costs_described_as_received",
        "invented_labels",
        "category_claims",
    ],
    "additionalProperties": False,
}

#: Relative-time words a clause may never rely on (PLAN.md: write calendar
#: dates, never words that expire). A clause that still uses one is unsupported
#: no matter what the model says, unless the word is inside a quoted name.
DEFAULT_RELATIVE_TIME_WORDS: tuple[str, ...] = (
    "today", "now", "currently", "tonight", "this week", "yesterday",
    "tomorrow", "recently", "soon",
)

#: Comparison terms for the numeric performance/marketing rule. A sentence is a
#: performance or marketing claim only when a number sits within three tokens of
#: one of these terms, or when one of them is followed by "than" and the sentence
#: has a number ("46 percent faster than Steam"). Such a sentence must carry an
#: attribution cue naming a source in the same sentence, or it is unsupported
#: ("unattributed performance claim"). Overridable via
#: ``verify.company_claim_terms`` in config.yaml. Bare qualifiers ("up to",
#: "most", "best", "record") are deliberately absent, so plain facts (dates,
#: prices, version numbers, counts) are never flagged.
DEFAULT_COMPANY_CLAIM_TERMS: tuple[str, ...] = (
    "faster", "slower", "smoother", "higher", "lower", "smaller", "larger",
    "more efficient", "improves", "improvement", "boosts", "reduces",
    "increase", "decrease",
)

#: A bare multiplier counts as a numeric performance claim on its own
#: ("2x the frame rate", "3 times the load speed"). "times" is only a multiplier
#: when a number precedes it, so an outlet name ("The New York Times") never
#: matches.
_MULTIPLIER_RE = re.compile(r"\b\d[\d.,]*(?:\s*[x\u00d7]|\s+times)\b", re.I)

#: Tokens within this many positions of a comparison term count as numeric
#: performance claims.
_COMPANY_CLAIM_DISTANCE = 3

#: Verbs that attribute a claim when the sentence also names a source before
#: them ("Epic says", "Digital Foundry measured").
_CLAIM_CUE_VERBS = (
    "says", "said", "claims", "claimed", "announced", "reports", "reported",
    "measured", "tested", "found", "estimates",
)
#: Lead phrases that attribute a claim when a capitalized name follows.
_CLAIM_CUE_LEADS = ("according to", "in a statement", "in a blog post")
#: A capitalized name (up to four words) before a cue verb or after a lead.
_CAP_NAME = r"[A-Z][\w'’&.\-]*(?:\s+[A-Z][\w'’&.\-]*){0,3}"
_CLAIM_CUE_RE = re.compile(
    rf"\b{_CAP_NAME}\s+(?i:"
    + "|".join(sorted(_CLAIM_CUE_VERBS, key=len, reverse=True))
    + r")\b"
)
_CLAIM_LEAD_RE = re.compile(
    rf"\b(?i:{'|'.join(sorted(_CLAIM_CUE_LEADS, key=len, reverse=True))})\b"
    rf"[^.!?]{{0,20}}?[A-Z][\w'’&.\-]*"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

#: A quoted span (straight or curly double quotes) is a name, not reader text;
#: relative-time words inside it are ignored.
_QUOTED_SPAN_RE = re.compile(r'"[^"]*"|\u201c[^\u201d]*\u201d')


SYSTEM_INSTRUCTION = (
    "You are an independent fact checker. You receive a news article and the "
    "verified facts sheet it was written from. Each fact in the sheet is "
    "numbered F1, F2, ... and you must cite those ids. The article passes only "
    "if every factual clause is fully supported by the sheet.\n"
    "Split each factual sentence into its clauses (every separate claim it "
    "makes). A sentence with one supported part and one unsupported part must "
    "be split so the unsupported clause is flagged on its own: \"deploy with "
    "vehicles or cash\" is two clauses, \"deploy with vehicles\" and \"deploy "
    "with cash\".\n"
    "A clause is supported only when one fact, or a short list of facts that "
    "each state a distinct part of it, states every element of the clause: who "
    "said or did it (the speaker, the outlet, or the fact's attribution), "
    "qualifier and hedge words (could, may, expected, first, only, full, "
    "reportedly), the time or date, numbers, and the relationship between the "
    "ideas. If the clause changes any of these relative to the facts, it is "
    "\"partial\" and unsupported, even when the meaning is close. \"Close "
    "enough\" is not a pass: if you notice a difference, set match to "
    "\"partial\" (or \"none\") and record it in \"difference\"; never note a "
    "difference and then pass the clause.\n"
    "Reply with JSON only, no prose and no code fences, in exactly this shape:\n"
    '{"clauses":[{"clause":"...","fact_ids":["F1"],"match":"exact",'
    '"difference":""}],"unsupported_claims":["..."],'
    '"rumors_stated_as_fact":["..."],"unsupported_regional":["..."],'
    '"costs_described_as_received":["..."],"invented_labels":["..."],'
    '"category_claims":["..."]}\n'
    "List every factual clause of the article in \"clauses\". Keep the "
    "\"clause\" text short. For each clause put the number(s) of the sheet "
    "fact(s) it rests on in \"fact_ids\" (always include the key; use [] when "
    "no fact applies). Set \"match\" to exactly one of \"exact\" (the fact "
    "states the clause word for word), \"paraphrase\" (the fact states the same "
    "thing in different words, keeping every element), \"partial\" (the fact "
    "states only part of it, or changes a speaker, qualifier, date, number or "
    "relationship) or \"none\". A clause whose match is \"partial\" or \"none\" "
    "is unsupported. In \"difference\" give a short note naming what is missing "
    "or changed whenever \"match\" is \"partial\" or \"none\"; return \"\" when "
    "\"match\" is \"exact\" or \"paraphrase\".\n"
    "When a cited fact's kind is \"statement\", the clause must name the same "
    "speaker as that fact, or attribute it with \"says\" or \"according to\" "
    "and that speaker; a clause that credits the statement to a group (\"the "
    "team\", \"the narrative team\") or to anyone other than the fact's speaker "
    "is unsupported.\n"
    "Never accept a clause that uses a relative-time word (today, now, "
    "currently, tonight, this week, yesterday, tomorrow, recently, soon) when "
    "the facts give a calendar date; such a clause is unsupported unless the "
    "word is part of a quoted name.\n"
    "In \"unsupported_claims\" list any factual clause (full text) the sheet "
    "does not support. In \"rumors_stated_as_fact\" list anything the sheet "
    "marks as a rumor (kind \"rumor\") that the article states as fact. In "
    "\"unsupported_regional\" list any region-specific price or availability "
    "the article states that the sheet does not contain. In "
    "\"costs_described_as_received\" list clauses that describe a price or cost "
    "from the sheet as something the player receives, earns or is given. In "
    "\"invented_labels\" list labels the article uses that the sheet never "
    "uses, such as \"mid-tier\". In \"category_claims\" list claims that "
    "categorize the game or mode (such as \"an extraction shooter\") when the "
    "sheet does not state that category. Do not add claims, do not edit the "
    "article and do not add fields."
)


@dataclass
class VerifyReport:
    """Outcome of the verifier stage for one article."""

    id: str
    passed: bool
    writer_family: str | None
    verifier_family: str | None
    verifier_provider: str | None
    verifier_model: str | None
    unsupported: list[str] = field(default_factory=list)
    unmatched_numbers: list[str] = field(default_factory=list)
    model_sentences: list[dict] = field(default_factory=list)
    writer_families: list[str] = field(default_factory=list)
    error: str | None = None
    duplicate_sources: str | None = None
    failure_reason: str | None = None
    finish_reason: str | None = None
    token_usage: dict | None = None
    raw_response: Any = None
    repair: dict | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "passed": self.passed,
            "writer_family": self.writer_family,
            "writer_families": self.writer_families,
            "verifier_family": self.verifier_family,
            "verifier_provider": self.verifier_provider,
            "verifier_model": self.verifier_model,
            "unsupported": self.unsupported,
            "unmatched_numbers": self.unmatched_numbers,
            "model_sentences": self.model_sentences,
            "error": self.error,
            "duplicate_sources": self.duplicate_sources,
            "failure_reason": self.failure_reason,
            "finish_reason": self.finish_reason,
            "token_usage": self.token_usage,
            "raw_response": self.raw_response,
            "repair": self.repair,
        }


# --- model check -------------------------------------------------------------


def _fold(text: object) -> str:
    """Lowercase, diacritics-folded text for tolerant phrase matching."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _mentions(text: object, name: object) -> bool:
    """True when ``name`` appears in ``text`` on word boundaries, folded."""
    haystack = _fold(text)
    needle = _fold(name).strip()
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _relative_time_hits(text: object, words: tuple[str, ...]) -> list[str]:
    """Relative-time words in ``text`` outside any quoted span (model-independent)."""
    folded = _fold(text)
    spans = [(match.start(), match.end()) for match in _QUOTED_SPAN_RE.finditer(folded)]
    hits: list[str] = []
    for word in words:
        needle = _fold(word).strip()
        if not needle:
            continue
        for match in re.finditer(rf"(?<!\w){re.escape(needle)}(?!\w)", folded):
            if any(start <= match.start() and match.end() <= end for start, end in spans):
                continue
            hits.append(match.group(0))
    return hits


def _facts_by_id(facts: Any) -> dict[str, dict]:
    """The sheet's claims keyed by their number (F1, F2, ...)."""
    claims = facts.get("claims") if isinstance(facts, dict) else None
    if not isinstance(claims, list):
        return {}
    return {
        f"F{index}": claim
        for index, claim in enumerate(claims, start=1)
        if isinstance(claim, dict)
    }


def _link_key(url: object) -> str:
    """Match key for a source URL: scheme/``www.``, fragment and trailing slash dropped."""
    text = str(url or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    return text.split("#", 1)[0].rstrip("/")


def _fact_speakers(claim: dict, facts: dict) -> list[str]:
    """Names a ``statement`` fact may be attributed to: its origin/affiliation, or
    the outlet that published it when the speaker is unstated."""
    names: list[str] = []
    for key in ("origin", "affiliation"):
        value = str(claim.get(key) or "").strip()
        if value and value.lower() != "unstated":
            names.append(value)
    if names:
        return names
    url_key = _link_key(claim.get("source_url"))
    for source in facts.get("sources") or []:
        if not isinstance(source, dict):
            continue
        if url_key and _link_key(source.get("link")) == url_key:
            name = str(source.get("source_name") or "").strip()
            if name:
                names.append(name)
    return names


def _clause_supported(item: dict) -> bool:
    """A model clause passes only on an exact or paraphrase match with no difference."""
    if item.get("match") not in ("exact", "paraphrase"):
        return False
    return not str(item.get("difference") or "").strip()


def _statement_attribution_ok(item: dict, facts: dict, by_id: dict[str, dict]) -> bool:
    """A clause resting on a ``statement`` fact must name that fact's speaker."""
    for fact_id in item.get("fact_ids") or []:
        claim = by_id.get(str(fact_id))
        if not isinstance(claim, dict):
            continue
        if str(claim.get("kind") or "").strip().lower() != "statement":
            continue
        speakers = _fact_speakers(claim, facts)
        if speakers and not any(_mentions(item.get("clause"), name) for name in speakers):
            return False
    return True


def _numbered_facts(facts: dict) -> str:
    claims = facts.get("claims")
    if not isinstance(claims, list) or not claims:
        return json.dumps(facts, indent=2, ensure_ascii=False)
    lines: list[str] = []
    for index, claim in enumerate(claims, start=1):
        text = str(claim.get("claim", "")).strip()
        value = str(claim.get("value", "")).strip()
        line = f"F{index}: {text!r}" + (f" (value: {value!r})" if value else "")
        attribution: list[str] = []
        kind = str(claim.get("kind") or "").strip()
        if kind:
            attribution.append(f"kind {kind}")
        for field_name in ("origin", "affiliation"):
            name = str(claim.get(field_name) or "").strip()
            if name and name.lower() != "unstated":
                attribution.append(f"{field_name} {name}")
        if attribution:
            line += f" [{'; '.join(attribution)}]"
        lines.append(line)
    return "\n".join(lines)


def _known_fact_ids(facts: Any) -> set[str] | None:
    """Return the valid fact ids (F1, F2, ...) for a sheet, or None when unknown."""
    claims = facts.get("claims") if isinstance(facts, dict) else None
    if not isinstance(claims, list) or not claims:
        return None
    return {f"F{index}" for index in range(1, len(claims) + 1)}


def build_prompt(article: str, facts: Any) -> str:
    if isinstance(facts, dict):
        sheet = _numbered_facts(facts)
    elif isinstance(facts, str):
        sheet = facts
    else:
        sheet = json.dumps(facts, indent=2, ensure_ascii=False)
    return f"{SYSTEM_INSTRUCTION}\n\nArticle:\n{_as_text(article)}\n\nFacts sheet:\n{sheet}\n\nJSON:"


def _extract_json(text: str) -> Any:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _with_clause_defaults(data: Any) -> Any:
    """Return ``data`` with a missing ``fact_ids``/``difference`` filled by default.

    The schema lists every property in ``required`` so strict JSON-schema
    providers accept it, and the prompt asks for ``[]``/``""`` on clauses where
    they do not apply. This keeps the reader tolerant of an output that omits
    those keys. ``match`` is deliberately *not* defaulted: a missing ``match``
    is a malformed verdict and must fail schema validation. Non-dict payloads are
    returned untouched for :func:`schema_errors` to reject.
    """
    if not isinstance(data, dict):
        return data
    clauses = data.get("clauses")
    if not isinstance(clauses, list):
        return data
    normalized = [
        {
            **clause,
            "fact_ids": clause.get("fact_ids", []),
            "difference": clause.get("difference", ""),
        }
        if isinstance(clause, dict)
        else clause
        for clause in clauses
    ]
    return {**data, "clauses": normalized}


def _model_check(
    article: Any,
    facts: Any,
    *,
    run_state: RunState | None,
    generate: Callable[..., Generation],
    prompt: str | None,
    relative_time_words: tuple[str, ...] = DEFAULT_RELATIVE_TIME_WORDS,
    **kwargs: Any,
) -> tuple[Generation, dict]:
    prompt = prompt if prompt is not None else build_prompt(article, facts)
    generation = generate("verifier", prompt, run_state=run_state, **kwargs)

    empty = {
        "ok": False,
        "error": None,
        "clauses": [],
        "unsupported_clauses": [],
        "unsupported_claims": [],
        "rumors_stated_as_fact": [],
        "unsupported_regional": [],
        "costs_described_as_received": [],
        "invented_labels": [],
        "category_claims": [],
    }

    # finish_reason="length" means the output is truncated JSON. It must never be
    # parsed; it is its own failure (verifier_output_invalid), not a skip.
    if generation.finish_reason == "length":
        return generation, {
            **empty,
            "error": "verifier output truncated (finish_reason=length); output was not parsed",
        }

    raw = generation.value
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        if not raw.strip():
            return generation, {**empty, "error": "verifier returned no output"}
        try:
            data = _extract_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            return generation, {**empty, "error": f"verifier output is not valid JSON: {exc}"}
    else:
        return generation, {**empty, "error": "verifier returned no output"}

    data = _with_clause_defaults(data)
    errors = schema_errors(data, VERIFIER_SCHEMA)
    if errors:
        return generation, {**empty, "error": "verifier JSON failed schema validation: " + "; ".join(errors)}

    # A clause that claims support (an exact or paraphrase match with an empty
    # difference) must cite at least one sheet fact id. A cited id we did not
    # number is a hallucinated reference, so it is an invalid output, not a
    # rejection.
    clauses = data["clauses"]
    missing_fact = [
        item["clause"]
        for item in clauses
        if _clause_supported(item) and not item.get("fact_ids")
    ]
    if missing_fact:
        return generation, {
            **empty,
            "error": "clause marked exact or paraphrase without a cited fact_id: "
            + "; ".join(missing_fact),
        }
    known_ids = _known_fact_ids(facts)
    if known_ids is not None:
        unknown_ids = {
            str(fact_id)
            for item in clauses
            if _clause_supported(item)
            for fact_id in item.get("fact_ids") or []
            if str(fact_id) not in known_ids
        }
        if unknown_ids:
            return generation, {
                **empty,
                "error": "clause cites a fact_id not in the sheet: " + "; ".join(sorted(unknown_ids)),
            }

    # Code rules on top of the model's verdict, independent of its reasoning: a
    # partial or absent match, or any recorded difference, is unsupported; a
    # clause resting on a statement fact must name that fact's speaker; and a
    # relative-time word outside a quoted name is unsupported. The verifier may
    # not note a difference and then pass the clause.
    by_id = _facts_by_id(facts)
    unsupported_clauses: list[str] = []
    for item in clauses:
        clause = item["clause"]
        if (
            not _clause_supported(item)
            or not _statement_attribution_ok(item, facts, by_id)
            or _relative_time_hits(clause, relative_time_words)
        ):
            unsupported_clauses.append(clause)
    ok = not (
        unsupported_clauses
        or data["unsupported_claims"]
        or data["rumors_stated_as_fact"]
        or data["unsupported_regional"]
        or data["costs_described_as_received"]
        or data["invented_labels"]
        or data["category_claims"]
    )
    return generation, {
        "ok": ok,
        "error": None,
        "clauses": clauses,
        "unsupported_clauses": unsupported_clauses,
        "unsupported_claims": data["unsupported_claims"],
        "rumors_stated_as_fact": data["rumors_stated_as_fact"],
        "unsupported_regional": data["unsupported_regional"],
        "costs_described_as_received": data["costs_described_as_received"],
        "invented_labels": data["invented_labels"],
        "category_claims": data["category_claims"],
    }


# --- configuration -----------------------------------------------------------


def load_verify_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    section = raw.get("verify") or {}
    whitelist = []
    for entry in section.get("number_whitelist") or []:
        if isinstance(entry, str):
            whitelist.append({"pattern": entry, "context": None})
        elif isinstance(entry, dict) and entry.get("pattern"):
            whitelist.append({"pattern": entry["pattern"], "context": entry.get("context")})
    words = section.get("relative_time_words")
    if isinstance(words, (list, tuple)):
        relative_time_words = tuple(str(word) for word in words if str(word).strip())
    else:
        relative_time_words = DEFAULT_RELATIVE_TIME_WORDS
    terms = section.get("company_claim_terms")
    if isinstance(terms, (list, tuple)):
        company_claim_terms = tuple(str(term) for term in terms if str(term).strip())
    else:
        company_claim_terms = DEFAULT_COMPANY_CLAIM_TERMS
    return {
        "list_markers": section.get("list_markers") or DEFAULT_LIST_MARKERS,
        "whitelist": whitelist,
        "relative_time_words": relative_time_words,
        "company_claim_terms": company_claim_terms,
    }


# --- code check --------------------------------------------------------------

_MONTHS = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)

_MODEL_RE = re.compile(
    r"(?<![\w.])(?:rtx|gtx|rx|arc|ryzen|threadripper|snapdragon|dimensity|exynos|tensor)"
    r"\s?\d{2,5}(?:\s?(?:xtx|xt|ti|super|x3d|g))?(?![\w])",
    re.I,
)
_RESOLUTION_RE = re.compile(r"(?<![\w.])(\d{3,4})\s*[x×]\s*(\d{3,4})(?![\w])")
_BROAD_RESOLUTION_RE = re.compile(
    r"(?<![\w.])(4k|8k|1080p|1440p|2160p|900p|720p|480p)(?![\w])", re.I
)
_SPEC_RE = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?"
    r"(gib|mib|gb|tb|mb|kb|ghz|mhz|hz|kw|w|fps|nm|mm|mah|wh|bits|bit|cores|core|cuda|vram|tflops|tops|ppi|dpi|ms)"
    r"(?![\w])",
    re.I,
)
_PRICE_RE = re.compile(
    r"(?<![\w])(?:US\$|USD|GBP|INR|Rs\.?|₹|£|\$)\s?(\d[\d,]*(?:\.\d+)?)\s?(k)?(?![\w])",
    re.I,
)
_PRICE_SUFFIX_RE = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(k)?\s?(USD|GBP|INR|Rs)(?![\w])", re.I
)
_PERCENT_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?%")
_K_NUMBER_RE = re.compile(r"(?<![\w.])(\d[\d,]*\.\d+)\s?k(?![\w])", re.I)
_WEEKDAYS = (
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
)
_WEEKDAY_PREFIX = rf"(?:\b(?:{_WEEKDAYS})\b[,]?\s+)?"
_DATE_DAY = r"\d{1,2}"
_DATE_ORDINAL = r"(?:st|nd|rd|th)?"
_DATE_WITHIN_YEAR = r"(?:[, ]\s*(\d{4}))?"

_DATE_RANGE_DMY_RE = re.compile(
    rf"(?<![\w.]){_WEEKDAY_PREFIX}({_DATE_DAY}){_DATE_ORDINAL}\s+to\s+({_DATE_DAY})"
    rf"{_DATE_ORDINAL}\s+({_MONTHS})(?![\w])",
    re.I,
)
_DATE_RANGE_MDY_RE = re.compile(
    rf"(?<![\w.]){_WEEKDAY_PREFIX}({_MONTHS})\s+({_DATE_DAY}){_DATE_ORDINAL}"
    rf"\s*[-–]\s*({_DATE_DAY}){_DATE_ORDINAL}(?![\w])",
    re.I,
)
_DATE_RANGE_MDY_TO_RE = re.compile(
    rf"(?<![\w.]){_WEEKDAY_PREFIX}({_MONTHS})\s+({_DATE_DAY}){_DATE_ORDINAL}"
    rf"\s+to\s+({_DATE_DAY}){_DATE_ORDINAL}(?![\w])",
    re.I,
)
_DATE_MDY_RE = re.compile(
    rf"(?<![\w.]){_WEEKDAY_PREFIX}({_MONTHS})\s+({_DATE_DAY}){_DATE_ORDINAL}"
    rf"{_DATE_WITHIN_YEAR}(?![\w])",
    re.I,
)
_DATE_DMY_RE = re.compile(
    rf"(?<![\w.]){_WEEKDAY_PREFIX}({_DATE_DAY}){_DATE_ORDINAL}\s+({_MONTHS})"
    rf"{_DATE_WITHIN_YEAR}(?![\w])",
    re.I,
)
_DATE_ISO_RE = re.compile(r"(?<![\w.])(\d{4})-(\d{2})-(\d{2})(?![\w])")
_DATE_SLASH_RE = re.compile(r"(?<![\w.])(\d{1,2})/(\d{1,2})/(\d{4})(?![\w])")
_TIME_RE = re.compile(
    r"(?<![\w.])(\d{1,2}):(\d{2})(?::\d{2})?\s?(am|pm|utc|gmt|pt|pst|pdt|et|est|edt|bst|ist)?(?![\w])",
    re.I,
)
_VERSION_RE = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+(?:\.\d+)?)(?![\w.])")
_NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


def _decimal_token(number: str, *, thousands: bool = False) -> str:
    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return number.lower()
    if thousands:
        value *= 1000
    return format(value.normalize(), "f")


def _unit_token(unit: str) -> str:
    unit = unit.lower()
    if unit in ("bit", "bits"):
        return "bit"
    if unit in ("core", "cores"):
        return "core"
    return unit


_DATE_PREFIX = "D:"


def _date_token(year: str | None, month: str, day: str) -> str:
    try:
        month_num = int(month)
    except (TypeError, ValueError):
        month_num = int(_month_number(month))
    day_num = int(day)
    if year:
        return f"{_DATE_PREFIX}{year}-{month_num:02d}-{day_num:02d}"
    return f"{_DATE_PREFIX}{month_num:02d}-{day_num:02d}"


def _money(match: re.Match) -> str:
    return _decimal_token(match.group(1), thousands=bool(match.group(2)))


def _money_suffix(match: re.Match) -> str:
    return _decimal_token(match.group(1), thousands=bool(match.group(2)))


def _k_number(match: re.Match) -> str:
    return _decimal_token(match.group(1), thousands=True)


def _percent(match: re.Match) -> str:
    return _decimal_token(match.group(1)) + "%"


def _model(match: re.Match) -> str:
    return re.sub(r"\s+", "", match.group(0)).lower()


def _resolution(match: re.Match) -> str:
    return f"{match.group(1)}x{match.group(2)}"


def _broad_resolution(match: re.Match) -> str:
    return match.group(1).lower()


def _spec(match: re.Match) -> str:
    return _decimal_token(match.group(1)) + _unit_token(match.group(2))


def _date_mdy(match: re.Match) -> str:
    return _date_token(match.group(3), match.group(1), match.group(2))


def _date_dmy(match: re.Match) -> str:
    return _date_token(match.group(3), match.group(2), match.group(1))


def _date_iso(match: re.Match) -> str:
    return _date_token(match.group(1), match.group(2), match.group(3))


def _date_slash(match: re.Match) -> str:
    return _date_token(match.group(3), match.group(1), match.group(2))


def _date_range_dmy(match: re.Match) -> list[tuple[str, str]]:
    raw = match.group(0).strip()
    month = match.group(3)
    return [
        (raw, _date_token(None, month, match.group(1))),
        (raw, _date_token(None, month, match.group(2))),
    ]


def _date_range_mdy(match: re.Match) -> list[tuple[str, str]]:
    raw = match.group(0).strip()
    return [
        (raw, _date_token(None, match.group(1), match.group(2))),
        (raw, _date_token(None, match.group(1), match.group(3))),
    ]


def _time(match: re.Match) -> str:
    return f"{match.group(1)}:{match.group(2)}{match.group(3) or ''}".lower()


def _version(match: re.Match) -> str:
    return match.group(1)


def _number(match: re.Match) -> str:
    return _decimal_token(match.group(0))


_MONTH_NUMBERS = {
    "jan": "1", "feb": "2", "mar": "3", "apr": "4", "may": "5", "jun": "6",
    "jul": "7", "aug": "8", "sep": "9", "oct": "10", "nov": "11", "dec": "12",
}


def _month_number(name: str) -> str:
    return _MONTH_NUMBERS[name[:3].lower()]


_STEPS: list[tuple[re.Pattern, Callable[[re.Match], str | list[tuple[str, str]]]]] = [
    (_PRICE_RE, _money),
    (_PRICE_SUFFIX_RE, _money_suffix),
    (_K_NUMBER_RE, _k_number),
    (_PERCENT_RE, _percent),
    (_MODEL_RE, _model),
    (_RESOLUTION_RE, _resolution),
    (_BROAD_RESOLUTION_RE, _broad_resolution),
    (_SPEC_RE, _spec),
    (_DATE_RANGE_DMY_RE, _date_range_dmy),
    (_DATE_RANGE_MDY_RE, _date_range_mdy),
    (_DATE_RANGE_MDY_TO_RE, _date_range_mdy),
    (_DATE_MDY_RE, _date_mdy),
    (_DATE_DMY_RE, _date_dmy),
    (_DATE_ISO_RE, _date_iso),
    (_DATE_SLASH_RE, _date_slash),
    (_TIME_RE, _time),
    (_VERSION_RE, _version),
    (_NUMBER_RE, _number),
]


def _split_front_matter(text: str) -> tuple[str, str]:
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.DOTALL)
    return (match.group(1), match.group(2)) if match else ("", text)


_SOURCES_HEADING_RE = re.compile(r"^#{1,6}[ \t]+sources[ \t]*$", re.IGNORECASE | re.MULTILINE)


def find_sources_heading(article: Any) -> str | None:
    """Return a body Sources heading when the article repeats front-matter sources.

    The news page renders Sources from the front matter ``sources`` field, so a
    Sources section in the Markdown body would duplicate it. The front matter is
    stripped before searching, so its ``sources:`` key is never matched.
    """
    _front, body = _split_front_matter(_as_text(article))
    match = _SOURCES_HEADING_RE.search(body)
    return match.group(0).strip() if match else None


def _extract_from_line(line: str, context: str) -> list[dict]:
    found: list[dict] = []

    def record(
        normalizer: Callable[[re.Match], str | list[tuple[str, str]]],
    ) -> Callable[[re.Match], str]:
        def repl(match: re.Match) -> str:
            result = normalizer(match)
            if isinstance(result, str):
                found.append({"raw": match.group(0).strip(), "token": result, "line": context})
            else:
                found.extend(
                    {"raw": raw, "token": token, "line": context} for raw, token in result
                )
            return " " * len(match.group(0))

        return repl

    remaining = line
    for pattern, normalizer in _STEPS:
        remaining = pattern.sub(record(normalizer), remaining)
    return found


def extract_values(text: str, config: dict) -> list[dict]:
    """Extract every checkable value from ``text`` (one entry per occurrence)."""
    markers = re.compile(config["list_markers"])
    values: list[dict] = []
    for raw_line in text.splitlines():
        line = markers.sub("", raw_line, count=1)
        line = re.sub(r"https?://\S+", " ", line)
        values.extend(_extract_from_line(line, raw_line))
    return values


def _whitelisted(value: dict, config: dict) -> bool:
    for entry in config["whitelist"]:
        if re.fullmatch(entry["pattern"], value["token"]):
            if not entry["context"] or re.search(entry["context"], value["line"]):
                return True
    return False


def _facts_text(facts: dict) -> str:
    parts: list[str] = []
    for claim in facts.get("claims") or []:
        parts.append(str(claim.get("claim", "")))
        parts.append(str(claim.get("value", "")))
    return "\n".join(parts)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _date_parts(token: str) -> dict | None:
    if not token.startswith(_DATE_PREFIX):
        return None
    parts = token[len(_DATE_PREFIX):].split("-")
    if len(parts) == 3:
        return {"year": parts[0], "month": parts[1], "day": parts[2]}
    return {"year": None, "month": parts[0], "day": parts[1]}


def _date_in_facts(token: str, facts_tokens: set[str]) -> bool:
    want = _date_parts(token)
    if want is None:
        return False
    for candidate in facts_tokens:
        have = _date_parts(candidate)
        if have is None or have["month"] != want["month"] or have["day"] != want["day"]:
            continue
        if want["year"] is not None and have["year"] is not None:
            return want["year"] == have["year"]
        return True
    return False


def code_check(article: Any, facts: dict, config: dict) -> dict:
    """Check the article body's values against the facts sheet (model-independent)."""
    _front, body = _split_front_matter(_as_text(article))
    article_values = extract_values(body, config)
    facts_tokens = {value["token"] for value in extract_values(_facts_text(facts), config)}

    unmatched: list[str] = []
    for value in article_values:
        if value["token"] in facts_tokens or _whitelisted(value, config):
            continue
        if value["token"].startswith(_DATE_PREFIX) and _date_in_facts(value["token"], facts_tokens):
            continue
        unmatched.append(value["raw"])
    return {"ok": not unmatched, "unmatched": _dedupe(unmatched), "checked": len(article_values)}


def _numeric_performance_claim(sentence: str, term_spans: list[tuple[str, ...]]) -> bool:
    """True when ``sentence`` states a numeric performance/marketing claim.

    A claim is a number within :data:`_COMPANY_CLAIM_DISTANCE` tokens of a
    comparison term ("12 percent higher", "20 percent smaller than before"), one
    of those terms followed by "than" with a number anywhere ("46 percent faster
    than Steam"), or a bare multiplier ("2x", "3 times"). A sentence with no
    number, or with no such term near a number, is left alone.
    """
    tokens = re.findall(r"[a-z0-9]+", sentence.lower())
    if not any(any(char.isdigit() for char in token) for token in tokens):
        return False
    if _MULTIPLIER_RE.search(sentence):
        return True
    number_at = [
        index for index, token in enumerate(tokens) if any(char.isdigit() for char in token)
    ]
    for span in term_spans:
        words = " ".join(span)
        if re.search(rf"\b{re.escape(words)}\s+than\b", sentence.lower()):
            return True
        width = len(span)
        for start in range(len(tokens) - width + 1):
            if tuple(tokens[start : start + width]) != span:
                continue
            low, high = start - _COMPANY_CLAIM_DISTANCE, start + width - 1 + _COMPANY_CLAIM_DISTANCE
            if any(low <= index <= high for index in number_at):
                return True
    return False


def _company_claim_check(article: Any, config: dict) -> list[str]:
    """Sentences with a number near a comparison term but no attribution.

    Deterministic and model-independent: a numeric performance or marketing
    claim must carry an attribution cue naming a source in the same sentence
    ("Epic says", "according to Digital Foundry", "in a blog post"), or the
    sentence is unsupported with the reason "unattributed performance claim". A
    number must sit within three tokens of a comparison term (or the term must
    read "… than" with a number present): a bare qualifier, date, price, version
    number or count is not a performance claim and is left alone.
    """
    terms = tuple(config.get("company_claim_terms") or DEFAULT_COMPANY_CLAIM_TERMS)
    if not terms:
        return []
    term_spans = [tuple(str(term).lower().split()) for term in terms if str(term).strip()]
    _front, body = _split_front_matter(_as_text(article))
    flagged: list[str] = []
    for line in body.splitlines():
        line = re.sub(r"^\s*#{1,6}\s*", "", line).strip()
        if not line:
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            sentence = sentence.strip()
            if not sentence or not re.search(r"\d", sentence):
                continue
            if not _numeric_performance_claim(sentence, term_spans):
                continue
            if _CLAIM_CUE_RE.search(sentence) or _CLAIM_LEAD_RE.search(sentence):
                continue
            flagged.append(sentence)
    return flagged


# --- stage -------------------------------------------------------------------


def verify(
    article: Any,
    facts: Any,
    *,
    run_state: RunState | None = None,
    generate: Callable[..., Generation] = default_generate,
    prompt: str | None = None,
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    verify_config: dict | None = None,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    id: str | None = None,
    repair: dict | None = None,
    **kwargs: Any,
) -> VerifyReport:
    """Independently verify ``article`` against the facts sheet and report.

    Raises :class:`MissingWriterFamilyError` or :class:`SkipRun` from the
    family guard before any provider is called; otherwise returns a
    :class:`VerifyReport` (never raises on a failed check). ``repair`` records
    that a repair pass produced ``article`` (or None when there was none); it is
    echoed into the report but never sent to the verifier.
    """
    sheet = _load_facts(facts)
    story_id = re.sub(r"[^0-9A-Za-z._-]", "-", str(id or sheet.get("id") or "unnamed"))
    writer_family = run_state.writer_family if run_state is not None else None
    # Every family that wrote this article: the original draft and, after a
    # repair, the repair. ``writer_family`` above is the last entry (the family
    # of the text being verified); fall back to it when a run state predates the
    # writer-history tracking.
    writer_families = getattr(run_state, "writer_families", None) if run_state is not None else None
    writer_families = list(writer_families) if writer_families else ([writer_family] if writer_family else [])

    config = verify_config if verify_config is not None else load_verify_config(config_path)
    generation, model_result = _model_check(
        article,
        sheet,
        run_state=run_state,
        generate=generate,
        prompt=prompt,
        relative_time_words=config.get("relative_time_words", DEFAULT_RELATIVE_TIME_WORDS),
        **kwargs,
    )

    code_result = code_check(article, sheet, config)
    company_claim_issues = _company_claim_check(article, config)
    duplicate_sources = find_sources_heading(article)

    unsupported = _dedupe(
        list(model_result["unsupported_clauses"])
        + list(model_result["unsupported_claims"])
        + list(model_result["rumors_stated_as_fact"])
        + list(model_result["unsupported_regional"])
        + list(model_result["costs_described_as_received"])
        + list(model_result["invented_labels"])
        + list(model_result["category_claims"])
        + list(company_claim_issues)
    )
    passed = bool(
        model_result["ok"]
        and code_result["ok"]
        and duplicate_sources is None
        and not company_claim_issues
    )
    if model_result["error"] is not None:
        # The model could not produce a usable verdict at all: truncated,
        # empty, non-JSON, schema-invalid, or missing/unknown fact ids.
        failure_reason = "verifier_output_invalid"
    elif not passed:
        # The model produced a valid verdict but found something wrong with the
        # article (unsupported clauses, code-check mismatch, duplicate Sources).
        failure_reason = "article_rejected"
    else:
        failure_reason = None
    report = VerifyReport(
        id=story_id,
        passed=passed,
        writer_family=writer_family,
        writer_families=writer_families,
        verifier_family=generation.family,
        verifier_provider=generation.provider,
        verifier_model=generation.model,
        unsupported=unsupported,
        unmatched_numbers=code_result["unmatched"],
        model_sentences=model_result["clauses"],
        error=model_result["error"],
        duplicate_sources=duplicate_sources,
        failure_reason=failure_reason,
        finish_reason=generation.finish_reason,
        token_usage=generation.usage,
        raw_response=_raw_response(generation),
        repair=repair,
    )
    _write_report(report_dir, story_id, report.to_dict())
    if report.passed:
        logger.info("verify passed: id=%s (%d value(s) checked)", story_id, code_result["checked"])
    else:
        logger.warning(
            "verify failed: id=%s failure_reason=%s unsupported=%d unmatched=%d "
            "duplicate_sources=%s error=%s",
            story_id, failure_reason, len(unsupported), len(code_result["unmatched"]),
            duplicate_sources, report.error,
        )
    return report


def _raw_response(generation: Generation) -> Any:
    """Return the provider's raw response for the report, if one is available."""
    if generation.raw is not None:
        return generation.raw
    if isinstance(generation.value, str):
        return generation.value
    return None


def _load_facts(facts: Any) -> dict:
    if isinstance(facts, dict):
        return facts
    if isinstance(facts, (str, Path)):
        path = Path(facts)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        if isinstance(facts, str):
            return json.loads(facts)
    raise TypeError("facts must be a dict, a JSON string or a path to a facts sheet")


def _write_report(report_dir: str | Path, story_id: str, data: dict) -> Path:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"verify-{story_id}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


__all__ = [
    "DEFAULT_COMPANY_CLAIM_TERMS",
    "DEFAULT_REPORT_DIR",
    "VERIFIER_SCHEMA",
    "VerifyReport",
    "build_prompt",
    "code_check",
    "extract_values",
    "find_sources_heading",
    "load_verify_config",
    "verify",
]