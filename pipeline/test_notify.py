import json
import logging
import urllib.error

import pytest

import notify

TOKEN = "123456:SECRET-TOKEN-VALUE"


class FakeResponse:
    def __init__(self, payload=None, status=200, raw=None):
        self.status = status
        self._raw = raw if raw is not None else json.dumps(payload if payload is not None else {"ok": True}).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_opener(response=None, error=None, calls=None):
    def opener(request, timeout=None):
        if calls is not None:
            calls.append((request, timeout))
        if error is not None:
            raise error
        return response or FakeResponse()

    return opener


# --- send_telegram ------------------------------------------------------------


def test_send_posts_the_message_and_reports_success():
    calls = []
    sent = notify.send_telegram(
        "hello <b>there</b>", token=TOKEN, chat_id="42", opener=make_opener(calls=calls), timeout=7
    )
    assert sent is True
    request, timeout = calls[0]
    assert request.full_url.endswith(f"/bot{TOKEN}/sendMessage")
    assert request.get_method() == "POST"
    assert timeout == 7
    body = json.loads(request.data)
    assert body == {
        "chat_id": "42",
        "text": "hello <b>there</b>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


def test_send_without_credentials_is_skipped_and_never_calls_out(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    calls = []
    assert notify.send_telegram("x", opener=make_opener(calls=calls)) is False
    assert calls == []


def test_send_reads_credentials_from_the_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    calls = []
    assert notify.send_telegram("x", opener=make_opener(calls=calls)) is True
    assert json.loads(calls[0][0].data)["chat_id"] == "99"


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None),
        urllib.error.URLError("no route"),
        TimeoutError("slow"),
        OSError("boom"),
        ValueError("bad"),
    ],
)
def test_send_never_raises_and_never_logs_the_token(error, caplog):
    caplog.set_level(logging.DEBUG)
    assert notify.send_telegram("x", token=TOKEN, chat_id="1", opener=make_opener(error=error)) is False
    assert "SECRET-TOKEN-VALUE" not in caplog.text
    assert "api.telegram.org" not in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({"ok": False}),
        FakeResponse({"ok": True}, status=500),
        FakeResponse(raw=b"not json"),
        FakeResponse(raw=b'["a list"]'),
    ],
)
def test_send_is_false_when_telegram_does_not_accept(response, caplog):
    caplog.set_level(logging.DEBUG)
    assert notify.send_telegram("x", token=TOKEN, chat_id="1", opener=make_opener(response=response)) is False
    assert "SECRET-TOKEN-VALUE" not in caplog.text


def test_long_messages_are_cut_on_a_line_boundary():
    line = "<b>row</b> " + "x" * 90
    text = "\n".join([line] * 80)
    sent_text = []

    def opener(request, timeout=None):
        sent_text.append(json.loads(request.data)["text"])
        return FakeResponse()

    assert notify.send_telegram(text, token=TOKEN, chat_id="1", opener=opener)
    assert len(sent_text[0]) <= notify.MAX_MESSAGE_CHARS
    assert all(row == line for row in sent_text[0].split("\n"))


# --- article_title ------------------------------------------------------------


def test_article_title_reads_the_front_matter(tmp_path):
    path = tmp_path / "a.md"
    path.write_text('---\ntitle: "Sony shows a PS5 collection"\ndescription: d\n---\n\nbody\n', encoding="utf-8")
    assert notify.article_title(path) == "Sony shows a PS5 collection"


def test_article_title_ignores_a_title_line_in_the_body(tmp_path):
    path = tmp_path / "a.md"
    path.write_text("---\ndescription: d\n---\n\ntitle: not the title\n", encoding="utf-8")
    assert notify.article_title(path) is None


def test_article_title_of_a_missing_file_is_none(tmp_path):
    assert notify.article_title(tmp_path / "missing.md") is None
    assert notify.article_title(None) is None


# --- build_message ------------------------------------------------------------

PR = "https://github.com/aman4ds1/gamersxpress/pull/7"
RUN = "https://github.com/aman4ds1/gamersxpress/actions/runs/55"


def test_completed_run_announces_the_draft_with_the_pull_request_link():
    message = notify.build_message(
        {"status": "completed", "slug": "a-slug", "category": "hardware"},
        title="NVIDIA <launches> a card",
        pr_url=PR,
        run_url=RUN,
    )
    assert "Draft ready" in message
    assert "NVIDIA &lt;launches&gt; a card" in message
    assert f'<a href="{PR}">Review the pull request</a>' in message
    assert "hardware" in message


def test_a_relayed_report_is_flagged():
    message = notify.build_message(
        {"status": "completed", "slug": "s", "verification": "single-origin"}, pr_url=PR
    )
    assert "Relayed report" in message


def test_completed_run_without_a_pull_request_says_so():
    message = notify.build_message({"status": "completed", "slug": "s"}, run_url=RUN)
    assert "No pull request was opened" in message
    assert RUN in message


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://evil.example/x", "https://github.com.evil.example/x"])
def test_links_that_do_not_point_at_github_are_dropped(url):
    message = notify.build_message({"status": "completed", "slug": "s"}, pr_url=url, run_url=url)
    assert "href" not in message


def test_failed_run_lists_the_first_failures_and_counts_the_rest():
    report = {
        "status": "failed",
        "slug": "draft-slug",
        "error": "publish gates failed",
        "failures": ["f1", "f2", "f3", "f4", "f5"],
    }
    message = notify.build_message(report, run_url=RUN)
    assert "Draft failed its checks" in message
    assert "• f1" in message and "• f3" in message
    assert "f4" not in message
    assert "and 2 more" in message
    assert RUN in message


def test_failed_run_without_a_slug_is_a_run_failure():
    message = notify.build_message({"status": "failed", "error": "boom"})
    assert "Pipeline run failed" in message
    assert "boom" in message


def test_an_open_breaker_is_announced_on_the_failing_run():
    message = notify.build_message({"status": "failed", "slug": "s", "breaker": {"open": True}})
    assert "Circuit breaker is open" in message


@pytest.mark.parametrize("status", ["nothing", "skipped", "paused"])
def test_quiet_runs_send_nothing(status):
    assert notify.build_message({"status": status}) is None


def test_a_paused_run_with_an_open_breaker_does_not_repeat_the_alert():
    assert notify.build_message({"status": "paused", "breaker": {"open": True}}) is None
