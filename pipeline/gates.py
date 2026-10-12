"""Publish gates: the code-only checks an article must pass before it ships.

PLAN.md section 9. Nothing here trusts a model: every gate reads the finished
Markdown, the facts sheet and the verifier's report and returns a pass/fail
verdict. A single failure blocks publishing; warnings are recorded but do not
block. The gates are:

* **Schema and lengths** (``src/content.config.ts``): title/description lengths,
  category in the site enum, non-empty tags/entities, >= 2 distinct http(s)
  sources, and ``imageAlt`` whenever ``image`` is set.
* **Structure**: at least ``content_limits.min_words`` words, at most one H1, no
  skipped heading levels, and no body ``Sources`` section (the page renders
  sources from front matter).
* **Sources**: every source URL is http(s), distinct, present in the facts
  sheet, and rendered by the news page template.
* **Text overlap**: the article must not lift n-grams from the gathered source
  text (configurable n-gram size and max ratio).
* **Banned phrases** from ``config.yaml`` (title, description or body).
* **Duplicates**: no duplicate slug or title, and no near-duplicate title inside
  the configurable window.
* **Links**: every internal ``/news/<slug>`` link resolves to an article; every
  source URL resolves (404/410 fail, 403/429/timeout warn).
* **Verify report**: the verifier stage must have passed for this story.
* **Model families**: the writer and verifier families must differ.
* **Site checks**: the repo's own ``checks/check-articles.mjs`` runner.

Network (source link checks) and the Node site check are injectable so dry runs
and tests never touch the network or shell out.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from cluster import load_known_names, similarity, story_phrases

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config.yaml")
DEFAULT_ARTICLES_DIR = PROJECT_ROOT / "src" / "content" / "articles"
DEFAULT_PAGE_TEMPLATE = PROJECT_ROOT / "src" / "pages" / "news" / "[id].astro"
DEFAULT_CHECKS_DIR = PROJECT_ROOT / "checks"
USER_AGENT = "GamersXpress/0.1 (+https://gamersxpress.com; news-pipeline)"
LINK_TIMEOUT_SECONDS = 20

ALLOWED_CATEGORIES = (
    "gaming-news", "pc", "playstation", "xbox", "nintendo",
    "hardware", "tech", "esports", "india",
)

logger = logging.getLogger("gamersxpress.pipeline.gates")

_FRONT_MATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)
_FENCE_BLOCK_RE = re.compile(r"^\s*```[^\n]*\n.*?^\s*```", re.M | re.DOTALL)
_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_HREF_RE = re.compile(r"\]\(([^)]+)\)")
_SOURCES_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+sources\s*$", re.IGNORECASE | re.MULTILINE)

# Fallback content limits used only when ``content_limits`` is missing from
# config.yaml. config.yaml is the single source of truth, shared with the SEO
# stage and checks/check-articles.mjs; these defaults keep the pipeline running
# with a partial config.
DEFAULT_CONTENT_LIMITS: dict[str, int] = {
    "max_title": 65,
    "min_description": 70,
    "max_description": 160,
    "min_words": 200,
}

DEFAULT_GATES: dict[str, Any] = {
    "max_title": DEFAULT_CONTENT_LIMITS["max_title"],
    "min_description": DEFAULT_CONTENT_LIMITS["min_description"],
    "max_description": DEFAULT_CONTENT_LIMITS["max_description"],
    "min_words": DEFAULT_CONTENT_LIMITS["min_words"],
    "min_sources": 2,
    "overlap": {"ngram": 8, "max_ratio": 0.2},
    "near_duplicate": {"title_similarity": 0.8, "window_days": 7},
    "banned_phrases": [],
}

# Reader-text rules shared with the SEO stage (``pipeline/seo.py``). config.yaml
# is the single source of truth; these defaults keep the pipeline running with a
# partial config.
DEFAULT_RULES: dict[str, tuple[str, ...]] = {
    "relative_time_phrases": (
        "today", "tonight", "now", "currently", "this week", "this weekend",
        "yesterday", "tomorrow", "recently", "soon", "next week",
    ),
    "offer_phrases": (
        "sale", "discount", "game trial", "free weekend", "free to play",
        "free play", "play free", "% off",
    ),
    "contrast_phrases": ("rather than", "instead of"),
    "generic_attribution_phrases": (
        "the team", "the studio", "the developer", "the developers",
        "the narrative team", "narrative team", "the dev team", "dev team",
        "the design team", "design team",
    ),
    "completeness_words": ("full", "complete"),
}

# Origin words that mark a claim's origin as an organisation rather than a
# person the writer can quote by name.
_ORG_ORIGIN_WORDS = frozenset({
    "works", "studio", "studios", "games", "game", "entertainment", "inc",
    "ltd", "llc", "corp", "corporation", "company", "team", "interactive",
    "media", "software", "systems", "blog", "news",
})

# A quoted span (straight or curly double quotes) is a name, not reader text;
# relative-time words inside it are ignored.
_QUOTED_SPAN_RE = re.compile(r'"[^"]*"|\u201c[^\u201d]*\u201d')
_ALNUM_RE = re.compile(r"[a-z0-9]+")
_MONTH_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


@dataclass
class GateResult:
    """Aggregate verdict for one article."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)
    checks: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "failures": self.failures,
            "warnings": self.warnings,
            "skips": self.skips,
            "checks": self.checks,
        }


def load_content_limits(path: str | Path | None = None) -> dict:
    """Read the shared content length limits from ``content_limits`` in config.yaml.

    These limits (title/description lengths and the minimum body word count) are
    the single source of truth shared by the SEO stage (``pipeline/seo.py``),
    these publish gates and ``checks/check-articles.mjs``. Missing keys fall back
    to :data:`DEFAULT_CONTENT_LIMITS`.
    """
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    section = raw.get("content_limits") or {}
    if not isinstance(section, dict):
        section = {}
    return {key: int(section.get(key, default)) for key, default in DEFAULT_CONTENT_LIMITS.items()}


def load_content_rules(path: str | Path | None = None) -> dict:
    """Read the reader-text phrase rules from the ``gates`` block in config.yaml.

    These lists (relative-time words, offer phrases, contrast phrases, generic
    attributions and completeness words) are shared by these gates and the SEO
    snippet validation in ``pipeline/seo.py``. Missing keys fall back to
    :data:`DEFAULT_RULES`.
    """
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    section = raw.get("gates") or {}
    if not isinstance(section, dict):
        section = {}
    rules: dict[str, tuple[str, ...]] = {}
    for key, defaults in DEFAULT_RULES.items():
        values = section.get(key)
        if isinstance(values, (list, tuple)):
            rules[key] = tuple(str(value) for value in values if str(value).strip())
        else:
            rules[key] = defaults
    return rules


def load_gates_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    section = raw.get("gates") or {}
    limits = load_content_limits(path)
    config = {
        "max_title": limits["max_title"],
        "min_description": limits["min_description"],
        "max_description": limits["max_description"],
        "min_words": limits["min_words"],
        "min_sources": int(section.get("min_sources", DEFAULT_GATES["min_sources"])),
        "banned_phrases": [str(p).lower() for p in section.get("banned_phrases") or []],
    }
    config.update(load_content_rules(path))
    overlap = section.get("overlap") or {}
    config["overlap"] = {
        "ngram": int(overlap.get("ngram", DEFAULT_GATES["overlap"]["ngram"])),
        "max_ratio": float(overlap.get("max_ratio", DEFAULT_GATES["overlap"]["max_ratio"])),
    }
    near = section.get("near_duplicate") or {}
    config["near_duplicate"] = {
        "title_similarity": float(near.get("title_similarity", DEFAULT_GATES["near_duplicate"]["title_similarity"])),
        "window_days": int(near.get("window_days", DEFAULT_GATES["near_duplicate"]["window_days"])),
    }
    return config


# --- parsing helpers ---------------------------------------------------------


def parse_front_matter(article: str) -> tuple[dict, str]:
    match = _FRONT_MATTER_RE.match(article or "")
    if not match:
        return {}, article or ""
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        front = {}
    return (front if isinstance(front, dict) else {}), match.group(2)


def _count_words(body: str) -> int:
    text = _FENCE_BLOCK_RE.sub(" ", body)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.M)
    text = re.sub(r"\*\*|__|~~|`", "", text)
    return len([word for word in text.split() if word])


def _headings(body: str) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    in_fence = False
    for line in body.splitlines():
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((len(match.group(1)), match.group(2)))
    return headings


def _ngrams(words: list[str], n: int) -> set[tuple]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def overlap_ratio(body: str, sources_texts: list[str], ngram: int) -> float:
    body_ngrams = _ngrams(_WORD_RE.findall(body.lower()), ngram)
    if not body_ngrams:
        return 0.0
    source_ngrams: set[tuple] = set()
    for text in sources_texts:
        source_ngrams |= _ngrams(_WORD_RE.findall(str(text).lower()), ngram)
    if not source_ngrams:
        return 0.0
    return len(body_ngrams & source_ngrams) / len(body_ngrams)


def _internal_slugs(body: str) -> list[str]:
    slugs: list[str] = []
    for href in _HREF_RE.findall(body):
        href = href.strip().strip("<>").split("#")[0].split("?")[0]
        if href.startswith("/news/"):
            slug = href[len("/news/") :].strip("/")
            if slug:
                slugs.append(slug)
    return slugs


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and bool(re.match(r"^https?://\S+$", value))


def _parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


# --- reader-text rule helpers -------------------------------------------------


def _phrase_hits(text: str, phrases: Iterable[str]) -> list[str]:
    """Phrases present in ``text`` on word boundaries, ignoring quoted spans."""
    haystack = _QUOTED_SPAN_RE.sub(" ", str(text or "")).lower()
    hits: list[str] = []
    for phrase in phrases:
        phrase = str(phrase).strip().lower()
        if not phrase:
            continue
        if re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", haystack):
            hits.append(phrase)
    return hits


def _facts_claim_text(facts: dict | None) -> str:
    """The claim and value text of every claim in the sheet, joined."""
    parts: list[str] = []
    for claim in (facts or {}).get("claims") or []:
        parts.append(str(claim.get("claim", "")))
        parts.append(str(claim.get("value", "")))
    return "\n".join(parts)


def _sentences(body: str) -> list[str]:
    """Reader sentences in ``body`` (blank lines and terminal punctuation)."""
    text = _FENCE_BLOCK_RE.sub(" ", body or "")
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


def _date_token(sentence: str) -> str | None:
    """The first calendar date token in ``sentence`` (month, ISO date or year)."""
    for pattern in (_ISO_DATE_RE, _MONTH_RE, _YEAR_RE):
        match = pattern.search(sentence)
        if match:
            return match.group(0)
    return None


def _is_person_origin(origin: str) -> bool:
    """True when a claim origin looks like a named person, not an organisation."""
    words = _WORD_RE.findall(str(origin or "").lower())
    if not words:
        return False
    return not any(word in _ORG_ORIGIN_WORDS for word in words)


def _fold(text: str) -> str:
    """Lowercased ASCII text with accents folded away (for tolerant matching)."""
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()


def _entity_tokens(text: str) -> tuple[str, ...]:
    """Lowercased ASCII alphanumeric tokens, accents folded away."""
    return tuple(_ALNUM_RE.findall(_fold(text)))


def _is_subsequence(needle: tuple[str, ...], haystack: tuple[str, ...]) -> bool:
    if not needle:
        return True
    iterator = iter(haystack)
    return all(token in iterator for token in needle)


def _contains_run(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """True when ``needle`` appears as a contiguous run inside ``haystack``."""
    size = len(needle)
    if size == 0:
        return True
    return any(haystack[index : index + size] == needle for index in range(len(haystack) - size + 1))


def _name_covers(candidate: tuple[str, ...], name: tuple[str, ...]) -> bool:
    """True when one token sequence contains the other, in order."""
    return _is_subsequence(candidate, name) or _is_subsequence(name, candidate)


def title_entity_coverage(
    title: str,
    entities: Iterable[str],
    facts: dict | None = None,
    *,
    known: Iterable[str] | None = None,
) -> list[str]:
    """Name phrases from ``title`` that ``entities`` does not cover.

    Rule: an article always names the main game or product from the headline and
    every named character or product in the title as entities. A phrase is
    checked when it is a known name or appears in the facts sheet, and phrases
    that carry a number are only checked when they are known names (so a topical
    phrase like "Year 1 DLC" is not demanded as an entity).
    """
    known_names = list(known) if known is not None else list(load_known_names())
    known_tokens = [_entity_tokens(name) for name in known_names]
    entity_tokens = [_entity_tokens(name) for name in entities if str(name).strip()]
    facts_tokens = _entity_tokens(_facts_claim_text(facts))
    missing: list[str] = []
    for phrase in story_phrases(title):
        tokens = _entity_tokens(phrase)
        if not tokens or not any(token.isalpha() for token in tokens):
            continue
        is_known = any(_name_covers(tokens, known_name) for known_name in known_tokens)
        in_facts = bool(facts_tokens) and _contains_run(facts_tokens, tokens)
        if not (is_known or in_facts):
            continue
        if any(token.isdigit() for token in tokens) and not is_known:
            continue
        if not any(_name_covers(tokens, name) for name in entity_tokens):
            missing.append(phrase)
    return missing


def _empty_sections(body: str) -> list[str]:
    """Heading text of every section that has no body words before the next one."""
    empty: list[str] = []
    current: str | None = None
    words = 0

    def flush() -> None:
        if current is not None and words == 0:
            empty.append(current)

    for line in (body or "").splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            flush()
            current = match.group(1)
            words = 0
        elif current is not None:
            words += len(_WORD_RE.findall(line))
    flush()
    return empty


# --- external checks (injectable) --------------------------------------------


def default_link_checker(url: str, *, timeout: int = LINK_TIMEOUT_SECONDS) -> int | str:
    """Return the HTTP status for ``url``, or 'timeout'/'error' on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except TimeoutError:
        return "timeout"
    except Exception:  # noqa: BLE001 - network problems are warnings, not crashes
        return "error"


def default_site_checks(
    article: str,
    slug: str,
    *,
    checks_dir: str | Path | None = None,
    node: str = "node",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[bool, str]:
    """Run ``checks/check-articles.mjs`` against the single article."""
    checks_dir = Path(checks_dir) if checks_dir else DEFAULT_CHECKS_DIR
    module = checks_dir / "check-articles.mjs"
    if not module.is_file():
        return False, f"site check runner not found: {module}"
    workdir = Path(tempfile.mkdtemp(prefix="gx-gate-"))
    try:
        (workdir / f"{slug}.md").write_text(article, encoding="utf-8")
        script = (
            f"const m = await import({json.dumps(module.as_uri())});"
            f"await m.run({json.dumps(str(workdir))});"
        )
        try:
            proc = runner(
                [node, "--input-type=module", "-e", script],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            return False, f"node executable not found: {node}"
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output.strip()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def load_existing_articles(articles_dir: str | Path | None = None) -> list[dict]:
    directory = Path(articles_dir) if articles_dir else DEFAULT_ARTICLES_DIR
    if not directory.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob("*.md")):
        front, _body = parse_front_matter(path.read_text(encoding="utf-8"))
        records.append(
            {
                "slug": path.stem,
                "title": str(front.get("title") or ""),
                "entities": [str(e) for e in front.get("entities") or []],
                "pubDate": front.get("pubDate"),
            }
        )
    return records


# --- the gates ---------------------------------------------------------------


def run_gates(
    article: str,
    *,
    slug: str,
    facts: dict,
    cluster_id: str,
    gathered: dict | None = None,
    verify_report: dict | None = None,
    run_state: Any = None,
    config: dict | None = None,
    articles_dir: str | Path | None = None,
    page_template: str | Path | None = None,
    now: dt.datetime | None = None,
    link_checker: Callable[[str], int | str] | None = None,
    site_checks: Callable[[str, str], tuple[bool, str]] | None = None,
    existing_articles: list[dict] | None = None,
) -> GateResult:
    """Run every publish gate and return the aggregate verdict."""
    config = config if config is not None else load_gates_config()
    now = now or dt.datetime.now(dt.timezone.utc)
    link_checker = link_checker or default_link_checker
    if site_checks is None:
        site_checks = lambda text, s: default_site_checks(text, s)  # noqa: E731
    if existing_articles is None:
        existing_articles = load_existing_articles(articles_dir)

    front, body = parse_front_matter(article)
    result = GateResult(passed=True)

    def record(name: str, failures: list[str], warnings: list[str] | None = None, details: dict | None = None) -> None:
        warnings = warnings or []
        result.checks[name] = {"ok": not failures, "failures": failures, "warnings": warnings, **(details or {})}
        result.failures.extend(failures)
        result.warnings.extend(warnings)

    title = str(front.get("title") or "")
    description = str(front.get("description") or "")
    sources = front.get("sources") if isinstance(front.get("sources"), list) else []
    images = front.get("image")
    image_alt = front.get("imageAlt")

    # 1. schema and lengths
    schema_failures: list[str] = []
    if not title.strip():
        schema_failures.append("missing title")
    elif len(title) > config["max_title"]:
        schema_failures.append(f"title is {len(title)} characters (max {config['max_title']})")
    if not description.strip():
        schema_failures.append("missing description")
    elif not (config["min_description"] <= len(description) <= config["max_description"]):
        schema_failures.append(
            f"description is {len(description)} characters (must be {config['min_description']}-{config['max_description']})"
        )
    if str(front.get("category") or "") not in ALLOWED_CATEGORIES:
        schema_failures.append(f"category must be one of {', '.join(ALLOWED_CATEGORIES)}: {front.get('category')!r}")
    for key in ("tags", "entities"):
        if not isinstance(front.get(key), list) or not front.get(key):
            schema_failures.append(f"{key} must be a non-empty list")
    if images and not (isinstance(image_alt, str) and image_alt.strip()):
        schema_failures.append("image is set without imageAlt")
    record("schema", schema_failures)

    # 2. structure
    structure_failures: list[str] = []
    words = _count_words(body)
    if words < config["min_words"]:
        structure_failures.append(f"body has {words} words (min {config['min_words']})")
    headings = _headings(body)
    h1 = sum(1 for level, _ in headings if level == 1)
    if h1 > 1:
        structure_failures.append(f"body has {h1} H1 headings (max 1)")
    previous = 1
    for level, text in headings:
        if level > previous + 1:
            structure_failures.append(f"heading levels skip: h{previous} -> h{level} at {text!r}")
            break
        previous = level
    if _SOURCES_HEADING_RE.search(body):
        structure_failures.append("body must not repeat a Sources section; the page renders front-matter sources")
    record("structure", structure_failures, details={"words": words, "headings": len(headings), "h1": h1})

    # 3. sources
    sources_failures: list[str] = []
    urls = [str(source.get("url") or "") for source in sources if isinstance(source, dict)]
    if len(sources) < config["min_sources"]:
        sources_failures.append(f"has {len(sources)} source(s) (min {config['min_sources']})")
    invalid = [url for url in urls if not _is_http_url(url)]
    if invalid:
        sources_failures.append(f"sources must be http(s) URLs: {invalid}")
    if len(set(urls)) != len(urls):
        sources_failures.append("sources must be distinct")
    facts_urls = {_normalize(str(source.get("link") or "")) for source in (facts.get("sources") or [])}
    unknown = [url for url in urls if url and _normalize(url) not in facts_urls]
    if unknown:
        sources_failures.append(f"sources not present in the facts sheet: {unknown}")
    template_path = Path(page_template) if page_template else DEFAULT_PAGE_TEMPLATE
    try:
        template_text = template_path.read_text(encoding="utf-8")
    except OSError:
        template_text = ""
    if "sources" not in template_text or "map" not in template_text:
        sources_failures.append(f"news page template does not render sources: {template_path}")
    record("sources", sources_failures, details={"count": len(sources)})

    # 4. text overlap
    overlap_cfg = config["overlap"]
    source_texts = [str((source or {}).get("text") or "") for source in (gathered or {}).get("sources") or []]
    ratio = overlap_ratio(body, source_texts, overlap_cfg["ngram"])
    overlap_failures: list[str] = []
    if ratio > overlap_cfg["max_ratio"]:
        overlap_failures.append(
            f"article reuses {ratio:.0%} of source {overlap_cfg['ngram']}-grams (max {overlap_cfg['max_ratio']:.0%})"
        )
    record("overlap", overlap_failures, details={"ratio": round(ratio, 4)})

    # 5. banned phrases
    banned_failures: list[str] = []
    haystack = f"{title}\n{description}\n{body}".lower()
    for phrase in config["banned_phrases"]:
        if phrase and phrase in haystack:
            banned_failures.append(f"banned filler phrase: {phrase!r}")
    record("banned_phrases", banned_failures, details={"found": len(banned_failures)})

    # 5a. relative time (rule: write calendar dates, never words that expire)
    relative_failures: list[str] = []
    for label, text in (("title", title), ("description", description), ("body", body)):
        hits = _phrase_hits(text, config["relative_time_phrases"])
        if hits:
            relative_failures.append(f"{label} uses relative-time wording that will expire: {hits}")
    record("relative_time", relative_failures)

    # 5b. attribution (rule: statements go to the speaker the sheet names, and
    # no contrast is invented)
    attribution_failures: list[str] = []
    facts_text = _facts_claim_text(facts).lower()
    body_lower = body.lower()
    for phrase in config["contrast_phrases"]:
        if phrase and phrase in body_lower and phrase not in facts_text:
            attribution_failures.append(f"body uses a contrast phrase no fact states: {phrase!r}")
    speakers = sorted({
        str(claim.get("origin") or "").strip()
        for claim in facts.get("claims") or []
        if claim.get("kind") == "statement"
        and str(claim.get("origin") or "").strip().lower() not in ("", "unstated")
        and _is_person_origin(str(claim.get("origin") or ""))
    })
    if speakers:
        for phrase in config["generic_attribution_phrases"]:
            if phrase and phrase in body_lower and phrase not in facts_text:
                attribution_failures.append(
                    f"body attributes a statement to a group ({phrase!r}); the sheet names {speakers}"
                )
    record("attribution", attribution_failures)

    # 5c. limited-time offers need a date the sheet states
    offer_failures: list[str] = []
    for sentence in _sentences(body):
        lowered = sentence.lower()
        offer = next((p for p in config["offer_phrases"] if p and p in lowered), None)
        if not offer:
            continue
        date = _date_token(sentence)
        if not date or date.lower() not in facts_text:
            offer_failures.append(
                f"body offers {offer!r} without a date range or start date from the facts sheet: {sentence!r}"
            )
    record("time_limited_offers", offer_failures)

    # 5d. sections earn their place
    section_failures: list[str] = []
    filler = "no other details are confirmed"
    if filler in body_lower:
        section_failures.append(f"body writes filler instead of omitting the section: {filler!r}")
    for heading in _empty_sections(body):
        section_failures.append(f"section {heading!r} has no content; omit it instead")
    record("sections", section_failures)

    # 5e. entities name the headline's game/product and characters
    entity_failures: list[str] = []
    missing_entities = title_entity_coverage(
        title, front.get("entities") or [], facts,
    )
    if missing_entities:
        entity_failures.append(
            f"entities must include the main game or product and every named character "
            f"or product in the title: {missing_entities}"
        )
    record("entities", entity_failures, details={"missing": missing_entities})

    # 5f. no "full"/"complete" hype unless a fact states it
    completeness_failures: list[str] = []
    for word in config["completeness_words"]:
        if not word:
            continue
        if re.search(rf"\b{re.escape(word)}\b", haystack) and not re.search(
            rf"\b{re.escape(word)}\b", facts_text
        ):
            completeness_failures.append(f"body adds the word {word!r} that no fact states")
    record("completeness", completeness_failures)

    # 6. duplicates and near-duplicates
    duplicate_failures: list[str] = []
    near_cfg = config["near_duplicate"]
    window = dt.timedelta(days=near_cfg["window_days"])
    self_entities = {str(e).lower() for e in front.get("entities") or []}
    for record_entry in existing_articles:
        other_slug = str(record_entry.get("slug") or "")
        other_title = str(record_entry.get("title") or "")
        if other_slug.lower() == slug.lower():
            duplicate_failures.append(f"duplicate slug: {other_slug}")
            continue
        if other_title.strip().lower() and other_title.strip().lower() == title.strip().lower():
            duplicate_failures.append(f"duplicate title: {other_slug}")
            continue
        published = _parse_time(record_entry.get("pubDate"))
        if published is None or not (dt.timedelta(0) <= now - published <= window):
            continue
        sim = similarity(title, other_title)
        shared = self_entities & {str(e).lower() for e in record_entry.get("entities") or []}
        if sim >= near_cfg["title_similarity"] or (shared and sim >= 0.5):
            duplicate_failures.append(f"near-duplicate of {other_slug} (title similarity {sim:.2f})")
    record("duplicates", duplicate_failures)

    # 7. internal links resolve
    existing_slugs = {str(entry.get("slug") or "").lower() for entry in existing_articles}
    existing_slugs.add(slug.lower())
    link_failures: list[str] = []
    for target in _internal_slugs(body):
        if target.lower() not in existing_slugs:
            link_failures.append(f"internal link does not resolve: /news/{target}")
    record("internal_links", link_failures)

    # 8. source link resolution
    source_link_failures: list[str] = []
    source_link_warnings: list[str] = []
    for url in urls:
        if not _is_http_url(url):
            continue
        status = link_checker(url)
        if isinstance(status, int) and 200 <= status < 400:
            continue
        if isinstance(status, int) and status in (404, 410):
            source_link_failures.append(f"source URL returns {status}: {url}")
        else:
            source_link_warnings.append(f"source URL could not be confirmed ({status}): {url}")
    record("source_links", source_link_failures, source_link_warnings)

    # 9. verify report
    verify_failures: list[str] = []
    if verify_report is None:
        verify_failures.append("missing verify report")
    elif not verify_report.get("passed"):
        verify_failures.append("verify report did not pass")
    record("verify_report", verify_failures)

    # 10. writer/verifier families differ
    family_failures: list[str] = []
    writer_family = getattr(run_state, "writer_family", None)
    verifier_family = getattr(run_state, "verifier_family", None)
    if not writer_family or not verifier_family:
        family_failures.append("writer and verifier families must both be recorded")
    elif writer_family == verifier_family:
        family_failures.append(f"writer and verifier share family {writer_family!r}")
    record("model_families", family_failures)

    # 11. repo site checks
    ok, output = site_checks(article, slug)
    record("site_checks", [] if ok else [f"check-articles failed: {output}"], details={"output": output})

    result.passed = not result.failures
    if result.passed:
        logger.info("gates passed: slug=%s (%d warning(s))", slug, len(result.warnings))
    else:
        logger.warning("gates failed: slug=%s %s", slug, result.failures)
    return result


def _normalize(url: str) -> str:
    return url.strip().rstrip("/").lower()


__all__ = [
    "ALLOWED_CATEGORIES",
    "DEFAULT_CONTENT_LIMITS",
    "DEFAULT_GATES",
    "DEFAULT_RULES",
    "GateResult",
    "default_link_checker",
    "default_site_checks",
    "load_content_limits",
    "load_content_rules",
    "load_existing_articles",
    "load_gates_config",
    "overlap_ratio",
    "parse_front_matter",
    "run_gates",
    "title_entity_coverage",
]
