"""Telegram alerts for the scheduled pipeline (docs/CI-SETUP.md).

The workflow calls :func:`build_message` and :func:`send_telegram` after every
run. One short message at most: a draft is ready, a draft failed its checks, a
run failed, or the circuit breaker opened. Quiet runs (nothing to cover, paused,
skipped) send nothing.

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


def _link(url: str | None, label: str) -> str | None:
    """An HTML link, only when the URL points at GitHub."""
    if not url or not str(url).startswith(GITHUB_URL_PREFIX):
        return None
    return f'<a href="{html.escape(str(url), quote=True)}">{_escape(label, 80)}</a>'


_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)


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
) -> str | None:
    """The alert for one run, or None when the run needs no alert."""
    status = str(report.get("status") or "")
    breaker = report.get("breaker") if isinstance(report.get("breaker"), dict) else {}
    lines: list[str] = []

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
