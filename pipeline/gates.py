"""Publish gates: the code-only checks an article must pass before it ships.

PLAN.md section 9. Nothing here trusts a model: every gate reads the finished
Markdown, the facts sheet and the verifier's report and returns a pass/fail
verdict. A single failure blocks publishing; warnings are recorded but do not
block. The gates are:

* **Schema and lengths** (``src/content.config.ts``): title/description lengths,
  category in the site enum, non-empty tags/entities, >= 2 distinct http(s)
  sources, and ``imageAlt`` whenever ``image`` is set.
* **Structure**: >= 300 words, at most one H1, no skipped heading levels, and no
  body ``Sources`` section (the page renders sources from front matter).
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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from cluster import similarity

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

DEFAULT_GATES: dict[str, Any] = {
    "max_title": 65,
    "min_description": 70,
    "max_description": 160,
    "min_words": 300,
    "min_sources": 2,
    "overlap": {"ngram": 8, "max_ratio": 0.2},
    "near_duplicate": {"title_similarity": 0.8, "window_days": 7},
    "banned_phrases": [],
}


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


def load_gates_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    section = raw.get("gates") or {}
    config = {
        "max_title": int(section.get("max_title", DEFAULT_GATES["max_title"])),
        "min_description": int(section.get("min_description", DEFAULT_GATES["min_description"])),
        "max_description": int(section.get("max_description", DEFAULT_GATES["max_description"])),
        "min_words": int(section.get("min_words", DEFAULT_GATES["min_words"])),
        "min_sources": int(section.get("min_sources", DEFAULT_GATES["min_sources"])),
        "banned_phrases": [str(p).lower() for p in section.get("banned_phrases") or []],
    }
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
    "DEFAULT_GATES",
    "GateResult",
    "default_link_checker",
    "default_site_checks",
    "load_existing_articles",
    "load_gates_config",
    "overlap_ratio",
    "parse_front_matter",
    "run_gates",
]
