"""SEO stage: draft search metadata for an article with the 'fast' role.

The "fast" model family drafts the wording (title, description, slug, category,
tags, canonical entities and cover-image alt text); **code owns the SEO**. Every
value is validated here and the model never writes HTML meta tags -- the site
layout and JSON-LD are generated in code (PLAN.md principle 2 and section 10).

Validation is code-only and independent of the model:

* ``title`` <= 65 chars, ``description`` 70-160 chars, ``slug`` <= 60 chars and
  a lowercase ASCII hyphenated token, ``category`` one of the site's categories.
* **No invented values.** The verifier's independent number/price/date/spec
  check (:func:`verify.extract_values`) is run over the title, description and
  alt text; a value that is not in the facts sheet -- or that the article body
  does not carry -- is a failure.
* **No unconfirmed claims.** A value the sheet marks as a rumor (``kind``
  ``"rumor"``) must not appear in the title or description.
* **No causal or contrast connectives.** A phrase that implies a cause, reason
  or contrast (``in contrast``, ``because``, ``driven by``, ``thanks to``,
  ``as a result``) is rejected in the title and description: code cannot prove
  the relationship from the facts, so the search snippet may not assert it.
* **No overlap rewritten as an origin.** A percentage paired with an origin
  phrase (``came from``, ``come from``) fails, because it turns the fact's
  stated overlap into an unsupported origin claim.

Formatting is fixed in code, not by the model. The slug is built here from the
final title (transliterated, hyphenated, cut to :data:`MAX_SLUG` at a word
boundary and de-duplicated) and tags and entities are normalized here before
validation. The model is asked again only for problems code cannot fix (title or
description length or content, or invented/unconfirmed values); if that second
answer also fails the story is dropped with :class:`SeoError`. Every field that
code rewrites is logged.

Entity names are canonicalized against ``data/entities.json`` (alias ``PS5`` ->
``PlayStation 5``) so the same product never appears under two names. A new
entity is added to that file only when the facts sheet supports it.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable

import verify
from json_schema import schema_errors
from providers import (
    PROJECT_ROOT,
    Generation,
    RunState,
    generate as default_generate,
)

DEFAULT_ENTITIES_PATH = PROJECT_ROOT / "data" / "entities.json"
DEFAULT_ARTICLES_DIR = PROJECT_ROOT / "src" / "content" / "articles"
DEFAULT_DRAFTS_DIR = PROJECT_ROOT / "drafts"

MAX_TITLE = 65
MIN_DESC = 70
MAX_DESC = 160
MAX_SLUG = 60
MAX_ALT = 200
MIN_TAGS = 3
MAX_TAGS = 6

# Mirrors src/data/categories.ts (the source of truth re-exported by
# src/content.config.ts). test_seo.py asserts the two lists stay in sync.
ALLOWED_CATEGORIES = (
    "gaming-news",
    "pc",
    "playstation",
    "xbox",
    "nintendo",
    "hardware",
    "tech",
    "esports",
    "india",
)

# Hype/filler the writer prompt bans; the same ban applies to search snippets.
BANNED_PHRASES = (
    "in today's fast-paced world",
    "game-changing",
    "revolutionary",
    "you won't believe",
    "shocking",
    "needless to say",
    "it's worth noting",
    "read on",
    "gamers everywhere",
)

# Filler and internal terms from the writer rules that must not appear in the
# search snippet (title and description). "facts sheet" appears here because it
# is a pipeline term, not reader language.
SNIPPET_BANNED_PHRASES = (
    "unlocks additional gear and opportunities",
    "mid-tier",
    "low confidence",
    "high confidence",
    "facts sheet",
)

# Connectives that link two facts as cause, reason or contrast. The writer may
# only use one when a numbered fact states that relationship, which a search
# snippet cannot prove, so the title and description must not use them at all.
CAUSAL_PHRASES = (
    "in contrast",
    "because",
    "driven by",
    "thanks to",
    "as a result",
)

# A percentage sorted with an origin phrase rewrites an overlap as "where it
# came from", which the writer rules forbid. A percentage in the same snippet as
# one of these phrases fails (PLAN.md principle 6 applied to search text).
OVERLAP_ORIGIN_PHRASES = (
    "came from",
    "come from",
)

# A percentage as it appears in reader text, used by the overlap-origin check.
_PERCENT_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s?%")

logger = logging.getLogger("gamersxpress.pipeline.seo")

SEO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "slug": {"type": "string"},
        "category": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "imageAlt": {"type": "string"},
    },
    "required": ["title", "description", "slug", "category", "tags", "entities", "imageAlt"],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTION = (
    "You write search metadata for one GamersXpress article. You receive the "
    "verified facts sheet and the article body. Reply with JSON only, no prose "
    "and no code fences, with exactly these fields: title, description, slug, "
    "category, tags, entities, imageAlt.\n"
    f"- title: at most {MAX_TITLE} characters, says what happened, no clickbait, "
    "no ALL CAPS, no exclamation marks, no trailing period.\n"
    f"- description: {MIN_DESC}-{MAX_DESC} characters, one or two plain sentences, one line.\n"
    f"- slug: lowercase ASCII words separated by hyphens, at most {MAX_SLUG} characters.\n"
    "- category: exactly one of the allowed categories listed below.\n"
    "- tags: 3 to 6 short lowercase tags from the facts sheet.\n"
    "- entities: organization, product or game names that appear in the facts sheet.\n"
    "- imageAlt: one honest sentence describing the cover image (its headline and category).\n"
    "Never invent a number, price, date, percentage, spec or version: use only "
    "values that appear in the facts sheet, and never state a rumor as fact. "
    "Every sentence must be supported by a claim in the facts sheet; omit "
    "anything unsupported rather than padding, and never write filler such as "
    "'unlocks additional gear and opportunities'. Never describe a cost as "
    "something players receive: a price is what players pay. Never invent "
    "labels such as 'mid-tier'. Never print confidence numbers or words like "
    "'low confidence'. Never use internal terms such as 'facts sheet' in the "
    "title or description; when a detail is not in the sheet, write 'no other "
    "details are confirmed'. Name in-game currencies wherever an amount "
    "appears: '100,000 in-game DMZ Cash', never a bare dollar figure. "
    "Attribute an estimate to its origin ('according to estimates from X') and "
    "a reported claim to the outlet that reported it ('Eurogamer reports'); "
    "when the origin is unstated, attribute to the outlet. Never state an "
    "estimate or a reported claim as fact. Never link facts with a causal or "
    "contrast connective ('in contrast', 'because', 'driven by', 'thanks to', "
    "'as a result') unless the facts sheet states that relationship, and never "
    "rewrite a percentage or overlap as an origin ('came from'). The "
    "slug is generated in code from the final title; do not copy a source "
    "headline."
)

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SeoError(Exception):
    """The model could not produce metadata that passed code validation."""


# --- entities ----------------------------------------------------------------


def load_entities(path: str | Path | None = None) -> list[dict]:
    """Return the canonical entity list, or an empty list when the file is absent."""
    path = Path(path) if path else DEFAULT_ENTITIES_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    entries = raw.get("entities", raw) if isinstance(raw, dict) else raw
    entities: list[dict] = []
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("canonical"):
            aliases = [str(a) for a in entry.get("aliases") or [] if str(a).strip()]
            entities.append({"canonical": str(entry["canonical"]), "aliases": aliases})
    return entities


def save_entities(entities: list[dict], path: str | Path | None = None) -> Path:
    path = Path(path) if path else DEFAULT_ENTITIES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"entities": [{"canonical": e["canonical"], "aliases": e.get("aliases", [])} for e in entities]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _alias_index(entities: Iterable[dict]) -> dict[str, str]:
    index: dict[str, str] = {}
    for entity in entities:
        canonical = entity["canonical"]
        index[canonical.lower()] = canonical
        for alias in entity.get("aliases", []):
            index[alias.lower()] = canonical
    return index


def canonicalize_entities(
    names: Iterable[str],
    facts: dict,
    entities: list[dict],
) -> tuple[list[str], list[dict]]:
    """Map model entity names to canonical names.

    Known names and aliases are canonicalized. An unknown name is kept only when
    the facts sheet supports it; the new entity is returned separately so the
    caller can persist it only after the whole result validates.
    """
    index = _alias_index(entities)
    facts_text = _facts_text(facts).lower()
    out: list[str] = []
    new_entities: list[dict] = []
    known = {entity["canonical"] for entity in entities}
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        canonical = index.get(name.lower())
        if canonical is None:
            if name.lower() not in facts_text:
                logger.info("dropping entity %r: not supported by the facts sheet", name)
                continue
            canonical = name
            if canonical not in known:
                new_entities.append({"canonical": canonical, "aliases": []})
                known.add(canonical)
        if canonical not in out:
            out.append(canonical)
    return out, new_entities


def canonicalize_text(text: str, entities: list[dict]) -> str:
    """Rewrite aliases in ``text`` to their canonical entity names."""
    for canonical, aliases in ((e["canonical"], e.get("aliases", [])) for e in entities):
        candidates = sorted({canonical, *aliases}, key=len, reverse=True)
        for candidate in candidates:
            if not candidate:
                continue
            pattern = re.compile(rf"(?<![\w-]){re.escape(candidate)}(?![\w-])", re.IGNORECASE)
            text = pattern.sub(canonical, text)
    return text


# --- slugs -------------------------------------------------------------------


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()


def existing_slugs(articles_dir: str | Path | None = None, drafts_dir: str | Path | None = None) -> set[str]:
    """Slugs already used by published articles and drafts (missing dirs are empty)."""
    slugs: set[str] = set()
    for directory in (articles_dir or DEFAULT_ARTICLES_DIR, drafts_dir or DEFAULT_DRAFTS_DIR):
        path = Path(directory)
        if not path.is_dir():
            continue
        for item in path.glob("*.md"):
            slugs.add(item.stem.lower())
    return slugs


def truncate_slug(slug: str, limit: int = MAX_SLUG) -> str:
    """Cut ``slug`` to at most ``limit`` characters at a word boundary.

    A single word longer than ``limit`` cannot be cut at a hyphen, so it is cut
    hard. The result is always a valid lowercase hyphenated slug (or empty).
    """
    slug = slug.strip("-")
    if len(slug) <= limit:
        return slug
    cut = slug[:limit]
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut.strip("-")


def unique_slug(base: str, existing: set[str]) -> str:
    """Return a slug that is not already in ``existing`` and is <= :data:`MAX_SLUG`.

    The slug is built from ``base`` (the final title): ASCII, lowercase and
    hyphen-separated, then cut at a word boundary so a long title never exceeds
    the length limit. A collision gets a ``-2``, ``-3`` ... suffix.
    """
    slug = truncate_slug(slugify(base)) or "article"
    if slug not in existing:
        return slug
    counter = 2
    while True:
        suffix = f"-{counter}"
        stem = truncate_slug(slug, MAX_SLUG - len(suffix)) or "article"
        candidate = f"{stem}{suffix}"
        if candidate not in existing:
            return candidate
        counter += 1


def normalize_tags(tags: Any, *, limit: int = MAX_TAGS) -> list[str]:
    """Normalize tags to the lowercase hyphenated tag format.

    The model often returns human labels such as ``"game pass"``. Code trims,
    lowercases, hyphenates, drops empties, dedupes and caps the count so a
    formatting slip never costs a model retry.
    """
    if isinstance(tags, str):
        raw: Iterable[Any] = [tags]
    elif isinstance(tags, (list, tuple)):
        raw = tags
    else:
        return []
    out: list[str] = []
    for item in raw:
        tag = slugify(item)
        if not tag or tag in out:
            continue
        out.append(tag)
    if limit and len(out) > limit:
        out = out[:limit]
    return out


# --- prompt ------------------------------------------------------------------


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


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


def _front_matter_body(article: Any) -> str:
    match = re.match(r"^---\r?\n.*?\r?\n---\r?\n?(.*)$", _as_text(article), re.DOTALL)
    return match.group(1) if match else _as_text(article)


def _facts_text(facts: dict) -> str:
    parts: list[str] = []
    for claim in facts.get("claims") or []:
        parts.append(str(claim.get("claim", "")))
        parts.append(str(claim.get("value", "")))
    return "\n".join(parts)


def build_prompt(
    article: Any,
    facts: Any,
    *,
    entities: list[dict] | None = None,
    errors: list[str] | None = None,
) -> str:
    sheet = facts if isinstance(facts, str) else json.dumps(facts, indent=2, ensure_ascii=False)
    known = ", ".join(entity["canonical"] for entity in (entities or []))
    parts = [
        SYSTEM_INSTRUCTION,
        "",
        "Allowed categories: " + ", ".join(ALLOWED_CATEGORIES) + ".",
        "Known entities (prefer these exact names): " + (known or "(none)"),
        "",
        "Article body:",
        _front_matter_body(article),
        "",
        "Facts sheet:",
        sheet,
        "",
    ]
    if errors:
        parts.append("Your previous answer was invalid for these reasons:")
        parts.extend(f"- {error}" for error in errors)
        parts.append("Fix every problem and reply with valid JSON only.")
        parts.append("")
    parts.append("JSON:")
    return "\n".join(parts)


# --- validation --------------------------------------------------------------


def _coerce(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    return None
        return None
    return None


def _mask_entities(text: str, entities: list[dict]) -> str:
    """Remove entity names so digits inside product names (PS5, RTX 5090) are not read as facts."""
    for entity in entities:
        for candidate in {entity["canonical"], *entity.get("aliases", [])}:
            if candidate:
                text = re.sub(re.escape(candidate), " ", text, flags=re.IGNORECASE)
    return text


def _values_in(text: str, config: dict, entities: list[dict]) -> list[dict]:
    return verify.extract_values(_mask_entities(text, entities), config)


_DOLLAR_RE = re.compile(r"(?<![\w])(?:US\$|USD|\$)\s?\d", re.IGNORECASE)
_IN_GAME_VALUE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9 ]*?(?:Cash|Credits|Coins|Points|Gold|Tokens|Bucks))\b"
)


def _in_game_currency_names(facts: dict) -> list[str]:
    """Currency names tied to in-game amounts in the sheet's claim values.

    A claim value that mentions in-game currency (for example "100,000 in-game
    DMZ Cash") names a currency that any matching dollar figure in the snippet
    must carry (rule: name in-game currencies everywhere).
    """
    names: set[str] = set()
    for claim in facts.get("claims") or []:
        value = str(claim.get("value", "") or "")
        if "in-game" not in value.lower():
            continue
        for match in _IN_GAME_VALUE_RE.finditer(value):
            names.add(re.sub(r"\s+", " ", match.group(1)).strip())
    return sorted(name for name in names if name)


def validate(
    data: dict,
    *,
    article: Any,
    facts: dict,
    entities: list[dict],
    config: dict | None = None,
) -> list[str]:
    """Return every code-validation error for ``data`` (empty list means valid)."""
    config = config if config is not None else verify.load_verify_config()
    errors: list[str] = []

    title = data.get("title", "")
    description = data.get("description", "")
    slug = data.get("slug", "")
    category = data.get("category", "")
    tags = data.get("tags", [])
    alt = data.get("imageAlt", "")

    if not isinstance(title, str) or not title.strip():
        errors.append("title is empty")
    else:
        if len(title) > MAX_TITLE:
            errors.append(f"title is {len(title)} characters (max {MAX_TITLE})")
        if title.rstrip().endswith("."):
            errors.append("title must not end with a period")
        if "!" in title:
            errors.append("title must not contain an exclamation mark")
        if any(ch.isalpha() for ch in title) and title == title.upper():
            errors.append("title must not be ALL CAPS")

    snippet = f"{title}\n{description}".lower()
    for phrase in BANNED_PHRASES + SNIPPET_BANNED_PHRASES:
        if phrase in snippet:
            errors.append(f"title or description contains banned phrase: {phrase!r}")

    # Causal and contrast connectives are banned from the snippet: code cannot
    # prove the relationship from the facts, so the search text must not assert
    # it (writer rule: no causal or contrast connectives).
    for phrase in CAUSAL_PHRASES:
        if phrase in snippet:
            errors.append(f"title or description uses a causal or contrast phrase: {phrase!r}")

    # A percentage paired with an origin phrase rewrites a stated overlap as
    # "where it came from" (writer rule: never turn an overlap into an origin).
    if _PERCENT_RE.search(f"{title}\n{description}"):
        for phrase in OVERLAP_ORIGIN_PHRASES:
            if phrase in snippet:
                errors.append(
                    f"title or description turns a percentage into an origin phrase: {phrase!r}"
                )

    if not isinstance(description, str):
        errors.append("description is not a string")
    else:
        if len(description) < MIN_DESC or len(description) > MAX_DESC:
            errors.append(f"description is {len(description)} characters (need {MIN_DESC}-{MAX_DESC})")
        if "\n" in description:
            errors.append("description must be a single line")

    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug or ""):
        errors.append(f"slug must be lowercase ASCII words separated by hyphens: {slug!r}")
    elif len(slug) > MAX_SLUG:
        errors.append(f"slug is {len(slug)} characters (max {MAX_SLUG})")

    if category not in ALLOWED_CATEGORIES:
        errors.append(f"category must be one of {', '.join(ALLOWED_CATEGORIES)}: {category!r}")

    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        errors.append("tags must be a list of strings")
    elif not MIN_TAGS <= len(tags) <= MAX_TAGS:
        errors.append(f"tags must have {MIN_TAGS} to {MAX_TAGS} entries, got {len(tags)}")
    else:
        for tag in tags:
            if not _TAG_RE.fullmatch(tag):
                errors.append(f"tag must be a lowercase hyphenated slug: {tag!r}")

    if not isinstance(alt, str) or not alt.strip():
        errors.append("imageAlt is empty")
    else:
        if len(alt) > MAX_ALT:
            errors.append(f"imageAlt is {len(alt)} characters (max {MAX_ALT})")
        if "\n" in alt:
            errors.append("imageAlt must be a single line")

    # No invented values: run the verifier's independent number check on the
    # generated text, against both the facts sheet and the article body.
    body = _front_matter_body(article)
    facts_tokens = {value["token"] for value in _values_in(_facts_text(facts), config, entities)}
    body_tokens = {value["token"] for value in _values_in(body, config, entities)}
    for field in ("title", "description", "imageAlt"):
        for value in _values_in(data.get(field, "") or "", config, entities):
            if value["token"] not in facts_tokens:
                errors.append(f"{field} contains a value not in the facts sheet: {value['raw']!r}")
            if value["token"] not in body_tokens:
                errors.append(f"{field} mentions {value['raw']!r} but the article body does not")

    # No unconfirmed claims: a rumor's value must not appear in the snippet.
    title_desc = f"{title}\n{description}".lower()
    for claim in facts.get("claims") or []:
        value = str(claim.get("value", "")).strip()
        if claim.get("kind") == "rumor" and len(value) >= 3 and value.lower() in title_desc:
            errors.append(f"title or description states a rumor as fact: {value!r}")

    # In-game currency amounts must name the currency: a bare dollar figure in
    # the snippet that the sheet ties to an in-game currency fails.
    currency_names = _in_game_currency_names(facts)
    for field in ("title", "description"):
        text = str(data.get(field) or "")
        if not _DOLLAR_RE.search(text):
            continue
        for name in currency_names:
            if name.lower() not in text.lower():
                errors.append(
                    f"{field} uses a dollar amount for the in-game currency {name!r} without naming it"
                )

    return errors


# --- stage -------------------------------------------------------------------


def generate_seo(
    article: Any,
    facts: Any,
    *,
    run_state: RunState | None = None,
    generate: Callable[..., Generation] = default_generate,
    prompt: str | None = None,
    entities_path: str | Path | None = None,
    articles_dir: str | Path | None = None,
    drafts_dir: str | Path | None = None,
    config: dict | None = None,
    **kwargs: Any,
) -> dict:
    """Draft and validate SEO metadata, retrying the model once on failure.

    Returns a dict with ``title``, ``description``, ``slug``, ``category``,
    ``tags``, ``entities`` (canonical) and ``imageAlt``, plus a ``generator``
    block recording the provider/model/family that served the call. Raises
    :class:`SeoError` when both attempts fail validation.
    """
    sheet = _load_facts(facts)
    entities = load_entities(entities_path)
    existing = existing_slugs(articles_dir, drafts_dir)
    config = config if config is not None else verify.load_verify_config()

    errors: list[str] = []
    for attempt in range(2):
        if attempt == 0 and prompt is not None:
            current_prompt = prompt
        else:
            current_prompt = build_prompt(article, sheet, entities=entities, errors=errors or None)
        generation = generate(
            "fast", current_prompt, json_schema=SEO_SCHEMA, run_state=run_state, **kwargs
        )
        data = _coerce(generation.value)
        if not isinstance(data, dict):
            errors = ["model output is not a JSON object"]
            continue
        schema_errs = schema_errors(data, SEO_SCHEMA)
        if schema_errs:
            errors = list(schema_errs)
            continue

        data = dict(data)
        for field in ("title", "description", "imageAlt"):
            if isinstance(data.get(field), str):
                normalized = canonicalize_text(data[field], entities)
                if normalized != data[field]:
                    logger.info("seo normalized %s entity names: %r -> %r", field, data[field], normalized)
                data[field] = normalized

        # Build the slug in code from the final title, cut to length and made
        # unique here so a long title or a collision never costs a model retry.
        # The model's slug and any source headline are never used.
        raw_slug = data.get("slug")
        data["slug"] = unique_slug(str(data.get("title") or ""), existing)
        if data["slug"] != raw_slug:
            logger.info("seo built slug in code from title: %r -> %r", raw_slug, data["slug"])

        # Normalize tags and entities in code before validating; only problems
        # code cannot fix (title/description wording, invented values) retry.
        raw_tags = data.get("tags")
        data["tags"] = normalize_tags(raw_tags)
        if data["tags"] != raw_tags:
            logger.info("seo normalized tags: %r -> %r", raw_tags, data["tags"])

        raw_entities = data.get("entities", [])
        data["entities"], new_entities = canonicalize_entities(raw_entities, sheet, entities)
        if data["entities"] != raw_entities:
            logger.info("seo normalized entities: %r -> %r", raw_entities, data["entities"])

        errors = validate(data, article=article, facts=sheet, entities=entities, config=config)
        if errors:
            logger.warning("seo attempt %d invalid: %s", attempt + 1, "; ".join(errors))
            continue

        if new_entities:
            save_entities(entities + new_entities, entities_path)
            logger.info("seo added %d new entity/entities", len(new_entities))
        logger.info("seo complete: slug=%s category=%s", data["slug"], data["category"])
        return {
            **data,
            "generator": {
                "provider": generation.provider,
                "model": generation.model,
                "family": generation.family,
            },
        }

    detail = "; ".join(errors) if errors else "unknown validation error"
    raise SeoError(f"SEO metadata failed validation after two attempts: {detail}")


__all__ = [
    "ALLOWED_CATEGORIES",
    "BANNED_PHRASES",
    "CAUSAL_PHRASES",
    "MAX_ALT",
    "MAX_DESC",
    "MAX_SLUG",
    "MAX_TAGS",
    "MAX_TITLE",
    "MIN_DESC",
    "MIN_TAGS",
    "OVERLAP_ORIGIN_PHRASES",
    "SEO_SCHEMA",
    "SNIPPET_BANNED_PHRASES",
    "SeoError",
    "build_prompt",
    "canonicalize_entities",
    "canonicalize_text",
    "existing_slugs",
    "generate_seo",
    "load_entities",
    "normalize_tags",
    "save_entities",
    "slugify",
    "truncate_slug",
    "unique_slug",
    "validate",
]
