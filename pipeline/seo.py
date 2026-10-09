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
* **No unconfirmed claims.** A value the sheet marks as a rumor (``is_rumor``)
  must not appear in the title or description.

If the first answer is invalid the model is asked once more with the validation
errors attached; if that also fails the story is dropped with :class:`SeoError`.

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
    "values that appear in the facts sheet, and never state a rumor as fact."
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


def unique_slug(base: str, existing: set[str]) -> str:
    """Return a slug that is not already in ``existing`` and is <= :data:`MAX_SLUG`."""
    slug = slugify(base)[:MAX_SLUG].rstrip("-") or "article"
    if slug not in existing:
        return slug
    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = slug[: MAX_SLUG - len(suffix)].rstrip("-") + suffix
        if candidate not in existing:
            return candidate
        counter += 1


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
        lowered = title.lower()
        for phrase in BANNED_PHRASES:
            if phrase in lowered:
                errors.append(f"title contains banned hype phrase: {phrase!r}")

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
    elif not 3 <= len(tags) <= 6:
        errors.append(f"tags must have 3 to 6 entries, got {len(tags)}")
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
        if claim.get("is_rumor") and len(value) >= 3 and value.lower() in title_desc:
            errors.append(f"title or description states a rumor as fact: {value!r}")

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
        data["slug"] = slugify(data.get("slug", ""))
        data["entities"], new_entities = canonicalize_entities(data.get("entities", []), sheet, entities)
        for field in ("title", "description", "imageAlt"):
            if isinstance(data.get(field), str):
                data[field] = canonicalize_text(data[field], entities)

        errors = validate(data, article=article, facts=sheet, entities=entities, config=config)
        if errors:
            logger.warning("seo attempt %d invalid: %s", attempt + 1, "; ".join(errors))
            continue

        data["slug"] = unique_slug(data["slug"], existing)
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

    raise SeoError("SEO metadata failed validation after two attempts: " + "; ".join(errors))


__all__ = [
    "ALLOWED_CATEGORIES",
    "BANNED_PHRASES",
    "MAX_ALT",
    "MAX_DESC",
    "MAX_SLUG",
    "MAX_TITLE",
    "MIN_DESC",
    "SEO_SCHEMA",
    "SeoError",
    "build_prompt",
    "canonicalize_entities",
    "canonicalize_text",
    "existing_slugs",
    "generate_seo",
    "load_entities",
    "save_entities",
    "slugify",
    "unique_slug",
    "validate",
]
