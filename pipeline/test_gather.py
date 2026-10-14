import datetime as dt
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import gather
import ingest

NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)

PAGE = "<html><body><article><h1>Headline</h1><p>" + ("article body. " * 40) + "</p></article></body></html>"


def cluster(items):
    return {"id": "story-1", "title": "Story", "items": items}


def item(id=1, link="https://a.example/1", name="Source A", tier=1, owner="co-a", region="us", published=None, fetchable=True):
    return {"link": link, "source_name": name, "tier": tier, "owner": owner, "region": region, "published": published, "id": id, "fetchable": fetchable}


def always_true(_url):
    return True


def always_false(_url):
    return False


def run_gather(tmp_path, *, items=None, fetcher=None, extractor=None, robots_call=True, **kwargs):
    fetcher = fetcher or (lambda url: None)
    return gather.gather(
        cluster(items or [item()]),
        output_dir=tmp_path,
        now=NOW,
        fetcher=fetcher,
        extractor=extractor or (lambda html: html),
        robots_call=always_true if robots_call is True else robots_call,
        delay=0,
        **kwargs,
    )


def test_gather_fetches_and_extracts_full_texts(tmp_path):
    fetched = {}

    def fetcher(url):
        fetched[url] = True
        return f"<html>{url}</html>"

    result = run_gather(
        tmp_path,
        items=[
            item(1, "https://a.example/1", "Alpha", 1, "co-a", "us"),
            item(2, "https://b.example/2", "Beta", 2, "co-b", "uk"),
        ],
        fetcher=fetcher,
        extractor=lambda html: f"full text of {html}",
    )
    assert result["id"] == "story-1"
    assert fetched == {"https://a.example/1": True, "https://b.example/2": True}
    by_link = {s["link"]: s for s in result["sources"]}
    assert by_link["https://a.example/1"]["status"] == "ok"
    assert by_link["https://b.example/2"]["text"] == "full text of <html>https://b.example/2</html>"


def test_gather_writes_json_per_cluster(tmp_path):
    run_gather(tmp_path, fetcher=lambda url: "<html>body</html>", extractor=lambda html: "text")
    written = json.loads((tmp_path / "story-1.json").read_text(encoding="utf-8"))
    assert written["id"] == "story-1"
    assert written["gathered_at"] == NOW.isoformat()
    assert written["sources"][0]["text"] == "text"


def test_gather_respects_robots_and_skips_fetch(tmp_path):
    calls = []

    def fetcher(url):
        calls.append(url)
        return "<html>nope</html>"

    def allowed(url):
        return url != "https://b.example/2"

    result = run_gather(
        tmp_path,
        items=[item(1), item(2, "https://b.example/2")],
        fetcher=fetcher,
        robots_call=allowed,
    )
    by_link = {s["link"]: s for s in result["sources"]}
    assert by_link["https://a.example/1"]["status"] == "ok"
    assert by_link["https://b.example/2"]["status"] == "denied-robots"
    assert by_link["https://b.example/2"]["error"] == "disallowed by robots.txt"
    assert by_link["https://b.example/2"]["text"] == ""
    assert calls == ["https://a.example/1"]


def test_gather_fetch_error_does_not_crash(tmp_path):
    def broken(url):
        raise ConnectionError("boom")

    result = run_gather(tmp_path, fetcher=broken)
    assert result["sources"][0]["status"] == "fetch-error"
    assert result["sources"][0]["error"] == "boom"


def test_gather_empty_body_and_empty_text(tmp_path):
    result = run_gather(tmp_path, id="no-body", fetcher=lambda url: None)
    assert result["sources"][0]["status"] == "fetch-error"
    assert result["sources"][0]["error"] == "no response body"

    result = run_gather(tmp_path, id="no-text", fetcher=lambda url: "<html>x</html>", extractor=lambda html: None)
    assert result["sources"][0]["status"] == "empty"


def test_gather_preserves_source_metadata(tmp_path):
    result = run_gather(
        tmp_path,
        items=[item(published="2026-10-07T17:00:00+00:00")],
        fetcher=lambda url: "<html>x</html>",
        extractor=lambda html: "text",
    )
    source = result["sources"][0]
    assert source["tier"] == 1
    assert source["owner"] == "co-a"
    assert source["region"] == "us"
    assert source["source_name"] == "Source A"
    assert source["published"] == "2026-10-07T17:00:00+00:00"


def test_gather_reuses_file_when_fingerprint_matches(tmp_path):
    calls = []

    def fetcher(url):
        calls.append(url)
        return "<html>body</html>"

    first = run_gather(tmp_path, fetcher=fetcher, extractor=lambda html: "text")
    assert calls == ["https://a.example/1"]

    second = run_gather(tmp_path, fetcher=fetcher, extractor=lambda html: "text")
    assert second == first
    assert second["fingerprint"]["value"] == first["fingerprint"]["value"]
    assert calls == ["https://a.example/1"], "fetcher must not run again on a fingerprint match"


def test_gather_regenerates_when_fingerprint_mismatches(tmp_path):
    calls = []

    def fetcher(url):
        calls.append(url)
        return "<html>body</html>"

    run_gather(tmp_path, fetcher=fetcher, extractor=lambda html: "text")
    path = tmp_path / "story-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fingerprint"]["value"] = "stale"
    path.write_text(json.dumps(payload), encoding="utf-8")

    calls.clear()
    run_gather(tmp_path, fetcher=fetcher, extractor=lambda html: "text")
    assert calls == ["https://a.example/1"], "a stale fingerprint must not be reused"
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["fingerprint"]["value"] != "stale"


def test_gather_regenerates_when_file_has_no_fingerprint(tmp_path):
    callers = []

    def fetcher(url):
        callers.append(url)
        return "<html>body</html>"

    run_gather(tmp_path, fetcher=fetcher, extractor=lambda html: "text")
    path = tmp_path / "story-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["fingerprint"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    callers.clear()
    run_gather(tmp_path, fetcher=fetcher, extractor=lambda html: "text")
    assert callers == ["https://a.example/1"], "a pre-fingerprint file must be re-fetched"


def test_extract_text_uses_trafilatura_on_real_html():
    text = gather.extract_text("<html><head><title>t</title></head><body><article><h1>Hello world</h1><p>This is an article body for extraction.</p></article></body></html>")
    assert text is not None
    assert "Hello world" in text
    assert "article body" in text


def test_trafilatura_settings_keep_all_default_options():
    from trafilatura.downloads import DEFAULT_CONFIG

    settings = gather._trafilatura_settings()
    for option in DEFAULT_CONFIG.defaults():
        assert settings.has_option("DEFAULT", option), option
    assert settings.getint("DEFAULT", "download_timeout") == gather.FETCH_TIMEOUT_SECONDS
    assert settings.get("DEFAULT", "USER_AGENTS") == gather.USER_AGENT


# --- robots.txt --------------------------------------------------------------


class _RobotsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /private/\n"
        elif self.path.startswith("/private/"):
            body = b""
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            body = b"<html><body><h1>public page</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def robots_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RobotsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield server, base
    server.shutdown()
    thread.join()


def test_robots_allowed_denies_disallowed_path(robots_server):
    server, base = robots_server
    cache = {}
    assert gather.robots_allowed(f"{base}/news/1", cache=cache) is True
    assert gather.robots_allowed(f"{base}/private/secret", cache=cache) is False


def test_robots_allowed_caches_per_host(robots_server):
    server, base = robots_server
    cache = {}
    gather.robots_allowed(f"{base}/news/1", cache=cache)
    gather.robots_allowed(f"{base}/news/2", cache=cache)
    assert len(cache) == 1


def test_robots_allowed_allow_on_unreachable():
    cache = {}
    assert gather.robots_allowed("http://127.0.0.1:1/x", timeout=1, cache=cache) is True


# --- fetch:false, final_url and fetch health ---------------------------------


def test_gather_skips_fetch_disabled_sources(tmp_path):
    calls = []

    def fetcher(url):
        calls.append(url)
        return "<html>x</html>"

    result = run_gather(tmp_path, items=[item(1, fetchable=False)], fetcher=fetcher)
    source = result["sources"][0]
    assert source["status"] == "skipped"
    assert source["error"] == "source fetch disabled"
    assert source["text"] == ""
    assert calls == [], "a fetch-disabled source must never hit the network"


def test_gather_records_final_url_from_tuple_fetcher(tmp_path):
    def fetcher(url):
        return "<html>x</html>", "https://redirect.example/final"

    result = run_gather(
        tmp_path,
        items=[item(1, "https://t.co/x")],
        fetcher=fetcher,
        extractor=lambda html: "text",
    )
    source = result["sources"][0]
    assert source["status"] == "ok"
    assert source["final_url"] == "https://redirect.example/final"


def test_gather_records_final_url_equal_to_link_for_plain_fetcher(tmp_path):
    result = run_gather(tmp_path, fetcher=lambda url: "<html>x</html>", extractor=lambda html: "text")
    assert result["sources"][0]["final_url"] == "https://a.example/1"


def test_redirect_to_another_domain_is_recorded_and_demoted_to_tier3(tmp_path):
    # A short link on a configured tier-1 feed redirects to a blog we do not
    # configure: the page must be credited to the domain it finally lived on.
    def fetcher(url):
        return "<html>page</html>", "https://someblog.example/post"

    result = run_gather(
        tmp_path,
        items=[item(1, "https://t.co/x", "Xbox Wire", 1, "microsoft")],
        fetcher=fetcher,
        extractor=lambda html: "text",
    )
    source = result["sources"][0]
    assert source["final_url"] == "https://someblog.example/post"

    sources = [{"name": "Xbox Wire", "site": "https://news.xbox.com", "tier": 1, "owner": "microsoft"}]
    ingest.reattribute_gathered(result, sources)
    # Post-gather re-attribution uses final_url, so the unconfigured domain wins:
    # the source drops to tier 3 and can no longer confirm a story (PLAN.md
    # principle: tier 3 never counts toward confirmation).
    assert source["source_name"] == "someblog.example"
    assert source["tier"] == 3
    assert source["owner"] == "someblog.example"
    assert source["external"] is True


def test_same_domain_redirect_keeps_the_source_tier(tmp_path):
    # eurogamer.net -> www.eurogamer.net is the same registrable domain, so the
    # configured source keeps its tier.
    def fetcher(url):
        return "<html>page</html>", "https://www.eurogamer.net/foo"

    result = run_gather(
        tmp_path,
        items=[item(1, "https://eurogamer.net/foo", "Eurogamer", 2, "ign-entertainment")],
        fetcher=fetcher,
        extractor=lambda html: "text",
    )
    sources = [{"name": "Eurogamer", "site": "https://www.eurogamer.net", "tier": 2, "owner": "ign-entertainment"}]
    ingest.reattribute_gathered(result, sources)
    source = result["sources"][0]
    assert source["final_url"] == "https://www.eurogamer.net/foo"
    assert source["tier"] == 2
    assert source["source_name"] == "Eurogamer"


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/short":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"<html><body><h1>final page</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_fetcher_follows_redirect_and_reports_final_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        html, final_url = gather.fetch_html_and_url(f"{base}/short")
        assert html is not None and "final page" in html
        assert final_url == f"{base}/final"
    finally:
        server.shutdown()
        thread.join()


class _ForbiddenHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_403_is_not_retried():
    _ForbiddenHandler.requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ForbiddenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/blocked"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            gather.fetch_html_and_url(url)
        assert excinfo.value.code == 403
        assert _ForbiddenHandler.requests == 1, "a 403 must not be retried"
    finally:
        server.shutdown()
        thread.join()


def test_non_html_response_is_rejected():
    class _JsonHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"not": "html"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/data.json"
        with pytest.raises(ValueError, match="not HTML"):
            gather.fetch_html_and_url(url)
    finally:
        server.shutdown()
        thread.join()


def test_update_fetch_health_tracks_success_and_failure_per_domain(tmp_path):
    path = tmp_path / "fetch-health.json"
    gathered = {
        "sources": [
            {"link": "https://news.xbox.com/a", "final_url": "https://news.xbox.com/a", "status": "ok", "text": "t"},
            {"link": "https://xbox.com/b", "final_url": "https://xbox.com/b", "status": "fetch-error", "error": "boom"},
            {"link": "https://ign.com/c", "final_url": "https://ign.com/c", "status": "skipped", "error": "source fetch disabled"},
        ]
    }
    data = gather.update_fetch_health(gathered, path, now=NOW)
    assert data["domains"]["xbox.com"]["successes"] == 1
    assert data["domains"]["xbox.com"]["failures"] == 1
    assert data["domains"]["xbox.com"]["last_failure_reason"] == "boom"
    assert "ign.com" not in data["domains"], "a skipped source is not a fetch attempt"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["updated_at"] == NOW.isoformat()


def test_print_fetch_health_summary_lists_domains():
    data = {"domains": {"xbox.com": {"successes": 1, "failures": 2}}}
    lines = gather.print_fetch_health_summary(data)
    assert lines == ["fetch health: xbox.com 1 ok / 2 failed"]