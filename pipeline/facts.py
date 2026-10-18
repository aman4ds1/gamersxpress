"""Facts stage: extract claims from gathered source text with the 'fast' role.

Input is the output of the gather stage (``data/gathered/<id>.json``). The
"fast" model family extracts JSON claims from the source texts: claim, value,
source_url, confidence (0..1), origin (who produced the claim, or "unstated"),
and kind (confirmed / reported / estimate / rumor / opinion).

Extraction runs **once per source**, one model call per source, so a long
article cannot crowd out the story's other sources. Each call sees only its own
source and extracts every distinct fact about the story's subject (features,
modes, dates, prices, platforms, named quotes) plus the headline's central
claim, up to a per-source maximum (``facts.max_claims_per_source`` in
config.yaml, default :data:`MAX_CLAIMS_PER_SOURCE`). Claims from all sources
are merged into one sheet afterwards.

The extracted JSON is validated against :data:`CLAIM_SCHEMA` in code; anything
that does not match raises :class:`FactsError` and the story is dropped. Each
claim's ``kind`` is one of ``confirmed`` (stated by the company or an official
source), ``reported`` (stated by an outlet citing a named source), ``estimate``
(a third-party analyst or data estimate, never a confirmation), ``statement``
(a company or person's statement about a plan, possibility, intention or survey
result, never labeled ``confirmed``), ``rumor`` (a leak or unnamed source) or
``opinion``. ``origin`` names who produced the claim (the analyst or firm the
source cites, or the company); ``affiliation`` names the organization a person
speaks for when the article states one. When the source does not say who
produced the claim, the extractor stores ``"unstated"`` and the claim is
treated as at most ``reported``. In-game currency amounts must name their
currency (for example "100,000 in-game DMZ Cash") and are never stored as a
bare dollar figure.

A coherence check then keeps every claim honest to the story: each claim's
source must be about the cluster's primary entity (the named entity shared by
the most source headlines). Claims whose source is off-topic are dropped and
logged, so a mixed cluster cannot smuggle another game's figures into the sheet.

Duplicate claims across sources are then merged (:func:`merge_claims`): one
claim per fact, listing every source URL that states it in ``source_urls``,
taking the weakest ``kind`` when sources disagree, and keeping hedge words
(estimated, reportedly, more than, over) in the value.

Each claim's origin is then canonicalized -- case, possessives and leading
titles are stripped, and ``data/origins.json`` maps aliases to one canonical
name ("Alinea", "Alinea Analytics" and "Rhys Elliott of Alinea Analytics" all
become "alinea analytics", a person named alongside a firm counting as the
firm) -- and verified against the text of the claim's own source: an origin the
source never mentions is downgraded to ``"unstated"`` and logged. A person's
claim also keeps the organization they speak for (``affiliation``); that
organization is what the person counts as, so an executive producer and the
studio they work for are one origin, and two origins from the same single
article never count as two.

The story is confirmed through its central claim: the claim that restates the
cluster's seed headline. It is confirmed when a tier-1 source states it, or at
least two independent attributed origins support that same claim; only claims
of kind ``confirmed``/``reported``/``estimate`` count (a rumor, an opinion or a
statement never confirms a story), outlets repeating the same origin count as
one, origins supporting any other claim never count, two origins from the same
single article never count as two, and tier 3 never counts toward confirmation.
When one canonical origin is contained in another from the same set ("alinea
analytics" inside "alinea analytics ltd") they are treated as the same origin.
``allow_single_origin_attributed`` (config flag, default false) additionally
lets a single attributed origin confirm a story. Source text is consumed here
only; it is never handed to the writer.

Output is written to ``data/facts/<id>.json``:

    {
      "id": "<id>",
      "confirmation": {"tier1_sources": 1, "tier3_sources": 0, "origins": [...], "rule": "...", "confirmed": true},
      "sources": [{"source_name", "link", "title", "tier", "owner", "region"}],
      "claims": [{"claim", "value", "source_urls": [...], "origin", "affiliation", "confidence", "kind", "central"}],
      "coherence": {"checked": true, "primary_entities": [...], "canonical_entities": [...], "dropped_claims": [...]},
      "extractor": {"provider", "model", "family"},
      "fingerprint": {"format": 1, "value": "<hash of id + model chain + prompt settings>"}
    }

Only sources that contributed at least one claim appear in ``sources``; a
source that yielded no claim (or whose claims were all dropped) is left out of
the sheet so the writer and the published article never cite it.

When the file already exists and its stored fingerprint still matches the
current one (the cluster id, the enabled "fast" role chain from config.yaml,
the extraction prompt/schema, and the extraction tuning values), the cached
sheet is returned without calling the model; any mismatch -- including a sheet
written before fingerprints existed -- means claims are re-extracted and the
file overwritten, so stale facts are never reused. Bump :data:`PROMPT_VERSION`
when extraction output rules change so cached sheets invalidate automatically.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional, Iterable

import cluster
from ingest import normalize_url
from providers import (
    Generation,
    RunState,
    generate as default_generate,
    load_config,
    DEFAULT_CONFIG_PATH,
)
from json_schema import schema_errors

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "facts"

MAX_SOURCE_CHARS = 4000
# A claim's text and value must stay short: longer fields are almost always a
# verbatim passage copied from a source, so they are clipped before the sheet is
# saved (PLAN.md principle 3: facts, not source prose).
MAX_FIELD_CHARS = 300
# A verbatim quote stored with a claim is a short evidence span, not a passage:
# the extractor copies the words that state the fact and the words that attribute
# it, and both are clipped so a sheet never carries a long slice of the article.
MAX_QUOTE_CHARS = 300
MAX_ATTRIBUTION_QUOTE_CHARS = 200
EVIDENCE_FIELD_CHARS = 200
# Extraction runs once per source; one long article may contribute at most this
# many claims, so it cannot crowd out the story's other sources. Overridable per
# story via `facts.max_claims_per_source` in config.yaml.
MAX_CLAIMS_PER_SOURCE = 15
# Cost control: extraction runs once per source, so a candidate with many
# sources could spend the whole model-call budget on one story. At most this
# many sources per candidate are extracted, chosen by tier first, then one per
# distinct owner, then by text length. Overridable via
# `score.max_sources_per_candidate` in config.yaml.
DEFAULT_MAX_SOURCES_PER_CANDIDATE = 3
# A source that carries more text than this yet produces fewer than
# WARN_MIN_CLAIMS claims is logged with a warning, because that strong of a gap
# usually means the extractor skipped most of what the source says.
WARN_LOW_SOURCE_CHARS = 1500
WARN_MIN_CLAIMS = 3
# Bump when the extraction prompt or the claim schema changes, so previously
# written facts sheets are treated as stale and re-extracted.
PROMPT_VERSION = "facts-v12"

# Claim kinds, weakest to strongest. Merging takes the weakest kind a fact's
# sources disagree on, so an estimate that one source calls an estimate is
# never upgraded to confirmed by another source's wording, and a plan stated as
# a possibility (``statement``) is never upgraded to a confirmed fact.
KIND_STRENGTH: dict[str, int] = {
    "confirmed": 6,
    "reported": 5,
    "estimate": 4,
    "statement": 3,
    "rumor": 2,
    "opinion": 1,
}
ALLOWED_KINDS = tuple(KIND_STRENGTH)

#: What kind of thing a claim's speaker is. ``speaker_type`` is the extractor's
#: own label for the speaker it named: an organization (a company, studio or
#: platform holder), a person, the publishing outlet itself, an unnamed source
#: ("people familiar with the matter"), or ``none`` when the extractor named no
#: speaker at all. Evidence checking uses it to tell a product masquerading as a
#: speaker from a real organization.
SPEAKER_TYPES = ("organization", "person", "outlet", "anonymous", "none")

#: Origin used when a source does not say who produced the claim. A claim with
#: this origin is capped at ``reported`` (requirement: treat as at most reported).
UNSTATED_ORIGIN = "unstated"

#: Hedge words a merged fact must keep in its value. When sources state the same
#: fact with different values, the value carrying the most hedge words wins, so
#: "estimated 168,000" is never flattened to the unhedged "168,000", and a plan
#: hedged as "may", "could" or "not a promise" keeps that hedge.
HEDGE_WORDS = (
    "estimated", "estimate", "estimates", "reportedly", "more than", "over",
    "approximately", "about", "around", "roughly", "nearly", "up to", "at least",
    "could", "may", "might", "possibly", "hopes to", "not a promise",
)

#: Story-level confirmation is one tier-1 source OR at least two independent
#: attributed origins. Setting `facts.allow_single_origin_attributed` in
#: config.yaml lets a single attributed (named) origin confirm a story instead.
DEFAULT_ALLOW_SINGLE_ORIGIN_ATTRIBUTED = False

#: Origin strings are canonicalized through this small data file: each alias on
#: the left maps to one canonical name on the right ("alinea" -> "Alinea
#: Analytics"). A missing or malformed file is treated as an empty map.
DEFAULT_ORIGIN_ALIASES_PATH = PROJECT_ROOT / "data" / "origins.json"

#: Allowlist of well-known video game organizations (publishers, studios,
#: platform holders, chip makers) from ``data/orgs.json``. Used to keep a
#: company from being mistaken for a product/title and to recognise a
#: first-party announcement. Optional: a missing file yields an empty allowlist
#: and the corporate-suffix and announcement-subject rules still apply.
DEFAULT_ORGS_PATH = PROJECT_ROOT / "data" / "orgs.json"

#: Leading words stripped from an origin before matching, so an article or a
#: role ("the analyst Alinea Analytics") does not split one origin into two.
_ORIGIN_TITLE_WORDS = frozenset({"the", "a", "an", "analyst", "analysts"})

#: A definite or possessive phrase that refers back to an organization already
#: named in the paragraph ("the company", "the studio", "its developer"). Such a
#: phrase is not an anonymous source: the nearest preceding organization is the
#: source it stands for. ("studio" stays a descriptor token so the phrase
#: "sources familiar with the studio's plans" remains anonymous.)
_COREFERENCE_NOUNS = frozenset({
    "company", "companies", "studio", "studios", "developer", "developers",
    "publisher", "publishers", "firm", "firms", "team", "teams",
    "platform holder", "platform holders", "game studio", "game developer",
    "game studios", "game developers",
})
#: Determiners and possessives that may preface a coreference phrase.
_COREFERENCE_LEADERS = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "its", "their",
    "his", "her", "our", "your",
})

#: Document nouns that name a piece of writing, never a source. A cue such as
#: "according to an Epic blog post" names Epic, not the "blog post".
_DOCUMENT_NOUNS = frozenset({
    "post", "posts", "blog", "blogs", "blog post", "blog posts", "statement",
    "statements", "announcement", "announcements", "faq", "spokesperson",
    "spokespeople", "press release", "news release", "release", "releases",
    "thread", "tweet", "email", "report", "article", "piece",
})
#: Product and software words that never name a source on their own.
_NON_SOURCE_TERMS = frozenset({
    "launcher", "wine", "client", "store", "engine", "software", "console",
    "hardware", "platform", "preview", "beta", "app", "tool", "programming",
})
#: Multi-word product names that never name a source on their own.
_NON_SOURCE_PHRASES = frozenset({
    "steam deck", "steam os", "unreal engine", "easy anti cheat",
    "epic games launcher", "source engine",
})
#: Function words that never make a captured phrase a source name.
_FILLER_WORDS = frozenset({
    "has", "have", "had", "did", "does", "do", "is", "are", "was", "were",
    "be", "been", "being", "will", "would", "should", "could", "may", "might",
    "can", "shall", "yet", "still", "just", "even", "only", "also",
    "according", "per", "via", "citing", "said", "says", "say", "stated",
    "states", "noted", "notes", "added", "adds", "reported", "reports",
    "something", "someone", "somebody", "whether", "because", "while", "when",
    "where", "which", "who", "whom", "whose", "this", "that", "these", "those",
    "there", "here", "then", "than", "with", "without", "from", "into", "onto",
    "about", "over", "under", "after", "before", "during", "between", "among",
    "more", "most", "less", "least", "very", "much", "many", "few", "several",
})

#: Claim kinds whose origins may count toward confirmation. A rumor, an opinion
#: or a statement never confirms a story, however many outlets repeat it.
CONFIRMING_KINDS = ("confirmed", "reported", "estimate")

#: Claim kinds a first-party announcement may rest on. Broader than
#: :data:`CONFIRMING_KINDS`: a company's own statement about its plan counts as
#: first party, while a rumor or an opinion never does.
FIRST_PARTY_KINDS = ("confirmed", "statement", "reported")

#: Claim kinds that may corroborate the central fact of another source. A rumor
#: or an opinion never corroborates; whether a corroborating version actually
#: counts toward confirmation still depends on :data:`CONFIRMING_KINDS` and
#: :data:`FIRST_PARTY_KINDS`.
CORROBORATING_KINDS = ("confirmed", "reported", "statement", "estimate")

#: How many independent sources (distinct owners) must each attribute the
#: central claim to an organization before the first-party rule confirms it.
#: Overridable via `facts.min_first_party_sources` in config.yaml.
DEFAULT_MIN_FIRST_PARTY_SOURCES = 2

#: How a story's central fact is confirmed. ``legacy`` is the origin/tier/
#: first-party rule; ``shadow`` lets the legacy verdict decide while computing
#: and storing the evidence verdict beside it; ``evidence`` lets the evidence
#: verdict decide. Overridable via `facts.confirmation_mode` in config.yaml.
CONFIRMATION_MODES = ("legacy", "shadow", "evidence")
DEFAULT_CONFIRMATION_MODE = "shadow"

#: Longest claim label the grouping prompt sends per claim. The grouper only
#: needs enough of the label to tell two facts apart, so a sheet-sized label is
#: clipped rather than sent whole.
GROUPING_LABEL_CHARS = 240

logger = logging.getLogger("gamersxpress.pipeline.facts")

#: Default vocabulary for the closed anonymous-descriptor rule (facts.py). A cue
#: whose origin is built only from these words names no real source -- "sources",
#: "unnamed sources", "people familiar with the matter", "four industry sources".
#: A phrase containing any token outside the vocabulary is a name ("Insider
#: Gaming", "Source Engine", "People Can Fly"), and a name listed in
#: ``data/origins.json`` always counts as named. Extendable through
#: ``facts.anonymous_vocabulary`` in config.yaml.
_DEFAULT_ANONYMOUS_VOCABULARY = frozenset({
    "sources", "source", "insiders", "insider", "officials", "official",
    "people", "person", "persons", "familiar", "matter", "close", "project",
    "plans", "studio", "industry", "unnamed", "anonymous", "unidentified",
    "several", "multiple", "some", "a", "an", "the", "with", "to", "of", "who",
    "requested", "anonymity", "knowledgeable", "aware", "involved", "inside",
    "within", "staff", "employees", "employee",
    "former", "current", "one", "two", "three", "four", "five",
})
# NOTE: bare "company" and "developer" are deliberately not descriptors: a
# definite phrase ("the company", "the developer") is a coreference handled by
# :func:`_is_coreference_phrase`. "studio" stays a descriptor token so "sources
# familiar with the studio's plans" is still anonymous.

#: Default press-release markers: a source whose title or text carries one is a
#: press release or a sponsored post, not an outlet's own reporting. An
#: organization named as the subject of an announcement verb in such a headline
#: ("Acme Games announces ...") is the announcer of its own news and needs no
#: explicit attribution quote. Extendable through
#: ``facts.press_release_markers`` in config.yaml.
_DEFAULT_PRESS_RELEASE_MARKERS = ("(PR)", "PR:", "Press Release", "Sponsored")

#: Absence markers (pipeline/facts.py). A claim whose value or label says the
#: subject has *not* disclosed something -- "Activision hasn't revealed an exact
#: time" -- is the outlet's own report of an absence, never a statement by the
#: subject. Each marker must be followed within
#: :data:`DEFAULT_ABSENCE_MAX_GAP_WORDS` words by a disclosure word to mark the
#: claim. Extendable through ``facts.absence_patterns`` in config.yaml.
_DEFAULT_ABSENCE_PATTERNS = (
    "hasn't", "has not", "have not", "haven't", "didn't", "did not",
    "not yet", "no official", "no word",
)
#: Disclosure words that, within a few words of an absence marker, complete the
#: absence ("hasn't revealed", "not yet confirmed", "no official info").
#: Extendable through ``facts.absence_disclosure_words`` in config.yaml.
_DEFAULT_ABSENCE_DISCLOSURE_WORDS = (
    "revealed", "announced", "confirmed", "said", "stated", "commented",
    "disclosed", "shared", "detailed", "released", "info", "information", "word",
)
#: How many words may separate an absence marker from its disclosure word.
#: Overridable through ``facts.absence_max_gap_words`` in config.yaml.
DEFAULT_ABSENCE_MAX_GAP_WORDS = 4

#: Attribution cues (pipeline/facts.py). A claim's ``attribution_quote`` must
#: carry one of these cues, name its speaker and stay affirmative to credit
#: that speaker; an attribution that sits inside the claim's own sentence is
#: accepted. Extendable through ``facts.attribution_cues`` in config.yaml.
_DEFAULT_ATTRIBUTION_CUES = (
    "said", "says", "stated", "announced", "confirmed", "revealed",
    "according to", "told", "wrote", "posted", "claims", "reports",
    "noted", "explained", "added", "in a statement", "in a blog post",
    "unveiled", "detailed", "shared",
)

_ANONYMOUS_VOCABULARY_CACHE: tuple[tuple[int, int] | None, frozenset[str]] | None = None
_PRESS_RELEASE_MARKERS_CACHE: tuple[tuple[int, int] | None, tuple[str, ...]] | None = None
_ABSENCE_CONFIG_CACHE: tuple[tuple[int, int] | None, tuple[tuple[str, ...], tuple[str, ...], int]] | None = None
_ATTRIBUTION_CUES_CACHE: tuple[tuple[int, int] | None, tuple[str, ...]] | None = None


def anonymous_vocabulary() -> frozenset[str]:
    """The anonymous-descriptor vocabulary: the code defaults plus config.

    A cue phrase is anonymous only when every one of its tokens is in this set,
    so extending ``facts.anonymous_vocabulary`` in config.yaml can teach the
    pipeline a new anonymous descriptor ("a well-placed source" -> "well",
    "placed"). Cached against the config file's size and mtime.
    """
    global _ANONYMOUS_VOCABULARY_CACHE
    resolved = DEFAULT_CONFIG_PATH
    try:
        stat = Path(resolved).stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = None
    if _ANONYMOUS_VOCABULARY_CACHE is not None and _ANONYMOUS_VOCABULARY_CACHE[0] == signature:
        return _ANONYMOUS_VOCABULARY_CACHE[1]
    entries = (load_config().get("facts") or {}).get("anonymous_vocabulary") or []
    words = set(_DEFAULT_ANONYMOUS_VOCABULARY)
    for entry in entries:
        for word in re.split(r"\s+", _strip_possessive(str(entry).lower())):
            token = re.sub(r"[^a-z0-9]+", "", word)
            if token:
                words.add(token)
    result = frozenset(words)
    _ANONYMOUS_VOCABULARY_CACHE = (signature, result)
    return result


def press_release_markers() -> tuple[str, ...]:
    """The press-release markers: the code defaults plus config additions.

    A source whose title or text carries one of these marks a press release or a
    sponsored post, so an organization named as the subject of an announcement
    verb in the headline may be trusted as the announcer of its own news. Config
    entries in ``facts.press_release_markers`` extend the defaults. Cached
    against the config file's size and mtime.
    """
    global _PRESS_RELEASE_MARKERS_CACHE
    resolved = DEFAULT_CONFIG_PATH
    try:
        stat = Path(resolved).stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = None
    if _PRESS_RELEASE_MARKERS_CACHE is not None and _PRESS_RELEASE_MARKERS_CACHE[0] == signature:
        return _PRESS_RELEASE_MARKERS_CACHE[1]
    entries = (load_config().get("facts") or {}).get("press_release_markers") or []
    markers = list(_DEFAULT_PRESS_RELEASE_MARKERS)
    for entry in entries:
        text = str(entry).strip()
        if text and text not in markers:
            markers.append(text)
    result = tuple(markers)
    _PRESS_RELEASE_MARKERS_CACHE = (signature, result)
    return result


def _config_signature() -> tuple[int, int] | None:
    """The config file's (mtime_ns, size), or ``None`` when it cannot be read."""
    try:
        stat = Path(DEFAULT_CONFIG_PATH).stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def absence_config() -> tuple[tuple[str, ...], tuple[str, ...], int]:
    """``(markers, disclosure_words, max_gap_words)`` for the absence rule.

    ``facts.absence_patterns`` and ``facts.absence_disclosure_words`` extend the
    code defaults; ``facts.absence_max_gap_words`` sets how many words may sit
    between a marker and its disclosure word. Cached against the config file's
    size and mtime.
    """
    global _ABSENCE_CONFIG_CACHE
    signature = _config_signature()
    if _ABSENCE_CONFIG_CACHE is not None and _ABSENCE_CONFIG_CACHE[0] == signature:
        return _ABSENCE_CONFIG_CACHE[1]
    facts = load_config().get("facts") or {}
    markers = list(_DEFAULT_ABSENCE_PATTERNS)
    for entry in facts.get("absence_patterns") or []:
        text = str(entry).strip()
        if text and text not in markers:
            markers.append(text)
    disclosures = list(_DEFAULT_ABSENCE_DISCLOSURE_WORDS)
    for entry in facts.get("absence_disclosure_words") or []:
        text = str(entry).strip()
        if text and text not in disclosures:
            disclosures.append(text)
    try:
        gap = int(facts.get("absence_max_gap_words", DEFAULT_ABSENCE_MAX_GAP_WORDS))
    except (TypeError, ValueError):
        gap = DEFAULT_ABSENCE_MAX_GAP_WORDS
    gap = max(0, gap)
    result = (tuple(markers), tuple(disclosures), gap)
    _ABSENCE_CONFIG_CACHE = (signature, result)
    return result


def attribution_cues() -> tuple[str, ...]:
    """The attribution cues: the code defaults plus config additions.

    A claim's ``attribution_quote`` must carry one of these to credit its
    speaker. Config entries in ``facts.attribution_cues`` extend the defaults.
    Cached against the config file's size and mtime.
    """
    global _ATTRIBUTION_CUES_CACHE
    signature = _config_signature()
    if _ATTRIBUTION_CUES_CACHE is not None and _ATTRIBUTION_CUES_CACHE[0] == signature:
        return _ATTRIBUTION_CUES_CACHE[1]
    entries = (load_config().get("facts") or {}).get("attribution_cues") or []
    cues = list(_DEFAULT_ATTRIBUTION_CUES)
    for entry in entries:
        text = str(entry).strip().lower()
        if text and text not in cues:
            cues.append(text)
    result = tuple(cues)
    _ATTRIBUTION_CUES_CACHE = (signature, result)
    return result


CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "value": {"type": "string"},
                    "source_url": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "origin": {"type": "string"},
                    "affiliation": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KIND_STRENGTH)},
                    "quote": {"type": "string"},
                    "attribution_quote": {"type": "string"},
                    "speaker": {"type": "string"},
                    "speaker_type": {"type": "string", "enum": list(SPEAKER_TYPES)},
                },
                "required": [
                    "claim",
                    "value",
                    "source_url",
                    "confidence",
                    "origin",
                    "affiliation",
                    "kind",
                    "quote",
                    "attribution_quote",
                    "speaker",
                    "speaker_type",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}


#: What the grouping call returns: each group names the ids of the claims that
#: state one fact. Every property is strict (Groq/OpenAI reject a json_schema
#: unless every object lists all its properties and forbids extras).
GROUPING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTION = (
    "You are a news-room fact extractor, working one article at a time. From "
    "the single article text below, extract every distinct fact about the "
    "story's subject: the features, mechanics, modes, dates, prices, "
    "platforms, requirements and named quotes the text states. Do not stop "
    "early because the article is long: extract every distinct fact, up to the "
    "per-source limit given with the article. Never invent facts, values, or "
    "URLs; write only what the text actually says.\n"
    "Always extract the headline's central claim as one of the claims, even "
    "when the body states other facts first.\n"
    "Return each fact as an object with: claim (a short statement), value (the "
    "concrete value or a one-line summary), source_url (the exact URL given "
    "for this source), confidence (0 to 1), origin, affiliation, kind, quote, "
    "attribution_quote, speaker, and speaker_type.\n"
    "origin names who produced the claim: the company or official source, or "
    "the analyst or firm the article cites (for example 'Sony', 'Sensor "
    "Tower'). When the article does not say who produced the claim, set origin "
    "to 'unstated'.\n"
    "affiliation names the organization a person speaks for, when the article "
    "states one (for example 'Embark Studios' for that studio's executive "
    "producer). Always include the affiliation field in every claim: when the "
    "origin is a company itself, or the article names no organization, set "
    "affiliation to an empty string \"\" (never omit the key).\n"
    "quote is the exact sentence or clause in the text that states the fact, "
    "copied word for word: never paraphrase it, never shorten it with an "
    "ellipsis, and never fix its spelling, its quotation marks or its dashes. "
    "attribution_quote is the exact words in the text that attribute the fact to "
    "its speaker (for example 'according to Epic Games' or 'Epic Games said'), "
    "copied the same way. When a sentence names its source inside itself (for "
    "example 'Rebel Wolves has confirmed that ...', 'Devs X claim to ...', "
    "'according to Y'), copy that phrase as attribution_quote and set speaker to "
    "the named source. When the attribution sits inside the sentence that "
    "carries the claim, attribution_quote must be an exact substring of quote: "
    "copy those words word for word and never rephrase a cue ('announced' stays "
    "'announced', never 'confirmed'). When the article is a company's own press release or a "
    "post on a company's own site that states the fact in the company's own "
    "voice, set speaker to that company and speaker_type to 'organization' and "
    "use the headline or the sentence that names the company as the announcer "
    "(for example 'Acme Games announces ...') as the attribution_quote. When an "
    "outlet reports a fact without attributing it to anyone, leave speaker and "
    "attribution_quote empty and set speaker_type to 'none'. speaker is the "
    "person, organization or outlet the fact is attributed to, spelled as the "
    "text does; when the text attributes it to a pronoun or a common noun ('the "
    "company', 'the studio'), resolve that to the organization the text named. "
    "speaker_type is exactly one of "
    "'organization' (a company, studio or platform holder), 'person', 'outlet' "
    "(the publishing site itself), 'anonymous' (an unnamed source such as "
    "'people familiar with the matter') or 'none'. Use 'none' and empty strings "
    "\"\" for quote, attribution_quote and speaker only when the text gives no "
    "such words; never omit the keys.\n"
    "kind is exactly one of: 'confirmed' (the company or an official source "
    "states it, with a named origin), 'reported' (an outlet citing a named "
    "source), 'estimate' (a third-party analyst or data estimate; never label "
    "an estimate 'confirmed'), 'statement' (a company or person's statement "
    "about a plan, possibility, intention or survey result; never label a "
    "statement 'confirmed'), 'rumor' (a leak or unnamed source), or 'opinion' "
    "(a writer's or source's judgement). origin 'unstated' caps a claim at "
    "'reported'.\n"
    "Keep hedge words (estimated, reportedly, more than, over, could, may, "
    "might, possibly, hopes to, not a promise) in the value. " 
    "For percentages and overlap or audience figures, state the exact "
    "relationship in words (for example 'overlap with players of X'), never a "
    "vague phrase. Any in-game currency amount must name the currency (for "
    "example '100,000 in-game DMZ Cash'), never appear as a bare dollar figure.\n"
    "Write every date as an absolute calendar date, never a relative time word: "
    "convert 'today', 'tonight', 'now', 'currently', 'out now', 'this week', "
    "'this weekend', 'yesterday', 'tomorrow', 'recently', 'soon' and 'next week' "
    "to the calendar date the article supports (using its publication date when "
    "given). Write each date in the form 'November 2, 2026' (month name, day, "
    "four-digit year), never as '2026-11-02' and never with a zero-padded day. "
    "A value must not claim more than the article states: keep the exact "
    "figures, names and dates the text gives, and never add words of your own "
    "such as 'full', 'complete', 'all', 'every', 'only' or 'official' unless the "
    "text uses them.\n"
    "A claim label must not add a contrast the article never states: do not write "
    "'and not', 'but not', 'rather than' or 'instead of' unless the article "
    "itself states that contrast.\n"
    "When the article is a company's own post and names a byline (for example "
    "'I'm [Name], [role] at [Company]'), attribute statements and explanations to "
    "that named person and use the company they name as the affiliation; never "
    "set the affiliation to the publishing outlet and never leave a bylined "
    "claim's origin 'unstated'.\n"
    "A named person describing a plan, possibility, intention, design decision or "
    "reason is a 'statement' attributed to that person (kind 'statement', origin "
    "the speaker), never 'confirmed'; a company's own post describing features, "
    "mechanics or a roster it is shipping is 'confirmed' and must carry the "
    "publisher or named speaker as its origin."
)


#: The grouping call's instruction. It sees only claim ids, source ids and
#: labels -- never a source body -- and returns groups of ids that state one
#: fact, so the code can confirm a story from independent sources that agree.
GROUPING_INSTRUCTION = (
    "You are a news-room fact grouper. You are given a list of extracted claims "
    "from several outlets covering one story. Each line names the claim id, the "
    "id of the source it came from and its label.\n"
    "Group together only the claims that state the SAME fact: the same "
    "happening, the same subject and the same specifics. Never merge two "
    "different facts, even when they are about the same game or company: a "
    "sales milestone and a round of layoffs are different facts, and a launch "
    "date and a price are different facts.\n"
    "Return every group as {\"ids\": [claim id, ...]}. Put each claim id in at "
    "most one group. Leave a claim out of every group when no other claim states "
    "the same fact. A fact that only one source states forms no group.\n"
    "Return JSON: {\"groups\": [{\"ids\": [\"<id>\", \"<id>\"]}]}."
)


def _extraction_chain() -> list[dict]:
    """The enabled 'fast' role entries from config.yaml: they pick the extractor.

    This is what actually changes which model extracts claims, so a config edit
    must invalidate already-written facts sheets.
    """
    roles = load_config().get("roles") or {}
    return [
        {"provider": entry.get("provider"), "model": entry.get("model"), "family": entry.get("family")}
        for entry in (roles.get("fast") or [])
        if entry.get("enabled", True)
    ]


class FactsError(Exception):
    """The model output did not match the claim schema; the story is dropped."""


class UnconfirmedStory(Exception):
    """Not enough independent sources; the story must not be published.

    ``budget_truncated`` marks the special case where the run's model-call
    budget stopped extraction before every selected source was read, so the
    verdict is an artifact of the budget, not of the story's coverage. The run
    reports such a candidate as incomplete without remembering it as a failed
    story, so the same cluster is eligible again on the next run.
    """

    def __init__(self, *args: object, budget_truncated: bool = False) -> None:
        super().__init__(*args)
        self.budget_truncated = bool(budget_truncated)


def build_prompt(
    id: str,
    source: dict,
    *,
    max_claims: int = MAX_CLAIMS_PER_SOURCE,
    published: object = None,
) -> str:
    """Build the extraction prompt for ONE source (one model call per source)."""
    parts = [SYSTEM_INSTRUCTION, "", f"Story ID: {id}", ""]
    parts.append(
        f"SOURCE: {source['source_name']} "
        f"(tier {source['tier']}, owner {source['owner']}, region {source['region']})"
    )
    parts.append(f"URL: {source['link']}")
    date = _as_date(published) or _relative_anchor(source)
    if date:
        parts.append(f"PUBLISHED: {_MONTH_NAMES[date.month]} {date.day}, {date.year}")
    if source.get("title") and str(source["title"]).strip():
        parts.append(f"TITLE: {source['title']}")
    parts.append(
        f"Extract up to {max_claims} distinct facts from the text below, always "
        f"including the headline's central claim; every claim's source_url must "
        f"be exactly {source['link']}."
    )
    text = (source.get("text") or "").strip()
    if text:
        parts.append("TEXT:\n" + _truncate(text))
    else:
        parts.append("TEXT: (no text available)")
    return "\n".join(parts)


def _truncate(text: str, limit: int = MAX_SOURCE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [truncated]"


def build_grouping_prompt(items: list[dict]) -> str:
    """Build the grouping prompt from claim ids, source ids and labels only.

    The grouper never sees a source body: it is given one line per claim with
    the claim's id, the id of the source it came from and its clipped label, so
    two outlets restating one fact can be grouped without another look at the
    articles.
    """
    parts = [GROUPING_INSTRUCTION, "", "CLAIMS:"]
    for item in items:
        claim = item.get("claim") or {}
        label = f"{claim.get('claim', '')} {claim.get('value', '')}"
        parts.append(
            f"- {item['id']} | {item['source_id']} | "
            f"{_clip_field(label, GROUPING_LABEL_CHARS)}"
        )
    return "\n".join(parts)


def parse_groups(value: Any) -> list[list[str]]:
    """The id groups of a grouping response, or raise on a malformed shape."""
    errors = schema_errors(value, GROUPING_SCHEMA)
    if errors:
        raise FactsError("; ".join(errors))
    return [
        [str(identifier) for identifier in group.get("ids") or []]
        for group in value.get("groups") or []
        if isinstance(group, dict)
    ]


def extract_claims(
    id: str,
    source: dict,
    *,
    max_claims: int = MAX_CLAIMS_PER_SOURCE,
    generate: Callable[..., Generation] = default_generate,
    run_state: RunState | None = None,
    **kwargs: Any,
) -> Generation:
    """Run the 'fast' role on a single source, returning its Generation."""
    prompt = build_prompt(id, source, max_claims=max_claims)
    return generate("fast", prompt, json_schema=CLAIM_SCHEMA, run_state=run_state, **kwargs)


def _clip_field(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    """Keep claim text and values short so a sheet never stores a long passage."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _with_claim_defaults(data: Any) -> Any:
    """Return ``data`` with the strict string keys filled in when omitted.

    Strict JSON schemas (Groq, OpenAI) require every property to appear in
    ``required``, so the extraction prompt always returns ``affiliation``,
    ``quote``, ``attribution_quote``, ``speaker`` and ``speaker_type`` and uses
    ``""`` (or ``"none"`` for ``speaker_type``) when there is none, coercing a
    ``speaker_type`` outside :data:`SPEAKER_TYPES` to ``"none"``. This keeps our
    own reader tolerant of an output that nevertheless omits a key -- the empty
    value and a missing key are the same -- or misspells the type, and of an
    older sheet written before these fields existed. Anything not shaped like a claims payload is
    returned untouched and left to :func:`schema_errors` to reject.
    """
    if not isinstance(data, dict):
        return data
    claims = data.get("claims")
    if not isinstance(claims, list):
        return data
    normalized = [
        {
            **claim,
            "affiliation": claim.get("affiliation", ""),
            "quote": claim.get("quote", ""),
            "attribution_quote": claim.get("attribution_quote", ""),
            "speaker": claim.get("speaker", ""),
            "speaker_type": _normalize_speaker_type(claim.get("speaker_type")),
        }
        if isinstance(claim, dict)
        else claim
        for claim in claims
    ]
    return {**data, "claims": normalized}


def validate_claims(data: Any, *, source_url: str | None = None) -> list[dict]:
    """Validate the model output against CLAIM_SCHEMA and return the claims.

    ``claim`` and ``value`` are clipped to :data:`MAX_FIELD_CHARS` so a facts
    sheet never holds a long verbatim passage copied from a source article.
    ``origin`` is normalized (trimmed, lowercased; empty becomes
    :data:`UNSTATED_ORIGIN`), ``affiliation`` is normalized (trimmed,
    lowercased; empty stays empty), and a claim whose origin is ``unstated`` is
    capped at ``reported`` because an unattributed claim is never confirmed and
    never an estimate (requirement: treat an unstated origin as at most
    reported).

    ``source_url`` overrides every claim's ``source_url`` with the URL of the
    extraction call that produced it: the model is asked to echo the source URL
    but a misspelled or truncated one must never reach the sheet, so the code
    sets it. Without an override the model's URL is kept and must be http(s).

    The evidence fields are kept verbatim but bounded: ``quote`` and
    ``attribution_quote`` are trimmed and clipped to
    :data:`MAX_QUOTE_CHARS` / :data:`MAX_ATTRIBUTION_QUOTE_CHARS`, ``speaker``
    is trimmed, and ``speaker_type`` falls back to ``"none"`` when it is not one
    of :data:`SPEAKER_TYPES`. Nothing here is lowercased: the quotes are matched
    case-insensitively by :func:`verify_evidence`, which normalizes both sides.

    A missing ``affiliation`` (or evidence) key is treated exactly like an empty
    string before validation, so a model that omits the (now strictly required)
    keys still yields a valid claim.
    """
    data = _with_claim_defaults(data)
    errors = schema_errors(data, CLAIM_SCHEMA)
    if errors:
        raise FactsError("; ".join(errors))
    claims = data["claims"]
    override = str(source_url).strip() if source_url else None
    clipped: list[dict] = []
    for index, claim in enumerate(claims):
        url = override or claim["source_url"]
        if not re.match(r"^https?://", url):
            raise FactsError(f"claims[{index}].source_url must be an http(s) URL: {url!r}")
        origin = _normalize_origin(claim.get("origin"))
        kind = claim["kind"]
        if origin == UNSTATED_ORIGIN and kind in ("confirmed", "estimate"):
            kind = "reported"
        clipped.append(
            _normalize_absence_claim(
                {
                    **claim,
                    "source_url": url,
                    "claim": _clip_field(claim["claim"]),
                    "value": _clip_field(claim["value"]),
                    "origin": origin,
                    "affiliation": _normalize_affiliation(claim.get("affiliation")),
                    "kind": kind,
                    "quote": _clip_field(
                        str(claim.get("quote") or "").strip(), MAX_QUOTE_CHARS
                    ),
                    "attribution_quote": _clip_field(
                        str(claim.get("attribution_quote") or "").strip(),
                        MAX_ATTRIBUTION_QUOTE_CHARS,
                    ),
                    "speaker": str(claim.get("speaker") or "").strip(),
                    "speaker_type": _normalize_speaker_type(claim.get("speaker_type")),
                }
            )
        )
    return clipped


def _normalize_speaker_type(value: object) -> str:
    """A ``speaker_type`` in :data:`SPEAKER_TYPES`, or ``"none"`` when unknown."""
    token = str(value or "").strip().lower()
    return token if token in SPEAKER_TYPES else "none"


def _normalize_origin(origin: object) -> str:
    value = str(origin or "").strip().lower()
    return value or UNSTATED_ORIGIN


def _normalize_affiliation(affiliation: object) -> str:
    value = str(affiliation or "").strip().lower()
    return value if value != UNSTATED_ORIGIN else ""


# --- relative time: absolutize before a claim can be trusted ------------------

#: Relative-time words a claim must never keep: a page that says "runs today"
#: goes stale the moment it is read. Overridable via ``facts.relative_time_words``
#: in config.yaml; defaults keep the code usable without the config.
DEFAULT_RELATIVE_TIME_WORDS = (
    "today", "tonight", "now", "currently", "this week", "this weekend",
    "yesterday", "tomorrow", "recently", "soon", "next week", "out now",
)

_MONTH_NAMES = (
    "", "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)

#: ``published`` values are normally a date or an ISO datetime; parse either.
_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_LINK_DATE_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})(?:/|$)")


def _relative_time_words() -> tuple[str, ...]:
    """The configured relative-time words, longest first for greedy matching."""
    config = load_config().get("facts") or {}
    words = config.get("relative_time_words")
    if not isinstance(words, list) or not words:
        words = list(DEFAULT_RELATIVE_TIME_WORDS)
    cleaned = [str(word).strip().lower() for word in words if str(word).strip()]
    return tuple(sorted(set(cleaned), key=len, reverse=True))


def _as_date(value: object) -> Optional[dt.date]:
    """Parse a date or ISO datetime string into a :class:`datetime.date`."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    match = _DATE_PREFIX_RE.match(str(value or "").strip())
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _source_url_date(source: dict) -> Optional[dt.date]:
    """The date a source's URL path states (``/2026/10/07/``), else ``None``.

    Prefers the ``url_date`` field the gather stage derived from the final URL
    after redirects; falls back to reading the date out of the item's ``link`` so
    sheets built before that field existed still work.
    """
    from_field = _as_date(source.get("url_date"))
    if from_field:
        return from_field
    match = _LINK_DATE_RE.search(str(source.get("link") or ""))
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _source_feed_date(source: dict) -> Optional[dt.date]:
    """The date a source's feed ``published`` field states, else ``None``."""
    return _as_date(source.get("published"))


def _source_anchors(source: dict) -> list[dt.date]:
    """Both publication anchors for a source, distinct, URL date first."""
    anchors: list[dt.date] = []
    for date in (_source_url_date(source), _source_feed_date(source)):
        if date is not None and date not in anchors:
            anchors.append(date)
    return anchors


def _relative_anchor(source: dict) -> Optional[dt.date]:
    """The date to absolutize a claim from: the URL date, else the feed date.

    A dated URL (``/2026/10/07/``) is a page's own publication date; a feed's
    ``published`` can instead be an "updated" or crawl time, so it is only the
    fallback.
    """
    return _source_url_date(source) or _source_feed_date(source)


#: Words that already introduce a date: "from today" must become "from <date>",
#: never "from on <date>".
_DATE_PREPOSITIONS = (
    "as of", "from", "on", "until", "till", "through", "by", "after", "before",
)


def _follows_date_preposition(text: str, match: re.Match) -> bool:
    """True when the matched phrase follows a date preposition ("from today")."""
    prefix = text[: match.start()].rstrip().lower()
    return any(
        re.search(rf"(?<!\w){re.escape(prep)}(?!\w)\s*$", prefix)
        for prep in _DATE_PREPOSITIONS
    )


def absolutize_relative_time(value: object, published: object) -> Optional[str]:
    """Rewrite relative-time words in ``value`` to grammatical absolute dates.

    Every rewrite keeps the date attached to a preposition, so a sentence never
    ends up with a bare calendar date: "begins today" becomes "begins on October
    7, 2026", "is out now on PS5 and PC" becomes "is available on PS5 and PC as
    of October 7, 2026", and "now" or "currently" become "as of <date>". When a
    phrase already follows a date preposition ("from today") the duplicate "on"
    is dropped. "yesterday" and "tomorrow" shift by a day.

    Returns the rewritten value, the unchanged value when it holds no
    relative-time word, or ``None`` when it does but ``published`` gives no date
    to convert from (the caller drops that claim rather than store a stale one).
    """
    text = str(value or "")
    words = _relative_time_words()
    if not text or not words:
        return text
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(word) for word in words) + r")(?!\w)",
        re.IGNORECASE,
    )
    if not pattern.search(text):
        return text
    published_date = _as_date(published)
    if published_date is None:
        return None

    def _date(day: dt.date) -> str:
        return f"{_MONTH_NAMES[day.month]} {day.day}, {day.year}"

    had_out_now = False

    def replace(match: re.Match) -> str:
        nonlocal had_out_now
        word = match.group(0).lower()
        if word == "out now":
            # "is out now on PS5 and PC" rewrites to "is available on PS5 and
            # PC"; the "as of <date>" clause is appended once, at the end.
            had_out_now = True
            return "available"
        delta = {"yesterday": -1, "tomorrow": 1}.get(word, 0)
        day = published_date + dt.timedelta(days=delta)
        if word in ("now", "currently"):
            return f"as of {_date(day)}"
        prefix = "" if _follows_date_preposition(text, match) else "on "
        return prefix + _date(day)

    rewritten = pattern.sub(replace, text)
    if had_out_now:
        rewritten = f"{rewritten} as of {_date(published_date)}"
    return rewritten


def _has_relative_time(value: object) -> bool:
    """True when a value contains a configured relative-time word."""
    text = str(value or "")
    words = _relative_time_words()
    if not text or not words:
        return False
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(word) for word in words) + r")(?!\w)",
        re.IGNORECASE,
    )
    return pattern.search(text) is not None


def _anchor_info(source: dict) -> dict:
    """Resolve a source's two publication anchors and which one to use.

    ``url``/``feed`` are the anchors as dates, ``feed_raw`` the feed string as
    stored (for the sheet), ``used`` the anchor to absolutize from (the URL date
    when present, else the feed date), and ``ambiguous`` true when the two dates
    differ by more than a day -- too far apart to trust either.
    """
    url = _source_url_date(source)
    feed = _source_feed_date(source)
    info: dict = {
        "url": url, "feed": feed, "feed_raw": source.get("published"),
        "used": None, "gap_days": None, "ambiguous": False,
    }
    if url is not None and feed is not None:
        info["gap_days"] = abs((url - feed).days)
        if info["gap_days"] <= 1:
            info["used"] = "url"
        else:
            info["ambiguous"] = True
    elif url is not None:
        info["used"] = "url"
    elif feed is not None:
        info["used"] = "feed"
    return info


def _anchor_strings(info: dict) -> dict:
    """The two anchors as stored (ISO date / raw feed string or ``None``)."""
    return {
        "url": info["url"].isoformat() if info["url"] is not None else None,
        "feed": info["feed_raw"] if info["feed"] is not None else None,
    }


def _anchor_note(link: str, info: dict) -> Optional[dict]:
    """A sheet note about a source's anchors, or ``None`` when none is needed.

    Anchors that agree need no note; a single anchor is recorded (which one was
    used) and so is a one-day disagreement (the URL date is used). The note never
    touches the claim text.
    """
    url, feed = info["url"], info["feed"]
    if url is not None and feed is not None and url == feed:
        return None
    if not info["ambiguous"] and info["used"] is None:
        return None
    note = {
        "source": link,
        "anchors": _anchor_strings(info),
        "used": info["used"],
        "differs": bool(url is not None and feed is not None and url != feed),
        "gap_days": info["gap_days"],
    }
    if info["ambiguous"]:
        note["reason"] = "ambiguous publication date"
    return note


def _absolutize_claims(
    claims: list[dict],
    sources: list[dict],
    *,
    story_id: str = "",
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Replace relative-time values with dates using each source's anchor.

    Returns ``(kept, dropped, anchor_notes, ambiguous_details)``. ``dropped`` are
    claims whose value holds a relative-time word but which have no date to
    convert from. When the URL date and the feed date disagree by more than a day
    the source's anchors are too far apart to trust, so every claim of that source
    whose value holds a relative-time word is dropped as ``ambiguous_details``
    (with both anchors, for the sheet's grounding block) instead of being dated
    from either; the source's other claims are kept unchanged. ``anchor_notes``
    records which anchor was used for each source (and any disagreement).
    """
    by_url = _sources_by_url(sources)
    kept: list[dict] = []
    dropped: list[dict] = []
    notes: list[dict] = []
    ambiguous_details: list[dict] = []
    noted: set[str] = set()
    for claim in claims:
        source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
        link = str(source.get("link") or claim.get("source_url") or "")
        info = _anchor_info(source)
        if link not in noted:
            note = _anchor_note(link, info)
            if note is not None:
                notes.append(note)
            noted.add(link)
        if info["ambiguous"]:
            if _has_relative_time(claim.get("value")):
                ambiguous_details.append(
                    {
                        "claim": claim,
                        "reason": "ambiguous publication date",
                        "anchors": _anchor_strings(info),
                    }
                )
                logger.warning(
                    "story %s: dropping claim with a relative time and an ambiguous "
                    "publication date (url %s vs feed %s): %r",
                    story_id or "?",
                    _anchor_strings(info)["url"],
                    _anchor_strings(info)["feed"],
                    claim.get("value"),
                )
            else:
                kept.append(claim)
            continue
        anchor = info["url"] if info["used"] == "url" else info["feed"]
        rewritten = absolutize_relative_time(claim.get("value"), anchor)
        if rewritten is None:
            dropped.append(claim)
            logger.warning(
                "story %s: dropping claim with a relative time and no date: %r",
                story_id or "?",
                claim.get("value"),
            )
            continue
        kept.append({**claim, "value": rewritten})
    return kept, dropped, notes, ambiguous_details


# --- grounding: a claim's value must stay inside its own source ---------------

_GROUNDING_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_GROUNDING_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")

#: Absolute qualifiers that only a source may state. A value that adds one on
#: its own ("full", "complete") claims more than the article does.
_ABSOLUTE_QUALIFIERS = (
    "full", "complete", "entire", "all", "every", "only",
    "first-ever", "official", "officially", "out now", "currently",
)

#: Contrast connectives that must be stated by the source before a claim label
#: may use them ("... and not a day-one fighter").
_CONTRAST_MARKERS = ("and not", "but not", "rather than", "instead of")


def _numbers_in(text: object) -> set[str]:
    """Digit figures in ``text`` with thousands separators removed."""
    return {
        match.replace(",", "").rstrip(".")
        for match in _GROUNDING_NUMBER_RE.findall(str(text or ""))
    }


def _capitalized_terms(text: object) -> set[str]:
    """Folded capitalized terms in a value, ignoring its first (sentence) word.

    Sentence-initial capitalization is not evidence, so "The" is skipped while
    real names and acronyms ("PvE", "Steam", "October") are checked against the
    source text. Folded for comparison, so "Tōkon" matches "Tokon".
    """
    raw = str(text or "")
    tokens = _GROUNDING_WORD_RE.findall(raw)
    terms: set[str] = set()
    for index, token in enumerate(tokens):
        if index == 0:
            continue
        if token[0].isupper() and any(char.isalpha() for char in token):
            terms.add(cluster.strip_diacritics(token).lower())
    return terms


def _word_in(text: object, phrase: str) -> bool:
    """True when ``phrase`` appears in ``text`` on word boundaries, folded."""
    haystack = cluster.strip_diacritics(text).lower()
    needle = cluster.strip_diacritics(phrase).lower()
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


#: Month name (full or abbreviated) -> calendar month number, for parsing
#: dates written as words.
_MONTH_NUMBERS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_WORD = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_MONTH_FIRST_DATE_RE = re.compile(
    rf"\b(?P<month>{_MONTH_WORD})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*,?\s*(?P<year>\d{{4}}))?(?!\d)",
    re.IGNORECASE,
)
_DAY_FIRST_DATE_RE = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>{_MONTH_WORD})\.?"
    rf"(?:\s*,?\s*(?P<year>\d{{4}}))?(?!\d)",
    re.IGNORECASE,
)

#: A calendar date normalized to ``(year or None, month, day)``.
DateTuple = tuple[Optional[int], int, int]


def _date_matches(text: object) -> list[tuple[DateTuple, set[str]]]:
    """Every date in ``text`` as ``((year|None, month, day), digits-used)``."""
    raw = str(text or "")
    found: list[tuple[DateTuple, set[str]]] = []
    for match in _ISO_DATE_RE.finditer(raw):
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            found.append(((year, month, day), {match.group(1), match.group(2), match.group(3)}))
    for pattern in (_MONTH_FIRST_DATE_RE, _DAY_FIRST_DATE_RE):
        for match in pattern.finditer(raw):
            month = _MONTH_NUMBERS.get(match.group("month").lower())
            if month is None:
                continue
            day = int(match.group("day"))
            if not 1 <= day <= 31:
                continue
            year = int(match.group("year")) if match.group("year") else None
            digits = {match.group("day")}
            if match.group("year"):
                digits.add(match.group("year"))
            found.append(((year, month, day), digits))
    return found


def _dates_in(text: object) -> set[DateTuple]:
    """Calendar dates in ``text``, in ISO, "November 2" or "2nd November" form."""
    return {date for date, _digits in _date_matches(text)}


def _date_number_tokens(text: object) -> set[str]:
    """Digit tokens that make up a date in ``text`` (so they are not read as figures)."""
    tokens: set[str] = set()
    for _date, digits in _date_matches(text):
        tokens |= digits
    return tokens


def _format_date(date: DateTuple) -> str:
    """Render a normalized date the way the pipeline writes dates."""
    year, month, day = date
    text = f"{_MONTH_NAMES[month]} {day}"
    return f"{text}, {year}" if year is not None else text


def _date_grounded(
    date: DateTuple,
    source_dates: set[DateTuple],
    anchors: Optional[Iterable[dt.date]],
) -> bool:
    """True when a value's date appears in the source or either anchor date.

    A date matches on month and day; a year given in the value must match too,
    but a source or anchor without a year still grounds the month and day
    ("2026-11-02" is grounded by a body that says only "November 2"). Both
    publication anchors count, so a date taken from either the URL date or the
    feed date is grounded.
    """
    year, month, day = date
    for source_year, source_month, source_day in source_dates:
        if source_month == month and source_day == day:
            if year is None or source_year is None or source_year == year:
                return True
    for anchor in anchors or ():
        if anchor.month == month and anchor.day == day:
            if year is None or anchor.year == year:
                return True
    return False


def _value_month_words(dates: set[DateTuple]) -> set[str]:
    """Month words (full and abbreviated) used by a value's dates, folded."""
    words: set[str] = set()
    for _year, month, _day in dates:
        name = _MONTH_NAMES[month].lower()
        words.add(name)
        words.add(name[:3])
    return words


def _anchor_tokens(anchor: Optional[dt.date]) -> tuple[set[str], set[str]]:
    """The figures and month names an anchor publication date grounds.

    A claim may state the feed item's own publication date even when the body
    only gives it relatively ("out now", "today"): every component of
    ``anchor`` -- year, month and day, in any common format, the month's name
    and abbreviation included -- counts as grounded, because those tokens come
    from the feed/URL date rather than the article text.
    """
    if anchor is None:
        return set(), set()
    figures = {
        str(anchor.year),
        f"{anchor.year:02d}"[-2:],
        str(anchor.month),
        f"{anchor.month:02d}",
        str(anchor.day),
        f"{anchor.day:02d}",
    }
    terms: set[str] = set()
    if 1 <= anchor.month <= 12:
        name = _MONTH_NAMES[anchor.month].lower()
        terms.add(name)
        terms.add(name[:3])
    return figures, terms


def grounding_failure(
    claim: dict,
    source_text: object,
    *,
    anchor: Optional[dt.date] = None,
    anchors: Optional[Iterable[dt.date]] = None,
) -> Optional[str]:
    """Why a claim's value or label goes beyond its source, or ``None``.

    Checked against the claim's own source text, diacritics-insensitive: every
    number in the value must appear in the text, every capitalized name or
    acronym the value uses must appear in the text, an absolute qualifier
    ("full", "complete") is allowed only when the text uses it, and a contrast
    connective in the label ("and not", "rather than") is allowed only when the
    text states it. ``anchors`` are the source's publication dates (its URL date
    and its feed date); a single ``anchor`` is accepted for callers that have
    only one. Figures equal to any component of an anchor (year, month, day, in
    any common format, the month's name and abbreviation included) count as
    grounded too, because such tokens come from the date rather than the body.

    Dates are compared as calendar dates, not digit strings, so a value written
    as "2026-11-02" is grounded by a body that says "November 2", and a value's
    date is grounded when it matches the source's month and day or either anchor
    date. The digits a date is made of are not treated as loose figures.
    """
    if anchors is None:
        anchors = [anchor] if anchor is not None else []
    else:
        anchors = list(anchors)
        if anchor is not None and anchor not in anchors:
            anchors.append(anchor)
    value = str(claim.get("value") or "")
    label = str(claim.get("claim") or "")
    source = str(source_text or "")
    anchor_figures: set[str] = set()
    anchor_terms: set[str] = set()
    for item in anchors:
        figures, terms = _anchor_tokens(item)
        anchor_figures |= figures
        anchor_terms |= terms
    value_dates = _dates_in(value)
    date_tokens = _date_number_tokens(value)
    missing_numbers = (
        _numbers_in(value) - _numbers_in(source) - anchor_figures - date_tokens
    )
    if missing_numbers:
        return f"value states {sorted(missing_numbers)} the source never gives"
    source_dates = _dates_in(source)
    month_words = _value_month_words(value_dates)
    for date in sorted(value_dates, key=lambda item: -1 if item[0] is None else item[0]):
        if not _date_grounded(date, source_dates, anchors):
            return f"value states the date {_format_date(date)} the source never gives"
    for term in _capitalized_terms(value):
        if term in anchor_terms or term in month_words:
            continue
        if not _word_in(source, term):
            return f"value names {term!r} the source never gives"
    for qualifier in _ABSOLUTE_QUALIFIERS:
        if _word_in(value, qualifier) and not _word_in(source, qualifier):
            return f"value uses {qualifier!r} the source never states"
    for marker in _CONTRAST_MARKERS:
        if _word_in(label, marker) and not _word_in(source, marker):
            return f"label adds the contrast {marker!r} the source never states"
    return None


def ground_claims_details(
    claims: list[dict],
    sources: list[dict],
    *,
    story_id: str = "",
) -> tuple[list[dict], list[dict]]:
    """Drop unsupported claims, returning ``(kept, dropped_details)``.

    Each entry in ``dropped_details`` is ``{"claim": <claim>, "reason": <str>}``
    so the caller can persist why a claim was discarded. Each claim is checked
    against its own source's text; the source's publication anchors (its URL date
    and its feed date) are passed, so either date's tokens count as grounded and a
    feed-only or URL-only date is never treated as ungrounded.
    """
    by_url = _sources_by_url(sources)
    kept: list[dict] = []
    dropped: list[dict] = []
    for claim in claims:
        source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
        reason = grounding_failure(
            claim,
            source.get("text") or "",
            anchors=_source_anchors(source),
        )
        if reason is None:
            kept.append(claim)
        else:
            dropped.append({"claim": claim, "reason": reason})
            logger.warning(
                "story %s: dropping ungrounded claim from %s (%s): %r",
                story_id or "?",
                claim.get("source_url"),
                reason,
                claim.get("claim"),
            )
    return kept, dropped


def ground_claims(
    claims: list[dict],
    sources: list[dict],
    *,
    story_id: str = "",
) -> tuple[list[dict], list[dict]]:
    """Drop claims whose value or label is not supported by their own source."""
    kept, dropped_details = ground_claims_details(claims, sources, story_id=story_id)
    return kept, [entry["claim"] for entry in dropped_details]


# --- bylines: a company post's speaker is the person it names -----------------

#: A company's own post opens with its author. Three explicit forms are
#: trusted: a self-introduction ("I'm <Name>, <role> at <Company>"), a byline
#: ("by <Name>, <Role>, <Company>") and a byline line ("<Name>, <Role>,
#: <Company>"). Ordinary prose that merely mentions a name is never a byline.
_BYLINE_NAME = r"[^\W_][\w'’-]*(?:\s+[^\W_][\w'’-]*){0,3}?"
_BYLINE_ORG = r"[^\W_][\w'’&\-]*(?:\s+[^\W_][\w'’&\-]*){0,4}"
_BYLINE_RE = re.compile(
    r"\bI(?:['’]m| am)\s+"
    rf"({_BYLINE_NAME})\s*,\s*[^.\n]{{0,80}}?\b(?:at|of|for)\s+({_BYLINE_ORG})",
    re.IGNORECASE,
)
#: "by <Name>, <Role>, <Company>" — a byline line where the role is optional.
_BYLINE_BY_RE = re.compile(
    rf"\bby\s+({_BYLINE_NAME})\s*,\s*([^,.\n]{{0,60}}?),\s*({_BYLINE_ORG})",
    re.IGNORECASE,
)
#: "<Name>, <Role>, <Company>" opening a company post.
_BYLINE_ROLE_RE = re.compile(
    rf"^\s*({_BYLINE_NAME})\s*,\s*([^,.\n]{{0,60}}?),\s*({_BYLINE_ORG})",
    re.IGNORECASE,
)


def _extract_byline(text: object) -> tuple[str, str]:
    """The ``(name, organization)`` a post's byline states, or ``("", "")``."""
    raw = str(text or "")
    for match in _BYLINE_RE.finditer(raw):
        return match.group(1).strip(), match.group(2).strip()
    byline = _BYLINE_BY_RE.search(raw)
    if byline:
        return byline.group(1).strip(), byline.group(3).strip()
    role_line = _BYLINE_ROLE_RE.search(raw)
    if role_line:
        return role_line.group(1).strip(), role_line.group(3).strip()
    return "", ""


def _is_own_site(source: dict) -> bool:
    """True when the gathered source is a tier-1 organization's own site."""
    try:
        return int(source.get("tier") or 0) == 1
    except (TypeError, ValueError):
        return False


def _self_attribution(
    text: str, source: dict, byline_name: str, byline_org: str, aliases: dict[str, str]
) -> str:
    """The origin a company's own first-person post attributes to itself, or "".

    Only a tier-1 own-site post that carries a byline self-attributes. The
    organization the byline states wins ("Producer, Arc System Works"); else the
    named person; else the source's owner. An outlet article using "we" or "I"
    never reaches here, so its unattributed claims stay unstated.
    """
    if not _is_own_site(source) or not (byline_name or byline_org):
        return ""
    if byline_org:
        return canonicalize_origin(byline_org, aliases)
    if byline_name and _origin_in_text(byline_name, text, aliases):
        return canonicalize_origin(byline_name, aliases)
    owner = source.get("owner")
    if owner:
        return canonicalize_origin(owner, aliases)
    return ""


def _is_publication(name: object, source: dict) -> bool:
    """True when ``name`` is the publishing outlet itself, not a speaker's org.

    A publication ("PlayStation Blog") is not the organization a bylined person
    speaks for, so it must be replaced by the byline's own organization.
    """
    key = _origin_key(name)
    if not key:
        return False
    for field in ("source_name", "owner"):
        target = _origin_key(source.get(field))
        if target and (key == target or _contains_name(key, target)):
            return True
    return False


# --- origin canonicalization and verification --------------------------------


def _strip_possessive(text: str) -> str:
    return re.sub(r"['\u2019]s\b", "", text)


def _origin_key(origin: object) -> str:
    """Match key for an origin: punctuation-free, lowercased, titles stripped.

    Used for alias lookup, source-text verification and containment, so casing,
    possessives, leading titles and punctuation never split one origin into
    several spellings of the same name.
    """
    words = cluster.normalize_title(_strip_possessive(str(origin or "").lower())).split()
    while words and words[0] in _ORIGIN_TITLE_WORDS:
        words.pop(0)
    return " ".join(words)


def _origin_display(origin: object) -> str:
    """Canonical human-readable origin: lowercased, no possessive, no leading title."""
    text = re.sub(r"\s+", " ", _strip_possessive(str(origin or "").lower())).strip()
    text = text.strip(" \t.,;:!?")
    words = text.split()
    while words and words[0] in _ORIGIN_TITLE_WORDS:
        words.pop(0)
    return " ".join(words)


def _contains_name(haystack: str, needle: str) -> bool:
    """True when match-normalized ``needle`` appears in ``haystack`` on word boundaries."""
    if not needle:
        return False
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None


# --- source-name validity and coreference ------------------------------------


def _origin_alias_pairs(aliases: dict[str, str]) -> list[tuple[str, str]]:
    """``(match key, canonical name)`` pairs from an alias map.

    Both the alias keys and the canonical values contribute, so a phrase either
    spelled like an alias or like the canonical name resolves.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for alias, canonical in aliases.items():
        key = _origin_key(alias)
        pair = (key, canonical)
        if key and pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    for canonical in set(aliases.values()):
        key = _origin_key(canonical)
        pair = (key, canonical)
        if key and pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def _is_coreference_phrase(value: object) -> bool:
    """True for a definite/possessive back-reference ("the company", "its studio")."""
    words = _origin_key(value).split()
    while words and words[0] in _COREFERENCE_LEADERS:
        words.pop(0)
    return " ".join(words) in _COREFERENCE_NOUNS


def _is_document_or_product(key: str) -> bool:
    """True when a match key names a document or product, never a source."""
    if not key:
        return True
    if key in _DOCUMENT_NOUNS or key in _NON_SOURCE_TERMS or key in _NON_SOURCE_PHRASES:
        return True
    words = key.split()
    return bool(words) and words[-1] in _NON_SOURCE_TERMS


def _is_filler_capture(key: str) -> bool:
    """True when every token of a capture is a stop word or an anonymous token."""
    tokens = _anonymous_tokens(key)
    if not tokens:
        return True
    vocabulary = _STOP_WORDS | anonymous_vocabulary() | _FILLER_WORDS
    return all(token in vocabulary for token in tokens)


def _is_valid_source_name(
    raw: object,
    resolved: object,
    aliases: dict[str, str],
    entities: list[str] | None = None,
) -> bool:
    """True when a cue capture may name a source at all.

    A source is a proper-noun phrase that is neither a document ("blog post",
    "statement"), a product or piece of software ("Launcher", "Wine", "Steam
    Deck") nor an all-filler capture ("four industry sources", "the"). A name in
    an alias map always counts.
    """
    key = _origin_key(resolved) or _origin_key(raw)
    if not key:
        return False
    if _is_known_origin_name(key, aliases):
        return True
    if _is_coreference_phrase(key):
        return True
    if _is_document_or_product(key):
        return False
    if _is_filler_capture(key):
        return False
    if _is_product_origin(resolved if resolved is not None else raw, entities):
        return False
    return True


def _strip_leading_roles(raw: object) -> str:
    """Drop a leading lowercase role phrase ("senior editor Alice") from a capture."""
    words = re.findall(r"[A-Za-z][\w'’\-]*", str(raw or ""))
    while len(words) > 1 and words[0][:1].islower():
        words.pop(0)
    return " ".join(words)


def _resolve_cue_source(
    raw: object,
    text: str = "",
    position: int = 0,
    aliases: dict[str, str] | None = None,
    entities: list[str] | None = None,
) -> str:
    """Resolve one cue capture to a canonical source name, or "".

    An alias anywhere in the capture wins ("Epic blog post" -> "Epic Games"). A
    coreference phrase ("the company") resolves to the nearest organization named
    before it in the same paragraph, or "" when none precedes it.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    key = _origin_key(raw)
    if not key:
        return ""
    if _is_coreference_phrase(key):
        return _nearest_preceding_organization(text, position, aliases)
    best = ""
    best_len = -1
    for name, canonical in _origin_alias_pairs(aliases):
        if len(name) > best_len and _contains_name(key, name):
            best, best_len = canonical, len(name)
    if best:
        return best
    remainder = _strip_leading_roles(raw)
    rkey = _origin_key(remainder)
    if rkey and rkey != key:
        best = ""
        best_len = -1
        for name, canonical in _origin_alias_pairs(aliases):
            if len(name) > best_len and _contains_name(rkey, name):
                best, best_len = canonical, len(name)
        if best:
            return best
    return _origin_display(remainder or raw)


def _is_organization_name(value: object, organizations: frozenset[str]) -> bool:
    """True when ``value`` is a known organization or has a corporate suffix."""
    return _matches_organization(value, organizations) or _has_corporate_suffix(value)


def _nearest_preceding_organization(
    text: str, position: int, aliases: dict[str, str] | None = None
) -> str:
    """The closest organization named before ``position`` in the same paragraph.

    A coreference phrase ("the company") stands for the last organization the
    paragraph named. The search is clamped to the paragraph so a name from an
    unrelated section is never mistaken for the antecedent.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    prefix = str(text or "")[: max(0, int(position))]
    split = prefix.rfind("\n\n")
    if split != -1:
        prefix = prefix[split + 2 :]
    organizations = load_organizations()
    cand = ""
    cand_end = -1
    for match in re.finditer(_CAP_NAME_CHUNK, prefix):
        raw = match.group(0)
        key = _origin_key(raw)
        if not key:
            continue
        if not (_matches_organization(raw, organizations) or _has_corporate_suffix(raw)):
            if not any(_contains_name(key, name) for name, _ in _origin_alias_pairs(aliases)):
                continue
        if match.end() > cand_end:
            cand, cand_end = raw, match.end()
    if not cand:
        return ""
    resolved = _resolve_cue_source(cand, text, 0, aliases)
    return resolved or _origin_display(cand)


def load_origin_aliases(path: str | Path | None = None) -> dict[str, str]:
    """Alias -> canonical origin map from ``data/origins.json``.

    Optional: a missing, unreadable or malformed file yields ``{}`` so
    extraction still runs. Alias keys are stored under their match key
    (:func:`_origin_key`) and values under their canonical display name, so
    lookups ignore case, possessives, titles and punctuation.
    """
    path = Path(path) if path else DEFAULT_ORIGIN_ALIASES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, str] = {}
    for alias, canonical in raw.items():
        if not isinstance(canonical, str):
            continue
        key = _origin_key(alias)
        name = _origin_display(canonical)
        if key and name:
            aliases[key] = name
    # Organization short forms ("Epic") resolve to the canonical name declared in
    # data/orgs.json ("Epic Games"), so the sheet, the confirmation block and the
    # writer never see the short form. Explicit origins.json aliases win.
    for key, name in load_organization_aliases().items():
        aliases.setdefault(key, name)
    return aliases


def load_organization_aliases(path: str | Path | None = None) -> dict[str, str]:
    """Alias -> canonical organization name map from ``data/orgs.json``.

    Only entries that declare both a canonical name and aliases contribute;
    a plain string entry has no short form. Alias keys and values follow the
    same :func:`_origin_key` / :func:`_origin_display` normalization as
    :func:`load_origin_aliases`, so casing, possessives and punctuation never
    matter. A missing or malformed file yields ``{}``.
    """
    path = Path(path) if path else DEFAULT_ORGS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("organizations") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return {}
    aliases: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        canonical = entry.get("canonical")
        if not isinstance(canonical, str):
            continue
        name = _origin_display(canonical)
        if not name:
            continue
        for alias in entry.get("aliases") or []:
            key = _origin_key(alias)
            if key:
                aliases[key] = name
    return aliases


_ORGANIZATIONS_CACHE: tuple[tuple[int, int] | None, frozenset[str]] | None = None


def load_organizations(path: str | Path | None = None) -> frozenset[str]:
    """Known organization match keys from ``data/orgs.json``.

    The file is ``{"organizations": [...]}`` where each entry is either a name
    string or ``{"canonical": ..., "aliases": [...]}``. Keys are stored under
    :func:`_origin_key`, so casing, possessives and punctuation never matter.
    Cached by file mtime/size; a missing or malformed file yields an empty set
    so extraction still runs.
    """
    global _ORGANIZATIONS_CACHE
    path = Path(path) if path else DEFAULT_ORGS_PATH
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _ORGANIZATIONS_CACHE = (None, frozenset())
        return frozenset()
    if _ORGANIZATIONS_CACHE is not None and _ORGANIZATIONS_CACHE[0] == stamp:
        return _ORGANIZATIONS_CACHE[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _ORGANIZATIONS_CACHE = (stamp, frozenset())
        return frozenset()
    raw = data.get("organizations") if isinstance(data, dict) else data
    keys: set[str] = set()
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                key = _origin_key(entry)
                if key:
                    keys.add(key)
            elif isinstance(entry, dict):
                for name in [entry.get("canonical"), *(entry.get("aliases") or [])]:
                    key = _origin_key(name)
                    if key:
                        keys.add(key)
    frozen = frozenset(keys)
    _ORGANIZATIONS_CACHE = (stamp, frozen)
    return frozen


def canonicalize_origin(origin: object, aliases: dict[str, str] | None = None) -> str:
    """Resolve one origin string to its canonical name.

    An alias in :func:`load_origin_aliases` is mapped to its canonical name, and
    when a canonical name appears inside the origin ("Rhys Elliott of Alinea
    Analytics") the firm wins, so a person named alongside a firm counts as the
    firm. Empty or ``unstated`` input stays :data:`UNSTATED_ORIGIN`.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    key = _origin_key(origin)
    if not key or key == UNSTATED_ORIGIN:
        return UNSTATED_ORIGIN
    if key in aliases:
        return aliases[key]
    embedded = [
        canonical
        for canonical in set(aliases.values())
        if _contains_name(key, _origin_key(canonical))
    ]
    if embedded:
        return max(embedded, key=len)
    return _origin_display(origin) or UNSTATED_ORIGIN


def _origin_names(origin: object, aliases: dict[str, str]) -> set[str]:
    """Every match key that identifies an origin: its own form, its canonical
    name, and any alias that resolves to that canonical name."""
    canonical = canonicalize_origin(origin, aliases)
    if canonical == UNSTATED_ORIGIN:
        return set()
    canonical_key = _origin_key(canonical)
    names = {_origin_key(origin), canonical_key}
    names.update(
        alias for alias, target in aliases.items() if _origin_key(target) == canonical_key
    )
    return {name for name in names if name}


def _origin_in_text(origin: object, text: str, aliases: dict[str, str]) -> bool:
    """True when any spelling of ``origin`` appears in the source ``text``."""
    haystack = _origin_key(text)
    if not haystack:
        return False
    return any(_contains_name(haystack, name) for name in _origin_names(origin, aliases))


def _anonymous_tokens(folded: str) -> list[str]:
    """The word tokens of a normalized origin, possessive "s" already stripped."""
    return [token for token in re.split(r"[^a-z0-9]+", folded) if token]


def _is_known_origin_name(folded: str, aliases: dict[str, str]) -> bool:
    """True when ``folded`` matches a known origin name or alias in origins.json."""
    if not folded:
        return False
    if folded in aliases:
        return True
    return any(_origin_key(canonical) == folded for canonical in aliases.values())


#: Speech verbs: their subject is a person who said something, so that person
#: stays a speaker even when they are also a headline entity.
_ATTRIBUTION_SPEECH_VERBS = (
    "said", "says", "say", "told", "tells", "tell", "admitted", "admits",
    "blames", "blamed", "claims", "claimed", "wrote", "writes", "posted",
    "posts", "tweeted", "tweets", "stated", "states", "explains", "explained",
    "added", "adds", "noted", "notes", "warned", "warns",
)
#: Announcement and reporting verbs: their subject is an organization (a company
#: or studio) that put something out. They never make a game, character or
#: product a speaker, so a game that "reveals" a roster stays a subject.
_ATTRIBUTION_ANNOUNCEMENT_VERBS = (
    "announced", "announces", "revealed", "reveals", "confirmed", "confirms",
    "unveiled", "unveils", "detailed", "details", "shared", "shares",
    "reported", "reports", "estimated", "estimates", "suggested", "suggests",
    "alleged", "alleges",
)
_ATTRIBUTION_VERBS = tuple(
    dict.fromkeys(_ATTRIBUTION_SPEECH_VERBS + _ATTRIBUTION_ANNOUNCEMENT_VERBS)
)
_SPEECH_VERB_RE = "|".join(sorted(set(_ATTRIBUTION_SPEECH_VERBS), key=len, reverse=True))
_ATTRIBUTION_VERB_RE = "|".join(sorted(set(_ATTRIBUTION_VERBS), key=len, reverse=True))
_ATTRIBUTION_LEAD_RE = (
    r"in a statement from|in a statement|"
    r"according to|data from|figures from|numbers from|estimates from|per|citing"
)
#: A capitalized origin chunk (up to four words) after a lead cue or before a
#: verb. Capitalization is the cheap way to find a name without an NLP parser.
_NAME_CHUNK = r"[A-Za-z][\w'’&\-\']*(?:\s+(?!has\b|have\b|had\b|is\b|was\b|were\b|will\b)[A-Za-z][\w'’&\-\']*){0,3}"
#: The lead cue is matched case-insensitively (``(?i:...)``) but the origin and
#: its filler are not: the filler may only be lowercase (adverbs, articles), so
#: "according to an Epic blog post" captures "Epic blog post" rather than the
#: capitalized words eating into the name. "via" and "in an interview with" are
#: deliberately not leads: they place a product or an outlet next to the claim,
#: never a source of it.
_LEAD_CUE_RE = re.compile(
    rf"\b(?i:{_ATTRIBUTION_LEAD_RE})\s+(?:[a-z]+\s+){{0,4}}(?P<origin>{_NAME_CHUNK})"
)
_SUBJECT_CUE_RE = re.compile(
    rf"\b(?P<origin>{_NAME_CHUNK})(?:\s*,[^,.\n]{{0,40}},)?"
    rf"\s+(?:(?:has|have|had|is|was|were|will)\s+)?(?P<verb>{_ATTRIBUTION_VERB_RE})\b",
    re.I,
)
#: A capitalized name chunk (up to four words). Unlike :data:`_NAME_CHUNK` the
#: first word must be capitalized, for the inverted headline form where the
#: organization follows the verb ("..., confirms Epic Games").
_CAP_NAME_CHUNK = (
    r"[A-Z][\w'’&\-]*(?:\s+(?!has\b|have\b|had\b|is\b|was\b|were\b|will\b)"
    r"[A-Za-z][\w'’&\-]*){0,3}"
)
_ANNOUNCEMENT_VERB_RE = "|".join(
    sorted(set(_ATTRIBUTION_ANNOUNCEMENT_VERBS), key=len, reverse=True)
)
#: Inverted headline attribution: the organization follows an announcement verb
#: ("Fortnite is coming to Linux, confirms Epic Games"). Only announcement verbs
#: qualify, so "said Dan Houser" stays a speech cue handled by the speech path.
_POST_VERB_CUE_RE = re.compile(
    rf"(?:,\s*|\s+)(?i:(?P<verb>{_ANNOUNCEMENT_VERB_RE}))\s+(?P<origin>{_CAP_NAME_CHUNK})\b"
)


def _sentences(text: str) -> list[str]:
    """Split ``text`` into sentences on end punctuation kept simple."""
    return [part for part in re.split(r"(?<=[.!?])\s+", str(text or "")) if part.strip()]


def _sentences_with_positions(text: str) -> list[tuple[int, int, str]]:
    """Sentences tagged ``(paragraph_index, index_in_paragraph)``.

    Paragraphs are separated by a blank line; the attribution window is clamped
    to one paragraph so a cue in an unrelated section never vouches for a claim.
    """
    out: list[tuple[int, int, str]] = []
    for pi, para in enumerate(re.split(r"\n\s*\n", str(text or ""))):
        for si, sent in enumerate(_sentences(para)):
            out.append((pi, si, sent))
    return out


def _sentence_starts(text: str) -> dict[tuple[int, int], int]:
    """Absolute character offset of each sentence, keyed ``(paragraph, index)``."""
    starts: dict[tuple[int, int], int] = {}
    cursor = 0
    for pi, para in enumerate(re.split(r"\n\s*\n", str(text or ""))):
        offset = cursor
        for si, sent in enumerate(_sentences(para)):
            idx = text.find(sent, offset)
            if idx < 0:
                idx = offset
            starts[(pi, si)] = idx
            offset = idx + len(sent)
        cursor = offset
    return starts


def _claim_key_terms(claim: dict) -> set[str]:
    """Stemmed content words of a claim's text and value, for sentence matching."""
    return _topic_words(claim.get("claim")) | _topic_words(claim.get("value"))


def _claim_attribution_window(
    text: str, claim: dict
) -> tuple[list[tuple[int, int, str]], list[tuple[int, int]]]:
    """The claim's attribution window and its strongest claim-hit positions.

    A sentence is in the window when it shares a paragraph with a sentence that
    carries the claim's key terms and sits within three sentences of it. The
    anchor hits are the sentence(s) with the most key terms in common, so a
    passing mention ("GTA Online") never outweighs the sentence that actually
    states the claim ("GTA 6 ... PC ... 2027"). When the claim shares no content
    word with the text (a paraphrase) the whole text is searched, so a genuine
    paraphrase is never dropped for lack of a local match.
    """
    sents = _sentences_with_positions(text)
    terms = _claim_key_terms(claim)
    scored = [
        (pi, si, len(_topic_words(sent) & terms))
        for pi, si, sent in sents
        if terms and (_topic_words(sent) & terms)
    ]
    if not terms or not scored:
        return sents, []
    all_hits = [(pi, si) for pi, si, _ in scored]
    best = max(score for _, _, score in scored)
    anchors = [(pi, si) for pi, si, score in scored if score == best]
    window = [
        (pi, si, sent)
        for pi, si, sent in sents
        if any(pi == hpi and abs(si - hsi) <= 3 for hpi, hsi in all_hits)
    ]
    return window, anchors


def _make_occurrence(
    raw: object,
    kind: str,
    text: str,
    position: int,
    pi: int,
    si: int,
    aliases: dict[str, str],
    entities: list[str] | None = None,
) -> Optional[dict]:
    """Build one cue occurrence, resolving coreference and alias captures.

    A coreference phrase ("the company") resolves to the nearest preceding
    organization, or yields no occurrence when none precedes it. For a normal
    capture the occurrence keeps the raw text as its ``origin`` (so callers see
    the wording the text used) and records the resolved canonical name as
    ``resolved``.
    """
    key = _origin_key(raw)
    if not key:
        return None
    if _is_coreference_phrase(key):
        resolved = _nearest_preceding_organization(text, abs(int(position)), aliases)
        if not resolved:
            return None
        return {"origin": resolved, "resolved": resolved, "kind": kind, "pi": pi, "si": si}
    raw_text = str(raw).strip()
    resolved = _resolve_cue_source(raw_text, text, abs(int(position)), aliases, entities)
    return {
        "origin": raw_text,
        "resolved": resolved or raw_text,
        "kind": kind,
        "pi": pi,
        "si": si,
    }


def _leading_source(
    sent: str,
    base: int,
    text: str,
    aliases: dict[str, str],
    entities: list[str] | None,
) -> Optional[tuple[str, int, int]]:
    """The first plausible source named at the start of a sentence, or None.

    A clause subject ("Epic Games, which earlier said ...") is only trusted when
    it is at least a two-word name or a known organization, so a stray capitalized
    word ("Fortnite", "Staff", "Per") never becomes a source.
    """
    organizations = load_organizations()
    for match in re.finditer(_CAP_NAME_CHUNK, sent):
        raw = match.group(0)
        resolved = _resolve_cue_source(raw, text, base + match.start(), aliases, entities)
        if not resolved:
            continue
        key = _origin_key(resolved)
        if not key or not _is_valid_source_name(raw, resolved, aliases, entities):
            continue
        if (
            len(key.split()) >= 2
            or _is_known_origin_name(key, aliases)
            or _is_organization_name(raw, organizations)
        ):
            return resolved, match.start(), match.end()
    return None


def _clause_subject_occurrences(
    sent: str,
    base: int,
    text: str,
    covered: list[tuple[int, int]],
    pi: int,
    si: int,
    aliases: dict[str, str],
    entities: list[str] | None,
) -> list[dict]:
    """Cue occurrences for a clause subject the subject regex missed.

    A headline such as "Epic Games vibe codes a new launcher, claims it starts
    46 percent faster" names its source once at the front and then attributes a
    later clause to it. For each attribution verb the subject regex did not
    already bind, when the sentence's leading source sits before the verb with a
    comma in between, the leading source owns that verb.
    """
    lead = _leading_source(sent, base, text, aliases, entities)
    if lead is None:
        return []
    resolved, _lstart, lend = lead
    out: list[dict] = []
    for match in re.finditer(rf"\b(?:{_ATTRIBUTION_VERB_RE})\b", sent, re.I):
        vstart, _vend = match.span()
        if any(cs <= vstart < ce for cs, ce in covered):
            continue
        if lend > vstart:
            continue
        between = sent[lend:vstart]
        if "," not in between or len(between) > 200:
            continue
        verb = match.group(0).lower()
        kind = "speech" if re.fullmatch(_SPEECH_VERB_RE, verb) else "announcement"
        out.append({"origin": resolved, "resolved": resolved, "kind": kind, "pi": pi, "si": si})
    return out


def _cue_occurrences(
    text: str,
    claim: dict,
    aliases: dict[str, str] | None = None,
    entities: list[str] | None = None,
) -> tuple[list[dict], list[tuple[int, int]]]:
    """Every attribution cue in the claim's window, with its verb kind.

    ``kind`` is ``"speech"`` (a person speaks), ``"announcement"`` (an
    organization puts something out) or ``"lead"`` (a reporting phrase such as
    "according to"). A cue that only names a document or product is dropped, and
    a coreference phrase is mapped to the organization it stands for.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    window, hits = _claim_attribution_window(text, claim)
    starts = _sentence_starts(text)
    occurrences: list[dict] = []
    for pi, si, sent in window:
        base = starts.get((pi, si), 0)
        covered: list[tuple[int, int]] = []
        for match in _LEAD_CUE_RE.finditer(sent):
            occ = _make_occurrence(
                match.group("origin").strip(), "lead", text,
                base + match.start("origin"), pi, si, aliases, entities,
            )
            if occ:
                occurrences.append(occ)
        for match in _SUBJECT_CUE_RE.finditer(sent):
            verb = match.group("verb").lower()
            kind = "speech" if re.fullmatch(_SPEECH_VERB_RE, verb) else "announcement"
            occ = _make_occurrence(
                match.group("origin").strip(), kind, text,
                base + match.start("origin"), pi, si, aliases, entities,
            )
            if occ:
                occurrences.append(occ)
            covered.append((match.start("verb"), match.end("verb")))
        occurrences.extend(
            _clause_subject_occurrences(sent, base, text, covered, pi, si, aliases, entities)
        )
    return occurrences, hits


def _cue_distance(occurrence: dict, hits: list[tuple[int, int]]) -> tuple[int, int, int]:
    """Sort key for a cue: the nearest preceding cue to the claim wins.

    A cue in the claim's own sentence (distance 0) sorts first, then the closest
    sentence before it, then the closest sentence after it. Without a claim-hit
    sentence every cue ties, so the earliest in the text wins.
    """
    if not hits:
        return (0, 0, 0)
    best: tuple[int, int, int] | None = None
    for hpi, hsi in hits:
        if occurrence["pi"] != hpi:
            continue
        delta = occurrence["si"] - hsi
        key = (0, -delta, abs(delta)) if delta <= 0 else (1, delta, abs(delta))
        if best is None or key < best:
            best = key
    return best if best is not None else (2, 0, 0)


def _is_anonymous_origin(origin: object, aliases: dict[str, str]) -> bool:
    """True when a cue's origin names no real source ("sources", "unnamed sources").

    Closed-vocabulary rule: a phrase is anonymous only when every one of its
    tokens (lowercased, possessive "s" stripped) is in
    :func:`anonymous_vocabulary`. Any token outside the vocabulary makes it a
    name ("Insider Gaming", "Source Engine", "People Can Fly"), and a name in
    ``data/origins.json`` is always named.
    """
    folded = _origin_key(origin)
    if not folded:
        return True
    if _is_coreference_phrase(folded):
        return False
    if _is_known_origin_name(folded, aliases):
        return False
    tokens = _anonymous_tokens(folded)
    if not tokens:
        return True
    vocabulary = anonymous_vocabulary()
    return all(token in vocabulary for token in tokens)


def _attribution_winner(text: str, claim: dict, aliases: dict[str, str] | None = None) -> str:
    """The origin the nearest cue in the claim's window attributes it to, or "".

    "According to Bloomberg, A happened. Rockstar said B happened." attributes B
    to Rockstar: the later cue names a different source, so Bloomberg no longer
    carries forward, and with several cues the nearest preceding one wins.
    Anonymous cues ("according to sources") never outrank a named source, so a
    named outlet in the same sentence wins over a trailing "sources".
    """
    if aliases is None:
        aliases = load_origin_aliases()
    occurrences, hits = _cue_occurrences(text, claim, aliases)
    best = ""
    best_key: tuple | None = None
    for index, occ in enumerate(occurrences):
        resolved = occ.get("resolved") or occ["origin"]
        if _is_anonymous_origin(resolved, aliases):
            continue
        if not _is_valid_source_name(occ["origin"], resolved, aliases):
            continue
        # A subject cue ("Epic Games claims ...") outranks a same-sentence lead
        # cue ("according to ..."), so the sentence's own subject owns its verb.
        kind_rank = 1 if occ["kind"] == "lead" else 0
        key = (_cue_distance(occ, hits), kind_rank, index)
        if best_key is None or key < best_key:
            best_key = key
            best = occ["origin"]
    return best


def _attribution_trace_entry(
    claim: dict, token: str, text: str, aliases: dict[str, str]
) -> dict:
    """A compact record of why a claim's origin could not be attributed.

    It lists the valid source names the claim's own window offered and the cue
    the winner rule settled on, so a downgrade can be diagnosed from the sheet
    without re-reading the article.
    """
    occurrences, _ = _cue_occurrences(text, claim, aliases)
    cue_sources: list[str] = []
    for occurrence in occurrences:
        resolved = occurrence.get("resolved") or occurrence["origin"]
        if not _is_valid_source_name(occurrence["origin"], resolved, aliases):
            continue
        label = str(resolved) if str(resolved) == str(occurrence["origin"]) else (
            f"{occurrence['origin']} -> {resolved}"
        )
        if label not in cue_sources:
            cue_sources.append(label)
    return {
        "claim": str(claim.get("claim", ""))[:200],
        "origin": str(token),
        "cue_sources": cue_sources,
        "winner": _attribution_winner(text, claim, aliases),
        "rule": "cue does not attribute the claim to this origin",
    }


def _speech_subjects(text: str, claim: dict) -> list[str]:
    """Origins that are the subject of a speech verb in the claim's window."""
    occurrences, _ = _cue_occurrences(text, claim)
    return [occurrence["origin"] for occurrence in occurrences if occurrence["kind"] == "speech"]


def _announcement_subjects(text: str, claim: dict) -> list[str]:
    """Origins that are the subject of an announcement verb in the claim's window.

    "Epic Games announced ...", "Nintendo confirmed ...": the subject puts the
    news out, which is what makes it an organization (and a first party).
    """
    occurrences, _ = _cue_occurrences(text, claim)
    return [
        occurrence["origin"] for occurrence in occurrences if occurrence["kind"] == "announcement"
    ]


def _inverted_origins(text: str, claim: dict) -> list[str]:
    """Origins named *after* an announcement verb ("..., confirms Epic Games")."""
    window, _ = _claim_attribution_window(text, claim)
    origins: list[str] = []
    for _pi, _si, sent in window:
        for match in _POST_VERB_CUE_RE.finditer(sent):
            origins.append(match.group("origin").strip())
    return origins


def _origins_overlap(left: set[str], right: set[str]) -> bool:
    """True when two origin-name sets name the same source (containment allowed)."""
    for a in left:
        for b in right:
            if a == b or _contains_name(a, b) or _contains_name(b, a):
                return True
    return False


def _token_matches_any(token: object, candidates: Iterable[object], aliases: dict[str, str]) -> bool:
    """True when ``token`` names any origin in ``candidates``."""
    names = _origin_names(token, aliases)
    if not names:
        return False
    return any(_origins_overlap(names, _origin_names(candidate, aliases)) for candidate in candidates)


def _origin_attributed(origin: object, text: str, claim: dict, aliases: dict[str, str]) -> bool:
    """True when the claim's own window attributes it to ``origin``.

    Accepted forms are the nearest cue ("according to X", "X said/reports/..."),
    a first-person self-introduction ("I'm X") and "X data/figures/...". A bare
    mention of the name never counts, and when a later cue names a different
    source the earlier cue no longer carries forward.
    """
    names = _origin_names(origin, aliases)
    if not names:
        return False
    winner = _attribution_winner(text, claim, aliases)
    if winner and _origins_overlap(names, _origin_names(winner, aliases)):
        return True
    for inverted in _inverted_origins(text, claim):
        if _origins_overlap(names, _origin_names(inverted, aliases)):
            return True
    target = cluster.normalize_title(text)
    for name in names:
        escaped = re.escape(name)
        # A bylined first-person post attributes itself: "I'm Takeshi Yamanaka".
        if re.search(rf"\b(?:i m|i am|my name is)\s+{escaped}\b", target):
            return True
        if re.search(rf"\b{escaped}\s+(?:data|figures|numbers|estimates)\b", target):
            return True
    return False


def _is_product_origin(value: object, entities: list[str] | None) -> bool:
    """True when ``value`` is, or contains, a canonical entity of the story.

    Canonical entities are what a story is about -- game titles, characters,
    platforms -- so they name the subject, not a speaker. An origin equal to or
    containing one ("the Hollow Knight: Silksong game") is not an origin and is
    downgraded. The check is one-way: it does not reject a name that is merely
    part of an entity ("Nintendo" against the entity "Nintendo Switch 2"), and a
    company that makes the product is kept when the byline or affiliation names
    it (the caller passes that exception in).
    """
    key = _origin_key(value)
    if not key:
        return False
    for entity in entities or []:
        entity_key = _origin_key(entity)
        if entity_key and (key == entity_key or _contains_name(key, entity_key)):
            return True
    return False


#: Corporate suffixes: a name whose last word is one of these is an organization
#: even when it is not in ``data/orgs.json`` ("Some New Studio", "Foo Games").
_CORPORATE_SUFFIXES = frozenset({
    "games", "studios", "studio", "interactive", "entertainment", "software",
    "labs", "inc", "inc.", "ltd", "ltd.", "corp", "corp.", "corporation",
    "publishing",
})


def _matches_organization(value: object, organizations: frozenset[str]) -> bool:
    """True when ``value`` names an organization in the allowlist (containment allowed)."""
    key = _origin_key(value)
    if not key:
        return False
    for org in organizations:
        if key == org or _contains_name(key, org) or _contains_name(org, key):
            return True
    return False


def _has_corporate_suffix(value: object) -> bool:
    """True when the last word of ``value`` is a corporate suffix ("... Studios")."""
    words = _origin_key(value).split()
    if not words:
        return False
    return words[-1] in _CORPORATE_SUFFIXES


def _is_organization(
    value: object,
    text: str = "",
    claim: dict | None = None,
    aliases: dict[str, str] | None = None,
    entities: list[str] | None = None,
    organizations: frozenset[str] | None = None,
) -> bool:
    """True when ``value`` names an organization, not a product or title.

    An organization is a name in the allowlist, a name with a corporate suffix,
    or the grammatical subject of an announcement verb. When ``entities`` are
    given, a name that is a canonical product/title entity is not an
    organization -- unless the allowlist or suffix marks it as one -- so a game
    title the extractor treats as a speaker is still rejected.
    """
    if not _origin_key(value):
        return False
    orgs = load_organizations() if organizations is None else organizations
    if _matches_organization(value, orgs) or _has_corporate_suffix(value):
        return True
    if entities is not None and _is_product_origin(value, entities):
        return False
    if text and claim is not None:
        subjects = _announcement_subjects(text, claim)
        if subjects and _token_matches_any(
            value, subjects, aliases if aliases is not None else load_origin_aliases()
        ):
            return True
    return False


def _is_first_party_origin(
    origin: object,
    text: str,
    claim: dict,
    aliases: dict[str, str],
    entities: list[str] | None,
    organizations: frozenset[str],
) -> bool:
    """True when the source attributes the claim to an organization as its owner.

    First party means the organization itself put the news out: the source names
    it as the subject of an announcement verb ("Epic Games announced ...") and
    attributes the claim to it ("according to Epic Games").
    """
    if not _is_organization(origin, text, claim, aliases, entities, organizations):
        return False
    return _origin_attributed(origin, text, claim, aliases)


# --- evidence: a claim's quote and attribution must exist in its source ------

#: Curly quotes, en/em dashes and the ellipsis character, mapped to their plain
#: ASCII forms so a quote copied from a rendered page still matches the source
#: text after both sides are normalized.
_EVIDENCE_TRANSLATION = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2032": "'", "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-", "\u2026": "...",
})
_EVIDENCE_MARKDOWN_RE = re.compile(r"[*_`#>]+")


def _normalize_evidence(text: object) -> str:
    """A comparable form of ``text`` for verbatim quote matching.

    Lowercased, with curly quotes/dashes/ellipses straightened, markdown
    emphasis markers stripped and runs of whitespace collapsed to one space, so
    the same words match whether they came from a rendered page or plain text.
    """
    value = str(text or "").translate(_EVIDENCE_TRANSLATION)
    value = _EVIDENCE_MARKDOWN_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


_ABSENCE_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _boundary_pattern(phrase: str) -> re.Pattern[str]:
    """A regex matching ``phrase`` (already lowercased) on word boundaries."""
    return re.compile(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])")


def is_absence_claim(claim: object) -> bool:
    """True when a claim's label or value states the subject has NOT disclosed.

    An absence marker (``hasn't``, ``not yet``, ``no official``) followed within
    ``facts.absence_max_gap_words`` words by a disclosure word (``revealed``,
    ``confirmed``, ``info``) marks the claim. Such a claim is the outlet's own
    report of an absence, never a statement by the subject, so the origin, the
    speaker and the first-party/origin rules must not credit it.
    """
    if not isinstance(claim, dict):
        return False
    text = _normalize_evidence(f"{claim.get('claim', '')} {claim.get('value', '')}")
    if not text:
        return False
    markers, disclosures, gap = absence_config()
    disclosure_set = {
        folded for word in disclosures if (folded := _normalize_evidence(word))
    }
    if not disclosure_set:
        return False
    for marker in markers:
        folded = _normalize_evidence(marker)
        if not folded:
            continue
        for match in _boundary_pattern(folded).finditer(text):
            tokens = _ABSENCE_TOKEN_RE.findall(text[match.end():])[: gap + 1]
            if any(token in disclosure_set for token in tokens):
                return True
    return False


def _normalize_absence_claim(claim: dict) -> dict:
    """Force an absence claim to ``reported``, origin ``unstated``, no speaker.

    Keeps ``rumor`` when the claim already is one. Adds an ``absence`` marker so
    downstream count rules can skip it without re-detecting it.
    """
    if not is_absence_claim(claim):
        return claim
    kind = str(claim.get("kind") or "")
    return {
        **claim,
        "kind": "rumor" if kind == "rumor" else "reported",
        "origin": UNSTATED_ORIGIN,
        "affiliation": "",
        "speaker": "",
        "speaker_type": "none",
        "absence": True,
    }


def apply_absence_rule(claims: list[dict]) -> list[dict]:
    """Return ``claims`` with the absence rule applied to each (claim post-processing)."""
    return [_normalize_absence_claim(claim) for claim in claims]


def _attribution_cue_pattern() -> re.Pattern[str] | None:
    """The boundary pattern matching any of ``facts.attribution_cues``."""
    cues = [_normalize_evidence(cue) for cue in attribution_cues()]
    cues = [cue for cue in cues if cue]
    if not cues:
        return None
    return re.compile(
        r"(?<![a-z0-9])(?:" + "|".join(re.escape(cue) for cue in cues) + r")(?![a-z0-9])"
    )


def _attribution_cue_match(text: str) -> re.Match[str] | None:
    """The leftmost attribution cue occurring in ``text`` (already normalized)."""
    if not text:
        return None
    pattern = _attribution_cue_pattern()
    return pattern.search(text) if pattern is not None else None


def _attribution_has_cue(attribution: str) -> bool:
    """True when an attribution quote carries one of ``facts.attribution_cues``."""
    return _attribution_cue_match(attribution) is not None


#: Negations that turn an attribution into a non-statement. An attribution
#: quote is affirmative only when none of these sits between its start and its
#: first cue: "Activision hasn't revealed" is the outlet's own wording, never
#: an Activision statement. The absence rule covers the same words in a
#: claim's label and value; this covers them in the attribution quote.
_ATTRIBUTION_NEGATIONS = (
    "hasn't", "has not", "have not", "haven't", "didn't", "did not",
    "not yet", "never", "without",
)


def _attribution_is_negated(attribution: str) -> bool:
    """True when a negation sits between the attribution start and its first cue."""
    if not attribution:
        return False
    cue = _attribution_cue_match(attribution)
    if cue is None:
        return False
    prefix = attribution[: cue.start()]
    return any(_contains_name(prefix, neg) for neg in _ATTRIBUTION_NEGATIONS)


def _attribution_names_speaker(attribution: str, names: set[str]) -> bool:
    """True when the attribution names its speaker or corefers back to it.

    The speaker's own name (or an alias) must occur inside the attribution
    quote; a coreference phrase ("the company says") qualifies instead, and
    the grounding rule then decides whether its referent is findable in the
    headline or the sentences before it.
    """
    if not attribution:
        return False
    if any(_contains_name(attribution, name) for name in names):
        return True
    cue = _attribution_cue_match(attribution)
    subject = attribution[: cue.start()] if cue is not None else attribution
    return _is_coreference_phrase(subject.strip(" \t.,;:!?"))


def _attribution_from_quote(quote: str, names: set[str]) -> str:
    """The window of ``quote`` naming the speaker and carrying its first cue.

    Used when the model paraphrased the cue ("has confirmed that" where the
    source says "has announced that"): the shortest substring of the found
    quote that contains the speaker's name and an attribution cue becomes the
    attribution quote. Empty when the quote names no speaker, carries no cue,
    or the window is negated -- a negated phrase never credits a speaker.
    """
    if not quote or not names:
        return ""
    pattern = _attribution_cue_pattern()
    if pattern is None:
        return ""
    name_match = None
    for name in names:
        match = re.search(rf"\b{re.escape(name)}\b", quote)
        if match and (name_match is None or match.start() < name_match.start()):
            name_match = match
    if name_match is None:
        return ""
    cues = list(pattern.finditer(quote))
    after = [match for match in cues if match.start() >= name_match.start()]
    before = [match for match in cues if match.end() <= name_match.start()]
    if after:
        cue_match = after[0]
    elif before:
        cue_match = before[-1]
    else:
        return ""
    window = quote[min(name_match.start(), cue_match.start()): max(name_match.end(), cue_match.end())]
    if _attribution_is_negated(window):
        return ""
    return window


def _evidence_speaker_names(speaker: object, aliases: dict[str, str]) -> set[str]:
    """Every match key that identifies a claim's speaker (own form plus aliases)."""
    names = set(_origin_names(speaker, aliases))
    own = _origin_key(speaker)
    if own:
        names.add(own)
    return names


def _evidence_speaker_grounded(
    source_text: str,
    headline: str,
    attribution: str,
    names: set[str],
) -> bool:
    """True when the speaker is named by its own attribution or just before it.

    Grounded when the speaker's name appears in the attribution quote itself, or
    -- for a pronoun/common-noun attribution ("the company says") -- in the
    headline or in the same paragraph within three sentences before the sentence
    that carries the attribution quote.
    """
    if not names:
        return False
    if attribution and any(_contains_name(attribution, name) for name in names):
        return True
    if headline and any(_contains_name(headline, name) for name in names):
        return True
    if not attribution:
        return False
    normalized = {
        (pi, si): _normalize_evidence(sent)
        for pi, si, sent in _sentences_with_positions(source_text)
    }
    for pi, si, _sent in _sentences_with_positions(source_text):
        if attribution not in _normalize_evidence(_sent):
            continue
        window = [
            text for (p, s), text in normalized.items() if p == pi and si - 3 <= s <= si - 1
        ]
        return any(any(_contains_name(text, name) for name in names) for text in window)
    return False


#: Announcement verbs whose grammatical subject in a headline is the
#: organization that put the news out ("Acme Games announces the Acme X1").
#: Used by the ``headline_announcement`` attribution mode; both the present and
#: past forms are listed so a headline reads the same either way.
_HEADLINE_ANNOUNCEMENT_VERBS = (
    "announces", "announced", "launches", "launched", "unveils", "unveiled",
    "reveals", "revealed", "introduces", "introduced", "confirms", "confirmed",
    "releases", "released", "opens", "opened", "adds", "added",
)


def _has_press_release_marker(source_title: object, source_text: object) -> bool:
    """True when the title or text carries a configured press-release marker."""
    haystack = f"{_normalize_evidence(source_title)} {_normalize_evidence(source_text)}"
    return any(
        _normalize_evidence(marker) in haystack
        for marker in press_release_markers()
        if _normalize_evidence(marker)
    )


def _headline_announces_speaker(headline: str, names: set[str]) -> bool:
    """True when a speaker name is the subject of an announcement verb.

    ``headline`` is already normalized (lowercased); a name qualifies when it
    sits directly before an announcement verb, allowing up to two lowercase
    adverbs between them ("Acme Games officially announces ...").
    """
    if not headline or not names:
        return False
    verb_re = "|".join(sorted(set(_HEADLINE_ANNOUNCEMENT_VERBS), key=len, reverse=True))
    for name in names:
        if re.search(
            rf"\b{re.escape(name)}\b(?:\s+[a-z]+){{0,2}}\s+(?:{verb_re})\b",
            headline,
        ):
            return True
    return False


def _source_owner_names(
    source_owner: object, source_name: object, aliases: dict[str, str]
) -> set[str]:
    """Match keys for the organization(s) that own the source site.

    The owner slug ("microsoft"), the source's own name ("Xbox Wire") and any
    known organization (:func:`load_organizations`) contained in either are all
    collected under :func:`_origin_key`, plus their aliases.
    """
    keys: set[str] = set()
    organizations = load_organizations()
    for value in (source_owner, source_name):
        if value is None:
            continue
        key = _origin_key(value)
        if not key:
            continue
        keys.add(key)
        keys |= _origin_names(value, aliases)
        keys.update(org for org in organizations if _contains_name(key, org))
    return {name for name in keys if name}


def _evidence_attribution_mode(
    *,
    attribution_found: bool,
    source_tier: object,
    owner_names: set[str],
    names: set[str],
    headline: str,
    source_title: object,
    source_text: object,
) -> str:
    """The way a claim's speaker is credited, one of the four attribution modes.

    * ``explicit`` -- the claim's attribution quote is present and found.
    * ``first_party_site`` -- a tier-1 source whose speaker is the organization
      that owns the site; its own post speaks for itself.
    * ``headline_announcement`` -- the speaker is an organization named as the
      subject of an announcement verb in the headline, and the source is tier-1
      or carries a press-release marker.
    * ``none`` -- nothing credits the speaker.
    """
    if attribution_found:
        return "explicit"
    try:
        tier = int(source_tier) if source_tier is not None else 0
    except (TypeError, ValueError):
        tier = 0
    if tier == 1 and names and names & owner_names:
        return "first_party_site"
    if names and _headline_announces_speaker(headline, names):
        if tier == 1 or _has_press_release_marker(source_title, source_text):
            return "headline_announcement"
    return "none"


def verify_evidence(
    claim: dict,
    source_text: object,
    source_title: object = "",
    source_outlet: object = "",
    config: dict | None = None,
    *,
    aliases: dict[str, str] | None = None,
    entities: list[str] | None = None,
    source_tier: object = None,
    source_owner: object = None,
) -> dict[str, Any]:
    """Check one claim's stored quote and attribution against its own source.

    Pure for a fixed set of inputs: it reads nothing but its arguments (aliases,
    entities and the anonymous vocabulary override ``config``/defaults). Every
    field is answered independently so a sheet can show exactly which rule a
    claim failed:

    * ``quote_found`` / ``attribution_found`` -- the claim's ``quote`` and
      ``attribution_quote`` occur verbatim in the source text or, for the
      attribution, the source title (after the normalization in
      :func:`_normalize_evidence`). An attribution additionally must carry an
      attribution cue, name its speaker (or corefer back to it) and be
      affirmative; when it is not found verbatim, a window of the found quote
      that names the speaker and carries a cue repairs it and sets
      ``repaired_attribution``.
    * ``attribution_mode`` -- how the speaker is credited: ``explicit`` (the
      attribution quote is found), ``first_party_site`` (a tier-1 source whose
      speaker is the organization that owns the site), ``headline_announcement``
      (the speaker is the subject of an announcement verb in the headline and
      the source is tier-1 or carries a press-release marker) or ``none``.
    * ``speaker_grounded`` -- the speaker is named by the attribution quote, or
      (for a pronoun/noun attribution) by the headline or a sentence within
      three before it in the same paragraph. A first-party own-site or headline
      announcement grounds its speaker by naming it in the title.
    * ``anonymous`` -- the speaker is an unnamed descriptor ("people familiar
      with the matter"), using the same closed vocabulary as origin checking.
    * ``self_reference`` -- the speaker is the publishing outlet itself; never
      set in ``first_party_site`` mode, where the speaker owns the site.
    * ``product`` -- the speaker names a product, title or piece of software and
      is not labeled an organization or person.

    ``ok`` is true only when the quote is found, the speaker is grounded and not
    anonymous, self-referential or a product, and the speaker is credited either
    by an attribution quote or by one of the first-party/headline modes.
    ``reason`` is the first failed rule, or ``""`` when ``ok`` is true.
    """
    if aliases is None:
        aliases = load_origin_aliases()
    if entities is None:
        entities = []

    text = _normalize_evidence(source_text)
    headline = _normalize_evidence(source_title)
    quote = _normalize_evidence(claim.get("quote"))
    attribution = _normalize_evidence(claim.get("attribution_quote"))
    speaker = str(claim.get("speaker") or "").strip()
    speaker_type = _normalize_speaker_type(claim.get("speaker_type"))

    names = _evidence_speaker_names(speaker, aliases) if speaker else set()
    absence = is_absence_claim(claim)

    quote_found = bool(quote) and quote in text
    attribution_found = bool(attribution) and (attribution in text or attribution in headline)
    # Paraphrase repair: when the model's attribution quote is not in the
    # source but the found quote itself names the speaker and carries a cue
    # ("Rebel Wolves has announced that ..."), that substring of the quote
    # becomes the attribution and the entry is flagged repaired. A negated
    # attribution or a quote without a cue is never repaired.
    repaired_attribution = False
    if attribution and not attribution_found and quote_found and not absence:
        repaired = _attribution_from_quote(quote, names)
        if repaired:
            attribution = repaired
            attribution_found = True
            repaired_attribution = True
    # Attribution hardening: a quote only credits a speaker when it carries an
    # attribution cue, names that speaker (or corefers back to it) and is
    # affirmative: no negation between its start and its first cue. An
    # attribution that sits inside the claim's own sentence is accepted.
    # "Activision hasn't revealed" is the outlet's own wording, not a speaker.
    # A first-party own-site post or a headline announcement credits its speaker
    # through its mode, so it never needs the cue.
    if attribution_found and not _attribution_has_cue(attribution):
        attribution_found = False
    if attribution_found and not _attribution_names_speaker(attribution, names):
        attribution_found = False
    if attribution_found and _attribution_is_negated(attribution):
        attribution_found = False

    if absence:
        attribution_found = False

    owner_names = _source_owner_names(source_owner, source_outlet, aliases)
    mode = _evidence_attribution_mode(
        attribution_found=attribution_found,
        source_tier=source_tier,
        owner_names=owner_names,
        names=names,
        headline=headline,
        source_title=source_title,
        source_text=source_text,
    )
    # In headline_announcement mode the speaker is named in the title; in
    # first_party_site mode the speaker is the site's own owner, so the source
    # speaks for itself and a name need not appear in the body.
    anonymous = bool(speaker) and _is_anonymous_origin(speaker, aliases)
    self_reference = (
        bool(speaker)
        and mode != "first_party_site"
        and _is_publication(speaker, {"source_name": source_outlet})
    )
    product = (
        bool(speaker)
        and speaker_type not in ("organization", "person")
        and (
            _is_document_or_product(_origin_key(speaker))
            or _is_product_origin(speaker, entities)
        )
    )
    if mode == "first_party_site":
        speaker_grounded = bool(names)
    else:
        speaker_grounded = bool(names) and _evidence_speaker_grounded(
            str(source_text or ""), headline, attribution, names
        )

    no_attr_needed = mode in ("first_party_site", "headline_announcement")
    if absence:
        reason = "absence claim"
    elif not quote:
        reason = "quote missing"
    elif not quote_found:
        reason = "quote not found"
    elif anonymous:
        reason = "speaker is anonymous"
    elif self_reference:
        reason = "source cites itself"
    elif product:
        reason = "speaker is a product"
    elif not no_attr_needed and not attribution:
        reason = "attribution quote missing"
    elif not no_attr_needed and not attribution_found:
        reason = "attribution quote not found"
    elif not speaker_grounded:
        reason = "speaker not grounded"
    else:
        reason = ""

    ok = (
        quote_found
        and speaker_grounded
        and (attribution_found or no_attr_needed)
        and not absence
        and not anonymous
        and not self_reference
        and not product
    )
    return {
        "quote_found": quote_found,
        "attribution_found": attribution_found,
        "repaired_attribution": repaired_attribution,
        "attribution_mode": mode,
        "speaker_grounded": speaker_grounded,
        "anonymous": anonymous,
        "self_reference": self_reference,
        "product": product,
        "ok": ok,
        "reason": reason,
    }


def _min_first_party_sources() -> int:
    """How many independent sources the first-party rule requires (configurable)."""
    value = load_config().get("facts", {}).get("min_first_party_sources")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_MIN_FIRST_PARTY_SOURCES
    return max(1, int(value))


def _confirmation_mode() -> str:
    """The configured confirmation mode, defaulting to ``shadow`` when unknown."""
    value = str(
        (load_config().get("facts") or {}).get(
            "confirmation_mode", DEFAULT_CONFIRMATION_MODE
        )
    ).strip().lower()
    return value if value in CONFIRMATION_MODES else DEFAULT_CONFIRMATION_MODE


def _first_party_origins(
    claims: list[dict],
    by_url: dict[str, dict],
    aliases: dict[str, str],
    entities: list[str] | None,
    *,
    corroborating: list[dict] | None = None,
) -> tuple[list[str], dict[str, str], set[str]]:
    """First-party organizations among ``claims``, with their source owners.

    Returns ``(organizations, source_org, owners)``: the canonical organization
    names seen in :data:`FIRST_PARTY_KINDS` claims, the organization each source
    attributed its claim to, and the set of distinct owners backing them. A
    source must have its own text carrying the attribution, and an outlet that
    is merely reporting about someone else never counts. ``corroborating`` names
    other sources' claims that corroborate the central fact; they count as those
    sources stating the central fact, using their own origin and kind.
    """
    organizations = load_organizations()
    orgs_seen: list[str] = []
    source_org: dict[str, str] = {}
    owners: set[str] = set()
    for claim in [*(claims or []), *(corroborating or [])]:
        if claim.get("absence") or is_absence_claim(claim):
            continue
        if claim.get("kind") not in FIRST_PARTY_KINDS:
            continue
        url_key = _source_url_key(claim.get("source_url"))
        source = by_url.get(url_key) or {}
        text = str(source.get("text") or "")
        if not text:
            continue
        for token in str(claim.get("origin") or "").split(";"):
            token = token.strip()
            if not token or _origin_key(token) == UNSTATED_ORIGIN:
                continue
            if _is_publication(token, source):
                continue
            if not _is_first_party_origin(token, text, claim, aliases, entities, organizations):
                continue
            canonical = canonicalize_origin(token, aliases)
            if canonical == UNSTATED_ORIGIN:
                continue
            if canonical not in orgs_seen:
                orgs_seen.append(canonical)
            source_org[url_key] = canonical
            owner = _origin_key(source.get("owner")) or url_key
            owners.add(owner)
    return sorted(orgs_seen), source_org, owners


def _source_url_key(url: object) -> str:
    """Match key for a source URL: scheme/host lowercased, ``www.`` and fragment
    dropped, trailing slash ignored.

    A claim's ``source_url`` must find the gathered source it was extracted from
    even when the two differ only in casing, a trailing slash or a fragment;
    otherwise the origin is verified against no text and wrongly downgraded.
    """
    return normalize_url(str(url or "")).rstrip("/")


def _sources_by_url(sources: list[dict]) -> dict[str, dict]:
    """Gathered sources keyed by :func:`_source_url_key` for claim lookup."""
    by_url: dict[str, dict] = {}
    for source in sources:
        key = _source_url_key(source.get("link"))
        if key:
            by_url[key] = source
    return by_url


def _select_sources(sources: list[dict], max_sources: int | None) -> list[dict]:
    """The sources to extract from, capped and reduced to distinct owners.

    A candidate with many sources is capped so one story cannot spend the whole
    model-call budget. Sources are ranked by tier (tier-1 first), a single best
    source per distinct owner is taken, and any remaining slots go to the
    longest texts. The chosen sources are returned in their original order, so
    extraction and every downstream positional assumption stay stable.
    """
    if max_sources is None or max_sources <= 0 or len(sources) <= max_sources:
        return list(sources)

    def _tier(source: dict) -> int:
        try:
            return int(source.get("tier") or 99)
        except (TypeError, ValueError):
            return 99

    def _rank(pair: tuple[int, dict]) -> tuple[int, int, int]:
        index, source = pair
        return (_tier(source), -len(str(source.get("text") or "")), index)

    ordered = sorted(enumerate(sources), key=_rank)
    chosen: list[int] = []
    seen_owners: set[str] = set()
    for index, source in ordered:
        owner = _origin_key(source.get("owner")) or _source_url_key(source.get("link"))
        if owner in seen_owners:
            continue
        seen_owners.add(owner)
        chosen.append(index)
        if len(chosen) >= max_sources:
            break
    if len(chosen) < max_sources:
        for index, _source in ordered:
            if index in chosen:
                continue
            chosen.append(index)
            if len(chosen) >= max_sources:
                break
    return [sources[index] for index in sorted(chosen)]


def resolve_claim_origins(
    claims: list[dict],
    sources: list[dict],
    *,
    aliases: dict[str, str] | None = None,
    story_id: str = "",
    entities: list[str] | None = None,
    downgrades: list[dict] | None = None,
    attribution_traces: list[dict] | None = None,
) -> list[dict]:
    """Canonicalize each claim's origin and drop any its source never names.

    Each ``;``-separated origin token is canonicalized, then checked against the
    text of the claim's own source. A token the text does not mention is removed
    and the downgrade is logged; a token the text mentions but does not attribute
    to the claim (its sentence carries no "according to X" / "X said" link) is
    removed too, so a speaker named only elsewhere in a long article never
    vouches for this claim. When no token survives, the origin becomes
    :data:`UNSTATED_ORIGIN`. A token that is a canonical entity of the story
    (``entities``: a game, character or platform) is a subject, not a speaker,
    and is downgraded the same way -- unless the byline or the claim's
    affiliation names it, which marks it as the company that makes the product.
    The claim's ``affiliation`` (the organization a person speaks for) is
    canonicalized the same way and dropped when its own source text never names
    it. No other claim field is touched.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    by_url = _sources_by_url(sources)
    resolved: list[dict] = []
    # Per-source tallies of why origins were dropped, so the run logs one summary
    # line per source instead of one warning per token. The individual verdicts
    # are still collected (``downgrades``) for the sheet, so nothing is lost.
    per_source: dict[str, dict] = {}
    for claim in claims:
        if is_absence_claim(claim):
            # An absence claim is the outlet's own report of what a subject has
            # not said: it never carries that subject's origin or speaker, no
            # matter what the extractor put in the fields.
            resolved.append(_normalize_absence_claim(claim))
            continue
        source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
        text = str(source.get("text") or "")
        claim_url = claim.get("source_url")
        source_key = _source_url_key(claim_url)
        byline_name, byline_org = _extract_byline(text)
        byline_org_key = _origin_key(byline_org)
        speaker_org_keys = {
            key
            for key in (byline_org_key, _origin_key(claim.get("affiliation")))
            if key
        }
        speech_subjects = _speech_subjects(text, claim)
        kept: list[str] = []

        def _record(token: str, reason: str) -> None:
            tally = per_source.setdefault(source_key, {"url": claim_url, "reasons": Counter()})
            tally["reasons"][reason] += 1
            if downgrades is not None:
                downgrades.append({"source_url": claim_url, "origin": token, "reason": reason})

        for token in str(claim.get("origin") or "").split(";"):
            token = token.strip()
            token_key = _origin_key(token)
            if not token_key or token_key == UNSTATED_ORIGIN:
                continue
            # A person who is the subject of a speech verb ("Dan Houser says…")
            # stays a speaker even when their name is also a headline entity;
            # an announcement verb never rescues a game, character or platform.
            speech_person = _token_matches_any(token, speech_subjects, aliases)
            reason = ""
            if _is_anonymous_origin(token, aliases):
                reason = "anonymous source"
            elif (
                _is_product_origin(token, entities)
                and token_key not in speaker_org_keys
                and not speech_person
                and not _is_organization(token, text, claim, aliases, entities)
            ):
                reason = "product or title"
            elif not _origin_in_text(token, text, aliases):
                reason = "not named in the source text"
            elif not _origin_attributed(token, text, claim, aliases):
                # The name appears in the article but not as the producer of
                # this claim (e.g. it speaks about a different fact). Crediting
                # it would conflate two statements, so it is downgraded.
                reason = "not attributed"
            if reason:
                # An outlet naming itself (usually the extractor mistaking the
                # publisher for the speaker) is a self-reference, not a genuine
                # attribution gap, so it is dropped quietly.
                if _is_publication(token, source):
                    reason = "outlet self-reference"
                elif reason == "not attributed" and attribution_traces is not None:
                    tally = per_source.setdefault(
                        source_key, {"url": claim_url, "reasons": Counter()}
                    )
                    traced = int(tally.get("traced") or 0)
                    if traced < 20:
                        attribution_traces.append(
                            _attribution_trace_entry(claim, token, text, aliases)
                        )
                        tally["traced"] = traced + 1
                _record(token, reason)
                continue
            canonical = canonicalize_origin(token, aliases)
            if canonical not in kept:
                kept.append(canonical)
        origin = "; ".join(kept) if kept else UNSTATED_ORIGIN
        # A company's own bylined post speaks for itself: a tier-1 own-site post
        # with a byline self-attributes ("I'm <Name>, <role> at <Company>"), so
        # its unattributed claims are not left unstated. An outlet that merely
        # uses "we"/"I" never self-attributes through this path.
        if origin == UNSTATED_ORIGIN:
            self_origin = _self_attribution(text, source, byline_name, byline_org, aliases)
            if self_origin and self_origin != UNSTATED_ORIGIN:
                origin = self_origin
        affiliation = _normalize_affiliation(claim.get("affiliation"))
        if affiliation and _is_publication(affiliation, source):
            # The extractor named the publishing outlet, not the speaker's
            # organization; the byline's own organization is the real one.
            affiliation = byline_org
        if (
            affiliation
            and _is_product_origin(affiliation, entities)
            and _origin_key(affiliation) != byline_org_key
            and not _is_organization(affiliation, text, claim, aliases, entities)
        ):
            # A game title can never be the organization a person speaks for.
            affiliation = ""
        if affiliation and _origin_in_text(affiliation, text, aliases):
            canonical_affiliation = canonicalize_origin(affiliation, aliases)
            if canonical_affiliation == UNSTATED_ORIGIN:
                canonical_affiliation = ""
        else:
            canonical_affiliation = ""
        resolved.append(
            {
                **claim,
                "origin": origin,
                "affiliation": canonical_affiliation,
            }
        )
    # One summary line per source. A source whose only drops are its own
    # self-references is logged at INFO; a genuine attribution gap (unattributed
    # or unnamed) stays a WARNING so it is not missed.
    for tally in per_source.values():
        reasons = tally["reasons"]
        quiet = all(reason == "outlet self-reference" for reason in reasons)
        parts = ", ".join(
            f"{count} {reason}" for reason, count in sorted(reasons.items())
        )
        logger.log(
            logging.INFO if quiet else logging.WARNING,
            "story %s: %d origin(s) downgraded for %s (%s)",
            story_id or "?",
            sum(reasons.values()),
            tally["url"],
            parts,
        )
    return resolved


# --- merging duplicate claims across sources ---------------------------------


def _claim_key(claim: dict) -> str:
    """Normalized claim text: the key that identifies one fact across sources.

    Sources word the same fact differently, but the extractor is told to state
    the same claim the same way; two claims whose normalized text matches are
    the same fact and must be merged into one.
    """
    text = re.sub(r"[\W_]+", " ", str(claim.get("claim", "")).lower()).strip()
    return re.sub(r"\s+", " ", text)


_MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})

_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "does",
    "for", "from", "in", "into", "is", "it", "its", "not", "of", "on", "or",
    "says", "said", "say", "that", "the", "this", "to", "was", "were", "will",
    "with",
})


def _word_tokens(text: str) -> set[str]:
    """Lowercased alphanumeric word tokens of ``text``."""
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _stem(word: str) -> str:
    """Very light suffix stemmer: testing/test/modes/mode all count together."""
    for suffix, length in (("ing", 3), ("ed", 2), ("es", 2), ("s", 1)):
        if len(word) > length and word.endswith(suffix):
            return word[:-length] or word
    return word


def _topic_words(text: str) -> set[str]:
    """Stemmed content words of a claim, excluding stop words, months and numbers."""
    words: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if token in _STOP_WORDS or token in _MONTHS or token.isdigit():
            continue
        if re.match(r"^\d", token):
            continue
        words.add(_stem(token))
    return words


def _value_key_tokens(value: str) -> set[str]:
    """Key tokens in a value: month names and numbers, ordinals stripped.

    "13th to 20th October" yields {13, 20, october}; "October 13" yields
    {october, 13}, so the two values share a figure and can merge, while
    "October 12" shares only the month with "October 13" and stays separate.
    """
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
        if token in _MONTHS:
            tokens.add(token)
            continue
        digits = re.match(r"^\d+", token)
        if digits:
            tokens.add(digits.group(0))
    return tokens


def _same_value_fact(a: dict, b: dict) -> bool:
    """True when two claims share a concrete date or figure and topic words.

    The values must share at least one number, or two key tokens (so "October
    12" and "October 13" never merge on the month alone), and the claims must
    share topic words, so two unrelated claims quoting the same date stay apart.
    """
    a_keys = _value_key_tokens(str(a.get("value") or ""))
    b_keys = _value_key_tokens(str(b.get("value") or ""))
    shared = a_keys & b_keys
    if not shared:
        return False
    if sum(1 for token in shared if token.isdigit()) < 1 and len(shared) < 2:
        return False
    return bool(_topic_words(str(a.get("claim") or "")) & _topic_words(str(b.get("claim") or "")))


def _value_figures(value: object) -> set[str]:
    """Digit figures in a value (thousands separators already dropped)."""
    return _value_key_tokens(str(value or "")) - _MONTHS


def _label_figures(claim: dict) -> set[str]:
    """Digit figures a claim's label (claim text) states."""
    return _value_key_tokens(str(claim.get("claim") or "")) - _MONTHS


def _year_like(token: str) -> bool:
    """True for a four-digit calendar-year figure (1000-2999)."""
    return len(token) == 4 and token.isdigit() and 1000 <= int(token) <= 2999


def _claim_text_hard_tokens(claim: dict) -> tuple[set[str], set[str]]:
    """The figures and month names a claim's label (claim text) states."""
    tokens = _value_key_tokens(str(claim.get("claim") or ""))
    return {token for token in tokens if token not in _MONTHS}, tokens & _MONTHS


def _values_hard_conflict(a: dict, b: dict) -> bool:
    """True when two claims' values state contradicting figures or dates.

    The subject's own number (a figure the label also states, such as the "4"
    in "Modern Warfare 4") and four-digit calendar years are not facts the
    values disagree about, so they never force a conflict. A figure or month
    only one side's value states is no conflict either (the other value simply
    states less): two claims conflict only when both values carry a figure
    (or a month) the other does not share, so "October 16" and "October 23"
    are different facts while "13th to 20th October" and "October 13" are not.
    """
    a_label = _label_figures(a)
    b_label = _label_figures(b)
    a_figures = {
        token for token in _value_figures(a.get("value"))
        if token not in a_label and not _year_like(token)
    }
    b_figures = {
        token for token in _value_figures(b.get("value"))
        if token not in b_label and not _year_like(token)
    }
    if a_figures and b_figures and not (a_figures & b_figures):
        return True
    a_months = _value_key_tokens(str(a.get("value") or "")) & _MONTHS
    b_months = _value_key_tokens(str(b.get("value") or "")) & _MONTHS
    if a_months and b_months and not (a_months & b_months):
        return True
    return False


def _labels_differ_in_date_or_number(a: dict, b: dict) -> bool:
    """True when the two labels state different figures or month names.

    A claim's label carries its own fact, so two labels that name a different
    number or month ("...early access on October 16" against "...release on
    October 23") describe different facts even when the values happen to
    overlap. A label with no figure or month states neither, so it never
    differs from the other on its own.
    """
    a_numbers, a_months = _claim_text_hard_tokens(a)
    b_numbers, b_months = _claim_text_hard_tokens(b)
    return bool((a_numbers ^ b_numbers) or (a_months ^ b_months))


def _claim_entities(claim: dict, entities: Optional[Iterable[str]]) -> set[str]:
    """The story's primary entities a claim's text or value names.

    ``entities`` is the story's canonical entity list (the "primary entities"),
    and each one the claim names is returned. The check needs that list: a
    claim's own proper-name phrases are not a reliable primary-entity list for
    claims (a lowercase or sentence-leading subject yields none), so without it
    the entity condition is skipped rather than guessed.
    """
    if not entities:
        return set()
    text = f"{claim.get('claim', '')} {claim.get('value', '')}"
    return {entity for entity in entities if _claim_names_entity(text, entity)}


def _claims_share_entity(
    a: dict, b: dict, entities: Optional[Iterable[str]]
) -> bool:
    """True when two claims share a primary entity (or no list is given).

    A claim that names none of the story's primary entities cannot be shown to
    be about the same subject as another, so it is not blocked here; the other
    merge conditions still apply.
    """
    a_entities = _claim_entities(a, entities)
    b_entities = _claim_entities(b, entities)
    if not a_entities or not b_entities:
        return True
    return bool(a_entities & b_entities)


def _same_fact(
    a: dict, b: dict, *, entities: Optional[Iterable[str]] = None
) -> bool:
    """True when two claims describe one fact and may merge into one.

    They may merge only when every condition holds: (a) they come from
    different sources (two claims from one source never merge -- one article is
    never two versions of its own fact); (b) their values state no contradicting
    figure, date or month; (c) they share a primary entity and their content
    words overlap under the existing measure (same normalized claim text, or a
    shared date/figure plus topic words); and (d) their labels state the same
    figures and months, so a label that names a different date or number is a
    different fact.
    """
    if _source_url_key(a.get("source_url")) == _source_url_key(b.get("source_url")):
        return False
    if _values_hard_conflict(a, b):
        return False
    if _labels_differ_in_date_or_number(a, b):
        return False
    if not _claims_share_entity(a, b, entities):
        return False
    if _claim_key(a) == _claim_key(b):
        return True
    return _same_value_fact(a, b)


def _claim_groups(
    claims: list[dict], *, entities: Optional[Iterable[str]] = None
) -> list[list[dict]]:
    """Group claims that describe one fact.

    Claims group when :func:`_same_fact` holds: different sources, values whose
    figures/dates do not contradict, a shared primary entity with overlapping
    content words, and labels that agree on their figures and months. Each
    group holds at most one claim per source, so a fact's versions always come
    from distinct sources: "The test runs from 13th to 20th October" and
    "October 13" are the same fact, while a different date ("October 12") or a
    second claim from the same source stays its own fact.
    """
    groups: list[list[dict]] = []
    for claim in claims or []:
        claim_source = _source_url_key(claim.get("source_url"))
        for group in groups:
            if any(
                _same_fact(claim, other, entities=entities) for other in group
            ) and all(
                _source_url_key(other.get("source_url")) != claim_source
                for other in group
            ):
                group.append(claim)
                break
        else:
            groups.append([claim])
    return groups


def _hedge_count(value: str) -> int:
    text = str(value or "").lower()
    return sum(1 for word in HEDGE_WORDS if word in text)


def _merge_score(claim: dict) -> tuple[int, int, int]:
    """Pick the value that keeps the story honest: most hedged, then weakest kind.

    A fact whose sources disagree (one wrote "estimated", another wrote a bare
    figure) must keep the hedged value, and the weakest kind, so an estimate is
    never flattened into a confirmation and a plan stated as a possibility is
    never upgraded to a fact.
    """
    return (-_hedge_count(str(claim.get("value", ""))), KIND_STRENGTH.get(claim["kind"], 0), -len(str(claim.get("value", ""))))


def _merge_origins(group: list[dict]) -> str:
    """One or more named origins, or 'unstated' when none is named."""
    named = sorted({claim["origin"] for claim in group if claim["origin"] != UNSTATED_ORIGIN})
    return "; ".join(named) if named else UNSTATED_ORIGIN


def _merge_affiliations(group: list[dict]) -> str:
    """One or more named affiliations, or '' when none is named."""
    named = sorted({
        str(claim.get("affiliation") or "").strip()
        for claim in group
        if str(claim.get("affiliation") or "").strip()
    })
    return "; ".join(named)


def _merge_group(group: list[dict], *, prefer_url: str | None = None) -> dict:
    """Collapse the claims of one fact (same fact) into one.

    ``prefer_url`` names the source whose wording should represent the merged
    fact: the central claim takes the tier-1 source's own value (so the source
    that confirms it is the source that wrote it), while the other sources'
    versions are kept as corroboration.
    """
    candidates = group
    if prefer_url is not None:
        preferred = [
            claim for claim in group
            if _source_url_key(claim.get("source_url")) == prefer_url
        ]
        if preferred:
            candidates = preferred
    best = min(candidates, key=_merge_score)
    return {
        "claim": str(best["claim"]),
        "value": str(best["value"]),
        "source_urls": sorted({claim["source_url"] for claim in group}),
        "origin": _merge_origins(group),
        "affiliation": _merge_affiliations(group),
        "confidence": min(float(claim["confidence"]) for claim in group),
        "kind": min((claim["kind"] for claim in group), key=KIND_STRENGTH.get),
        # Each source's own wording, so confirmation can require that a
        # confirming source actually states the merged value's specifics and a
        # tier-1 source never backs words only another source used.
        "versions": [
            {"source_url": claim["source_url"], "value": str(claim["value"])}
            for claim in group
        ],
        # The evidence verdict of each source behind this fact (quote and
        # attribution checked verbatim, speaker grounded, not anonymous/self/
        # product). Diagnostics only: confirmation does not read it yet.
        "evidence": _merge_evidence(group),
    }


def _value_specifics(value: object) -> set[str]:
    """The concrete bits of a value: its figures and absolute qualifiers."""
    specifics = _numbers_in(value)
    for qualifier in _ABSOLUTE_QUALIFIERS:
        if _word_in(value, qualifier):
            specifics.add(qualifier)
    return specifics


def _version_supports(merged: dict, version_value: object) -> bool:
    """True when a source's own value carries every specific the merged value has.

    A merged fact (for example "full Year 1 DLC roster") must not be confirmed by
    a source whose own value omits a specific word ("Year 1 DLC roster"): the
    tier-1 source would be backing words it never wrote.
    """
    specifics = _value_specifics(merged.get("value"))
    if not specifics:
        return True
    return specifics <= _value_specifics(version_value)


def _source_supports(merged: dict, url_key: str) -> bool:
    """True when one source's own value backs the merged fact.

    A tier-1 source confirms only as long as one of its claims carries every
    specific of the merged value: it never confirms wording that only another
    source stated. When the source stated the fact more than once (for example
    splitting a date into its own claim), any of its versions may support it.
    """
    versions = [
        version for version in merged.get("versions") or []
        if _source_url_key(version.get("source_url")) == url_key
    ]
    return bool(versions) and any(
        _version_supports(merged, version.get("value")) for version in versions
    )


def merge_claims(
    claims: list[dict], *, entities: Optional[Iterable[str]] = None
) -> list[dict]:
    """Merge duplicate claims across sources: one claim per fact.

    A fact groups every claim that describes it (:func:`_claim_groups`) -- from
    different sources, with no contradicting figures or dates, sharing a primary
    entity and overlapping content words, and with labels that agree on their
    dates and numbers -- and is stored once, listing every source URL that
    states it (``source_urls``), taking the weakest ``kind`` when sources
    disagree, keeping the hedge words in the value, joining the named origins,
    and keeping the lowest confidence. ``entities`` is the story's primary
    entity list, used to check that the merged claims share a subject.
    """
    return [_merge_group(group) for group in _claim_groups(claims, entities=entities)]


def _collapse_contained_origins(names: set[str]) -> list[str]:
    """Drop any origin whose match key contains another origin's from the set.

    "alinea analytics ltd" contains "alinea analytics", so only the shorter,
    more general name is kept; two spellings of one origin thus count once. A
    known organization (from ``data/orgs.json``) is never dropped in favour of a
    shorter form that is not itself a known organization, so "embark studios"
    wins over a stray "embark".
    """
    organizations = load_organizations()
    ordered = sorted(names, key=lambda name: (len(name.split()), len(name), name))
    kept: list[str] = []
    for name in ordered:
        key = _origin_key(name)
        replaced = False
        for index, other in enumerate(kept):
            if not _contains_name(key, _origin_key(other)):
                continue
            if key in organizations and _origin_key(other) not in organizations:
                kept[index] = name
            replaced = True
            break
        if not replaced:
            kept.append(name)
    return sorted(kept)


def _collapse_origin_identities(names: set[str]) -> dict[str, str]:
    """Map every canonical name to the single identity it collapses to.

    Applies the same containment rule as :func:`_collapse_contained_origins` --
    two spellings of one name where one is a whole-word part of the other count
    once, and a known organization (``data/orgs.json``) is never dropped in
    favour of a shorter form that is not itself a known organization -- but
    returns the full mapping instead of a list, so a caller can group counts by
    the representative. A name with no counterpart maps to itself.
    """
    organizations = load_organizations()
    ordered = sorted(names, key=lambda name: (len(name.split()), len(name), name))
    kept: list[str] = []
    for name in ordered:
        key = _origin_key(name)
        replaced = False
        for index, other in enumerate(kept):
            if not _contains_name(key, _origin_key(other)):
                continue
            if key in organizations and _origin_key(other) not in organizations:
                kept[index] = name
            replaced = True
            break
        if not replaced:
            kept.append(name)
    identities: dict[str, str] = {}
    for name in names:
        key = _origin_key(name)
        representative = name
        for candidate in kept:
            candidate_key = _origin_key(candidate)
            if (
                candidate_key == key
                or _contains_name(key, candidate_key)
                or _contains_name(candidate_key, key)
            ):
                representative = candidate
                break
        identities[name] = representative
    return identities


def _claim_source_url_key(claim: dict) -> str:
    """Match key for the source that states a claim.

    Kept claims carry a single ``source_url``; merged claims carry
    ``source_urls``. Either form must resolve to the same gathered source.
    """
    urls = claim.get("source_urls")
    if isinstance(urls, list) and urls:
        return _source_url_key(next((url for url in urls if url), None))
    return _source_url_key(claim.get("source_url"))


def _confirmation_origins(
    claims: list[dict],
    by_url: dict[str, dict],
    *,
    aliases: dict[str, str] | None = None,
    corroborating: list[dict] | None = None,
) -> list[str]:
    """Distinct attributed origins among the claims, tier- and kind-filtered.

    An origin counts only when its claim is of a confirming kind
    (:data:`CONFIRMING_KINDS`) and its source is not tier-3; a rumor, an opinion
    or a statement never counts toward confirmation. Each origin is canonicalized
    (case, possessives, titles, aliases). A claim whose ``affiliation`` names the
    organization a person speaks for counts as that organization, so an executive
    producer and the studio they work for are one origin. One article never
    counts as two independent origins: whatever canonicals a single source
    states, it contributes a single name (the longest of them), and when one
    canonical origin is contained in another they are treated as one, keeping the
    shorter name. ``corroborating`` names other sources' claims that corroborate
    the central fact; they count as those sources stating it, each using its own
    origin, subject to the same tier and kind filters.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    per_source: dict[str, set[str]] = {}
    for claim in [*(claims or []), *(corroborating or [])]:
        if claim.get("absence") or is_absence_claim(claim):
            continue
        if claim.get("kind") not in CONFIRMING_KINDS:
            continue
        source = by_url.get(_claim_source_url_key(claim))
        if source is None or source.get("tier") == 3:
            continue
        bucket = per_source.setdefault(_source_url_key(source.get("link")), set())
        affiliation = str(claim.get("affiliation") or "").strip().lower()
        if affiliation and affiliation != UNSTATED_ORIGIN:
            canonical = canonicalize_origin(affiliation, aliases)
            if canonical and canonical != UNSTATED_ORIGIN and not _is_anonymous_origin(canonical, aliases):
                bucket.add(canonical)
                continue
        for token in str(claim.get("origin") or "").split(";"):
            canonical = canonicalize_origin(token, aliases)
            if canonical and canonical != UNSTATED_ORIGIN and not _is_anonymous_origin(canonical, aliases):
                bucket.add(canonical)
    names = {max(bucket, key=len) for bucket in per_source.values() if bucket}
    return _collapse_contained_origins(names)


def confirmation_from_sources(
    sources: list[dict],
    *,
    claims: list[dict] | None = None,
    merged: dict | None = None,
    allow_single_origin_attributed: bool = False,
    aliases: dict[str, str] | None = None,
    entities: list[str] | None = None,
    corroborating: list[dict] | None = None,
) -> dict:
    """Assess the confirmation rule against the cluster's sources and claims.

    Confirmed when at least one tier-1 source states a passed claim, OR the
    passed claims carry at least two independent attributed origins (outlets
    repeating the same origin count as one, and two origins from the same single
    article never count as two), OR the headline claim is an organization's own
    announcement independently attributed by enough sources (the "first-party"
    rule; see :func:`_first_party_origins`). Origins are canonicalized through
    ``aliases`` (:func:`load_origin_aliases`), so different spellings of one name
    collapse, and only claims of kind confirmed/reported/estimate count: a rumor,
    an opinion or a statement never confirms. When ``allow_single_origin_attributed``
    is set, a single attributed (named, non-"unstated") origin also confirms.
    Tier 3 never counts. ``claims`` are the pre-merge claims of the story's
    central fact (``source_url`` singular, or ``source_urls`` on merged claims);
    confirmation is about who produced that claim, so the origins of any other
    claim never count.

    ``merged`` is the merged central claim: when given, a tier-1 source counts
    only if its own value carries every specific of the merged value, so a tier-1
    source never confirms wording that only another source stated.

    ``corroborating`` names other sources' claims that corroborate the central
    fact: they count toward the two-origins rule and the first-party rule, each
    using its own origin and kind, but never toward the tier-1 or tier-3 counts.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    by_url = _sources_by_url(sources)
    origins = _confirmation_origins(
        claims, by_url, aliases=aliases, corroborating=corroborating
    )
    if claims is not None:
        claim_urls = {_claim_source_url_key(claim) for claim in claims}
        tier1 = 0
        for source in sources:
            if source["tier"] != 1 or _source_url_key(source.get("link")) not in claim_urls:
                continue
            if merged is not None and not _source_supports(merged, _source_url_key(source.get("link"))):
                continue
            tier1 += 1
        tier3 = sum(
            1 for source in sources
            if source["tier"] == 3 and _source_url_key(source.get("link")) in claim_urls
        )
        first_party_orgs, _source_org, first_party_owners = _first_party_origins(
            claims, by_url, aliases, entities, corroborating=corroborating
        )
    else:
        tier1 = sum(1 for source in sources if source["tier"] == 1)
        tier3 = sum(1 for source in sources if source["tier"] == 3)
        first_party_orgs, first_party_owners = [], set()
    single_origin_attributed = len(origins) == 1
    first_party = (
        claims is not None and len(first_party_owners) >= _min_first_party_sources()
    )
    if merged is not None and str(merged.get("kind") or "") == "opinion":
        # A candidate whose central fact is a judgement is never confirmable:
        # an opinion can be repeated by every outlet and still be no fact.
        confirmed, rule, reason = False, "unconfirmed", "the central claim is an opinion"
    elif tier1 >= 1:
        confirmed, rule = True, "tier1"
        reason = "a tier-1 source states the headline claim"
    elif len(origins) >= 2:
        confirmed, rule = True, "two-independent-origins"
        reason = "two independent named origins support the headline claim"
    elif first_party:
        confirmed, rule = True, "first-party"
        organization = ", ".join(first_party_orgs) or "an organization"
        reason = (
            f"{len(first_party_owners)} independent sources attribute the "
            f"headline claim to {organization}"
        )
    elif allow_single_origin_attributed and single_origin_attributed:
        confirmed, rule = True, "single-origin-attributed"
        reason = (
            "a single attributed origin supports the headline claim and "
            "single-origin confirmation is enabled"
        )
    else:
        confirmed, rule = False, "unconfirmed"
        reason = "no tier-1 source and fewer than two independent named origins"
    return {
        "tier1_sources": tier1,
        "tier3_sources": tier3,
        "origins": origins,
        "first_party_organizations": first_party_orgs,
        "first_party_sources": len(first_party_owners),
        "single_origin_attributed": single_origin_attributed,
        "rule": rule,
        "confirmed": confirmed,
        "reason": reason,
    }


# --- the central claim -------------------------------------------------------


def _title_overlap_score(claim: dict, title_tokens: set[str]) -> int:
    """How many headline words the claim's text and value restate."""
    text = f"{str(claim.get('claim', ''))} {str(claim.get('value', ''))}"
    return len(title_tokens & _word_tokens(text))


#: A tier-1 source only sets the central claim when its own claim restates at
#: least this fraction of the seed headline's significant tokens. Overridable
#: through ``facts.min_central_overlap`` in config.yaml.
DEFAULT_MIN_CENTRAL_OVERLAP = 0.5


def _min_central_overlap() -> float:
    """The configured headline-overlap threshold for a tier-1 central claim."""
    config = load_config().get("facts") or {}
    try:
        value = float(config.get("min_central_overlap", DEFAULT_MIN_CENTRAL_OVERLAP))
    except (TypeError, ValueError):
        value = DEFAULT_MIN_CENTRAL_OVERLAP
    return min(max(value, 0.0), 1.0)


def _headline_significant_units(title: str, canonical: list[str]) -> list[tuple[str, str]]:
    """The headline's significant tokens: each entity, month and number once.

    "Significant token" means an entity, a date or a number, not each word of a
    long headline: a compound headline ("... but Xbox Game Pass ...") is not
    failed just because a claim about its main fact omits the other clause. A
    stop entity such as "Year 1 DLC" is no entity, but its bare number still
    counts here.
    """
    units: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str) -> None:
        key = (kind, value)
        if value and key not in seen:
            seen.add(key)
            units.append(key)

    for entity in canonical or []:
        add("entity", entity)
    for month in re.findall(r"[a-z]+", cluster.strip_diacritics(title).lower()):
        if month in _MONTHS:
            add("token", month)
    for token in _value_key_tokens(title):
        if token not in _MONTHS:
            add("token", token)
    return units


def _claim_names_entity(text: str, entity: str) -> bool:
    """True when the claim's text names the entity or any of its aliases."""
    if cluster.contains_entity(text, entity):
        return True
    return any(cluster.contains_entity(text, alias) for alias in entity_aliases(entity))


def _claim_overlap_fraction(claim: dict, units: list[tuple[str, str]]) -> float:
    """The share of the headline's significant units the claim restates."""
    if not units:
        return 1.0
    text = f"{str(claim.get('claim', ''))} {str(claim.get('value', ''))}"
    tokens = _word_tokens(cluster.strip_diacritics(text))
    shared = 0
    for kind, value in units:
        if kind == "token":
            shared += value in tokens
        else:
            shared += _claim_names_entity(text, value)
    return shared / len(units)


#: Headline words that state the headline's own action. The fallback in
#: ``_headline_restated`` uses them when the headline is nothing but the story's
#: entities, so a claim that names the entity still has to state what it does.
_HEADLINE_VERB_TOKENS = frozenset({
    "is", "are", "was", "were", "am", "be", "been", "being",
    "says", "said", "say", "will", "would", "does", "do", "did",
    "announces", "announced", "announce",
    "reveals", "revealed", "reveal",
    "confirms", "confirmed", "confirm",
    "launches", "launched", "launch",
    "adds", "added", "add",
    "gets", "got", "has", "have", "had",
})


def _headline_restated(
    claim: dict,
    *,
    title: str,
    units: list[tuple[str, str]],
    threshold: float,
) -> bool:
    """Whether a claim restates enough of the headline to be its central claim.

    The story's canonical entities are set aside: a claim that only names the
    company or the game must never clear ``facts.min_central_overlap``. What the
    measure counts is the headline's *claim-specific* content -- its figures and
    dates (the significant units that are not entities). When the headline has
    none (it is only entities and stop words), the old significant-unit measure
    applies, but only when the claim also states one of the headline's numbers,
    dates or verbs.
    """
    claim_specific = [(kind, value) for kind, value in units if kind != "entity"]
    if claim_specific:
        return _claim_overlap_fraction(claim, claim_specific) >= threshold
    if _claim_overlap_fraction(claim, units) < threshold:
        return False
    hard = _value_key_tokens(title)
    verbs = _word_tokens(cluster.strip_diacritics(title)) & _HEADLINE_VERB_TOKENS
    if not hard and not verbs:
        return False
    text = f"{str(claim.get('claim', ''))} {str(claim.get('value', ''))}"
    claim_words = _word_tokens(cluster.strip_diacritics(text))
    return bool(claim_words & (hard | verbs))


def central_claim(claims: list[dict], *, title: str = "") -> Optional[dict]:
    """Pick the claim that restates the cluster's seed headline.

    The cluster seed is its newest source; the seed headline becomes the cluster
    title, passed to ``facts()`` as ``title``. The central claim is the one
    whose text and value share the most words with that headline -- the claim
    the headline states is the claim this story must confirm. Among claims whose
    overlap is within 80% of the best, a confirmed claim with a named origin
    wins over a longer unstated statement; ties then break to the longer text.
    Returns None when no headline is given or no extracted claim overlaps it.
    """
    title = str(title or "").strip()
    if not title:
        return None
    title_tokens = _word_tokens(title)
    scored: list[tuple[float, dict]] = []
    for claim in claims or []:
        score = _title_overlap_score(claim, title_tokens)
        if score > 0:
            scored.append((score, claim))
    if not scored:
        return None
    best_score = max(score for score, _ in scored)
    ranked: list[tuple[int, float, int, str, dict]] = []
    for score, claim in scored:
        if score < 0.8 * best_score:
            continue
        origin_key = _origin_key(claim.get("origin"))
        named = bool(origin_key) and origin_key != UNSTATED_ORIGIN
        confirmed_named = 1 if str(claim.get("kind")) == "confirmed" and named else 0
        length = len(str(claim.get("claim", ""))) + len(str(claim.get("value", "")))
        ranked.append((confirmed_named, score, length, str(claim.get("claim", "")), claim))
    ranked.sort(key=lambda item: item[:4])
    return ranked[-1][4]


#: How much of the shared wording two claims must share -- the Jaccard overlap
#: over the content words of their text plus value -- before the one counts as
#: corroborating the other's fact. Overridable through
#: ``facts.corroboration_overlap`` in config.yaml.
DEFAULT_CORROBORATION_OVERLAP = 0.5


def _corroboration_overlap() -> float:
    """The configured Jaccard threshold for corroborating the central fact."""
    config = load_config().get("facts") or {}
    try:
        value = float(config.get("corroboration_overlap", DEFAULT_CORROBORATION_OVERLAP))
    except (TypeError, ValueError):
        value = DEFAULT_CORROBORATION_OVERLAP
    return min(max(value, 0.0), 1.0)


def _corroboration_words(claim: dict) -> set[str]:
    """Content words of a claim's text and value for corroboration overlap.

    Lowercased, diacritics folded and stop words dropped; plain word forms (no
    stemming) so the Jaccard overlap reflects wording the two claims literally
    share.
    """
    text = f"{claim.get('claim', '')} {claim.get('value', '')}"
    folded = cluster.strip_diacritics(str(text or "")).lower()
    return {
        token
        for token in re.findall(r"[a-z0-9]+", folded)
        if token not in _STOP_WORDS
    }


def _claim_hard_tokens(claim: dict) -> tuple[set[str], set[str]]:
    """The figures and month names a claim's value states."""
    numbers: set[str] = set()
    months: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", str(claim.get("value") or "").lower()):
        if token in _MONTHS:
            months.add(token)
            continue
        digits = re.match(r"^\d+", token)
        if digits:
            numbers.add(digits.group(0))
    return numbers, months


def _claims_conflict(a: dict, b: dict) -> bool:
    """True when two claims state figures or dates that contradict each other."""
    a_numbers, a_months = _claim_hard_tokens(a)
    b_numbers, b_months = _claim_hard_tokens(b)
    if a_numbers and b_numbers and not (a_numbers & b_numbers):
        return True
    if a_months and b_months and not (a_months & b_months):
        return True
    return False


def _overlap_score(candidate: dict, central: dict) -> float:
    """Raw Jaccard overlap of two claims' content words, or ``0.0`` when empty."""
    candidate_words = _corroboration_words(candidate)
    central_words = _corroboration_words(central)
    union = candidate_words | central_words
    if not union:
        return 0.0
    return len(candidate_words & central_words) / len(union)


def _claim_all_hard_tokens(claim: dict) -> tuple[set[str], set[str]]:
    """The figures and month names a claim's text *and* value state."""
    text = f"{claim.get('claim', '')} {claim.get('value', '')}"
    numbers: set[str] = set()
    months: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if token in _MONTHS:
            months.add(token)
            continue
        digits = re.match(r"^\d+", token)
        if digits:
            numbers.add(digits.group(0))
    return numbers, months


def _containment_score(candidate: dict, central: dict) -> float:
    """``1.0`` when the shorter claim's content words sit inside the longer one.

    A shorter restatement ("Fortnite is coming to Linux") is corroborated by a
    longer version of the same fact that adds context ("... once Epic improves
    Easy Anti-Cheat"), even though the extra words dilute the Jaccard overlap.
    Requires at least three content words so a two-word phrase cannot stand in
    for any claim that happens to contain it.
    """
    candidate_words = _corroboration_words(candidate)
    central_words = _corroboration_words(central)
    if not candidate_words or not central_words:
        return 0.0
    shorter, longer = (
        (candidate_words, central_words)
        if len(candidate_words) <= len(central_words)
        else (central_words, candidate_words)
    )
    if len(shorter) < 3 or not shorter <= longer:
        return 0.0
    return 1.0


def _central_adds_hard_token(candidate: dict, central: dict) -> bool:
    """True when the central claim states a figure or month the candidate omits.

    "coming to Linux in March" is a different fact from "coming to Linux": the
    month in the central claim is not restated by the candidate, so the candidate
    must not be read as corroborating it.
    """
    candidate_numbers, candidate_months = _claim_all_hard_tokens(candidate)
    central_numbers, central_months = _claim_all_hard_tokens(central)
    return bool((central_numbers - candidate_numbers) or (central_months - candidate_months))


def corroboration_score(
    candidate: dict, central: dict, *, entities: list[str] | None = None
) -> float:
    """The overlap by which ``candidate`` corroborates ``central``, or ``0.0``.

    The score is the Jaccard overlap of the two claims' content words (claim
    text plus value). It counts only when it reaches ``facts.corroboration_overlap``,
    the two claims state no contradicting figures or dates, and the candidate
    names at least one of the story's primary entities.
    """
    score = _overlap_score(candidate, central)
    if score < _corroboration_overlap():
        score = _containment_score(candidate, central)
    if score < _corroboration_overlap():
        return 0.0
    if _claims_conflict(candidate, central):
        return 0.0
    if _central_adds_hard_token(candidate, central):
        return 0.0
    if entities:
        text = f"{candidate.get('claim', '')} {candidate.get('value', '')}"
        if not any(_claim_names_entity(text, entity) for entity in entities):
            return 0.0
    return score


def corroborating_claims(
    claims: list[dict],
    central: dict,
    *,
    exclude: Iterable[dict] | None = None,
    entities: list[str] | None = None,
) -> list[tuple[float, dict]]:
    """The best corroborating claim per other source, with its overlap score.

    ``claims`` are the story's extracted claims; ``exclude`` names the central
    fact's own claims (already represented in its versions). For every other
    source the single best-matching claim that reaches the corroboration
    threshold is returned as ``(score, claim)``, sorted by source URL for stable
    output.
    """
    excluded_ids = {id(claim) for claim in (exclude or [])}
    best_by_source: dict[str, tuple[float, dict]] = {}
    for claim in claims or []:
        if id(claim) in excluded_ids:
            continue
        if claim.get("kind") not in CORROBORATING_KINDS:
            continue
        score = corroboration_score(claim, central, entities=entities)
        if score <= 0.0:
            continue
        url_key = _claim_source_url_key(claim)
        current = best_by_source.get(url_key)
        if current is None or score > current[0]:
            best_by_source[url_key] = (score, claim)
    return [best_by_source[key] for key in sorted(best_by_source)]


# --- model-assisted fact grouping and evidence confirmation ------------------


def grouping_items(claims: list[dict], sources: list[dict]) -> list[dict]:
    """One grouping item per claim: a stable id, its source id and the claim.

    Claim ids are ``c<index>`` in the order of ``claims`` and source ids are
    ``s<index>`` in the order of ``sources``; the grouper sees only these ids
    and the (clipped) labels, never a source body.
    """
    source_ids = {
        _source_url_key(source.get("link")): f"s{index}"
        for index, source in enumerate(sources or [])
    }
    items: list[dict] = []
    for index, claim in enumerate(claims or []):
        url_key = _source_url_key(claim.get("source_url"))
        items.append(
            {
                "id": f"c{index}",
                "source_id": source_ids.get(url_key, url_key or f"s{index}"),
                "claim": claim,
            }
        )
    return items


def _item_summary(item: dict) -> dict:
    claim = item.get("claim") or {}
    label = f"{claim.get('claim', '')} {claim.get('value', '')}".strip()
    return {
        "id": item["id"],
        "source_id": item["source_id"],
        "label": _clip_field(label, GROUPING_LABEL_CHARS),
    }


def _claim_owner(claim: dict, by_url: dict[str, dict]) -> str:
    """The distinct owner behind a claim's source, falling back to its URL key."""
    source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
    return _origin_key(source.get("owner")) or _source_url_key(claim.get("source_url"))


def _claim_primary_entities(claim: dict, entities: list[str] | None) -> set[str]:
    """The story's primary entities a claim names in its text or value."""
    text = f"{claim.get('claim', '')} {claim.get('value', '')}"
    return {entity for entity in (entities or []) if _claim_names_entity(text, entity)}


def group_reason(members: list[dict], entities: list[str] | None) -> str:
    """Why a returned group is not an accepted cross-source restatement.

    Empty string means the group passes every guard: at least two claims from
    two different sources, a shared primary entity, no contradicting figures or
    dates, and no rumor/opinion mixed with a confirmed claim.
    """
    if len(members) < 2:
        return "fewer than two claims"
    sources = {_source_url_key(claim.get("source_url")) for claim in members}
    if len(sources) < 2:
        return "all claims come from one source"
    shared: set[str] | None = None
    for claim in members:
        named = _claim_primary_entities(claim, entities)
        shared = named if shared is None else (shared & named)
    if not shared:
        return "the claims name no shared primary entity"
    for index, first in enumerate(members):
        for second in members[index + 1:]:
            if _claims_conflict(first, second):
                return "the claims state conflicting figures or dates"
    kinds = {str(claim.get("kind")) for claim in members}
    if kinds & {"rumor", "opinion"} and "confirmed" in kinds:
        return "a rumor or opinion is grouped with a confirmed claim"
    return ""


def apply_group_guards(
    groups: list[list[str]],
    items: list[dict],
    entities: list[str] | None,
) -> tuple[list[dict], list[dict]]:
    """Validate the model's id groups in code, returning (accepted, dropped).

    Unknown ids are ignored, each id is kept in at most one group, and a group
    that fails :func:`group_reason` is dropped with that reason.
    """
    item_by_id = {item["id"]: item for item in items}
    used: set[str] = set()
    accepted: list[dict] = []
    dropped: list[dict] = []
    for ids in groups or []:
        known = [identifier for identifier in ids if identifier in item_by_id]
        unique = [identifier for identifier in known if identifier not in used]
        used.update(unique)
        if len(unique) < 2:
            if known:
                dropped.append({"ids": known, "reason": "fewer than two usable claims"})
            continue
        members = [item_by_id[identifier]["claim"] for identifier in unique]
        reason = group_reason(members, entities)
        if reason:
            dropped.append({"ids": unique, "reason": reason})
            continue
        accepted.append({"ids": unique, "claims": members})
    return accepted, dropped


def _empty_group_record() -> dict:
    return {"groups": [], "dropped": [], "claims": [], "failed": None}


def group_facts(
    claims: list[dict],
    sources: list[dict],
    *,
    generate: Callable[..., Generation],
    run_state: RunState | None = None,
    model_call_budget: int | None = None,
    exclude_families: set[str] | None = None,
    entities: list[str] | None = None,
) -> dict:
    """Group the claims with one model call, then validate the groups in code.

    The call runs only when at least two distinct sources contributed a claim
    and the model-call budget still allows it. The grouper's family is excluded
    from the extractor's when possible, so a grouped fact is not merely the
    extractor agreeing with itself. A model, transport or schema failure is
    recorded and yields no accepted groups -- the candidate still continues.
    """
    items = grouping_items(claims, sources)
    record = {
        "groups": [],
        "dropped": [],
        "claims": [_item_summary(item) for item in items],
        "failed": None,
    }
    if len({item["source_id"] for item in items}) < 2:
        record["failed"] = "fewer than two sources contributed claims"
        return record
    if (
        run_state is not None
        and model_call_budget is not None
        and run_state.calls >= model_call_budget
    ):
        record["failed"] = "model-call budget spent before grouping"
        return record
    try:
        generation = generate(
            "verifier",
            build_grouping_prompt(items),
            json_schema=GROUPING_SCHEMA,
            run_state=run_state,
            exclude_families=set(exclude_families or ()),
        )
    except Exception as exc:  # noqa: BLE001 - grouping is best-effort
        record["failed"] = f"grouping call failed: {exc}"
        logger.warning("story grouping call failed: %s", exc)
        return record
    try:
        groups = parse_groups(generation.value)
    except FactsError as exc:
        record["failed"] = f"grouping response invalid: {exc}"
        logger.warning("story grouping response invalid: %s", exc)
        return record
    record["groups"], record["dropped"] = apply_group_guards(groups, items, entities)
    return record


def fact_groups_sheet(record: dict) -> dict:
    """The diagnostics-only sheet view of a grouping record (ids, no bodies)."""
    return {
        "accepted": [{"ids": list(group["ids"])} for group in record.get("groups") or []],
        "dropped": [
            {"ids": list(entry.get("ids") or []), "reason": entry.get("reason")}
            for entry in record.get("dropped") or []
        ],
        "claims": list(record.get("claims") or []),
        "failed": record.get("failed"),
    }


def evidence_central_claims(record: dict, central_group: list[dict] | None) -> list[dict]:
    """The central fact's claims for evidence confirmation.

    The accepted group that contains any claim of the central fact (the model's
    answer) is used; when no accepted group does, the code's own central group
    is used, so a story still gets an evidence verdict when grouping failed.
    """
    central_ids = {id(claim) for claim in (central_group or [])}
    for group in record.get("groups") or []:
        members = group.get("claims") or []
        if any(id(claim) in central_ids for claim in members):
            return list(members)
    return list(central_group or [])


def evidence_confirmation(
    claims: list[dict],
    by_url: dict[str, dict],
    *,
    tier1_confirms: bool = False,
    aliases: dict[str, str] | None = None,
    min_first_party: int | None = None,
) -> dict:
    """Confirm the central fact from verified evidence over the central group.

    Every speaker is first resolved to a single canonical identity: through the
    alias and organization files (``data/origins.json``, ``data/orgs.json``),
    then -- for names in neither -- by collapsing names where one is a
    whole-word part of the other, so one organization spelled two ways
    ("remedy", "remedy entertainment") counts as one speaker. A claim whose
    affiliation names an organization counts as that organization, so a person
    and the employer the evidence states are one identity. Two distinct persons
    or organizations with different names stay distinct.

    Rules in order: a tier-1 source states the headline claim (unchanged, passed
    in as ``tier1_confirms``); first-party, when ``min_first_party`` distinct
    owners each carry a claim in the group whose stored evidence passed, whose
    ``speaker_type`` is organization, whose canonical identity is the same
    organization and whose kind may be first party; two-independent-origins,
    when at least two distinct canonical speakers back the group from at least
    two distinct owners. Only claims whose stored evidence ``ok`` is true count,
    so an anonymous speaker, a product, the outlet citing itself or an
    unverified quote never confirms anything. Owners count once.
    """
    aliases = load_origin_aliases() if aliases is None else aliases
    if min_first_party is None:
        min_first_party = _min_first_party_sources()
    resolved: list[tuple[str, str, str, str]] = []
    verified = 0
    for claim in claims or []:
        evidence = claim.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("ok"):
            continue
        verified += 1
        speaker = claim.get("speaker") or claim.get("origin")
        # An unnamed descriptor never names an origin, and an outlet citing
        # itself never counts as an independent speaker -- the stored evidence
        # already rejects both, but a claim is guarded here too so neither can
        # slip in through a hand-built sheet.
        evidence = claim.get("evidence")
        first_party = isinstance(evidence, dict) and evidence.get("attribution_mode") == "first_party_site"
        if _is_anonymous_origin(speaker, aliases):
            continue
        source = by_url.get(_source_url_key(claim.get("source_url"))) or {}
        if _is_publication(speaker, source) and not first_party:
            continue
        owner = _claim_owner(claim, by_url)
        # A stated affiliation names the organization a speaker works for, so
        # the person and that organization are one identity -- but only when the
        # evidence states the affiliation. A first-party own-site speaker also
        # resolves to the owner's canonical organization.
        identity_source = speaker
        affiliation = str(claim.get("affiliation") or "").strip()
        if first_party and not affiliation:
            owner = _claim_owner(claim, by_url)
            if owner:
                identity_source = owner
        if affiliation and affiliation.lower() != UNSTATED_ORIGIN:
            canonical_affiliation = canonicalize_origin(affiliation, aliases)
            if (
                canonical_affiliation
                and canonical_affiliation != UNSTATED_ORIGIN
                and not _is_anonymous_origin(canonical_affiliation, aliases)
            ):
                identity_source = canonical_affiliation
        canonical = canonicalize_origin(identity_source, aliases)
        if canonical == UNSTATED_ORIGIN:
            continue
        resolved.append(
            (
                canonical,
                owner,
                str(claim.get("speaker_type") or "none"),
                str(claim.get("kind") or ""),
            )
        )

    # One organization spelled two ways is one speaker: collapse every canonical
    # name to a single identity before any rule counts it.
    identities = _collapse_origin_identities({canonical for canonical, *_ in resolved})

    org_owners: dict[str, set[str]] = {}
    speaker_owners: dict[str, set[str]] = {}
    for canonical, owner, speaker_type, kind in resolved:
        identity = identities.get(canonical, canonical)
        if speaker_type == "organization" and kind in FIRST_PARTY_KINDS:
            org_owners.setdefault(identity, set()).add(owner)
        if speaker_type in ("person", "organization", "outlet"):
            speaker_owners.setdefault(identity, set()).add(owner)

    first_party_orgs = sorted(
        org for org, owners in org_owners.items() if len(owners) >= min_first_party
    )
    origins = sorted(speaker_owners)
    speaker_owners_all = set().union(*speaker_owners.values()) if speaker_owners else set()
    if tier1_confirms:
        confirmed, rule = True, "tier1"
        reason = "a tier-1 source states the headline claim"
    elif first_party_orgs:
        confirmed, rule = True, "first-party"
        organization = ", ".join(first_party_orgs)
        owners = set().union(*(org_owners[org] for org in first_party_orgs))
        reason = (
            f"{len(owners)} independent sources attribute the central fact to "
            f"{organization} with verified evidence"
        )
    elif len(origins) >= 2 and len(speaker_owners_all) >= 2:
        confirmed, rule = True, "two-independent-origins"
        reason = "two independent named origins state the central fact"
    else:
        confirmed, rule = False, "unconfirmed"
        reason = "the central fact is not backed by enough verified cross-source evidence"
    return {
        "tier1_sources": 1 if tier1_confirms else 0,
        "tier3_sources": 0,
        "origins": origins,
        "first_party_organizations": first_party_orgs,
        "first_party_sources": max(
            (len(org_owners[org]) for org in first_party_orgs), default=0
        ),
        "single_origin_attributed": len(origins) == 1,
        "verified_claims": verified,
        "rule": rule,
        "confirmed": confirmed,
        "reason": reason,
        "rule_source": "evidence",
    }


def _preferred_central_index(
    groups: list[list[dict]],
    sources: list[dict],
    title: str,
    *,
    canonical: list[str] | None = None,
    min_overlap: float | None = None,
) -> Optional[tuple[int, str]]:
    """The group to center on, preferring a tier-1 source's own claims.

    A cluster that has a tier-1 source (a company's or platform's own post)
    takes its central claim from that source's claims: the headline-overlap
    pick is restricted to claims the tier-1 source itself wrote, so the fact the
    headline states is the fact the tier-1 source restates, and the other
    outlets' versions corroborate it rather than driving the central wording. A
    tier-1 claim must also restate at least ``min_overlap`` (default
    ``facts.min_central_overlap``) of the seed headline's significant tokens: a
    passing mention is not the headline's fact. Returns ``(group_index,
    url_key)``, or None when no headline is given, no tier-1 claim reaches the
    threshold, or no tier-1 source exists (the plain headline overlap rule
    applies to all claims then).
    """
    title = str(title or "").strip()
    if not title:
        return None
    if canonical is None:
        canonical = canonical_entities(title=title)
    threshold = _min_central_overlap() if min_overlap is None else min_overlap
    units = _headline_significant_units(title, canonical)
    title_tokens = _word_tokens(title)
    tier1_urls = {
        _source_url_key(source.get("link"))
        for source in sources
        if source.get("tier") == 1
    }
    best: Optional[tuple[int, str]] = None
    best_key = (0, 0, "")
    if tier1_urls:
        for index, group in enumerate(groups):
            for claim in group:
                if _source_url_key(claim.get("source_url")) not in tier1_urls:
                    continue
                if not _headline_restated(
                    claim, title=title, units=units, threshold=threshold
                ):
                    continue
                score = _title_overlap_score(claim, title_tokens)
                key = (
                    score,
                    len(str(claim.get("claim", ""))) + len(str(claim.get("value", ""))),
                    str(claim.get("claim", "")),
                )
                if score > 0 and key > best_key:
                    best, best_key = (
                        (index, _source_url_key(claim.get("source_url"))),
                        key,
                    )
    return best


def _fold_entity(entity: object) -> str:
    """Case- and diacritics-fold an entity so spelling variants count together."""
    return cluster.strip_diacritics(str(entity or "")).lower()


def _accented_char_count(text: str) -> int:
    """Count base characters that carry diacritics ("ō" in "Tōkon")."""
    return sum(
        1
        for char in text
        if unicodedata.combining(char) == 0 and len(unicodedata.normalize("NFKD", char)) > 1
    )


def _best_display_entity(variants: list[str], exact_counts: dict[str, int]) -> str:
    """The most reliably spelled display form of one folded entity.

    Prefer proper casing and diacritics over ALL-CAPS ASCII forms, then the
    most frequent spelling, then the longest explicit form.
    """
    def quality(name: str):
        words = [word for word in name.split() if word]
        title_case = bool(words) and all(
            word[0].isupper() and (len(word) == 1 or word[1:].islower())
            for word in words
        )
        return (
            _accented_char_count(name),
            title_case,
            exact_counts.get(name, 0),
            len(name),
        )

    return max(variants, key=quality)


def _chosen_display_entities(
    title: str | None,
    source_titles: list[str] | None,
) -> list[str]:
    """One best-display phrase per folded entity the headlines support.

    Counts each full name phrase (:func:`cluster.story_phrases`) across the
    cluster title and the source headlines, folding case and diacritics so
    spelling variants of one name ("Marvel Tōkon", "MARVEL Tōkon") merge into a
    single count. Keeps every entity whose folded count is at least half of the
    highest count and that more than one headline carries, plus every entity the
    seed headline itself names (so an entity the headline mentions only once is
    still kept); the best display form (preferring proper casing and diacritics)
    survives. The result is one phrase per folded entity, in folded-name order.
    """
    folded: dict[str, int] = {}
    exact_counts: dict[str, int] = {}
    displays: dict[str, list[str]] = {}
    for candidate in [title, *(source_titles or [])]:
        if not candidate:
            continue
        for entity in cluster.story_phrases(candidate):
            exact_counts[entity] = exact_counts.get(entity, 0) + 1
            key = _fold_entity(entity)
            folded[key] = folded.get(key, 0) + 1
            displays.setdefault(key, [])
            if entity not in displays[key]:
                displays[key].append(entity)
    if not folded:
        return []
    top = max(folded.values())
    floor_kept = {
        key
        for key, count in folded.items()
        if count >= 2 and count * 2 >= top
    }
    headline_kept = (
        {_fold_entity(entity) for entity in cluster.story_phrases(title) or []}
        if title else set()
    )
    kept = floor_kept | headline_kept
    return [_best_display_entity(displays[key], exact_counts) for key in sorted(kept)]


def primary_entities(
    *,
    title: str | None = None,
    source_titles: list[str] | None = None,
    entities: list[str] | None = None,
) -> list[str]:
    """The entities that define the story's subject, with their sub-entities.

    The supported, best-display name phrases (:func:`_chosen_display_entities`,
    never a stop word such as a month, a weekday or "Year 1 DLC") are each
    expanded into themselves plus the sub-entities their connectors split them
    into (:func:`cluster.sub_entities`), so a claim that names only part of a
    long phrase ("Call of Duty" inside "Call of Duty: Modern Warfare 4") is still
    recognized as on-topic. Generic entities ("PC", "AI", "IPs") are dropped: a
    category word never makes a claim on-topic on its own. Falls back to the
    cluster's reported entities when no headline parses.
    """
    chosen = _chosen_display_entities(title, source_titles)
    if not chosen:
        chosen = [str(entity).strip() for entity in (entities or []) if str(entity).strip()]
    stop = cluster.entity_stop_list()
    chosen = [entity for entity in chosen if not cluster.is_stop_entity(entity, stop=stop)]
    return [
        entity for entity in _expand_entities(chosen)
        if not cluster.is_generic_entity(entity)
    ]


def canonical_entities(
    *,
    title: str | None = None,
    source_titles: list[str] | None = None,
    entities: list[str] | None = None,
) -> list[str]:
    """The story's canonical entities, one per concept, for SEO and hubs.

    The same supported, best-display phrases as :func:`primary_entities` but
    without the sub-entity expansion: a colon/subtitle phrase stays one canonical
    entity ("Marvel Tōkon: Fighting Souls") and its parts are aliases used only
    for matching (:func:`entity_aliases`), never listed here. Stop words are
    dropped and folded duplicates collapse, so the SEO entities list can never
    contain an alias-only entry.
    """
    chosen = _chosen_display_entities(title, source_titles)
    if not chosen:
        chosen = [str(entity).strip() for entity in (entities or []) if str(entity).strip()]
    stop = cluster.entity_stop_list()
    seen: set[str] = set()
    canonical: list[str] = []
    for entity in chosen:
        if cluster.is_stop_entity(entity, stop=stop):
            continue
        key = _fold_entity(entity)
        if key in seen:
            continue
        seen.add(key)
        canonical.append(entity)
    return sorted(canonical)


def entity_aliases(canonical: str) -> list[str]:
    """The matching aliases of one canonical entity: its subtitle/connector parts.

    "Marvel Tōkon: Fighting Souls" yields "marvel tōkon" and "fighting souls";
    "Call of Duty: Modern Warfare 4" yields "call of duty" and "modern warfare
    4". Aliases are lowercased but keep their diacritics, are never listed as
    entities themselves, and stop words or a part equal to the whole are dropped.
    """
    whole = _fold_entity(canonical)
    stop = cluster.entity_stop_list()
    aliases: list[str] = []
    for segment in re.split(r"[:]", str(canonical or "")):
        part = re.sub(r"\s+", " ", segment).strip().lower()
        if not part:
            continue
        key = _fold_entity(part)
        if key == whole or key in {_fold_entity(alias) for alias in aliases}:
            continue
        if cluster.is_stop_entity(part, stop=stop):
            continue
        aliases.append(part)
    return aliases


def _expand_entities(entities: list[str]) -> list[str]:
    """Each entity followed by the sub-entities its connectors split it into."""
    expanded: list[str] = []
    for entity in entities:
        for sub in cluster.sub_entities(entity):
            if sub and sub not in expanded:
                expanded.append(sub)
    return sorted(expanded)


def _claim_on_topic(claim: dict, source: dict, entities: list[str]) -> bool:
    """True when a claim or its source headline names any entity or sub-entity.

    Either the claim's own text/value or the source article's title is enough;
    matching is case-insensitive and on whole words or phrases.
    """
    text = f"{claim.get('claim', '')} {claim.get('value', '')}"
    if any(cluster.contains_entity(text, entity) for entity in entities):
        return True
    source_title = str(source.get("title") or "")
    if source_title:
        return any(cluster.contains_entity(source_title, entity) for entity in entities)
    return False


def coherence_check(
    claims: list[dict],
    sources: list[dict],
    *,
    title: str | None = None,
    entities: list[str] | None = None,
    story_id: str = "",
) -> tuple[list[dict], list[dict], list[str]]:
    """Drop claims whose claim text and source both miss the story's entities.

    Returns ``(kept, dropped, primary)``. A claim is kept when its claim text or
    value names any primary entity or sub-entity, or when the title of its source
    article does. When no primary entity can be determined the check is skipped
    and every claim is kept. When more than half of a single source's claims are
    dropped, a warning naming that source and the entities is logged.
    """
    primary = primary_entities(
        title=title,
        source_titles=[str(source.get("title") or "") for source in sources],
        entities=entities,
    )
    if not primary:
        logger.warning("story %s: no primary entity found; skipping coherence check", story_id or "?")
        return list(claims), [], []
    by_url = _sources_by_url(sources)
    kept: list[dict] = []
    dropped: list[dict] = []
    totals: Counter[str] = Counter()
    dropped_by_source: Counter[str] = Counter()
    for claim in claims:
        key = _source_url_key(claim.get("source_url"))
        source = by_url.get(key) or {}
        totals[key] += 1
        if _claim_on_topic(claim, source, primary):
            kept.append(claim)
        else:
            dropped.append(claim)
            dropped_by_source[key] += 1
            logger.warning(
                "story %s: dropping off-topic claim from %s (mentions none of %s): %r",
                story_id or "?",
                claim.get("source_url"),
                primary,
                claim.get("claim"),
            )
    for key, dropped_count in dropped_by_source.items():
        total = totals.get(key, 0)
        if total and dropped_count * 2 > total:
            source = by_url.get(key) or {}
            label = str(
                source.get("source_name") or source.get("title") or key or "unknown source"
            )
            logger.warning(
                "story %s: dropped %d of %d claims from %s (%s) as off-topic; "
                "entities checked: %s",
                story_id or "?",
                dropped_count,
                total,
                label,
                key or "unknown url",
                primary,
            )
    return kept, dropped, primary


def _coherence_dropped_details(dropped: list[dict], primary: list[str]) -> list[dict]:
    """The coherence-dropped claims, each with the reason it was dropped.

    A claim is dropped off-topic when neither its own text/value nor its source
    article's title names any primary entity; the reason records the entities
    that were checked, so a saved sheet shows exactly why the claim was cut.
    """
    entities = ", ".join(primary) if primary else "the story's primary entities"
    reason = f"claim and its source headline name none of: {entities}"
    return [
        {
            "claim": claim.get("claim"),
            "source_url": claim.get("source_url"),
            "reason": reason,
        }
        for claim in dropped
    ]


def _confirmation_reason(confirmation: dict) -> str:
    """Plain-text reason for a confirmation verdict, for the saved sheet."""
    reason = str(confirmation.get("reason") or "").strip()
    if reason:
        return reason
    if confirmation.get("confirmed"):
        return "confirmed"
    return "no tier-1 source and fewer than two independent named origins"


#: How many claims of one source an unconfirmed sheet's diagnostics keep.
MAX_DIAGNOSTIC_CLAIMS_PER_SOURCE = 20


def _claims_by_source(claims: list[dict]) -> list[dict]:
    """Per-source claim labels for the unconfirmed sheet's diagnostics.

    The story's extracted claims are grouped by source URL (sorted), keeping each
    claim's text, kind and origin, capped at
    :data:`MAX_DIAGNOSTIC_CLAIMS_PER_SOURCE` per source. Claim text and labels
    only -- never the source body.
    """
    by_url: dict[str, list[dict]] = {}
    for claim in claims or []:
        key = _source_url_key(claim.get("source_url"))
        if key:
            by_url.setdefault(key, []).append(claim)
    diagnostics: list[dict] = []
    for key in sorted(by_url):
        entries = by_url[key]
        diagnostics.append(
            {
                "source_url": entries[0].get("source_url"),
                "claims": [
                    {
                        "claim": claim.get("claim"),
                        "kind": claim.get("kind"),
                        "origin": claim.get("origin"),
                    }
                    for claim in entries[:MAX_DIAGNOSTIC_CLAIMS_PER_SOURCE]
                ],
            }
        )
    return diagnostics


def _corroboration_diagnostics(
    claims: list[dict],
    central: Optional[dict],
    *,
    entities: list[str] | None = None,
) -> list[dict]:
    """Per non-central source: its best central-fact overlap and matched claim.

    The sources that already state the central fact are skipped. For every other
    source, the claim with the highest raw overlap against the central claim is
    reported with that score; ``matched_claim`` is its claim text when the
    source's claim actually corroborates the central fact (reaches the
    threshold, states no contradicting figures or dates and names an entity),
    else ``null``. Empty when there is no central claim.
    """
    if central is None:
        return []
    central_urls = {
        _source_url_key(url) for url in (central.get("source_urls") or [central.get("source_url")])
    }
    best: dict[str, tuple[float, bool, dict]] = {}
    for claim in claims or []:
        key = _source_url_key(claim.get("source_url"))
        if not key or key in central_urls:
            continue
        score = _overlap_score(claim, central)
        qualifies = corroboration_score(claim, central, entities=entities) > 0.0
        current = best.get(key)
        if current is None or score > current[0]:
            best[key] = (score, qualifies, claim)
    return [
        {
            "source_url": best[key][2].get("source_url"),
            "score": round(best[key][0], 4),
            "matched_claim": best[key][2].get("claim") if best[key][1] else None,
        }
        for key in sorted(best)
    ]


def _grounding_entry(entry: dict) -> dict:
    """One grounding-block row: the claim's text, its reason, and any anchors."""
    claim = entry.get("claim")
    row = {
        "claim": claim.get("claim") if isinstance(claim, dict) else claim,
        "reason": entry.get("reason"),
    }
    if entry.get("anchors") is not None:
        row["anchors"] = entry["anchors"]
    return row


def _evidence_field(value: object, limit: int = EVIDENCE_FIELD_CHARS) -> str:
    return str(value or "").strip()[:limit]


def _evidence_for_claim(
    claim: dict,
    source: dict,
    *,
    aliases: dict[str, str],
    entities: list[str] | None,
) -> dict:
    """Verify one claim against its own source and tag the result with the URL."""
    result = verify_evidence(
        claim,
        source.get("text") or "",
        source.get("title") or "",
        source.get("source_name") or "",
        aliases=aliases,
        entities=entities,
        source_tier=source.get("tier"),
        source_owner=source.get("owner"),
    )
    return {
        "source_url": claim.get("source_url"),
        "speaker": _evidence_field(claim.get("speaker")),
        "speaker_type": _normalize_speaker_type(claim.get("speaker_type")),
        "quote": _evidence_field(claim.get("quote")),
        "attribution_quote": _evidence_field(claim.get("attribution_quote")),
        **result,
    }


def _merge_evidence(group: list[dict]) -> list[dict]:
    """The evidence records of a merged fact's sources, one per source."""
    return [
        claim["evidence"]
        for claim in group
        if isinstance(claim.get("evidence"), dict)
    ]


def _evidence_summary(claims: list[dict]) -> list[dict]:
    """Per-source tally of evidence outcomes for the saved sheet.

    Counts every claim with an evidence record by its source URL, with how many
    passed, a breakdown of the failure reasons and a count of each attribution
    mode, so a sheet shows at a glance which source's claims carried verifiable
    quotes and how their speakers were credited.
    """
    rows: dict[str, dict] = {}
    for claim in claims or []:
        result = claim.get("evidence")
        if not isinstance(result, dict):
            continue
        key = _source_url_key(claim.get("source_url")) or str(claim.get("source_url") or "")
        row = rows.setdefault(
            key,
            {
                "source_url": claim.get("source_url"),
                "claims": 0,
                "ok": 0,
                "failed": 0,
                "reasons": {},
                "modes": {},
            },
        )
        row["claims"] += 1
        mode = str(result.get("attribution_mode") or "none")
        row["modes"][mode] = row["modes"].get(mode, 0) + 1
        if result.get("ok"):
            row["ok"] += 1
        else:
            row["failed"] += 1
            reason = str(result.get("reason") or "failed")
            row["reasons"][reason] = row["reasons"].get(reason, 0) + 1
    return [rows[key] for key in sorted(rows)]


def _write_unconfirmed_sheet(
    output_dir: Path,
    name: str,
    now: dt.datetime,
    confirmation: dict,
    central: Optional[dict],
    kept: list[dict],
    claims: list[dict],
    sheet_sources: list[dict],
    dropped: list[dict],
    *,
    primary: list[str] | None = None,
    canonical: list[str] | None = None,
    grounding_details: list[dict] | None = None,
    origin_downgrades: list[dict] | None = None,
    date_anchors: list[dict] | None = None,
    attribution_traces: list[dict] | None = None,
    evidence: list[dict] | None = None,
    fact_groups: dict | None = None,
    confirmation_legacy: dict | None = None,
    confirmation_evidence: dict | None = None,
) -> Optional[Path]:
    """Persist a rejected story's sheet under ``<output_dir>/unconfirmed/``.

    Unconfirmed stories are dropped from the run, but saving the sheet lets the
    owner inspect exactly what the extractor saw and why it was not confirmed.
    The sheet carries the same diagnostics as a confirmed one -- the full
    coherence block (checked, primary/canonical entities and the reason each
    dropped claim was cut), the grounding block (each ungrounded claim and its
    reason), and a plain-text ``confirmation_reason`` -- so a rejection can be
    diagnosed without rerunning the pipeline. Failures here are logged, never
    raised -- the story still raises :class:`UnconfirmedStory`.
    """
    primary = list(primary or [])
    canonical = list(canonical or [])
    payload = {
        "id": name,
        "generated_at": now.isoformat(),
        "confirmation": confirmation,
        "confirmation_reason": _confirmation_reason(confirmation),
        "central_claim": central,
        "kinds": sorted({str(claim.get("kind")) for claim in claims if claim.get("kind")}),
        "origins": list(confirmation.get("origins") or []),
        "sources": sheet_sources,
        "claims": claims,
        # Diagnostics: which source stated what, and how close each came to
        # corroborating the central fact. Present only in unconfirmed sheets
        # (the writer never sees these keys).
        "claims_by_source": _claims_by_source(kept),
        "evidence": list(evidence or []),
        "corroboration": _corroboration_diagnostics(kept, central, entities=canonical),
        "grounding": [_grounding_entry(entry) for entry in (grounding_details or [])],
        "date_anchors": list(date_anchors or []),
        "origins_downgraded": list(origin_downgrades or []),
        "attribution_trace": list(attribution_traces or []),
        "coherence": {
            "checked": bool(primary),
            "primary_entities": primary,
            "canonical_entities": canonical,
            "dropped_claims": _coherence_dropped_details(dropped, primary),
        },
    }
    if fact_groups is not None:
        payload["fact_groups"] = fact_groups
    if confirmation_legacy is not None:
        payload["confirmation_legacy"] = confirmation_legacy
    if confirmation_evidence is not None:
        payload["confirmation_evidence"] = confirmation_evidence
    directory = Path(output_dir) / "unconfirmed"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        logger.info("story %s: wrote unconfirmed sheet -> %s", name, path)
        return path
    except OSError as exc:  # noqa: BLE001 - never block the raise on a write failure
        logger.warning("story %s: could not write unconfirmed sheet (%s)", name, exc)
        return None


def _headline_only_claim(source: dict, aliases: dict[str, str]) -> dict:
    """Build the single claim a headline-only source states in its own title.

    A source whose page and feed both held no prose (``body_status``
    ``headline_only``) still announces whatever its title says, but nothing
    around it may be trusted: the page was navigation chrome and the tiles were
    other posts' headlines. This builds that one claim in code -- no extraction
    model call -- so only the words the source itself published can enter the
    sheet.

    The claim is ``confirmed`` with a ``headline_announcement`` evidence mode
    when the title names the source's own organization as the announcer ("Acme
    announces ..."), which is the source speaking for itself; otherwise it is
    ``reported`` and carries no confirming evidence. It may only ever confirm the
    claim it states, never another source's.
    """
    title = str(source.get("title") or "").strip()
    owner = source.get("owner")
    source_name = source.get("source_name")
    names = _source_owner_names(owner, source_name, aliases)
    announces = _headline_announces_speaker(_normalize_evidence(title), names)
    return {
        "claim": title,
        "value": title,
        "source_url": source.get("link"),
        "confidence": 0.5,
        "origin": canonicalize_origin(owner or source_name, aliases),
        "affiliation": "",
        "kind": "confirmed" if announces else "reported",
        "quote": "",
        "attribution_quote": "",
        "speaker": str(owner or source_name or ""),
        "speaker_type": "organization",
        "headline_only": True,
        "evidence": {
            "ok": bool(announces),
            "attribution_mode": "headline_announcement" if announces else "none",
            "reason": None if announces else "headline-only source: no body text to verify",
        },
    }


def facts(
    gathered: dict,
    *,
    id: str | None = None,
    title: str | None = None,
    entities: list[str] | None = None,
    output_dir: str | Path | None = None,
    now: dt.datetime | None = None,
    generate: Callable[..., Generation] = default_generate,
    run_state: RunState | None = None,
    max_claims_per_source: int | None = None,
    allow_single_origin_attributed: bool | None = None,
    max_sources: int | None = None,
    model_call_budget: int | None = None,
    **kwargs: Any,
) -> dict:
    """Extract claims from gathered text, enforce confirmation, save the sheet.

    Confirmation is assessed after extraction because it now depends on the
    claims' origins (who produced each claim), not just on the outlets. A story
    with no tier-1 source is not rejected before the model runs: a single source
    can still carry two independent origins. Only the claim that restates the
    seed headline (``title``) is confirmed; other claims' origins never count.

    Extraction runs once per source, so ``max_sources`` caps how many sources a
    single candidate may use (chosen by tier, then one per distinct owner, then
    by text length); ``model_call_budget`` stops extraction before it would
    exceed the run's model-call budget. Both default to ``score`` settings in
    config.yaml.
    """
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    name = re.sub(r"[^0-9A-Za-z._-]", "-", str(id or gathered.get("id") or "unnamed"))
    now = now or dt.datetime.now(dt.timezone.utc)
    sources = gathered.get("sources") or []

    config = load_config()
    config_facts = config.get("facts") or {}
    config_max = int(config_facts.get("max_claims_per_source", MAX_CLAIMS_PER_SOURCE))
    max_claims = max_claims_per_source if max_claims_per_source is not None else config_max
    if allow_single_origin_attributed is None:
        allow_single = bool(
            config_facts.get(
                "allow_single_origin_attributed", DEFAULT_ALLOW_SINGLE_ORIGIN_ATTRIBUTED
            )
        )
    else:
        allow_single = bool(allow_single_origin_attributed)
    if max_sources is None:
        config_score = config.get("score") or {}
        try:
            max_sources = int(
                config_score.get("max_sources_per_candidate", DEFAULT_MAX_SOURCES_PER_CANDIDATE)
            )
        except (TypeError, ValueError):
            max_sources = DEFAULT_MAX_SOURCES_PER_CANDIDATE

    aliases = load_origin_aliases()
    confirmation_mode = _confirmation_mode()
    min_first_party = _min_first_party_sources()
    fingerprint = cluster.stage_fingerprint(
        name, "facts", PROMPT_VERSION, SYSTEM_INSTRUCTION, CLAIM_SCHEMA,
        _extraction_chain(), MAX_SOURCE_CHARS, MAX_FIELD_CHARS, max_claims,
        allow_single, HEDGE_WORDS, sorted(aliases.items()), _relative_time_words(),
        max_sources,
        # The grouping prompt/schema and the confirmation mode decide the sheet's
        # grouping and verdict blocks, so a change in either re-extracts.
        confirmation_mode, min_first_party,
        GROUPING_INSTRUCTION, GROUPING_SCHEMA, GROUPING_LABEL_CHARS,
    )
    path = output_dir / f"{name}.json"
    if cluster.stored_fingerprint(path) == fingerprint:
        payload = json.loads(path.read_text(encoding="utf-8"))
        logger.info("facts reused cached sheet %s (fingerprint match)", path)
        return payload

    selected_sources = _select_sources(sources, max_sources)
    # A headline-only source (page and feed both held no prose) never reaches the
    # extraction model: the words around it are navigation chrome and other
    # posts' tile headlines. It contributes exactly the one claim its own title
    # states, built in code below.
    headline_only_sources = [
        source
        for source in selected_sources
        if str(source.get("body_status") or "") == "headline_only"
    ]
    extractable_sources = [
        source
        for source in selected_sources
        if str(source.get("body_status") or "") != "headline_only"
    ]
    extracted: list[dict] = []
    generation = None
    # The families that served extraction, so the grouping call can prefer a
    # different family (the grouper must not simply agree with the extractor).
    extractor_families: set[str] = set()
    # True when the model-call budget stopped extraction before every selected
    # source was read, so a later "unconfirmed" verdict reflects the budget
    # rather than the story's coverage.
    budget_truncated = False
    for position, source in enumerate(extractable_sources):
        text = str(source.get("text") or "")
        if not text.strip():
            logger.info("story %s: source %s has no text; nothing to extract", name, source.get("link"))
            continue
        if (
            run_state is not None
            and model_call_budget is not None
            and run_state.calls >= model_call_budget
        ):
            budget_truncated = position < len(extractable_sources)
            logger.warning(
                "story %s: model-call budget of %d reached; skipping remaining sources",
                name, model_call_budget,
            )
            break
        generation = extract_claims(
            name, source, max_claims=max_claims, generate=generate, run_state=run_state, **kwargs
        )
        extractor_families.add(str(generation.family))
        # The extraction call's own URL is authoritative: the model is asked to
        # echo it, but a misspelled or truncated URL must never reach the sheet.
        part = validate_claims(generation.value, source_url=source.get("link"))

        if len(part) > max_claims:
            logger.warning(
                "story %s: source %s returned %d claim(s), keeping the first %d",
                name, source.get("link"), len(part), max_claims,
            )
            part = part[:max_claims]
        logger.info("story %s: source %s extracted %d claim(s)", name, source.get("link"), len(part))
        if len(text) > WARN_LOW_SOURCE_CHARS and len(part) < WARN_MIN_CLAIMS:
            logger.warning(
                "story %s: source %s has %d chars but yielded only %d claim(s); "
                "most of what it says may be missing",
                name, source.get("link"), len(text), len(part),
            )
        extracted.extend(part)
    if generation is None and not headline_only_sources:
        raise FactsError(f"story {name}: none of the sources had extractable text")
    if headline_only_sources:
        logger.info(
            "story %s: %d headline-only source(s) contribute one title claim each",
            name, len(headline_only_sources),
        )

    kept, dropped, primary = coherence_check(
        extracted,
        sources,
        title=title,
        entities=entities,
        story_id=name,
    )
    if dropped:
        logger.warning("story %s: dropped %d off-topic claim(s) from off-topic sources", name, len(dropped))

    # A claim's value must stay inside its own source text: figures, names and
    # absolute qualifiers the source never states are dropped before merging.
    kept, ungrounded_details = ground_claims_details(kept, sources, story_id=name)
    ungrounded = [entry["claim"] for entry in ungrounded_details]
    if ungrounded:
        logger.warning("story %s: dropped %d ungrounded claim(s)", name, len(ungrounded))

    # Relative-time values are converted to absolute dates using each source's
    # anchor (its URL date, else the feed date); a relative claim with no date to
    # convert from is dropped, and a source whose two anchors differ by more than
    # a day is too ambiguous to date from at all.
    kept, _relative_dropped, date_anchors, ambiguous_details = _absolutize_claims(
        kept, sources, story_id=name
    )
    # The sheet's grounding block lists every claim cut for going beyond its
    # source, including relative-time claims dropped for an ambiguous date.
    grounding_details = list(ungrounded_details) + list(ambiguous_details)

    # One canonical entity per concept, for SEO and hub pages; the flat
    # primary-entity set is the matching surface used by the coherence check.
    # Computed before origins so a game or character named as an origin is
    # recognized as the story's subject, not a speaker.
    canonical = canonical_entities(
        title=title,
        source_titles=[str(source.get("title") or "") for source in sources],
        entities=entities,
    )

    # Origins are canonicalized and verified against each claim's own source
    # text before anything is counted or stored; a product/title is not an origin.
    origin_downgrades: list[dict] = []
    attribution_traces: list[dict] = []
    kept = resolve_claim_origins(
        kept,
        sources,
        aliases=aliases,
        story_id=name,
        entities=canonical,
        downgrades=origin_downgrades,
        attribution_traces=attribution_traces,
    )

    # A headline-only source contributes exactly the claim its own title states.
    # It is built here, after grounding, coherence and origin resolution, so the
    # title's words are never dropped for being absent from a body that never
    # existed and its origin (the source's own owner) is never downgraded against
    # that empty text. It still merges with any same-fact claim from a source
    # with a body.
    headline_claims = [
        _headline_only_claim(source, aliases) for source in headline_only_sources
    ]
    if headline_claims:
        kept = kept + headline_claims

    # Evidence diagnostics: each claim's stored quote and attribution are checked
    # verbatim against its own source text, and its speaker must be grounded,
    # non-anonymous and not a product or the outlet itself. This is recorded on
    # the claim (tagged with its source URL) and tallied per source below; it is
    # a diagnostic only and does not affect confirmation yet. A headline-only
    # claim already carries its own evidence (there is no text to check it
    # against), so it is left as built.
    sources_by_url = _sources_by_url(sources)
    for claim in kept:
        if claim.get("headline_only"):
            continue
        source = sources_by_url.get(_source_url_key(claim.get("source_url"))) or {}
        claim["evidence"] = _evidence_for_claim(
            claim, source, aliases=aliases, entities=canonical
        )
    evidence_summary = _evidence_summary(kept)

    tier1_present = any(int(source.get("tier") or 0) == 1 for source in sources)

    # Same facts merge before the central claim is picked: claims from different
    # sources whose values state no contradicting figure or date, that share a
    # primary entity and overlapping content words, and whose labels agree on
    # their dates and numbers. The merged central claim of a cluster with a
    # tier-1 source takes that source's own wording.
    groups = _claim_groups(kept, entities=canonical)
    preferred_central = _preferred_central_index(
        groups, sources, title, canonical=canonical
    )
    if preferred_central is not None:
        prefer_index, prefer_url = preferred_central
        claims = [
            _merge_group(group, prefer_url=prefer_url)
            if index == prefer_index
            else _merge_group(group)
            for index, group in enumerate(groups)
        ]
        central = claims[prefer_index]
    else:
        claims = [_merge_group(group) for group in groups]
        central = central_claim(claims, title=title)

    # The cluster seed headline (the cluster title) is restated by one claim;
    # mark that claim central and confirm only it, so origins supporting any
    # other claim never confirm the story.
    for claim in claims:
        claim["central"] = claim is central
    central_group = groups[claims.index(central)] if central is not None else None
    if title and central is None:
        logger.warning(
            "story %s: no extracted claim overlaps the seed headline %r; "
            "confirmation falls back to all claims",
            name, title,
        )

    # One grouping call tells which outlets restated one fact. It runs in the
    # shadow/evidence modes only (legacy decides from origins alone) and always
    # after extraction, on the pre-merge claims, so each source's own claim stays
    # its own member and counts its own owner. Failure here never fails the
    # story: the record keeps the reason and every claim stands alone.
    grouping: dict | None = None
    if confirmation_mode != "legacy":
        grouping = group_facts(
            kept,
            sources,
            generate=generate,
            run_state=run_state,
            model_call_budget=model_call_budget,
            exclude_families=extractor_families,
            entities=canonical,
        )

    # Other sources whose own wording restates the central fact corroborate it:
    # their claim is recorded as a corroborated version of the central claim
    # (keeping its own value, origin and kind) and counts as that source stating
    # the central fact. Corroboration is additive -- the claim stays its own
    # merged entry in ``claims``. A claim contradicting the central fact's
    # figures or dates, or reaching too little shared wording, never corroborates.
    corroborating = (
        corroborating_claims(
            kept,
            central,
            exclude=central_group,
            entities=canonical,
        )
        if central is not None
        else []
    )
    if corroborating:
        central.setdefault("versions", []).extend(
            {
                "source_url": claim.get("source_url"),
                "value": str(claim.get("value")),
                "origin": str(claim.get("origin") or UNSTATED_ORIGIN),
                "kind": str(claim.get("kind")),
                "corroborated": True,
            }
            for _score, claim in corroborating
        )

    # Confirmation uses the pre-merge claims of the central fact only: it is
    # about who produced that claim, so three outlets repeating one origin still
    # collapse to one origin, two origins from the same article never count as
    # two, and other claims' origins never count. Corroborating claims are the
    # exception: they explicitly restate the central fact, so their origins count.
    legacy_confirmation = confirmation_from_sources(
        sources,
        claims=central_group if central_group is not None else kept,
        merged=central,
        allow_single_origin_attributed=allow_single,
        aliases=aliases,
        entities=canonical,
        corroborating=[claim for _score, claim in corroborating],
    )
    # A tier-1 source that only mentions the topic in passing does not confirm
    # the story: it is the headline's fact the story must establish, not any
    # tier-1 sentence. Two outlets' agreement never stands in for it.
    if tier1_present and title and preferred_central is None:
        legacy_confirmation = dict(legacy_confirmation)
        legacy_confirmation.update(
            confirmed=False,
            rule="unconfirmed",
            reason="tier-1 source does not state the headline claim",
        )
    tier1_confirms = (
        bool(legacy_confirmation["confirmed"])
        and legacy_confirmation["rule"] == "tier1"
    )

    # The evidence verdict judges the same central fact from the evidence stored
    # on the central group's claims: its verdict is what the sheet carries in the
    # evidence mode, and is stored beside the legacy verdict in the shadow mode.
    evidence_conf = None
    if confirmation_mode != "legacy" and grouping is not None:
        evidence_conf = evidence_confirmation(
            evidence_central_claims(grouping, central_group),
            sources_by_url,
            tier1_confirms=tier1_confirms,
            aliases=aliases,
            min_first_party=min_first_party,
        )

    # legacy keeps deciding in legacy and shadow; evidence decides in evidence.
    confirmation = evidence_conf if confirmation_mode == "evidence" else legacy_confirmation
    confirmation_legacy = None
    confirmation_evidence = None
    if confirmation_mode == "shadow":
        confirmation_legacy = legacy_confirmation
        confirmation_evidence = evidence_conf
        logger.info(
            "story %s: confirmation legacy=%s(%s) evidence=%s(%s)%s",
            name,
            legacy_confirmation.get("rule"),
            legacy_confirmation.get("confirmed"),
            (evidence_conf or {}).get("rule"),
            (evidence_conf or {}).get("confirmed"),
            " DISAGREE"
            if evidence_conf is not None
            and evidence_conf.get("confirmed") != legacy_confirmation.get("confirmed")
            else "",
        )
    fact_groups = fact_groups_sheet(grouping) if grouping is not None else None

    # Only sources that contributed at least one claim are listed in the sheet;
    # a source that yielded no claim is never cited by the article.
    claimed_urls = {_source_url_key(url) for claim in claims for url in claim["source_urls"]}
    sheet_sources = [
        {
            key: source.get(key)
            for key in ("source_name", "link", "title", "tier", "owner", "region")
        }
        for source in sources
        if _source_url_key(source.get("link")) in claimed_urls
    ]

    if not confirmation["confirmed"]:
        message = (
            f"story {name}: needs one tier-1 source stating the headline claim "
            f"or two independent origins supporting it; got {confirmation}"
        )
        logger.warning("%s", message)
        _write_unconfirmed_sheet(
            output_dir, name, now, confirmation, central, kept, claims, sheet_sources, dropped,
            primary=primary, canonical=canonical, grounding_details=grounding_details,
            origin_downgrades=origin_downgrades, date_anchors=date_anchors,
            attribution_traces=attribution_traces, evidence=evidence_summary,
            fact_groups=fact_groups,
            confirmation_legacy=confirmation_legacy,
            confirmation_evidence=confirmation_evidence,
        )
        raise UnconfirmedStory(message, budget_truncated=budget_truncated)

    payload = {
        "id": name,
        "generated_at": now.isoformat(),
        "confirmation": confirmation,
        "confirmation_reason": _confirmation_reason(confirmation),
        "sources": sheet_sources,
        "claims": claims,
        "evidence": evidence_summary,
        "grounding": [_grounding_entry(entry) for entry in grounding_details],
        "date_anchors": date_anchors,
        "origins_downgraded": origin_downgrades,
        "attribution_trace": attribution_traces,
        "coherence": {
            "checked": bool(primary),
            "primary_entities": primary,
            "canonical_entities": canonical,
            "dropped_claims": _coherence_dropped_details(dropped, primary),
        },
        "extractor": (
            {
                "provider": generation.provider,
                "model": generation.model,
                "family": generation.family,
            }
            if generation is not None
            else None
        ),
        "fingerprint": {"format": cluster.FINGERPRINT_FORMAT, "value": fingerprint},
    }
    if fact_groups is not None:
        payload["fact_groups"] = fact_groups
    if confirmation_legacy is not None:
        payload["confirmation_legacy"] = confirmation_legacy
    if confirmation_evidence is not None:
        payload["confirmation_evidence"] = confirmation_evidence
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info(
        "facts complete: %d claim(s), %d source(s) -> %s",
        len(claims), len(sheet_sources), path,
    )
    return payload


__all__ = [
    "ALLOWED_KINDS",
    "CLAIM_SCHEMA",
    "CONFIRMING_KINDS",
    "CONFIRMATION_MODES",
    "DEFAULT_CONFIRMATION_MODE",
    "DEFAULT_ORIGIN_ALIASES_PATH",
    "DEFAULT_ORGS_PATH",
    "DEFAULT_RELATIVE_TIME_WORDS",
    "FIRST_PARTY_KINDS",
    "GROUPING_INSTRUCTION",
    "GROUPING_LABEL_CHARS",
    "GROUPING_SCHEMA",
    "HEDGE_WORDS",
    "KIND_STRENGTH",
    "MAX_ATTRIBUTION_QUOTE_CHARS",
    "MAX_FIELD_CHARS",
    "MAX_CLAIMS_PER_SOURCE",
    "MAX_QUOTE_CHARS",
    "PROMPT_VERSION",
    "SPEAKER_TYPES",
    "UNSTATED_ORIGIN",
    "FactsError",
    "UnconfirmedStory",
    "absolutize_relative_time",
    "apply_group_guards",
    "build_grouping_prompt",
    "build_prompt",
    "canonicalize_origin",
    "central_claim",
    "coherence_check",
    "canonical_entities",
    "entity_aliases",
    "evidence_confirmation",
    "extract_claims",
    "fact_groups_sheet",
    "group_facts",
    "group_reason",
    "grouping_items",
    "ground_claims",
    "ground_claims_details",
    "grounding_failure",
    "load_origin_aliases",
    "load_organizations",
    "merge_claims",
    "parse_groups",
    "primary_entities",
    "resolve_claim_origins",
    "validate_claims",
    "verify_evidence",
    "confirmation_from_sources",
    "facts",
]