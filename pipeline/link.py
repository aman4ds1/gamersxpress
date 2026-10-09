"""Internal linking stage: connect articles by entity, alias and title keyword.

The index at ``data/link-index.json`` maps every article to its canonical
entities and notable title keywords. :func:`add_links` inserts a small number of
inline Markdown links into a body:

* first mention only, and each target article at most once;
* never inside headings, code fences, existing links or bare URLs;
* never inside a Sources section, and no Sources section is ever added (the
  front matter ``sources`` field is the only source list);
* never a self-link, and only to articles published at least a day ago;
* at most 5 links, and no link at all when nothing matches.

:func:`select_related` picks up to 3 related articles by shared entity and
category for the front matter or a component prop.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX_PATH = PROJECT_ROOT / "data" / "link-index.json"

MAX_LINKS = 5
RELATED_LIMIT = 3
MIN_AGE = dt.timedelta(days=1)

logger = logging.getLogger("gamersxpress.pipeline.link")

_FRONT_MATTER_RE = re.compile(r"^(---\r?\n.*?\r?\n---\r?\n?)(.*)$", re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_SOURCES_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+sources\s*$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LINK_SPAN_RE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*")

_STOPWORDS = frozenset(
    {
        "about", "after", "again", "against", "along", "also", "among", "another",
        "because", "been", "before", "being", "between", "both", "could", "does",
        "doing", "done", "down", "during", "each", "every", "from", "gets", "goes",
        "have", "having", "here", "into", "just", "like", "made", "make", "many",
        "more", "most", "much", "must", "need", "news", "only", "other", "over",
        "said", "same", "some", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "under", "until",
        "very", "what", "when", "where", "which", "while", "will", "with", "would",
        "your", "yours", "amid", "gets", "new",
    }
)


class LinkError(Exception):
    """The index was malformed or the input was not usable."""


@dataclass
class LinkResult:
    """A body with internal links inserted, plus what was inserted."""

    article: str
    links: list[dict] = field(default_factory=list)

    def __iter__(self):
        return iter((self.article, self.links))


# --- index -------------------------------------------------------------------


def _keywords(title: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for word in _WORD_RE.findall(title or ""):
        key = word.lower()
        if len(key) < 4 or key in _STOPWORDS or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def build_index(articles: Iterable[dict], path: str | Path | None = None) -> dict:
    """Build (and optionally save) the entity/keyword index for ``articles``."""
    records: list[dict] = []
    by_entity: dict[str, list[str]] = {}
    by_keyword: dict[str, list[str]] = {}
    for article in articles:
        article_id = str(article.get("id") or "").strip()
        if not article_id:
            continue
        entities = [str(e).strip() for e in article.get("entities") or [] if str(e).strip()]
        keywords = _keywords(str(article.get("title") or ""))
        pub_date = article.get("pubDate") or article.get("pub_date")
        record = {
            "id": article_id,
            "title": str(article.get("title") or ""),
            "category": str(article.get("category") or ""),
            "pubDate": _iso(pub_date),
            "entities": entities,
            "keywords": keywords,
            "url": f"/news/{article_id}",
        }
        records.append(record)
        for entity in entities:
            by_entity.setdefault(entity.lower(), []).append(article_id)
        for keyword in keywords:
            by_keyword.setdefault(keyword, []).append(article_id)
    index = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "articles": records,
        "by_entity": by_entity,
        "by_keyword": by_keyword,
    }
    if path is not None:
        save_index(index, path)
    return index


def save_index(index: dict, path: str | Path | None = None) -> Path:
    path = Path(path) if path else DEFAULT_INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_index(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_INDEX_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def _iso(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat()
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc).isoformat()
    return str(value or "")


def _published(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _eligible(record: dict, self_id: str, now: dt.datetime) -> bool:
    if record["id"] == self_id:
        return False
    published = _published(record.get("pubDate"))
    if published is None:
        return False
    return now - published >= MIN_AGE


# --- related -----------------------------------------------------------------


def select_related(
    index: dict,
    article_id: str,
    *,
    now: dt.datetime | None = None,
    limit: int = RELATED_LIMIT,
) -> list[dict]:
    """Return up to ``limit`` related articles, ranked by shared entity and category."""
    now = now or dt.datetime.now(dt.timezone.utc)
    records = {record["id"]: record for record in index.get("articles") or []}
    self_record = records.get(article_id)
    if self_record is None:
        raise LinkError(f"article {article_id!r} is not in the index")
    self_entities = {e.lower() for e in self_record.get("entities") or []}
    self_keywords = set(self_record.get("keywords") or [])
    scored: list[tuple[int, dt.datetime, dict]] = []
    for record in records.values():
        if not _eligible(record, article_id, now):
            continue
        shared_entities = len(self_entities & {e.lower() for e in record.get("entities") or []})
        shared_keywords = len(self_keywords & set(record.get("keywords") or []))
        same_category = 1 if record.get("category") and record.get("category") == self_record.get("category") else 0
        score = 2 * shared_entities + same_category + shared_keywords
        if score <= 0:
            continue
        published = _published(record.get("pubDate")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        scored.append((score, published, record))
    scored.sort(key=lambda item: (-item[0], -item[1].timestamp()))
    return [
        {"id": record["id"], "title": record["title"], "category": record["category"], "url": record["url"]}
        for _score, _published_at, record in scored[:limit]
    ]


# --- inline links ------------------------------------------------------------


def split_front_matter(article: str) -> tuple[str, str]:
    match = _FRONT_MATTER_RE.match(article)
    return (match.group(1), match.group(2)) if match else ("", article)


def has_sources_section(article: str) -> bool:
    _front, body = split_front_matter(article)
    return any(_SOURCES_HEADING_RE.match(line) for line in body.splitlines())


def _link_spans(line: str) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in _LINK_SPAN_RE.finditer(line)]
    spans += [(m.start(), m.end()) for m in _URL_RE.finditer(line)]
    return spans


def _in_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _candidates(index: dict, self_id: str, now: dt.datetime) -> list[tuple[str, dict]]:
    records = {record["id"]: record for record in index.get("articles") or []}
    pairs: list[tuple[str, dict]] = []
    for term in sorted(index.get("by_entity") or {}, key=len, reverse=True):
        for article_id in index["by_entity"][term]:
            record = records.get(article_id)
            if record and _eligible(record, self_id, now):
                pairs.append((term, record))
    for term in sorted(index.get("by_keyword") or {}, key=len, reverse=True):
        for article_id in index["by_keyword"][term]:
            record = records.get(article_id)
            if record and _eligible(record, self_id, now):
                pairs.append((term, record))
    return pairs


def _find(line: str, term: str, spans: list[tuple[int, int]]) -> re.Match | None:
    pattern = re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])", re.IGNORECASE)
    for match in pattern.finditer(line):
        if not _in_spans(match.start(), match.end(), spans):
            return match
    return None


def add_links(
    article: str,
    index: dict,
    *,
    self_id: str,
    now: dt.datetime | None = None,
    max_links: int = MAX_LINKS,
) -> LinkResult:
    """Insert up to ``max_links`` inline internal links into the article body."""
    now = now or dt.datetime.now(dt.timezone.utc)
    front, body = split_front_matter(article)
    candidates = _candidates(index, self_id, now)

    used_targets: set[str] = set()
    used_terms: set[str] = set()
    links: list[dict] = []

    lines = body.splitlines(keepends=True)
    in_fence = False
    in_sources = False
    for position, raw_line in enumerate(lines):
        if len(links) >= max_links:
            break
        stripped = raw_line.rstrip("\r\n")
        ending = raw_line[len(stripped):]
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _SOURCES_HEADING_RE.match(stripped):
            in_sources = True
            continue
        if in_sources and _HEADING_RE.match(stripped):
            in_sources = False
        if in_sources or _HEADING_RE.match(stripped):
            continue

        line = stripped
        changed = True
        while changed and len(links) < max_links:
            changed = False
            spans = _link_spans(line)
            for term, record in candidates:
                if term in used_terms or record["id"] in used_targets:
                    continue
                match = _find(line, term, spans)
                if match is None:
                    continue
                label = match.group(0)
                line = f"{line[:match.start()]}[{label}]({record['url']}){line[match.end():]}"
                used_terms.add(term)
                used_targets.add(record["id"])
                links.append({"id": record["id"], "term": label, "url": record["url"], "line": position})
                changed = True
                break
        lines[position] = line + ending

    return LinkResult(article=front + "".join(lines), links=links)


__all__ = [
    "DEFAULT_INDEX_PATH",
    "MAX_LINKS",
    "RELATED_LIMIT",
    "LinkError",
    "LinkResult",
    "add_links",
    "build_index",
    "has_sources_section",
    "load_index",
    "save_index",
    "select_related",
    "split_front_matter",
]
