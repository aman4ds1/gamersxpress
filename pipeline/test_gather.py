import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import gather

NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)

PAGE = "<html><body><article><h1>Headline</h1><p>" + ("article body. " * 40) + "</p></article></body></html>"


def cluster(items):
    return {"id": "story-1", "title": "Story", "items": items}


def item(id=1, link="https://a.example/1", name="Source A", tier=1, owner="co-a", region="us"):
    return {"link": link, "source_name": name, "tier": tier, "owner": owner, "region": region, "id": id}


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
    result = run_gather(tmp_path, fetcher=lambda url: None)
    assert result["sources"][0]["status"] == "fetch-error"
    assert result["sources"][0]["error"] == "no response body"

    result = run_gather(tmp_path, fetcher=lambda url: "<html>x</html>", extractor=lambda html: None)
    assert result["sources"][0]["status"] == "empty"


def test_gather_preserves_source_metadata(tmp_path):
    result = run_gather(tmp_path, fetcher=lambda url: "<html>x</html>", extractor=lambda html: "text")
    source = result["sources"][0]
    assert source["tier"] == 1
    assert source["owner"] == "co-a"
    assert source["region"] == "us"
    assert source["source_name"] == "Source A"


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