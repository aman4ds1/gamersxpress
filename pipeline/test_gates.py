import datetime as dt
from types import SimpleNamespace

import yaml

import gates

NOW = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)
CONFIG = gates.load_gates_config()

_WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november".split()


def filler(count=340):
    return " ".join(_WORDS[index % len(_WORDS)] for index in range(count))


def make_article(*, body=None, front_overrides=None, title="Nvidia confirms RTX 5090 launch window"):
    front = {
        "title": title,
        "description": "Nvidia has confirmed the launch window for its next graphics card line, according to the facts sheet.",
        "pubDate": "2026-10-09",
        "category": "hardware",
        "tags": ["nvidia", "rtx", "gpu"],
        "entities": ["Nvidia", "RTX 5090"],
        "sources": [
            {"name": "Tier One", "url": "https://a.example/1"},
            {"name": "Tier Two", "url": "https://b.example/1"},
        ],
        "image": "/covers/nvidia-rtx-5090.webp",
        "imageAlt": "Cover image with the Hardware label.",
    }
    front.update(front_overrides or {})
    if body is None:
        body = "## Overview\n\n" + filler()
    return "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body, front


def make_facts():
    return {
        "id": "c1",
        "sources": [
            {"source_name": "Tier One", "link": "https://a.example/1", "tier": 1, "owner": "a", "region": "us"},
            {"source_name": "Tier Two", "link": "https://b.example/1", "tier": 2, "owner": "b", "region": "us"},
        ],
        "claims": [],
    }


def make_gathered(text="unrelated source prose about something else entirely"):
    return {
        "id": "c1",
        "sources": [
            {"link": "https://a.example/1", "text": text},
            {"link": "https://b.example/1", "text": text},
        ],
    }


def run(article, *, slug="nvidia-rtx-5090", **overrides):
    defaults = dict(
        slug=slug,
        facts=make_facts(),
        cluster_id="c1",
        gathered=make_gathered(),
        verify_report={"passed": True},
        run_state=SimpleNamespace(writer_family="google", verifier_family="mistral"),
        config=CONFIG,
        now=NOW,
        link_checker=lambda url: 200,
        site_checks=lambda text, s: (True, "ok"),
        existing_articles=[],
    )
    defaults.update(overrides)
    return gates.run_gates(article, **defaults)


def test_valid_article_passes():
    article, _ = make_article()
    result = run(article)
    assert result.passed, result.failures


def test_title_too_long_fails():
    article, _ = make_article(title="N" * 66)
    assert not run(article).passed


def test_short_description_fails():
    article, _ = make_article(front_overrides={"description": "too short"})
    assert not run(article).passed


def test_bad_category_fails():
    article, _ = make_article(front_overrides={"category": "crypto"})
    assert not run(article).passed


def test_single_source_fails():
    article, _ = make_article(front_overrides={"sources": [{"name": "One", "url": "https://a.example/1"}]})
    assert not run(article).passed


def test_image_without_alt_fails():
    article, _ = make_article(front_overrides={"imageAlt": ""})
    assert not run(article).passed


def test_short_body_fails():
    article, _ = make_article(body="## Overview\n\n" + filler(50))
    assert not run(article).passed


def test_body_sources_section_fails():
    article, _ = make_article(body="## Overview\n\n" + filler() + "\n\n## Sources\n\n- a\n- b\n")
    assert not run(article).passed


def test_heading_skip_fails():
    article, _ = make_article(body="### Deep\n\n" + filler())
    assert not run(article).passed


def test_source_url_not_in_facts_fails():
    article, _ = make_article(
        front_overrides={"sources": [
            {"name": "Tier One", "url": "https://a.example/1"},
            {"name": "Made Up", "url": "https://invented.example/9"},
        ]}
    )
    assert not run(article).passed


def test_overlap_with_source_text_fails():
    article, _ = make_article(body="## Overview\n\n" + filler())
    gathered = make_gathered(text=filler())
    assert not run(article, gathered=gathered).passed


def test_banned_phrase_fails():
    body = "Game-changing news. " + filler()
    article, _ = make_article(body="## Overview\n\n" + body)
    assert not run(article).passed


def test_duplicate_slug_fails():
    article, _ = make_article()
    existing = [{"slug": "nvidia-rtx-5090", "title": "Other", "entities": [], "pubDate": "2026-10-08"}]
    assert not run(article, existing_articles=existing).passed


def test_near_duplicate_inside_window_fails():
    article, _ = make_article()
    existing = [{
        "slug": "older-story",
        "title": "Nvidia confirms RTX 5090 launch window soon",
        "entities": ["Nvidia"],
        "pubDate": "2026-10-08",
    }]
    assert not run(article, existing_articles=existing).passed


def test_near_duplicate_outside_window_passes():
    article, _ = make_article()
    existing = [{
        "slug": "older-story",
        "title": "Nvidia confirms RTX 5090 launch window soon",
        "entities": ["Nvidia"],
        "pubDate": "2026-09-01",
    }]
    assert run(article, existing_articles=existing).passed


def test_unresolved_internal_link_fails():
    article, _ = make_article(body="## Overview\n\nSee [other](/news/other-slug) for more. " + filler())
    assert not run(article).passed


def test_resolved_internal_link_passes():
    article, _ = make_article(body="## Overview\n\nSee [other](/news/other-slug) for more. " + filler())
    existing = [{"slug": "other-slug", "title": "Other", "entities": [], "pubDate": "2026-09-01"}]
    assert run(article, existing_articles=existing).passed


def test_source_404_fails():
    article, _ = make_article()
    assert not run(article, link_checker=lambda url: 404).passed


def test_source_429_is_warning_not_failure():
    article, _ = make_article()
    result = run(article, link_checker=lambda url: 429)
    assert result.passed
    assert result.warnings


def test_missing_verify_report_fails():
    article, _ = make_article()
    assert not run(article, verify_report=None).passed


def test_failed_verify_report_fails():
    article, _ = make_article()
    assert not run(article, verify_report={"passed": False}).passed


def test_same_family_fails():
    article, _ = make_article()
    state = SimpleNamespace(writer_family="google", verifier_family="google")
    assert not run(article, run_state=state).passed


def test_missing_family_fails():
    article, _ = make_article()
    assert not run(article, run_state=SimpleNamespace(writer_family=None, verifier_family="mistral")).passed


def test_site_check_failure_fails():
    article, _ = make_article()
    assert not run(article, site_checks=lambda text, s: (False, "boom")).passed


def test_gate_result_to_dict_round_trips():
    article, _ = make_article()
    payload = run(article).to_dict()
    assert payload["passed"] is True
    assert "schema" in payload["checks"]
