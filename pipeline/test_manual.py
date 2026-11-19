"""Owner-requested topics: matching, request parsing, and the manual run path."""

from __future__ import annotations

import datetime as dt
import json

import dryrun
import manual
import run as run_module
from test_run import NOW, item, make_paths, run_with_items


def pooled(title, link, owner, age_hours=1):
    return item(title, link, owner=owner, age_hours=age_hours)


def test_parse_urls_keeps_order_and_drops_duplicates_and_trailing_punctuation():
    text = "see https://a.example/x, and https://b.example/y. also https://a.example/x"
    assert manual.parse_urls(text) == ["https://a.example/x", "https://b.example/y"]
    assert manual.parse_urls(None) == []


def test_parse_request_needs_a_topic_and_trims_fields():
    assert manual.parse_request({}) is None
    assert manual.parse_request({"MANUAL_TOPIC": "  "}) is None
    got = manual.parse_request(
        {"MANUAL_TOPIC": " GTA  6 ", "MANUAL_IDEA": " why\nit matters ", "MANUAL_URLS": "https://a.example/1"}
    )
    assert got == {"topic": "GTA 6", "idea": "why it matters", "urls": ["https://a.example/1"]}


def test_gta_6_topic_also_matches_gta_vi_and_grand_theft_auto():
    items = [
        pooled("GTA VI breaks Twitch record", "https://a.example/1", "a"),
        pooled("Grand Theft Auto 6 launch crowds", "https://b.example/1", "b"),
        pooled("GTA 5 gets an update", "https://c.example/1", "c"),
        pooled("Zelda news", "https://d.example/1", "d"),
    ]
    got = manual.matching_items("GTA 6", "", items)
    assert {i["owner"] for i in got} == {"a", "b"}


def test_one_item_per_outlet_and_idea_overlap_ranks_first():
    items = [
        pooled("GTA 6 map size revealed", "https://a.example/1", "a"),
        pooled("GTA 6 map size compared again", "https://a.example/2", "a"),
        pooled("GTA 6 price speculation", "https://b.example/1", "b"),
    ]
    got = manual.matching_items("GTA 6", "price speculation", items)
    assert got[0]["owner"] == "b"
    assert sorted(i["owner"] for i in got) == ["a", "b"]


def test_build_clusters_adds_owner_urls_and_returns_nothing_without_sources():
    now = NOW
    assert manual.build_clusters("GTA 6", "", [], [], now=now) == []
    built = manual.build_clusters("GTA 6", "", ["https://news.example/gta"], [], now=now)
    assert len(built) == 1
    assert built[0]["title"] == "GTA 6" and built[0]["manual"] is True
    assert built[0]["items"][0]["link"] == "https://news.example/gta"


def test_angle_is_marked_as_not_a_source():
    text = manual.angle_suffix("focus on the price")
    assert "focus on the price" in text and "NOT a source" in text
    assert manual.angle_suffix("  ") == ""


def test_manual_run_skips_ingest_and_writes_from_matching_pool_items(tmp_path):
    paths = make_paths(tmp_path)
    paths.pool.parent.mkdir(parents=True, exist_ok=True)
    pool = {"items": [
        pooled(dryrun.SAMPLE_TITLE, "https://b.example/1", "owner-c"),
        pooled(dryrun.SAMPLE_TITLE, "https://b.example/2", "owner-d"),
    ]}
    paths.pool.write_text(json.dumps(pool), encoding="utf-8")
    prompts = []

    def write_fn(facts, *, run_state, generate, prompt=None, **kwargs):
        prompts.append(prompt)
        return run_module.write_stage.write(facts, run_state=run_state, generate=generate, prompt=prompt, **kwargs)

    def no_ingest(*args, **kwargs):
        raise AssertionError("a manual topic must not run ingest")

    result = run_with_items(
        tmp_path, [], ingest_fn=no_ingest, write_fn=write_fn,
        manual={"topic": dryrun.SAMPLE_TITLE, "idea": "focus on the release date", "urls": []},
    )
    assert result.status == "completed", result.error
    assert list(paths.articles_dir.glob("*.md"))
    assert prompts and "focus on the release date" in prompts[0]


def test_manual_topic_without_any_source_publishes_nothing(tmp_path):
    result = run_with_items(
        tmp_path, [], manual={"topic": "Completely unknown topic", "idea": "", "urls": []},
    )
    assert result.status == "nothing"
    assert not list(make_paths(tmp_path).articles_dir.glob("*.md"))


def test_a_manual_topic_that_found_nothing_alerts_the_owner():
    import notify

    report = {"status": "nothing", "error": "no new stories to cover", "manual_topic": "GTA 6"}
    text = notify.build_message(report)
    assert text and "GTA 6" in text
    assert notify.build_message({"status": "nothing", "error": "x"}) is None
