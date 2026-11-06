"""Telegram alerts for the scheduled pipeline (docs/CI-SETUP.md).

The workflow calls :func:`build_message` and :func:`send_telegram` after every
run. One short message at most: a draft is ready, a draft failed its checks, a
run failed, or the circuit breaker opened. Quiet runs (nothing to cover, paused,
skipped) send nothing. In auto mode a published article is announced once the
site is live (:func:`build_live_message`, sent by the Announce workflow after a
successful deploy), not when the pipeline run ends.

An alert must never break a run, so :func:`send_telegram` never raises. The bot
token sits inside the request URL, so nothing here logs a URL, a request or a
response body: only a status code or an exception class name.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("gamersxpress.pipeline.notify")

TELEGRAM_API = "https://api.telegram.org"
#: Telegram allows 4096 characters; stay well under it.
MAX_MESSAGE_CHARS = 3500
#: Links in a message must point at GitHub, whatever the caller passes in.
GITHUB_URL_PREFIX = "https://github.com/"
#: The live site; an article link in a message must point here (override with SITE_URL).
SITE_URL = "https://gamersxpress.com"


def _clip(text: str) -> str:
    """Cut a message to the limit on a line boundary, so no HTML tag is split."""
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    head = text[:MAX_MESSAGE_CHARS]
    return head.rsplit("\n", 1)[0] if "\n" in head else head


def send_telegram(
    text: str,
    *,
    token: str | None = None,
    chat_id: str | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 15.0,
    api_base: str = TELEGRAM_API,
) -> bool:
    """Send ``text`` (Telegram HTML) to the configured chat. Never raises.

    Returns True only when Telegram accepted the message. Missing credentials
    are not an error: the alert is skipped.
    """
    token = (token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
    chat = str(chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat:
        logger.info("telegram is not configured: alert skipped")
        return False
    body = json.dumps(
        {
            "chat_id": chat,
            "text": _clip(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base}/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        logger.warning("telegram alert failed: HTTP %s", exc.code)
        return False
    except (OSError, ValueError, TimeoutError) as exc:
        # URLError is an OSError; the class name is enough, never the message.
        logger.warning("telegram alert failed: %s", type(exc).__name__)
        return False
    accepted = status == 200 and isinstance(payload, dict) and bool(payload.get("ok"))
    if not accepted:
        logger.warning("telegram alert was not accepted (status %s)", status)
    return accepted


def _escape(value: Any, limit: int = 200) -> str:
    """One line of plain text, shortened and escaped for Telegram HTML."""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(text, quote=False)


def _link(url: str | None, label: str, prefix: str = GITHUB_URL_PREFIX) -> str | None:
    """An HTML link, only when the URL starts with ``prefix`` (GitHub by default)."""
    if not url or not str(url).startswith(prefix):
        return None
    return f'<a href="{html.escape(str(url), quote=True)}">{_escape(label, 80)}</a>'


def site_url() -> str:
    """The live site's address without a trailing slash."""
    return (os.environ.get("SITE_URL") or SITE_URL).strip().rstrip("/")


def article_url(slug: str) -> str:
    """The public address of an article (``/news/<slug>/``)."""
    return f"{site_url()}/news/{slug}/"


_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)


_DESCRIPTION_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


def article_description(article_path: str | Path | None) -> str | None:
    """The ``description`` from an article's front matter, if the file is readable."""
    if not article_path:
        return None
    try:
        text = Path(article_path).read_text(encoding="utf-8")
    except OSError:
        return None
    front = text.split("\n---", 2)[0] if text.startswith("---") else ""
    match = _DESCRIPTION_RE.search(front)
    if not match:
        return None
    return match.group(1).strip().strip("\"'") or None


def article_title(article_path: str | Path | None) -> str | None:
    """The ``title`` from an article's front matter, if the file is readable."""
    if not article_path:
        return None
    try:
        text = Path(article_path).read_text(encoding="utf-8")
    except OSError:
        return None
    front = text.split("\n---", 2)[0] if text.startswith("---") else ""
    match = _TITLE_RE.search(front)
    if not match:
        return None
    return match.group(1).strip().strip("\"'") or None


def build_message(
    report: dict,
    *,
    title: str | None = None,
    run_url: str | None = None,
    pr_url: str | None = None,
    published: bool = False,
) -> str | None:
    """The alert for one run, or None when the run needs no alert.

    ``published`` is true when the run already pushed its article to ``main``
    (auto mode): there is no draft to review, and the Announce workflow sends
    the "published" message once the site is live, so this run says nothing.
    """
    status = str(report.get("status") or "")
    breaker = report.get("breaker") if isinstance(report.get("breaker"), dict) else {}
    lines: list[str] = []

    if status == "completed" and published:
        return None
    if status == "completed":
        label = title or report.get("slug") or "new article"
        lines.append(f"📝 <b>Draft ready:</b> {_escape(label)}")
        if report.get("category"):
            lines.append(f"Category: {_escape(report['category'], 40)}")
        if report.get("verification") == "single-origin":
            lines.append("⚠️ Relayed report: one named source, several outlets. Read it harder.")
        review = _link(pr_url, "Review the pull request")
        lines.append(review or "No pull request was opened; see the workflow run.")
        run_link = _link(run_url, "Workflow run")
        if run_link and not review:
            lines.append(run_link)
    elif status == "failed":
        slug = report.get("slug")
        if slug:
            lines.append(f"❌ <b>Draft failed its checks:</b> {_escape(slug, 90)}")
        else:
            lines.append("❌ <b>Pipeline run failed</b>")
        if report.get("error"):
            lines.append(_escape(report["error"], 160))
        failures = report.get("failures")
        if isinstance(failures, list):
            for failure in failures[:3]:
                lines.append(f"• {_escape(failure, 160)}")
            if len(failures) > 3:
                lines.append(f"… and {len(failures) - 3} more")
        run_link = _link(run_url, "Workflow run (draft attached)")
        if run_link:
            lines.append(run_link)
        if breaker.get("open"):
            lines.append("🛑 <b>Circuit breaker is open.</b> Runs stay paused until it is reset.")
    else:
        return None
    return "\n".join(lines)


def build_live_message(articles: list[dict], *, unpublish_url: str | None = None) -> str | None:
    """The "published" message for the articles a deploy just made live.

    ``articles`` holds ``{"slug", "title", "description"}`` dicts. Returns None
    when there is nothing new. Each article gets its live link and the way to
    take it down; the message is the owner's check of the final post.
    """
    blocks: list[str] = []
    for article in articles:
        slug = str(article.get("slug") or "")
        if not slug:
            continue
        lines = [f"✅ <b>Published:</b> {_escape(article.get('title') or slug, 140)}"]
        if article.get("description"):
            lines.append(_escape(article["description"], 220))
        live = _link(article_url(slug), "Read it live", prefix=site_url())
        if live:
            lines.append(live)
        takedown = _link(unpublish_url, "Unpublish it", prefix=GITHUB_URL_PREFIX)
        if takedown:
            lines.append(f"Wrong? {takedown}: run it with the slug <code>{_escape(slug, 100)}</code>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else None


def build_deploy_failed_message(run_url: str | None = None) -> str:
    """The alert for a failed deploy (a published article may not be live)."""
    lines = ["❌ <b>Deploy failed.</b> The site was not updated; a newly published article may not be live."]
    link = _link(run_url, "Deploy run")
    if link:
        lines.append(link)
    return "\n".join(lines)


def build_unpublished_message(slug: str, title: str | None = None, run_url: str | None = None) -> str:
    """The confirmation that an article was taken down."""
    lines = [f"🗑 <b>Unpublished:</b> {_escape(title or slug, 140)}", "The site redeploys without it."]
    link = _link(run_url, "Workflow run")
    if link:
        lines.append(link)
    return "\n".join(lines)
