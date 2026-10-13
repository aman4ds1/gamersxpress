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


def test_content_limits_are_read_from_config():
    limits = gates.load_content_limits()
    assert limits == {
        "max_title": 65,
        "min_description": 70,
        "max_description": 160,
        "min_words": 200,
        "min_sources": 2,
    }
    assert CONFIG["max_title"] == limits["max_title"]
    assert CONFIG["min_description"] == limits["min_description"]
    assert CONFIG["max_description"] == limits["max_description"]
    assert CONFIG["min_words"] == limits["min_words"]
    assert CONFIG["min_sources"] == limits["min_sources"]


def test_min_words_boundary_uses_the_config_limit():
    passing, _ = make_article(body=filler(200))
    assert run(passing).passed, run(passing).failures
    failing, _ = make_article(body=filler(199))
    assert not run(failing).passed


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


# --- reader-text rules (relative time, attribution, offers, sections, entities,
# --- completeness) -----------------------------------------------------------


def tokon_facts():
    """The Marvel Tōkon facts sheet: person speaker + "full" + a dated offer."""
    return {
        "id": "tokon",
        "coherence": {"primary_entities": ["fighting souls", "marvel t", "phoenix cyclops"]},
        "sources": [
            {"source_name": "Tier One", "link": "https://a.example/1", "tier": 1, "owner": "a",
             "region": "us", "title": "Marvel Tōkon: Fighting Souls reveals its Year 1 DLC roster"},
            {"source_name": "Tier Two", "link": "https://b.example/1", "tier": 2, "owner": "b",
             "region": "us", "title": "Phoenix Cyclops joins MARVEL Tōkon: Fighting Souls on November 2"},
        ],
        "claims": [
            {"claim": "Marvel Tōkon: Fighting Souls revealed its full Year 1 DLC roster",
             "value": "Marvel Tōkon: Fighting Souls revealed its full Year 1 DLC roster including Phoenix Cyclops on 2nd November",
             "source_url": "https://a.example/1", "confidence": 1.0, "kind": "confirmed"},
            {"claim": "Explanation for picking the Phoenix version of Cyclops",
             "value": "Narrative team proposed the Phoenix version to create fresher visuals",
             "source_url": "https://a.example/1", "confidence": 1.0,
             "origin": "takeshi yamanaka", "affiliation": "playstation blog", "kind": "statement"},
        ],
    }


def test_relative_time_rule_lists_come_from_config():
    rules = gates.load_content_rules()
    assert "today" in rules["relative_time_phrases"]
    assert "now" in rules["relative_time_phrases"]
    assert "this weekend" in rules["relative_time_phrases"]
    assert CONFIG["relative_time_phrases"] == rules["relative_time_phrases"]


def test_relative_time_in_body_fails():
    body = "## What happened\n\nThe sale is starting today for all players. " + filler()
    result = run(make_article(body=body)[0])
    assert not result.passed
    assert any("today" in failure for failure in result.checks["relative_time"]["failures"])


def test_relative_time_in_title_and_description_fails():
    result = run(make_article(front_overrides={"description": "The first ever sale begins now for players today."})[0])
    assert not result.passed
    assert result.checks["relative_time"]["failures"]


def test_relative_time_inside_quotes_is_ignored():
    body = '## What happened\n\nThe track is called "Now or Never" and it ships with the update. ' + filler()
    result = run(make_article(body=body)[0])
    assert result.checks["relative_time"]["failures"] == []


def test_attribution_to_a_group_fails_when_the_sheet_names_a_speaker():
    body = (
        "## What happened\n\n"
        + filler()
        + "\n\nThe decision to launch the character as DLC rather than a day-one fighter "
        + "was explained by the narrative team.\n"
    )
    result = run(make_article(body=body)[0], facts=tokon_facts())
    failures = result.checks["attribution"]["failures"]
    assert any("narrative team" in failure for failure in failures)
    assert any("rather than" in failure for failure in failures)


def test_limited_time_offer_without_a_date_fails():
    body = "## What happened\n\n" + filler() + "\n\nThe game is available at a discount for players.\n"
    result = run(make_article(body=body)[0])
    assert any("discount" in failure for failure in result.checks["time_limited_offers"]["failures"])


def test_offer_with_a_date_from_the_facts_sheet_passes():
    facts = make_facts()
    facts["claims"] = [{
        "claim": "The sale runs from October 7 to October 21.",
        "value": "October 7 to October 21",
        "source_url": "https://a.example/1", "confidence": 1.0, "kind": "confirmed",
    }]
    body = "## What happened\n\n" + filler() + "\n\nThe sale runs from October 7 to October 21.\n"
    result = run(make_article(body=body)[0], facts=facts)
    assert result.checks["time_limited_offers"]["failures"] == []


def test_no_other_details_filler_fails():
    body = "## What happened\n\n" + filler() + "\n\n## What is unconfirmed\n\nNo other details are confirmed.\n"
    result = run(make_article(body=body)[0])
    assert any("no other details are confirmed" in failure for failure in result.checks["sections"]["failures"])


def test_empty_optional_section_fails():
    body = "## What happened\n\n" + filler() + "\n\n## Context and comparison\n\n"
    result = run(make_article(body=body)[0])
    assert any("Context and comparison" in failure for failure in result.checks["sections"]["failures"])


def test_entities_must_cover_the_headline_product():
    article, _ = make_article(
        title="Marvel Tokon Fighting Souls reveals Year 1 DLC roster",
        front_overrides={
            "entities": ["PlayStation 5"],
            "category": "gaming-news",
            "tags": ["tokon", "dlc", "fighting"],
        },
    )
    result = run(article, slug="marvel-tokon", facts=tokon_facts())
    assert any(
        "Marvel Tokon Fighting Souls" in failure for failure in result.checks["entities"]["failures"]
    )


def test_entities_must_cover_a_named_character_in_the_title():
    article, _ = make_article(
        title="Marvel Tokon Fighting Souls adds Phoenix Cyclops",
        front_overrides={
            "entities": ["Marvel Tōkon: Fighting Souls", "PlayStation 5"],
            "category": "gaming-news",
            "tags": ["tokon", "dlc", "fighting"],
        },
    )
    result = run(article, slug="marvel-tokon-2", facts=tokon_facts())
    assert any("Phoenix Cyclops" in failure for failure in result.checks["entities"]["failures"])


def test_completeness_word_without_a_fact_fails():
    body = "## What happened\n\n" + filler() + "\n\nThe studio confirmed the complete character roster.\n"
    result = run(make_article(body=body)[0])
    assert any("complete" in failure for failure in result.checks["completeness"]["failures"])


def test_completeness_word_is_allowed_when_a_fact_states_it():
    facts = make_facts()
    facts["claims"] = [{
        "claim": "The full roster is confirmed.",
        "value": "full roster",
        "source_url": "https://a.example/1", "confidence": 1.0, "kind": "confirmed",
    }]
    body = "## What happened\n\n" + filler() + "\n\nThe full roster is confirmed.\n"
    result = run(make_article(body=body)[0], facts=facts)
    assert result.checks["completeness"]["failures"] == []
