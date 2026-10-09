import datetime as dt
import json

import pytest

import gates
import publish

NOW = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)

FACTS = {
    "id": "c1",
    "generated_at": "2026-10-09T10:00:00+00:00",
    "sources": [
        {"source_name": "Tier One", "link": "https://a.example/1"},
        {"source_name": "Tier Two", "link": "https://b.example/1"},
    ],
}
SEO = {
    "title": "Nvidia confirms RTX 5090 launch window",
    "description": "Nvidia has confirmed the launch window for its next graphics card line, according to the facts sheet.",
    "category": "hardware",
    "tags": ["nvidia", "rtx", "gpu"],
    "entities": ["Nvidia", "RTX 5090"],
}


def test_build_front_matter_uses_facts_and_seo():
    cover = {"image": "/covers/x.webp", "imageAlt": "Cover image."}
    front = publish.build_front_matter(FACTS, SEO, cover)
    assert front["pubDate"] == dt.date(2026, 10, 9)
    assert front["category"] == "hardware"
    assert front["sources"] == [
        {"name": "Tier One", "url": "https://a.example/1"},
        {"name": "Tier Two", "url": "https://b.example/1"},
    ]
    assert front["image"] == "/covers/x.webp"
    assert front["imageAlt"] == "Cover image."


def test_assemble_article_round_trips_through_parser():
    front = publish.build_front_matter(FACTS, SEO, {"image": "/covers/x.webp", "imageAlt": "Cover."})
    article = publish.assemble_article("## Body\n\nHello.", front)
    parsed, body = gates.parse_front_matter(article)
    assert parsed["title"] == SEO["title"]
    assert body.strip() == "## Body\n\nHello."


def test_publish_writes_article_cover_log_and_seen(tmp_path):
    cover = tmp_path / "staging.webp"
    cover.write_bytes(b"RIFFwebp")
    article = "---\ntitle: x\n---\nbody\n"
    result = publish.publish(
        article,
        slug="example-story",
        title="Example story",
        category="hardware",
        mode="auto",
        cover_path=cover,
        cluster_id="abc123",
        articles_dir=tmp_path / "articles",
        covers_dir=tmp_path / "covers",
        log_path=tmp_path / "log.json",
        seen_path=tmp_path / "seen.json",
        now=NOW,
    )
    assert result["published"] is True
    assert (tmp_path / "articles" / "example-story.md").read_text(encoding="utf-8") == article
    assert (tmp_path / "covers" / "example-story.webp").read_bytes() == b"RIFFwebp"
    log = json.loads((tmp_path / "log.json").read_text(encoding="utf-8"))
    assert log["articles"][0]["slug"] == "example-story"
    assert log["articles"][0]["mode"] == "auto"
    seen = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert "abc123" in seen["clusters"]


def test_invalid_publish_mode_raises(tmp_path):
    with pytest.raises(ValueError):
        publish.publish("body", slug="x", title="x", category="pc", mode="live", now=NOW)


def test_save_draft_writes_only_the_draft(tmp_path):
    cover = tmp_path / "staging.webp"
    cover.write_bytes(b"RIFFwebp")
    result = publish.save_draft(
        "body", slug="blocked", cover_path=cover, drafts_dir=tmp_path / "drafts", failures=["min words"],
    )
    assert result["published"] is False
    assert (tmp_path / "drafts" / "blocked" / "article.md").is_file()
    assert (tmp_path / "drafts" / "blocked" / "blocked.webp").is_file()
    assert not (tmp_path / "src" / "content" / "articles").exists()
    assert "min words" in (tmp_path / "drafts" / "blocked" / "failures.txt").read_text(encoding="utf-8")


def test_count_today_counts_only_today(tmp_path):
    log = {"articles": [
        {"published_at": "2026-10-09T08:00:00+00:00"},
        {"published_at": "2026-10-09T18:00:00+00:00"},
        {"published_at": "2026-10-08T18:00:00+00:00"},
    ]}
    assert publish.count_today(log, now=NOW) == 2


def test_write_issue_lists_recent_failures(tmp_path):
    breaker = {"consecutive_failures": 3, "last_failure_date": "2026-10-09", "open": True,
               "recent_failures": ["gate failed: x", "verify failed: y"]}
    path = publish.write_issue(breaker, tmp_path / "issue.md", now=NOW)
    text = path.read_text(encoding="utf-8")
    assert "circuit breaker open" in text.lower()
    assert "gate failed: x" in text


def test_write_last_run(tmp_path):
    path = publish.write_last_run({"status": "completed"}, tmp_path / "last-run.json")
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "completed"
