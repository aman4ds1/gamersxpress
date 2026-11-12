import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

import ingest
from ingest import ConfigError, FetchError


NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_feed_html_is_converted_to_text_with_paragraph_breaks():
    text = ingest.html_to_text("<p>First paragraph.</p><p>Second paragraph.</p>")
    assert text.splitlines() == ["First paragraph.", "Second paragraph."]
    assert ingest.html_to_text("<div>Only <b>one</b> line</div>") == "Only one line"
    assert ingest.html_to_text("<script>var x = 1;</script><p>Kept</p>") == "Kept"
    assert ingest.html_to_text("") == ""


def rss(items_xml: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel><title>Sample</title>\n'
        f"{items_xml}\n</channel></rss>"
    ).encode("utf-8")


def rss_with_content(items_xml: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        '<channel><title>Sample</title>\n'
        f"{items_xml}\n</channel></rss>"
    ).encode("utf-8")


def item_xml(title: str, link: str, pubdate: str | None = None) -> str:
    pub = f"<pubDate>{pubdate}</pubDate>" if pubdate else ""
    return f"<item><title>{title}</title><link>{link}</link>{pub}<description>Summary of {title}</description></item>"


def response(status=200, body=b"", content_type="application/rss+xml", etag=None, last_modified=None):
    return ingest.HttpResponse(status, content_type, body, etag, last_modified)


def make_fetcher(mapping: dict):
    def fetcher(url, *, etag=None, last_modified=None):
        if url in mapping:
            value = mapping[url]
            if isinstance(value, Exception):
                raise value
            return value
        raise FetchError(f"HTTP 404 {url}")

    return fetcher


def write_sources_config(tmp_path, sources):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"sources": sources}, sort_keys=False), encoding="utf-8")
    return path


def base_source(**overrides) -> dict:
    source = {
        "name": "Example Press",
        "site": "https://example.com",
        "feed_url": "https://example.com/feed",
        "tier": 2,
        "owner": "example-media",
        "region": "us",
        "categories": ["gaming-news"],
        "topic": "gaming",
    }
    source.update(overrides)
    return source


def run_minimal(tmp_path, sources, *, fetcher=None, cache=None, print_table=False):
    config_path = write_sources_config(tmp_path, sources)
    cache_path = tmp_path / "feed-cache.json"
    if cache is not None:
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
    health_path = tmp_path / "feed-health.json"
    return ingest.run(
        config_path=config_path,
        output_dir=tmp_path,
        cache_path=cache_path,
        health_path=health_path,
        now=NOW,
        fetcher=fetcher,
        delay=0,
        print_table=print_table,
    ), cache_path, health_path


# --- config validation -------------------------------------------------------


def test_load_sources_with_valid_entries(tmp_path):
    path = write_sources_config(
        tmp_path,
        [base_source(name="A", tier=1), base_source(name="B", tier=3)],
    )
    sources = ingest.load_sources(path)
    assert [s["name"] for s in sources] == ["A", "B"]


def test_load_sources_missing_field_refuses_to_run(tmp_path):
    path = write_sources_config(tmp_path, [base_source(owner=None)])
    with pytest.raises(ConfigError, match="missing 'owner'"):
        ingest.load_sources(path)


def test_load_sources_invalid_tier_refuses_to_run(tmp_path):
    path = write_sources_config(tmp_path, [base_source(tier=4)])
    with pytest.raises(ConfigError, match="invalid tier 4"):
        ingest.load_sources(path)


def test_load_sources_invalid_tier_string_rejected(tmp_path):
    path = write_sources_config(tmp_path, [base_source(tier="one")])
    with pytest.raises(ConfigError, match="invalid tier"):
        ingest.load_sources(path)


def test_load_sources_missing_categories_refuses_to_run(tmp_path):
    path = write_sources_config(tmp_path, [base_source(categories=[])])
    with pytest.raises(ConfigError, match="categories"):
        ingest.load_sources(path)
    path = write_sources_config(tmp_path, [base_source(categories=None)])
    with pytest.raises(ConfigError, match="categories"):
        ingest.load_sources(path)


def test_load_sources_missing_topic_refuses_to_run(tmp_path):
    path = write_sources_config(tmp_path, [base_source(topic=None)])
    with pytest.raises(ConfigError, match="topic"):
        ingest.load_sources(path)


def test_load_sources_invalid_topic_refuses_to_run(tmp_path):
    path = write_sources_config(tmp_path, [base_source(topic="fintech")])
    with pytest.raises(ConfigError, match="invalid topic"):
        ingest.load_sources(path)


def test_load_sources_empty_list_refuses_to_run(tmp_path):
    path = write_sources_config(tmp_path, [])
    with pytest.raises(ConfigError, match="sources"):
        ingest.load_sources(path)


def test_shipped_config_sources_validate():
    sources = ingest.load_sources()
    assert len(sources) >= 19
    assert all(s["tier"] in (1, 2, 3) for s in sources)
    assert all(s["topic"] in ("gaming", "mixed") for s in sources)


def test_shipped_sources_have_unique_names_and_feeds():
    sources = ingest.load_sources()
    names = [s["name"] for s in sources]
    feeds = [ingest.normalize_url(s["feed_url"]) for s in sources]
    assert len(names) == len(set(names)), "duplicate source name"
    assert len(feeds) == len(set(feeds)), "duplicate feed url"


def test_shipped_owner_names_are_lowercase_kebab_case():
    # "Valnet" and "valnet" would count one owner as two independent voices.
    import re

    for source in ingest.load_sources():
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source["owner"]), source


def test_shipped_sources_never_give_one_site_two_owners():
    owners_by_domain: dict[str, set[str]] = {}
    for source in ingest.load_sources():
        domain = ingest.registered_domain(source["site"])
        owners_by_domain.setdefault(domain, set()).add(source["owner"])
    assert {d: o for d, o in owners_by_domain.items() if len(o) > 1} == {}


# Outlets checked against their own pages or press coverage (October 2026). A
# split owner would count one voice twice and could confirm a story on one
# outlet's reporting, so every group below must share a single `owner`.
KNOWN_OWNERS = {
    "future": ["PC Gamer", "Tom's Hardware"],
    "ign-entertainment": ["Eurogamer", "Rock Paper Shotgun", "VG247"],
    "keleops": ["Kotaku"],
    "valnet": ["Polygon"],
    "1981-media": ["VGC"],
    "gematsu": ["Gematsu"],
    "insider-gaming": ["Insider Gaming"],
    "dexerto": ["Dexerto"],
    "kitguru": ["KitGuru"],
    # Esports outlets, checked November 2026: TalkEsport is independent (Mumbai); VLR.gg has no parent company
    # that could be found; esports.gg was acquired by eFuse, so it carries its parent's owner string.
    "talkesport": ["TalkEsport"],
    "vlr-gg": ["VLR.gg"],
    "efuse": ["esports.gg"],
}


def test_known_ownership_groups_share_one_owner_string():
    by_name = {s["name"]: s["owner"] for s in ingest.load_sources()}
    for owner, names in KNOWN_OWNERS.items():
        for name in names:
            assert by_name[name] == owner, f"{name} should be owned by {owner}, not {by_name[name]}"


def test_each_known_owner_is_a_distinct_voice():
    assert len(KNOWN_OWNERS) == len(set(KNOWN_OWNERS))
    owners = {s["owner"] for s in ingest.load_sources()}
    assert set(KNOWN_OWNERS) <= owners


def test_outlets_that_block_article_downloads_are_headline_only():
    by_name = {s["name"]: s for s in ingest.load_sources()}
    for name in ("VGC", "Gematsu"):
        assert by_name[name].get("fetch") is False, name


def test_outlets_with_off_topic_feeds_are_mixed_so_the_relevance_filter_applies():
    by_name = {s["name"]: s for s in ingest.load_sources()}
    for name in ("Dexerto", "Kotaku", "Polygon", "KitGuru"):
        assert by_name[name]["topic"] == "mixed", name


def test_a_blocked_feed_is_not_in_the_config():
    # Wccftech answers its feed with HTTP 403; the pipeline never works around a block.
    assert all("wccftech" not in s["feed_url"] for s in ingest.load_sources())


# --- URL normalization -------------------------------------------------------


def test_normalize_url():
    assert ingest.normalize_url("HTTPS://WWW.Example.com/Path?q=1#frag") == "https://example.com/Path?q=1"
    assert ingest.normalize_url("http://example.com") == "http://example.com/"
    assert ingest.normalize_url("https://blog.playstation.com/" ) == "https://blog.playstation.com/"
    assert ingest.normalize_url("") == ""


# --- registrable domains / re-attribution ------------------------------------


def test_registered_domain_handles_multilabel_suffixes():
    assert ingest.registered_domain("https://news.xbox.com/x") == "xbox.com"
    assert ingest.registered_domain("https://www.pushsquare.com/feeds/latest") == "pushsquare.com"
    assert ingest.registered_domain("https://esports-news.co.uk/feed") == "esports-news.co.uk"
    assert ingest.registered_domain("https://thebridge.in/x") == "thebridge.in"
    assert ingest.registered_domain("ign.com") == "ign.com"
    assert ingest.registered_domain("") == ""


def test_process_feed_records_discovery_and_fetch_flag():
    content = rss(item_xml("Meta", "https://news.xbox.com/m", "Thu, 09 Oct 2026 09:00:00 +0000"))
    source = base_source(name="Xbox Wire", site="https://news.xbox.com", tier=1, owner="microsoft", fetch=False)
    item = ingest.process_feed(source, content, NOW).items[0]
    assert item["discovered_via"] == "Xbox Wire"
    assert item["source_domain"] == "xbox.com"
    assert item["fetchable"] is False


def _reattribution_sources():
    return [
        base_source(name="Xbox Wire", site="https://news.xbox.com", tier=1, owner="microsoft"),
        base_source(name="IGN", site="https://www.ign.com", tier=2, owner="ign-entertainment"),
    ]


def test_reattribute_item_credits_configured_source_owning_the_link():
    items = [{
        "title": "t", "link": "https://www.ign.com/articles/x", "source_name": "Xbox Wire",
        "tier": 1, "owner": "microsoft", "region": "us", "source_domain": "xbox.com", "fetchable": True,
    }]
    ingest.reattribute_items(items, _reattribution_sources())
    assert items[0]["source_name"] == "IGN"
    assert items[0]["tier"] == 2
    assert items[0]["owner"] == "ign-entertainment"
    assert items[0]["external"] is True
    assert items[0]["discovered_via"] == "Xbox Wire"
    ingest_again = [dict(items[0])]
    ingest.reattribute_items(ingest_again, _reattribution_sources())
    assert ingest_again[0]["tier"] == 2, "re-attribution must be idempotent"


def test_reattribute_item_demotes_unknown_external_to_tier3():
    items = [{
        "title": "t", "link": "https://random.example/x", "source_name": "Xbox Wire",
        "tier": 1, "owner": "microsoft", "region": "us", "source_domain": "xbox.com",
    }]
    ingest.reattribute_items(items, _reattribution_sources())
    assert items[0]["tier"] == 3
    assert items[0]["owner"] == "random.example"
    assert items[0]["external"] is True


def test_reattribute_item_keeps_owned_link_untouched():
    items = [{
        "title": "t", "link": "https://news.xbox.com/x", "source_name": "Xbox Wire",
        "tier": 1, "owner": "microsoft", "region": "us", "source_domain": "xbox.com",
    }]
    ingest.reattribute_items(items, _reattribution_sources())
    assert items[0]["tier"] == 1
    assert "external" not in items[0]


def test_reattribute_item_skips_items_with_no_known_feed_domain():
    items = [{"title": "t", "link": "https://a.example/1", "source_name": "Tier One A", "tier": 1}]
    ingest.reattribute_items(items, _reattribution_sources())
    assert items[0]["tier"] == 1
    assert "external" not in items[0]


def test_reattribute_gathered_uses_final_url():
    gathered = {"id": "s", "sources": [{
        "link": "https://t.co/x", "final_url": "https://www.ign.com/articles/x",
        "source_name": "Xbox Wire", "tier": 1, "owner": "microsoft",
    }]}
    ingest.reattribute_gathered(gathered, _reattribution_sources())
    assert gathered["sources"][0]["source_name"] == "IGN"
    assert gathered["sources"][0]["tier"] == 2


# --- feed processing ---------------------------------------------------------


def test_process_feed_keeps_only_last_48_hours():
    content = rss(
        item_xml("Fresh story", "https://example.com/a", "Thu, 09 Oct 2026 09:00:00 +0000")
        + item_xml("Old story", "https://example.com/b", "Tue, 06 Oct 2026 09:00:00 +0000")
    )
    candidates = ingest.process_feed(base_source(), content, NOW)
    assert candidates.total_entries == 2
    assert len(candidates.items) == 1
    assert candidates.newest == "2026-10-09T09:00:00+00:00"
    assert candidates.items[0]["title"] == "Fresh story"
    assert candidates.items[0]["link"] == "https://example.com/a"
    assert candidates.items[0]["published"] == "2026-10-09T09:00:00+00:00"


def test_process_feed_drops_undated_items():
    content = rss(item_xml("No date", "https://example.com/c") + item_xml("Dated", "https://example.com/d", "Thu, 09 Oct 2026 09:00:00 +0000"))
    candidates = ingest.process_feed(base_source(), content, NOW)
    assert candidates.total_entries == 2
    assert [item["title"] for item in candidates.items] == ["Dated"]


def test_process_feed_records_source_metadata():
    content = rss(item_xml("Meta", "https://example.com/m", "Thu, 09 Oct 2026 09:00:00 +0000"))
    source = base_source(name="GameWire", tier=1, owner="big-co", region="india")
    candidates = ingest.process_feed(source, content, NOW)
    item = candidates.items[0]
    assert item["source_name"] == "GameWire"
    assert item["tier"] == 1
    assert item["owner"] == "big-co"
    assert item["region"] == "india"


def test_process_feed_empty_valid_xml_is_not_an_error():
    candidates = ingest.process_feed(base_source(), rss(), NOW)
    assert (candidates.total_entries, candidates.items) == (0, [])


def test_process_feed_garbage_raises():
    with pytest.raises(FetchError):
        ingest.process_feed(base_source(), b"not a feed", NOW)


# --- outcome classification --------------------------------------------------


@pytest.mark.parametrize("status", [403, 429, 503])
def test_classify_blocked_by_status(status):
    outcome, _ = ingest.classify(
        response(status=status, body=b"denied", content_type="text/html"), base_source(), NOW
    )
    assert outcome == "blocked"


def test_classify_blocked_by_html_body_on_ok_status():
    outcome, _ = ingest.classify(
        response(status=200, body=b"<!DOCTYPE html><html><body>humans only</body></html>", content_type="text/html"),
        base_source(), NOW,
    )
    assert outcome == "blocked"
    outcome, _ = ingest.classify(
        response(status=200, body=b"<html>blocked</html>", content_type="application/xml"),
        base_source(), NOW,
    )
    assert outcome == "blocked"


def test_classify_http_error():
    outcome, _ = ingest.classify(response(status=404, body=b"nope", content_type="text/html"), base_source(), NOW)
    assert outcome == "http-error"


def test_classify_not_modified():
    outcome, candidates = ingest.classify(response(status=304, body=b"", content_type=None), base_source(), NOW)
    assert outcome == "not-modified"
    assert candidates.total_entries == 0


def test_classify_empty_body_and_empty_feed():
    outcome, _ = ingest.classify(response(status=200, body=b"", content_type="application/rss+xml"), base_source(), NOW)
    assert outcome == "empty"
    outcome, _ = ingest.classify(response(status=200, body=rss(), content_type="application/rss+xml"), base_source(), NOW)
    assert outcome == "empty"


def test_classify_invalid_xml():
    outcome, _ = ingest.classify(response(status=200, body=b"not xml at all", content_type="application/rss+xml"), base_source(), NOW)
    assert outcome == "invalid-xml"


def test_classify_ok_with_counts():
    body = rss(
        item_xml("Fresh", "https://example.com/f", "Thu, 09 Oct 2026 09:00:00 +0000")
        + item_xml("Old", "https://example.com/o", "Tue, 01 Oct 2026 09:00:00 +0000")
    )
    outcome, candidates = ingest.classify(response(status=200, body=body), base_source(), NOW)
    assert outcome == "ok"
    assert candidates.total_entries == 2
    assert len(candidates.items) == 1
    assert candidates.newest == "2026-10-09T09:00:00+00:00"


# --- freshness config --------------------------------------------------------


def fresh_config(tmp_path, freshness):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"sources": [base_source()], "freshness": freshness}, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_load_freshness_parses_tier_windows_and_stale_after(tmp_path):
    path = fresh_config(
        tmp_path,
        {"windows": {1: "7 days", 2: "48 hours", 3: "24 hours"}, "stale_after": "7 days"},
    )
    windows, stale_after = ingest.load_freshness(path)
    assert windows == {
        1: dt.timedelta(days=7),
        2: dt.timedelta(hours=48),
        3: dt.timedelta(hours=24),
    }
    assert stale_after == dt.timedelta(days=7)


def test_load_freshness_missing_block_uses_defaults(tmp_path):
    path = write_sources_config(tmp_path, [base_source()])
    windows, stale_after = ingest.load_freshness(path)
    assert windows[1] == dt.timedelta(days=7)
    assert windows[2] == dt.timedelta(hours=48)
    assert windows[3] == dt.timedelta(hours=24)
    assert stale_after == dt.timedelta(days=7)


def test_load_freshness_accepts_bare_numbers_as_hours(tmp_path):
    path = fresh_config(tmp_path, {"windows": {1: 168, 2: 48, 3: 24}, "stale_after": 72})
    windows, stale_after = ingest.load_freshness(path)
    assert windows[1] == dt.timedelta(hours=168)
    assert stale_after == dt.timedelta(hours=72)


@pytest.mark.parametrize("freshness", [
    {"windows": {1: "7 days", 2: "48 hours"}, "stale_after": "7 days"},  # missing tier 3
    {"windows": {1: "7 days", 2: "48 hours", 3: "24 hours"}},            # missing stale_after
    {"windows": {1: "7 days", 2: "48 hours", 3: "parsecs"}, "stale_after": "7 days"},
    {"windows": {1: "0 days", 2: "48 hours", 3: "24 hours"}, "stale_after": "7 days"},
    {"windows": {1: "7 days", 2: "48 hours", 3: "24 hours", 4: "6 hours"}, "stale_after": "7 days"},  # tier 4
])
def test_load_freshness_invalid_refuses_to_run(tmp_path, freshness):
    path = fresh_config(tmp_path, freshness)
    with pytest.raises(ConfigError):
        ingest.load_freshness(path)


def test_freshness_config_in_shipped_config_valid(tmp_path):
    windows, stale_after = ingest.load_freshness()
    assert set(windows) == {1, 2, 3}
    assert stale_after > dt.timedelta(0)


# --- per-tier freshness windows ----------------------------------------------


def test_run_applies_freshness_window_per_tier(tmp_path):
    six_days_old = rss(
        item_xml("Tier1 story", "https://example.com/t1", "Fri, 03 Oct 2026 09:00:00 +0000")
    )

    def tiered_fetcher(url, *, etag=None, last_modified=None):
        return response(200, six_days_old, "application/rss+xml")

    for tier in (1, 2, 3):
        result, _, _ = run_minimal(
            tmp_path,
            [base_source(name=f"Tier{tier}", tier=tier, feed_url="https://example.com/feed")],
            fetcher=tiered_fetcher,
        )
        # NOW is 2026-10-09 12:00 UTC; the item is from 2026-10-03 09:00 UTC,
        # ~6.1 days old. Tier 1 (7 days) keeps it; tiers 2 (48h) and 3 (24h) drop it.
        if tier == 1:
            assert result["feeds"][0]["window_count"] == 1
            assert result["items"][0]["tier"] == 1
        else:
            assert result["feeds"][0]["window_count"] == 0
            assert result["items"] == []


def test_items_carry_age_hours(tmp_path):
    three_hours_ago = rss(
        item_xml("Recent", "https://example.com/r", "Thu, 09 Oct 2026 09:00:00 +0000")
    )

    def fetcher(url, *, etag=None, last_modified=None):
        return response(200, three_hours_ago, "application/rss+xml")

    result, _, _ = run_minimal(tmp_path, [base_source(feed_url="https://example.com/feed")], fetcher=fetcher)
    assert result["items"][0]["age_hours"] == 3.0


def test_process_feed_rejects_outside_custom_window():
    day_old = rss(item_xml("Day old", "https://example.com/d", "Thu, 08 Oct 2026 09:00:00 +0000"))
    candidates = ingest.process_feed(base_source(), day_old, NOW, window=dt.timedelta(hours=12))
    assert candidates.items == []
    assert candidates.newest == "2026-10-08T09:00:00+00:00"


def test_run_exposes_freshness_in_result(tmp_path):
    def fetcher(url, *, etag=None, last_modified=None):
        return response(200, rss(item_xml("x", "https://example.com/x", "Thu, 09 Oct 2026 09:00:00 +0000")), "application/rss+xml")

    result, _, health_path = run_minimal(tmp_path, [base_source()], fetcher=fetcher)
    assert result["freshness"]["windows"] == {1: 168.0, 2: 48.0, 3: 24.0}
    assert result["freshness"]["stale_after_hours"] == 168.0
    report = json.loads(health_path.read_text(encoding="utf-8"))
    assert report["freshness"]["stale_after_hours"] == 168.0
    assert report["feeds"][0]["window_hours"] == 48.0


# --- full run ----------------------------------------------------------------


def test_run_passes_config_and_writes_candidates(tmp_path):
    fresh = rss(
        item_xml("Good item", "https://www.example.com/g", "Thu, 09 Oct 2026 08:00:00 +0000")
        + item_xml("Old item", "https://example.com/o", "Tue, 01 Oct 2026 08:00:00 +0000")
    )
    sources = [
        base_source(name="Good", feed_url="https://good.example/feed", tier=1, owner="one"),
        base_source(name="Blocked", feed_url="https://blocked.example/feed"),
        base_source(name="Empty", feed_url="https://empty.example/feed"),
    ]
    fetcher = make_fetcher(
        {
            "https://good.example/feed": response(200, fresh, "application/rss+xml", etag='"g1"'),
            "https://blocked.example/feed": response(403, b"<html>no</html>", "text/html"),
            "https://empty.example/feed": response(200, rss(), "application/rss+xml"),
        }
    )

    result, cache_path, health_path = run_minimal(tmp_path, sources, fetcher=fetcher)

    assert result["timestamp"] == "20261009T120000Z"
    out_file = tmp_path / "20261009T120000Z.json"
    assert out_file.is_file()
    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(written) == 1
    assert written[0]["title"] == "Good item"
    assert written[0]["link"] == "https://example.com/g"  # www. dropped
    assert written[0]["source_name"] == "Good"
    assert written[0]["tier"] == 1
    assert written[0]["owner"] == "one"

    feeds = {f["name"]: f for f in result["feeds"]}
    assert feeds["Good"]["outcome"] == "ok"
    assert feeds["Good"]["status"] == 200
    assert feeds["Good"]["total_entries"] == 2
    assert feeds["Good"]["window_count"] == 1
    assert feeds["Good"]["error"] is None
    assert feeds["Blocked"]["outcome"] == "blocked"
    assert feeds["Blocked"]["status"] == 403
    assert feeds["Blocked"]["bytes_received"] == len(b"<html>no</html>")
    assert feeds["Empty"]["outcome"] == "empty"
    assert feeds["Empty"]["total_entries"] == 0


def test_run_fetch_timeout_and_network_error(tmp_path):
    def timeout_fetcher(url, *, etag=None, last_modified=None):
        raise FetchError("timed out", kind="timeout")

    result, _, _ = run_minimal(tmp_path, [base_source(name="Slow")], fetcher=timeout_fetcher)
    row = result["feeds"][0]
    assert row["outcome"] == "timeout"
    assert row["status"] is None
    assert row["total_entries"] == 0
    assert "timed out" in row["error"]

    def network_fetcher(url, *, etag=None, last_modified=None):
        raise FetchError("connection reset", kind="network")

    result, _, _ = run_minimal(tmp_path, [base_source(name="Down")], fetcher=network_fetcher)
    assert result["feeds"][0]["outcome"] == "http-error"


def test_run_sends_conditional_headers_from_cache(tmp_path):
    url = "https://example.com/feed"
    seen = {}

    def recording_fetcher(feed_url, *, etag=None, last_modified=None):
        seen["etag"] = etag
        seen["last_modified"] = last_modified
        return response(304, b"", None)

    _, cache_path, _ = run_minimal(
        tmp_path,
        [base_source(feed_url=url)],
        fetcher=recording_fetcher,
        cache={"feeds": {url: {"etag": '"abc"', "last_modified": "Wed, 07 Oct 2026 10:00:00 GMT"}}},
    )
    assert seen == {"etag": '"abc"', "last_modified": "Wed, 07 Oct 2026 10:00:00 GMT"}


def test_run_saves_etag_and_last_modified_to_cache(tmp_path):
    url = "https://example.com/feed"

    def etag_fetcher(feed_url, *, etag=None, last_modified=None):
        return response(
            200, rss(item_xml("Live", "https://example.com/l", "Thu, 09 Oct 2026 09:00:00 +0000")),
            "application/rss+xml", etag='"v42"', last_modified="Thu, 09 Oct 2026 09:00:00 GMT",
        )

    _, cache_path, _ = run_minimal(tmp_path, [base_source(feed_url=url)], fetcher=etag_fetcher)
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["feeds"][url] == {"etag": '"v42"', "last_modified": "Thu, 09 Oct 2026 09:00:00 GMT"}


def test_run_304_keeps_cached_etag(tmp_path):
    url = "https://example.com/feed"

    def not_modified_fetcher(feed_url, *, etag=None, last_modified=None):
        return response(304, b"", None)

    result, cache_path, _ = run_minimal(
        tmp_path,
        [base_source(feed_url=url)],
        fetcher=not_modified_fetcher,
        cache={"feeds": {url: {"etag": '"abc"'}}},
    )
    assert result["feeds"][0]["outcome"] == "not-modified"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["feeds"][url]["etag"] == '"abc"'


def test_run_writes_health_report(tmp_path):
    fresh = rss(item_xml("Hot", "https://example.com/h", "Thu, 09 Oct 2026 09:00:00 +0000"))
    sources = [
        base_source(name="Good", feed_url="https://good.example/feed"),
        base_source(name="Blocked", feed_url="https://blocked.example/feed"),
        base_source(name="Invalid", feed_url="https://invalid.example/feed"),
        base_source(name="Empty", feed_url="https://empty.example/feed"),
        base_source(name="Timeout", feed_url="https://timeout.example/feed"),
    ]

    def mixed_fetcher(url, *, etag=None, last_modified=None):
        mapping = {
            "https://good.example/feed": response(200, fresh, "application/rss+xml"),
            "https://blocked.example/feed": response(503, b"<html>maintenance</html>", "text/html"),
            "https://invalid.example/feed": response(200, b"<broken>", "application/rss+xml"),
            "https://empty.example/feed": response(200, rss(), "application/rss+xml"),
        }
        if url in mapping:
            return mapping[url]
        raise FetchError("timed out", kind="timeout")

    result, _, health_path = run_minimal(tmp_path, sources, fetcher=mixed_fetcher)
    report = json.loads(health_path.read_text(encoding="utf-8"))
    assert report["summary"] == {"total": 5, "ok": 1, "blocked": 1, "invalid-xml": 1, "empty": 1, "timeout": 1}

    by_name = {row["name"]: row for row in report["feeds"]}
    assert by_name["Good"]["status"] == 200
    assert by_name["Good"]["outcome"] == "ok"
    assert by_name["Good"]["total_entries"] == 1
    assert by_name["Good"]["window_count"] == 1
    assert by_name["Good"]["newest"] == "2026-10-09T09:00:00+00:00"
    assert by_name["Blocked"]["outcome"] == "blocked"
    assert by_name["Invalid"]["outcome"] == "invalid-xml"
    assert by_name["Empty"]["outcome"] == "empty"
    assert by_name["Timeout"]["outcome"] == "timeout"


def test_run_prints_feed_table(capsys, tmp_path):
    fresh = rss(item_xml("Hot", "https://example.com/h", "Thu, 09 Oct 2026 09:00:00 +0000"))

    def ok_fetcher(url, *, etag=None, last_modified=None):
        return response(200, fresh, "application/rss+xml")

    run_minimal(tmp_path, [base_source(name="Example Press")], fetcher=ok_fetcher, print_table=True)
    out = capsys.readouterr().out
    assert "FEED" in out
    assert "TIER" in out
    assert "OUTCOME" in out
    assert "ENTRIES" in out
    assert "NEWEST" in out
    assert "WINDOW" in out
    assert "Example Press" in out
    assert "ok" in out


# --- real HTTP fetch path ----------------------------------------------------


class _FeedHandler(BaseHTTPRequestHandler):
    seen = {}

    def do_GET(self):
        type(self).seen = {
            "if-none-match": self.headers.get("If-None-Match"),
            "if-modified-since": self.headers.get("If-Modified-Since"),
            "ua": self.headers.get("User-Agent"),
        }
        body = rss(item_xml("Live", "http://127.0.0.1:0/d", "Thu, 09 Oct 2026 09:00:00 +0000"))
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("ETag", '"live1"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _NotModifiedHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(304)
        self.send_header("ETag", '"abc"')
        self.end_headers()

    def log_message(self, *args):
        pass


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_http_fetch_sends_ua_and_conditional_headers():
    server, thread = _serve(_FeedHandler)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/feed"
        resp = ingest._http_fetch(url, etag='"abc"', last_modified="Wed, 07 Oct 2026 10:00:00 GMT")
    finally:
        server.shutdown()
        thread.join()
    assert resp.status == 200
    assert resp.content_type == "application/rss+xml"
    assert resp.etag == '"live1"'
    assert _FeedHandler.seen["if-none-match"] == '"abc"'
    assert _FeedHandler.seen["if-modified-since"] == "Wed, 07 Oct 2026 10:00:00 GMT"
    assert _FeedHandler.seen["ua"].startswith("GamersXpress")


def test_http_fetch_returns_304_response_not_error():
    server, thread = _serve(_NotModifiedHandler)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/feed"
        resp = ingest._http_fetch(url, etag='"abc"')
    finally:
        server.shutdown()
        thread.join()
    assert resp.status == 304
    assert resp.etag == '"abc"'


def test_run_uses_real_config_sources(tmp_path):
    config = ingest.load_sources()
    urls = {s["feed_url"] for s in config}

    def fake_fetcher(url, *, etag=None, last_modified=None):
        if url in urls:
            return response(200, b"", content_type=None)
        raise FetchError("unexpected url")

    result = ingest.run(
        config_path=None,
        output_dir=tmp_path,
        cache_path=tmp_path / "feed-cache.json",
        health_path=tmp_path / "feed-health.json",
        now=NOW,
        fetcher=fake_fetcher,
        delay=0,
    )
    assert len(result["feeds"]) == len(config)
    assert all(row["outcome"] == "empty" for row in result["feeds"])


# --- feed content: the long-form body kept alongside the summary --------------

CONTENT_ITEM = (
    "<item><title>Meta</title><link>https://news.xbox.com/m</link>"
    "<pubDate>Thu, 09 Oct 2026 09:00:00 +0000</pubDate>"
    "<description>Short summary of the post.</description>"
    "<content:encoded><![CDATA["
    "<p>First paragraph of the post with the actual reporting.</p>"
    "<p>Second paragraph that carries the rest of the detail.</p>"
    "]]></content:encoded></item>"
)


def test_process_feed_keeps_full_content_alongside_the_summary():
    candidates = ingest.process_feed(base_source(), rss_with_content(CONTENT_ITEM), NOW)
    item = candidates.items[0]
    assert item["title"] == "Meta"
    assert item["content"].splitlines() == [
        "First paragraph of the post with the actual reporting.",
        "Second paragraph that carries the rest of the detail.",
    ]


def test_process_feed_content_is_empty_when_the_feed_carries_none():
    candidates = ingest.process_feed(base_source(), rss(item_xml("Meta", "https://news.xbox.com/m", "Thu, 09 Oct 2026 09:00:00 +0000")), NOW)
    assert candidates.items[0]["content"] == ""


# --- the esports sources ---------------------------------------------------------------------------------------------

import re as _re
from pathlib import Path as _Path

_SITE_CATEGORIES = set(
    _re.findall(r"^\s*'?([a-z-]+)'?: '#[0-9A-Fa-f]{6}'", (_Path(__file__).resolve().parent.parent / "src" / "data" / "categories.ts").read_text(encoding="utf-8"), _re.M)
)
_NEW_ESPORTS_SOURCES = {
    "TalkEsport": ("https://www.talkesport.com", "talkesport", "india", ["india", "esports"]),
    "VLR.gg": ("https://www.vlr.gg", "vlr-gg", "global", ["esports"]),
    "esports.gg": ("https://esports.gg", "efuse", "us", ["esports"]),
}


def test_every_source_uses_only_categories_and_regions_the_site_knows():
    assert len(_SITE_CATEGORIES) == 9
    for source in ingest.load_sources():
        assert set(source["categories"]) <= _SITE_CATEGORIES, source["name"]
        assert source["region"] in {"global", "us", "uk", "india"}, source["name"]


def test_the_november_esports_sources_are_present_and_set_up_as_tier_two_gaming_feeds():
    by_name = {s["name"]: s for s in ingest.load_sources()}
    for name, (site, owner, region, categories) in _NEW_ESPORTS_SOURCES.items():
        source = by_name[name]
        assert (source["site"], source["owner"], source["region"], source["categories"]) == (site, owner, region, categories)
        assert source["tier"] == 2 and source["topic"] == "gaming"
        assert source["feed_url"].startswith("https://")
        assert ingest.registered_domain(source["feed_url"]) == ingest.registered_domain(source["site"]), "a feed on another site is a typo"
        assert source.get("fetch", True) is True


def test_the_esports_sources_are_all_different_voices():
    esports = [s for s in ingest.load_sources() if "esports" in s["categories"]]
    owners = [s["owner"] for s in esports]
    assert len(esports) >= 7
    assert len(owners) == len(set(owners)), "two esports outlets must not share an owner unless they share a parent"
    assert {"talkesport", "vlr-gg", "efuse", "dot-esports", "esports-news-uk", "the-bridge", "outlook-india"} <= set(owners)


def test_india_has_more_than_one_outlet_so_an_indian_story_can_be_confirmed():
    indian = {s["owner"] for s in ingest.load_sources() if "india" in s["categories"]}
    assert {"talkesport", "the-bridge", "outlook-india"} <= indian


def test_the_new_feeds_are_marked_untested_until_someone_runs_them():
    text = (_Path(__file__).resolve().parent / "config.yaml").read_text(encoding="utf-8")
    block = text[text.index("# Added November 2026: esports news."): text.index("# Added October 2026")]
    assert "UNVERIFIED" in block and "python -m pipeline.ingest" in block and "fetch: false" in block
    for name in _NEW_ESPORTS_SOURCES:
        assert f"- name: {name}" in block



def _sample_feed(link: str, title: str, hours_ago: int = 2) -> bytes:
    import datetime as _dt
    published = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>'
        f"<item><title>{title}</title><link>{link}</link><guid>{link}</guid><pubDate>{published}</pubDate>"
        "<description>A short summary of the story.</description></item></channel></rss>"
    ).encode()


@pytest.mark.parametrize("name, link", [
    ("TalkEsport", "https://www.talkesport.com/news/bgmi-masters-series-finals-announced/"),
    ("VLR.gg", "https://www.vlr.gg/123456/team-announces-new-roster"),
    ("esports.gg", "https://esports.gg/news/valorant/champions-update/"),
])
def test_an_item_from_each_new_esports_feed_is_tagged_with_its_outlet(name, link):
    import datetime as _dt
    source = next(s for s in ingest.load_sources() if s["name"] == name)
    now = _dt.datetime.now(_dt.timezone.utc)
    candidates = ingest.process_feed(source, _sample_feed(link, f"A story from {name}"), now)
    assert candidates.total_entries == 1 and len(candidates.items) == 1
    item = candidates.items[0]
    assert item["source_name"] == name and item["discovered_via"] == name and item["tier"] == 2
    assert item["owner"] == source["owner"] and item["region"] == source["region"]
    assert item["source_domain"] == ingest.registered_domain(source["site"])
    assert item["link"] == ingest.normalize_url(link)
    assert item["fetchable"] is True, "article text is downloaded for these outlets (no fetch: false)"


@pytest.mark.parametrize("name, link", [
    ("TalkEsport", "https://www.talkesport.com/news/some-story/"),
    ("VLR.gg", "https://www.vlr.gg/999/some-story"),
    ("esports.gg", "https://esports.gg/news/some-story/"),
])
def test_a_link_on_a_new_esports_site_is_recognised_as_that_outlet_even_in_a_pool_from_another_feed(name, link):
    sources = ingest.load_sources()
    item = {"link": link, "source_name": "Dexerto", "source_tier": 2, "owner": "dexerto"}
    ingest.reattribute_item(item, by_domain=ingest._source_index(sources)[0], by_name=ingest._source_index(sources)[1])
    expected = next(s for s in sources if s["name"] == name)
    assert item["source_name"] == name and item["owner"] == expected["owner"]
