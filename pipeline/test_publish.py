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


LONG_DESCRIPTION = (
    "Marvel Tokon: Fighting Souls has revealed its Year 1 DLC roster, bringing "
    "new fighters and stages to the tag-team brawler over the coming months."
)


def test_long_description_is_written_on_one_line_and_round_trips():
    front = publish.build_front_matter(FACTS, {**SEO, "description": LONG_DESCRIPTION})
    article = publish.assemble_article("## Body\n\nHello.", front)

    desc_lines = [line for line in article.splitlines() if line.startswith("description:")]
    assert len(desc_lines) == 1
    assert LONG_DESCRIPTION in desc_lines[0]

    parsed, _ = gates.parse_front_matter(article)
    for key, value in front.items():
        assert parsed[key] == value


def test_assemble_article_verifies_front_matter_matches_source():
    front = publish.build_front_matter(FACTS, SEO)
    article = publish.assemble_article("## Body\n\nHello.", front)
    publish.verify_front_matter(front, article)


def test_verify_front_matter_rejects_a_changed_field():
    front = publish.build_front_matter(FACTS, SEO)
    with pytest.raises(ValueError, match="round-trip"):
        publish.verify_front_matter(front, "---\ntitle: something else\n---\nbody\n")


def test_publish_writes_long_description_readable_by_the_gate_parser(tmp_path):
    seo = {**SEO, "description": LONG_DESCRIPTION}
    front = publish.build_front_matter(FACTS, seo)
    article = publish.assemble_article("## Body\n\nHello.", front)
    publish.publish(
        article,
        slug="long-description",
        title=seo["title"],
        category="hardware",
        mode="draft",
        articles_dir=tmp_path / "articles",
        covers_dir=tmp_path / "covers",
        log_path=tmp_path / "log.json",
        seen_path=tmp_path / "seen.json",
        now=NOW,
    )
    written = (tmp_path / "articles" / "long-description.md").read_text(encoding="utf-8")
    desc_lines = [line for line in written.splitlines() if line.startswith("description:")]
    assert len(desc_lines) == 1
    parsed, _ = gates.parse_front_matter(written)
    assert parsed["description"] == LONG_DESCRIPTION


# --- single-origin articles carry a label and are never auto-published ---------------

SINGLE_ORIGIN_FACTS = {
    **FACTS,
    "confirmation": {"rule": "single-origin", "confirmed": True, "single_origin": {"speaker": "Ana Ruiz"}},
}


def test_a_single_origin_story_gets_a_verification_label_in_its_front_matter():
    front = publish.build_front_matter(SINGLE_ORIGIN_FACTS, SEO, {"image": "/covers/x.webp", "imageAlt": "Cover."})
    assert front["verification"] == "single-origin"
    keys = list(front)
    assert keys.index("sources") < keys.index("verification") < keys.index("image")


@pytest.mark.parametrize("confirmation", [
    {"rule": "first-party"}, {"rule": "two-independent-origins"}, {"rule": "tier1"},
    {"rule": "unconfirmed"}, {}, None, "single-origin",
])
def test_every_other_story_has_no_verification_label(confirmation):
    front = publish.build_front_matter({**FACTS, "confirmation": confirmation}, SEO)
    assert "verification" not in front


def test_the_label_survives_the_article_round_trip():
    front = publish.build_front_matter(SINGLE_ORIGIN_FACTS, SEO, {"image": "/covers/x.webp", "imageAlt": "Cover."})
    article = publish.assemble_article("## Body\n\nAna Ruiz said so.", front)
    parsed, _body = gates.parse_front_matter(article)
    assert parsed["verification"] == "single-origin"
    assert publish._front_matter_dict(article)["verification"] == "single-origin"


@pytest.mark.parametrize("verification, mode, expected", [
    ("single-origin", "auto", "draft"),
    ("single-origin", "draft", "draft"),
    (None, "auto", "auto"),
    (None, "draft", "draft"),
])
def test_effective_mode_never_lets_a_single_origin_story_be_auto(verification, mode, expected):
    assert publish.effective_mode(mode, verification) == expected


def test_publish_records_a_single_origin_article_as_a_draft_even_when_asked_for_auto(tmp_path):
    result = publish.publish(
        "---\ntitle: x\n---\nbody\n", slug="relay", title="Relay", category="hardware", mode="auto",
        cluster_id="abc", articles_dir=tmp_path / "articles", covers_dir=tmp_path / "covers",
        log_path=tmp_path / "log.json", seen_path=tmp_path / "seen.json", now=NOW,
        verification="single-origin",
    )
    assert result["mode"] == "draft"
    assert result["verification"] == "single-origin"
    entry = json.loads((tmp_path / "log.json").read_text(encoding="utf-8"))
    entries = entry["articles"] if isinstance(entry, dict) and "articles" in entry else entry
    last = entries[-1] if isinstance(entries, list) else list(entries.values())[-1]
    assert last["mode"] == "draft"
    assert last["verification"] == "single-origin"


def test_publish_keeps_auto_and_adds_no_label_for_an_ordinary_story(tmp_path):
    result = publish.publish(
        "---\ntitle: x\n---\nbody\n", slug="plain", title="Plain", category="hardware", mode="auto",
        cluster_id="abc", articles_dir=tmp_path / "articles", covers_dir=tmp_path / "covers",
        log_path=tmp_path / "log.json", seen_path=tmp_path / "seen.json", now=NOW,
    )
    assert result["mode"] == "auto"
    assert result["verification"] is None
    assert "verification" not in tmp_path.joinpath("log.json").read_text(encoding="utf-8")


# --- the hero image --------------------------------------------------------------------------------

HERO = {"src": "/images/example-story.webp", "alt": "Key art for Example from its Steam store page",
        "credit": "Example Studio", "license": "Steam store", "sourceUrl": "https://store.steampowered.com/app/1/",
        "width": 1600, "height": 517}


def test_the_front_matter_carries_the_hero_when_there_is_one_and_leaves_it_out_otherwise():
    with_hero = publish.build_front_matter(FACTS, SEO, None, pub_date="2026-10-09", hero=HERO)
    assert with_hero["hero"] == HERO and with_hero["hero"] is not HERO
    assert "hero" not in publish.build_front_matter(FACTS, SEO, None, pub_date="2026-10-09")
    assert "hero" not in publish.build_front_matter(FACTS, SEO, None, pub_date="2026-10-09", hero={})


def test_an_article_with_a_hero_round_trips_through_the_parser():
    front = publish.build_front_matter(FACTS, SEO, None, pub_date="2026-10-09", hero=HERO)
    article = publish.assemble_article("body text", front)
    assert publish._front_matter_dict(article)["hero"] == HERO


def test_publish_copies_the_hero_picture_next_to_the_cover(tmp_path):
    hero = tmp_path / "staged.webp"
    hero.write_bytes(b"RIFFhero")
    result = publish.publish(
        "---\ntitle: x\n---\nbody\n", slug="example-story", title="Example story", category="pc", mode="draft",
        hero_path=hero, images_dir=tmp_path / "images", articles_dir=tmp_path / "articles", covers_dir=tmp_path / "covers",
        log_path=tmp_path / "log.json", seen_path=tmp_path / "seen.json", now=NOW,
    )
    assert (tmp_path / "images" / "example-story.webp").read_bytes() == b"RIFFhero"
    assert result["hero_path"] == str(tmp_path / "images" / "example-story.webp")


def test_publish_without_a_hero_writes_no_image_and_reports_none(tmp_path):
    result = publish.publish(
        "---\ntitle: x\n---\nbody\n", slug="example-story", title="Example story", category="pc", mode="draft",
        images_dir=tmp_path / "images", articles_dir=tmp_path / "articles", covers_dir=tmp_path / "covers",
        log_path=tmp_path / "log.json", seen_path=tmp_path / "seen.json", now=NOW,
    )
    assert result["hero_path"] is None and not (tmp_path / "images").exists()
