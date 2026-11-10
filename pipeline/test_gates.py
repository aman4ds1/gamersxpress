import datetime as dt
from types import SimpleNamespace

import pytest
import yaml

import dryrun
import gates

NOW = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)
CONFIG = gates.load_gates_config()

_WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november".split()


def filler(count=340):
    """Synthetic prose in ten-word sentences with a unique word per cycle.

    The draft-quality gates reject single-sentence sections and near-duplicate
    sentences, so the filler must look like prose: every sentence's words are
    distinct from every other sentence's.
    """
    words = []
    for index in range(count):
        word = _WORDS[index % len(_WORDS)]
        cycle = index // len(_WORDS)
        words.append(word if cycle == 0 else f"{word}{cycle}")
    chunks = []
    for start in range(0, len(words), 10):
        chunk = words[start : start + 10]
        if len(chunk) < 5 and chunks:
            chunks[-1] = chunks[-1][:-1] + " " + " ".join(chunk) + "."
        else:
            chunks.append(" ".join(chunk) + ".")
    return " ".join(chunks)


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


def test_leak_check_reads_the_body_only():
    article, _ = make_article()
    assert "facts sheet" in article, "the fixture description mentions the facts sheet"
    result = run(article)
    assert result.checks["leak"]["failures"] == []
    assert result.passed, result.failures


def test_facts_sheet_block_in_the_body_fails():
    body = "## Overview\n\n" + filler() + "\n\n## Facts sheet\n\nF1: 'A game was announced'\n"
    article, _ = make_article(body=body)
    result = run(article)
    assert not result.passed
    failures = result.checks["leak"]["failures"]
    assert any("facts-sheet heading" in failure for failure in failures), failures
    assert any("numbered fact line" in failure for failure in failures), failures
    assert any("leaks pipeline detail" in failure for failure in result.failures)


def test_facts_sheet_json_key_in_the_body_fails():
    body = "## Overview\n\n" + filler() + '\n\nA sheet row reads "source_urls": ["https://a.example/1"] here.\n'
    article, _ = make_article(body=body)
    result = run(article)
    assert not result.passed
    assert any("JSON key" in failure for failure in result.checks["leak"]["failures"])


def test_raw_json_block_in_the_body_fails():
    body = '## Overview\n\n' + filler() + '\n\n```json\n{"kind": "confirmed"}\n```\n'
    article, _ = make_article(body=body)
    result = run(article)
    assert not result.passed
    assert any("raw JSON block" in failure for failure in result.checks["leak"]["failures"])


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


def statement_facts(origin, *, speaker_type="person", kind="statement"):
    facts = make_facts()
    facts["claims"] = [{
        "claim": "The delay is final",
        "value": "The delay is final",
        "source_url": "https://a.example/1",
        "confidence": 1.0,
        "kind": kind,
        "origin": origin,
        "speaker_type": speaker_type,
    }]
    return facts


def test_generic_phrase_that_speaks_still_fails_with_a_person_speaker():
    body = "## What happened\n\n" + filler() + "\n\nThe developer said the delay is final.\n"
    result = run(make_article(body=body)[0], facts=statement_facts("Hidetaka Miyazaki"))
    failures = result.checks["attribution"]["failures"]
    assert any("the developer" in failure for failure in failures), failures


def test_according_to_a_group_still_fails_with_a_person_speaker():
    body = "## What happened\n\n" + filler() + "\n\nAccording to the studio, the game is delayed.\n"
    result = run(make_article(body=body)[0], facts=statement_facts("Hidetaka Miyazaki"))
    failures = result.checks["attribution"]["failures"]
    assert any("the studio" in failure for failure in failures), failures


def test_a_plain_mention_of_a_group_is_not_attribution():
    body = (
        "## What happened\n\n" + filler()
        + "\n\nPolygon reports that FromSoftware is the developer of the title.\n"
    )
    result = run(make_article(body=body)[0], facts=statement_facts("Hidetaka Miyazaki"))
    assert result.checks["attribution"]["failures"] == []


def test_a_generic_phrase_never_fails_when_the_sheet_names_only_an_organization():
    body = (
        "## What happened\n\n" + filler()
        + "\n\nThe studio said the decision was made to further enhance core aspects.\n"
    )
    result = run(
        make_article(body=body)[0],
        facts=statement_facts("fromsoftware", speaker_type="organization"),
    )
    assert result.checks["attribution"]["failures"] == []


def opinion_facts(value, origin="kitguru", source_name="KitGuru", kind="opinion"):
    facts = make_facts()
    facts["sources"][0]["source_name"] = source_name
    facts["claims"] = [{
        "claim": "An opinion about the design",
        "value": value,
        "source_url": "https://a.example/1",
        "confidence": 1.0,
        "kind": kind,
        "origin": origin,
    }]
    return facts


def test_an_opinion_attributed_to_the_wrong_party_fails():
    value = "Translucent shells always carry a heavy dose of late-90s nostalgia."
    body = (
        "## What happened\n\n" + filler()
        + "\n\nAccording to the color, material, and finish design team, "
        + "translucent shells always carry a heavy dose of late-90s nostalgia.\n"
    )
    result = run(make_article(body=body)[0], facts=opinion_facts(value))
    failures = result.checks["opinion_attribution"]["failures"]
    assert failures == [
        f"opinion by 'kitguru' is not attributed to them: "
        f"'According to the color, material, and finish design team, translucent shells "
        f"always carry a heavy dose of late-90s nostalgia.'"
    ]


def test_an_opinion_attributed_to_its_origin_passes():
    value = "Translucent shells always carry a heavy dose of late-90s nostalgia."
    body = (
        "## What happened\n\n" + filler()
        + "\n\nKitGuru says translucent shells always carry a heavy dose of late-90s nostalgia.\n"
    )
    result = run(make_article(body=body)[0], facts=opinion_facts(value))
    assert result.checks["opinion_attribution"]["failures"] == []


def test_an_opinion_attributed_to_a_source_display_name_passes():
    value = "The gameplay was far too weighted toward PvP aspects."
    body = (
        "## What happened\n\n" + filler()
        + "\n\nKotaku also reported an opinion that the gameplay was far too weighted "
        + "toward PvP aspects.\n"
    )
    result = run(
        make_article(body=body)[0],
        facts=opinion_facts(value, origin="kotaku", source_name="Kotaku"),
    )
    assert result.checks["opinion_attribution"]["failures"] == []


def test_an_opinion_attributed_to_a_display_name_with_a_different_origin_passes():
    value = "The gameplay was far too weighted toward PvP aspects."
    body = (
        "## What happened\n\n" + filler()
        + "\n\nKotaku also reported an opinion that the gameplay was far too weighted "
        + "toward PvP aspects.\n"
    )
    result = run(
        make_article(body=body)[0],
        facts=opinion_facts(value, origin="kotaku.com", source_name="Kotaku"),
    )
    assert result.checks["opinion_attribution"]["failures"] == []


def test_an_opinion_no_body_sentence_resembles_is_not_checked():
    body = "## What happened\n\n" + filler() + "\n"
    result = run(
        make_article(body=body)[0],
        facts=opinion_facts("The soundtrack is outstanding and memorable throughout."),
    )
    assert result.checks["opinion_attribution"]["failures"] == []


def test_non_opinion_claims_are_never_checked_for_origin():
    value = "Translucent shells always carry a heavy dose of late-90s nostalgia."
    body = (
        "## What happened\n\n" + filler()
        + "\n\nAccording to the color, material, and finish design team, "
        + "translucent shells always carry a heavy dose of late-90s nostalgia.\n"
    )
    result = run(make_article(body=body)[0], facts=opinion_facts(value, kind="confirmed"))
    assert result.checks["opinion_attribution"]["failures"] == []


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


# --- draft-quality gates (repetition, near-duplicate sentences, dangling
# --- references, circular attribution, thin sections, opener variety,
# --- entity mentions) --------------------------------------------------------


CLEAN_TITLE = "Novatek confirms the Apex controller launch window"
CLEAN_DESCRIPTION = (
    "Novatek has confirmed the launch window for its next controller line, "
    "several outlets report together."
)


def clean_body():
    """A synthetic article that satisfies every draft-quality gate."""
    return (
        "## What happened\n\n"
        "Novatek will unveil the Apex controller on 13 October, according to "
        "Copper Console and Input Weekly. "
        "The controller ships in black and white colors for every region where "
        "Novatek sells hardware. "
        "Novatek confirmed that the dock accessory arrives one month after the "
        "controller itself. "
        "The announcement covers the controller, the dock and the charging "
        "cable together.\n\n"
        "## Why gamers should care\n\n"
        "Retailers list the controller at 1,299 rupees in India and at 129 "
        "dollars in the United States. "
        "The firmware update reaches every existing Apex unit and adds "
        "remappable buttons for players. "
        "Two-year warranties apply in the regions the manufacturer already "
        "serves with repairs.\n\n"
        "## Context and comparison\n\n"
        "The previous Apex model shipped without a dock and without "
        "replaceable sticks. "
        "Novatek added replaceable sticks after players reported drift on the "
        "older controller. "
        "The new revision keeps the same trigger depth and the same stick "
        "height as the model it replaces.\n\n"
        "## What is unconfirmed\n\n"
        "Copper Console reports that a third color could arrive in 2027, but "
        "the manufacturer has not confirmed a plan. "
        "Input Weekly describes the 2027 window as an estimate from its own "
        "sources. "
        "Neither outlet states a firm release date for the unconfirmed color. "
        "Copper Console described the color plan as an intention, not a "
        "promise."
    )


def clean_facts():
    facts = make_facts()
    facts["coherence"] = {
        "primary_entities": ["novatek", "apex controller"],
        "canonical_entities": ["Apex Controller"],
    }
    return facts


def clean_article(**overrides):
    body = overrides.pop("body", None) or clean_body()
    front = {
        "title": CLEAN_TITLE,
        "description": CLEAN_DESCRIPTION,
        "category": "gaming-news",
        "tags": ["novatek", "controller", "launch"],
        "entities": ["Novatek", "Apex Controller"],
    }
    front.update(overrides.pop("front_overrides", {}))
    return make_article(body=body, title=CLEAN_TITLE, front_overrides=front)[0]


def config_without(*keys):
    config = dict(CONFIG)
    for key in keys:
        config[key] = False
    return config


def real_draft_body():
    """The real draft's structure in synthetic words.

    It passed every older gate while carrying a repeated date, a dangling
    reference, a circular attribution, a thin section and one outlet opening
    eight sentences.
    """
    return (
        "## What happened\n\n"
        "Copper Console reports that Novatek will unveil the Apex controller "
        "on 13 October. "
        "Copper Console reports that the Apex controller arrives in black and "
        "white colors. "
        "Copper Console reports that the dock ships separately from the "
        "controller itself. "
        "Copper Console reports that the firmware update reaches every "
        "existing unit worldwide. "
        "Copper Console reports that retailers listed the preorder pages "
        "several days early. "
        "Copper Console reports that the controller works on PC and console "
        "alike. "
        "Copper Console reports that the cable inside the box uses a single "
        "connector. "
        "Copper Console reports that the warranty lasts two years in most "
        "regions. "
        "Novatek confirmed that Novatek designed the controller with "
        "repairable parts. "
        "Beside this, the schedule for the second color remains unclear.\n\n"
        "## Thin section\n\n"
        "One short line.\n\n"
        "## Release timing\n\n"
        "The launch date is 13 October according to the announcement. "
        "Retailers listed 13 October on the preorder page. "
        "Novatek set 13 October as the shipping date for every region."
    )


def test_draft_quality_gate_switches_and_limits_come_from_config():
    config = gates.load_gates_config()
    assert config["max_repeat_tokens"] == 2
    assert config["max_same_opener"] == 3
    for key in (
        "check_repeated_facts",
        "check_near_duplicate_sentences",
        "check_dangling_references",
        "check_circular_attribution",
        "check_thin_sections",
        "check_opener_variety",
        "check_outlet_names",
        "check_entity_mentions",
        "check_hero",
    ):
        assert config[key] is True, key


def test_real_draft_style_body_fails_the_new_quality_gates():
    result = run(clean_article(body=real_draft_body()), facts=make_facts())
    assert not result.passed
    assert any(failure == "repeated fact: 13 October" for failure in result.failures), result.failures
    assert any(failure.startswith("dangling reference") for failure in result.failures), result.failures
    assert any(failure.startswith("circular attribution") for failure in result.failures), result.failures
    assert any(failure.startswith("thin section: Thin section") for failure in result.failures), result.failures
    assert any(
        failure.startswith("repetitive sentence openers") and "Copper Console" in failure
        for failure in result.failures
    ), result.failures


def test_clean_article_passes_every_new_quality_gate():
    result = run(clean_article(), facts=clean_facts())
    assert result.passed, result.failures
    for check in (
        "repeated_facts",
        "near_duplicate_sentences",
        "dangling_references",
        "circular_attribution",
        "thin_sections",
        "sentence_openers",
        "entity_mentions",
    ):
        assert result.checks[check]["failures"] == [], check


def test_repeated_date_three_times_fails():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe announcement says October 13, the retailer repeats "
        "October 13 and the store page still says October 13.\n"
    )
    result = run(make_article(body=body)[0])
    assert any(failure == "repeated fact: October 13" for failure in result.failures)


def test_repeated_price_three_times_fails():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe card costs 1,299 dollars at one retailer.\n"
        "A second shop lists the card at 1,299 dollars.\n"
        "The price tag reads 1,299 dollars at a third store.\n"
    )
    result = run(make_article(body=body)[0])
    assert any(failure == "repeated fact: 1,299" for failure in result.failures)


def test_same_price_twice_passes():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe card costs 1,299 dollars at one retailer.\n"
        "A second shop lists the card at 1,299 dollars.\n"
    )
    result = run(make_article(body=body)[0])
    assert result.passed, result.failures


def test_max_repeat_tokens_raises_the_repetition_limit():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe card costs 1,299 dollars at one retailer.\n"
        "A second shop lists the card at 1,299 dollars.\n"
        "The price tag reads 1,299 dollars at a third store.\n"
    )
    config = dict(CONFIG)
    config["max_repeat_tokens"] = 3
    result = run(make_article(body=body)[0], config=config)
    assert result.passed, result.failures


def test_three_different_dates_in_one_year_are_not_a_repeated_fact():
    # The year of a full date is part of the date, not a number repeated three times.
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe talk took place on October 16, 2026. The post is dated October 17, 2026.\n"
        "The page was updated on October 19, 2026.\n"
    )
    result = run(make_article(body=body)[0])
    assert result.checks["repeated_facts"]["failures"] == []
    assert result.passed, result.failures


def test_the_same_full_date_three_times_still_fails():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe talk took place on October 16, 2026. The report repeats October 16, 2026.\n"
        "The page still says October 16, 2026.\n"
    )
    result = run(make_article(body=body)[0])
    assert any(failure == "repeated fact: October 16, 2026" for failure in result.failures)


def test_a_date_with_and_without_its_year_counts_as_one_date():
    assert gates.repeated_fact_tokens(
        "October 16, 2026 then October 16 and again October 16."
    ) == ["repeated fact: October 16, 2026"]


def test_a_bare_year_repeated_is_not_a_repeated_fact():
    assert gates.repeated_fact_tokens("In 2026 it grew. By 2026 it doubled. Still 2026.") == []


def test_a_model_number_in_the_title_or_entities_may_repeat():
    # make_article's title and entities name the RTX 5090.
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe RTX 5090 is scarce. The RTX 5090 sold out. Buyers want the RTX 5090.\n"
        "Stores say the RTX 5090 is gone.\n"
    )
    result = run(make_article(body=body)[0])
    assert result.checks["repeated_facts"]["failures"] == []
    assert result.passed, result.failures


def test_a_number_that_is_not_part_of_the_name_still_fails_when_repeated():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe card ships with 7800 units. A shop lists 7800 units.\n"
        "The report again says 7800 units.\n"
    )
    result = run(make_article(body=body)[0])
    assert any(failure == "repeated fact: 7800" for failure in result.failures)


def test_repeated_fact_tokens_exempts_numbers_in_the_name_text_only():
    body = "The RTX 5090 is scarce. The RTX 5090 sold out. Buyers want the RTX 5090."
    assert gates.repeated_fact_tokens(body) == ["repeated fact: 5090"]
    assert gates.repeated_fact_tokens(body, name_text="GeForce RTX 5090") == []
    assert gates.repeated_fact_tokens(body, name_text="GeForce RTX 5080") == ["repeated fact: 5090"]


def test_the_price_of_a_named_product_still_fails_when_repeated_three_times():
    body = "The RTX 5090 costs 1,999. Shops say 1,999. The tag reads 1,999."
    assert gates.repeated_fact_tokens(body, name_text="GeForce RTX 5090") == ["repeated fact: 1,999"]


def test_a_comma_formatted_price_that_looks_like_a_year_is_still_counted():
    # "1,999" is a price, not the year 1999; "1999" with no comma is a year.
    assert gates.repeated_fact_tokens("It costs 1,999. Shops say 1,999. The tag reads 1,999.") == [
        "repeated fact: 1,999"
    ]
    assert gates.repeated_fact_tokens("In 1999 it began. By 1999 it grew. Still 1999.") == []


def test_repeated_facts_switch_disables_the_gate():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe card costs 1,299 dollars at one retailer.\n"
        "A second shop lists the card at 1,299 dollars.\n"
        "The price tag reads 1,299 dollars at a third store.\n"
    )
    result = run(make_article(body=body)[0], config=config_without("check_repeated_facts"))
    assert result.passed, result.failures


def test_near_identical_sentences_fail():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe Apex controller ships in black colors worldwide.\n"
        "The Apex controller also ships in black colors worldwide.\n"
    )
    result = run(make_article(body=body)[0])
    assert not result.passed
    assert any("near-duplicate sentences" in failure for failure in result.failures)


FISHER_SENTENCES = (
    "Jeff Fisher stated that discontinuation rumors are not true and confirmed that "
    "the company will continue to ship the RTX 5090.",
    "Jeff Fisher stated that discontinuation rumors are not true and that Nvidia "
    "will continue to ship the RTX 5090.",
)
OTHER_FISHER_SENTENCES = (
    "Addressing supply capacity, Jeff Fisher stated that supply chain capacity is indeed "
    "somewhat constrained, but Nvidia is doing everything it can to get the product on the market.",
    "Jeff Fisher stated that Nvidia is going all out to bring products to market.",
    "Jeff Fisher stated that although there are other variants of the RTX 5090 chip, "
    "production has not been discontinued.",
    "PC Gamer reports that the graphics card has disappeared from Nvidia's official US marketplace.",
    "TechPowerUp News reports that the RTX 5090 has a $1,999 MSRP.",
)


def _pair_scores(sentences):
    contents = [gates._content_words(sentence) for sentence in sentences]
    return [
        len(contents[i] & contents[j]) / len(contents[i] | contents[j])
        for i in range(len(contents))
        for j in range(i + 1, len(contents))
    ]


def test_a_real_repeated_denial_is_caught_by_the_default_threshold():
    # Measured on a real draft: the repeated sentence scored 0.77, below the old 0.8.
    score = _pair_scores(FISHER_SENTENCES)[0]
    assert 0.7 <= score < 0.8
    assert gates.near_duplicate_sentence_failures(" ".join(FISHER_SENTENCES))
    assert gates.near_duplicate_sentence_failures(" ".join(FISHER_SENTENCES), threshold=0.8) == []


def test_different_statements_by_the_same_speaker_are_not_near_duplicates():
    sentences = (FISHER_SENTENCES[0],) + OTHER_FISHER_SENTENCES
    assert max(_pair_scores(sentences)) < 0.7
    assert gates.near_duplicate_sentence_failures(" ".join(sentences)) == []


def test_the_near_duplicate_threshold_defaults_to_point_seven():
    assert gates.DEFAULT_GATES["near_duplicate_threshold"] == 0.7
    assert gates.load_gates_config()["near_duplicate_threshold"] == 0.7


@pytest.mark.parametrize("value, expected", [(0.9, 0.9), ("0.75", 0.75), ("abc", 0.7), (0, 0.7), (1.5, 0.7), (None, 0.7)])
def test_the_near_duplicate_threshold_is_read_from_config_and_bad_values_fall_back(tmp_path, value, expected):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"gates": {"near_duplicate_threshold": value}}), encoding="utf-8")
    assert gates.load_gates_config(path)["near_duplicate_threshold"] == expected


def test_the_gate_uses_the_configured_threshold():
    body = "## Overview\n\n" + filler(200) + "\n\n" + " ".join(FISHER_SENTENCES) + "\n"
    article = make_article(body=body)[0]
    strict = run(article, config={**CONFIG, "near_duplicate_threshold": 0.7})
    loose = run(article, config={**CONFIG, "near_duplicate_threshold": 0.8})
    assert strict.checks["near_duplicate_sentences"]["failures"]
    assert loose.checks["near_duplicate_sentences"]["failures"] == []


def test_near_duplicate_sentences_switch_disables_the_gate():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe Apex controller ships in black colors worldwide.\n"
        "The Apex controller also ships in black colors worldwide.\n"
    )
    result = run(
        make_article(body=body)[0],
        config=config_without("check_near_duplicate_sentences"),
    )
    assert result.passed, result.failures


def test_dangling_opener_without_the_noun_fails():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe schedule stays unclear worldwide.\n"
        "Beside this prediction, the window may shift later.\n"
    )
    result = run(make_article(body=body)[0])
    assert not result.passed
    assert any("dangling reference" in failure for failure in result.failures)


def test_dangling_opener_after_a_prediction_passes():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe studio shared a prediction about the release window.\n"
        "Beside this prediction, the window may shift later.\n"
    )
    result = run(make_article(body=body)[0])
    assert result.passed, result.failures
    assert result.checks["dangling_references"]["failures"] == []


def test_paragraph_starting_with_an_anaphoric_opener_fails():
    body = "## Overview\n\n" + filler(200) + "\n\nAs noted above, the color list stays short.\n"
    result = run(make_article(body=body)[0])
    assert not result.passed
    assert any("a paragraph starts" in failure for failure in result.failures)


def test_dangling_reference_switch_disables_the_gate():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nThe schedule stays unclear worldwide.\n"
        "Beside this prediction, the window may shift later.\n"
    )
    result = run(
        make_article(body=body)[0],
        config=config_without("check_dangling_references"),
    )
    assert result.passed, result.failures


def test_circular_attribution_fails():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nNovatek announced that Novatek will extend the warranty to "
        "three years worldwide.\n"
    )
    result = run(make_article(body=body)[0])
    assert not result.passed
    assert any(failure.startswith("circular attribution") for failure in result.failures)
    assert any("Novatek announced that Novatek" in failure for failure in result.failures)


def test_attributing_to_another_party_is_not_circular():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nNovatek announced that the warranty will extend to three "
        "years worldwide.\n"
    )
    result = run(make_article(body=body)[0])
    assert result.passed, result.failures
    assert result.checks["circular_attribution"]["failures"] == []


def test_circular_attribution_switch_disables_the_gate():
    body = (
        "## Overview\n\n" + filler(200)
        + "\n\nNovatek announced that Novatek will extend the warranty to "
        "three years worldwide.\n"
    )
    result = run(
        make_article(body=body)[0],
        config=config_without("check_circular_attribution"),
    )
    assert result.passed, result.failures


def test_thin_section_fails():
    body = (
        "## Overview\n\n" + filler(240)
        + "\n\n## Context\n\nOne lonely line here.\n"
    )
    result = run(make_article(body=body)[0])
    assert not result.passed
    assert any(
        failure.startswith("thin section: Context") for failure in result.failures
    )


def test_thin_section_switch_disables_the_gate():
    body = (
        "## Overview\n\n" + filler(240)
        + "\n\n## Context\n\nOne lonely line here.\n"
    )
    result = run(make_article(body=body)[0], config=config_without("check_thin_sections"))
    assert result.passed, result.failures


OPENER_SENTENCES = (
    "Copper Console reports that the Apex controller ships in three colors worldwide.\n"
    "Copper Console reports that the dock accessory arrives without any cable in the box.\n"
    "Copper Console reports that the firmware update adds remappable buttons for players.\n"
    "Copper Console reports that the manufacturer extended the warranty to two years.\n"
)


def test_repetitive_sentence_openers_fail():
    body = "## Overview\n\n" + filler(240) + "\n\n" + OPENER_SENTENCES
    result = run(make_article(body=body)[0])
    assert not result.passed
    assert any(
        failure.startswith("repetitive sentence openers") and "Copper Console" in failure
        for failure in result.failures
    )


def test_outlet_openers_up_to_the_limit_pass():
    body = "## Overview\n\n" + filler(240) + "\n\n" + "\n".join(OPENER_SENTENCES.splitlines()[:3])
    result = run(make_article(body=body)[0])
    assert result.passed, result.failures
    assert result.checks["sentence_openers"]["failures"] == []


def test_opener_variety_switch_disables_the_gate():
    body = "## Overview\n\n" + filler(240) + "\n\n" + OPENER_SENTENCES
    result = run(make_article(body=body)[0], config=config_without("check_opener_variety"))
    assert result.passed, result.failures


def test_entity_missing_from_the_body_and_title_fails():
    article = clean_article(front_overrides={"entities": ["Novatek", "Ghost Device"]})
    result = run(article, facts=clean_facts())
    assert not result.passed
    assert any(
        "entity not mentioned in the body or title: 'Ghost Device'" in failure
        for failure in result.failures
    )


def test_entity_matching_normalises_case_accents_and_possessives():
    body = clean_body() + "\n\nTokon's studio confirmed the firmware schedule worldwide.\n"
    result = run(
        clean_article(
            body=body,
            front_overrides={"entities": ["Novatek", "Apex Controller", "Tōkon Studio"]},
        ),
        facts=clean_facts(),
    )
    assert result.checks["entity_mentions"]["failures"] == []


def test_missing_top_primary_entity_fails():
    result = run(clean_article(front_overrides={"entities": ["Novatek"]}), facts=clean_facts())
    assert not result.passed
    assert any(
        "top primary entity 'Apex Controller' is missing" in failure
        for failure in result.failures
    )


def test_entity_mentions_switch_disables_the_gate():
    article = clean_article(front_overrides={"entities": ["Novatek", "Ghost Device"]})
    result = run(article, facts=clean_facts(), config=config_without("check_entity_mentions"))
    assert result.passed, result.failures


def test_dry_run_sample_body_passes_the_new_quality_gates():
    article = clean_article(
        body=dryrun.sample_body(),
        front_overrides={
            "title": dryrun.SAMPLE_TITLE,
            "description": dryrun.SAMPLE_DESCRIPTION,
            "category": "hardware",
            "tags": ["nvidia", "graphics-card", "hardware"],
            "entities": ["Nvidia"],
        },
    )
    facts = make_facts()
    facts["coherence"] = {"canonical_entities": ["Nvidia"]}
    result = run(article, facts=facts)
    assert result.passed, result.failures


# --- outlet names: the site states facts in its own voice -------------------------------


def outlet_facts(*claim_values, sources=("Eurogamer", "Polygon"), entities=()):
    return {
        "id": "c1",
        "sources": [
            {"source_name": name, "link": f"https://{name.lower().split()[0]}.example/1", "tier": 2,
             "owner": name.lower(), "region": "us"}
            for name in sources
        ],
        "claims": [{"claim": value, "value": value} for value in claim_values],
        "coherence": {"canonical_entities": list(entities)},
    }


@pytest.mark.parametrize("sentence, outlets", [
    ("Eurogamer reports that the game is delayed.", ["Eurogamer"]),
    ("According to Polygon, the studio is hiring.", ["Polygon"]),
    ("Polygon and Eurogamer reported on the launch.", ["Eurogamer", "Polygon"]),
    ("The delay, per Eurogamer's report, is final.", ["Eurogamer"]),
    ("According to EUROGAMER the game is late.", ["Eurogamer"]),
])
def test_a_sentence_naming_an_outlet_is_flagged(sentence, outlets):
    assert gates.outlet_mentions(sentence, outlet_facts()) == [{"sentence": sentence, "outlets": outlets}]


@pytest.mark.parametrize("text", [
    "The game is delayed to May 20, 2027.",
    "The renderer draws twice as many polygon edges.",   # a lowercase common word is not the outlet
    "Eurogamers gather every year.",                     # a longer word is not the outlet
    "## Eurogamer and Polygon coverage",                 # headings are not sentences
    "- [Polygon](https://polygon.example/pbz)",          # a source link list is rejected elsewhere
])
def test_text_without_an_outlet_name_is_not_flagged(text):
    assert gates.outlet_mentions(text, outlet_facts()) == []


def test_curly_apostrophes_still_match_an_outlet_name():
    facts = outlet_facts(sources=("Tom's Hardware",))
    assert gates.outlets_named("According to Tom\u2019s Hardware, it is fast.", facts) == ["Tom's Hardware"]


def test_an_outlet_a_claim_names_is_not_flagged():
    facts = outlet_facts("Eurogamer is owned by IGN Entertainment")
    assert gates.outlet_mentions("Eurogamer is a well known site.", facts) == []
    assert gates.outlet_mentions("Polygon reports that it is late.", facts)[0]["outlets"] == ["Polygon"]


def test_an_outlet_that_is_a_story_entity_is_not_flagged():
    facts = outlet_facts(entities=("Polygon",))
    assert gates.outlet_mentions("Polygon announced a new office.", facts) == []


def test_no_sources_or_malformed_facts_flag_nothing():
    for facts in (None, {}, {"sources": None}, {"sources": ["x", {"link": "https://a.example"}]}):
        assert gates.outlet_mentions("Eurogamer reports it.", facts) == []
        assert gates.outlets_named("Eurogamer reports it.", facts) == []


def test_outlet_name_failures_cover_the_title_the_description_and_the_body():
    failures = gates.outlet_name_failures(
        "Eurogamer says Phantom Blade Zero sold 4 million",
        "The launch, as reported by Polygon.",
        "## What happened\n\nThe game sold 4 million copies.\n\nPolygon reports that it peaked at 833,921 players.",
        outlet_facts(),
    )
    assert failures[0].startswith("title names a source outlet ('Eurogamer')")
    assert failures[1].startswith("description names a source outlet ('Polygon')")
    assert failures[2].startswith("body names a source outlet ('Polygon'); outlets belong only in the front matter sources")
    assert len(failures) == 3


def test_a_clean_article_has_no_outlet_name_failures():
    assert gates.outlet_name_failures(
        "Phantom Blade Zero sells 4 million copies", "The game sold 4 million copies in 72 hours.",
        "## What happened\n\nThe game sold 4 million copies.", outlet_facts(),
    ) == []


def test_the_gate_fails_an_article_that_names_an_outlet_in_the_body():
    body = "## Overview\n\n" + filler(240) + "\n\nTier One reports that the launch window moved."
    result = run(make_article(body=body)[0])
    assert not result.passed
    assert any(failure.startswith("body names a source outlet ('Tier One')") for failure in result.failures)
    assert len(result.checks["outlet_names"]["failures"]) == 1


def test_the_gate_fails_an_outlet_name_in_the_description():
    description = "Nvidia has confirmed the launch window for its next graphics card, as reported by Tier Two."
    result = run(make_article(front_overrides={"description": description})[0])
    assert not result.passed
    assert any(failure.startswith("description names a source outlet ('Tier Two')") for failure in result.failures)


def test_the_outlet_name_switch_disables_the_gate():
    body = "## Overview\n\n" + filler(240) + "\n\nTier One reports that the launch window moved."
    result = run(make_article(body=body)[0], config=config_without("check_outlet_names"))
    assert result.passed, result.failures
    assert result.checks["outlet_names"]["failures"] == []


def test_an_article_without_outlet_names_passes_the_outlet_gate():
    result = run(make_article()[0])
    assert result.checks["outlet_names"]["failures"] == []


def test_the_outlet_name_switch_is_read_from_the_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("gates:\n  check_outlet_names: false\n", encoding="utf-8")
    assert gates.load_gates_config(path)["check_outlet_names"] is False
    path.write_text("gates:\n  check_outlet_names: maybe\n", encoding="utf-8")
    assert gates.load_gates_config(path)["check_outlet_names"] is True  # not a boolean: the default


def test_every_sentence_that_names_an_outlet_is_listed():
    body = "Eurogamer reports it ships.\n\nThe game ships soon.\n\nPolygon says it is late."
    found = gates.outlet_mentions(body, outlet_facts())
    assert [item["outlets"] for item in found] == [["Eurogamer"], ["Polygon"]]


# --- sentences that copy source wording --------------------------------------------------------

SRC = "the studio said the new console will launch in march with a price of 449 dollars and two colour options for early buyers"


def test_gathered_texts_lists_each_sources_text():
    assert gates.gathered_texts({"sources": [{"text": "a"}, {"text": None}, {}]}) == ["a", "", ""]
    assert gates.gathered_texts(None) == [] and gates.gathered_texts({}) == []


def test_the_most_copied_sentences_are_flagged_until_the_overlap_is_under_the_target():
    body = f"## News\n\nSony confirmed it. {SRC.capitalize()}. A fresh sentence about something else entirely today, with new words."
    flagged = gates.overlap_sentences(body, [SRC], 8, 0.1)
    assert [f["sentence"] for f in flagged] == [f"{SRC.capitalize()}."]
    assert flagged[0]["copied"] >= 1


def test_an_article_under_the_target_flags_nothing():
    body = "Sony confirmed the new console today with a price that was not disclosed in any of the posts we read."
    assert gates.overlap_sentences(body, [SRC], 8, 0.5) == []
    assert gates.overlap_sentences(f"{SRC}.", [SRC], 8, 1.0) == []


@pytest.mark.parametrize("sources", [[], [""], ["too short"]])
def test_no_usable_source_text_flags_nothing(sources):
    assert gates.overlap_sentences(f"{SRC}.", sources, 8, 0.0) == []


def test_headings_are_never_flagged_as_copied():
    assert gates.overlap_sentences(f"## {SRC}", [SRC], 8, 0.0) == []


def test_flagging_stops_once_enough_is_removed():
    one, two = SRC.capitalize() + ".", "Another sentence copied from the source: " + SRC + "."
    flagged = gates.overlap_sentences(f"{one} {two}", [SRC], 8, 0.55)
    assert len(flagged) == 1
    assert len(gates.overlap_sentences(f"{one} {two}", [SRC], 8, 0.0)) == 2


# --- the top entity is only required when the article names it ---------------------------------


def test_a_top_primary_entity_the_article_never_names_is_not_required():
    # "Anticipated" is a headline word the extraction took for a name: no list could hold it.
    facts = clean_facts()
    facts["coherence"]["canonical_entities"] = ["Anticipated", "Apex Controller"]
    result = run(clean_article(front_overrides={"entities": ["Novatek", "Apex Controller"]}), facts=facts)
    assert not any("top primary entity" in failure for failure in result.failures)


def test_a_top_primary_entity_the_article_names_is_still_required():
    facts = clean_facts()
    facts["coherence"]["canonical_entities"] = ["Novatek", "Apex Controller"]
    result = run(clean_article(front_overrides={"entities": ["Apex Controller"]}), facts=facts)
    assert any("top primary entity 'Novatek' is missing" in failure for failure in result.failures)


# --- sentences that repeat a fact or each other --------------------------------------------------

REPEATING = (
    "The update arrived on November 5, 2026. It adds New Game Plus. "
    "Players get it free as of November 5, 2026. Nothing else changed. "
    "Servers went live on November 5, 2026 worldwide."
)


def test_repeated_fact_sentences_flags_the_sentence_that_pushes_a_date_over_the_limit():
    flagged = gates.repeated_fact_sentences(REPEATING, 2)
    assert [f["sentence"] for f in flagged] == ["Servers went live on November 5, 2026 worldwide."]
    assert flagged[0]["fact"] == "November 5, 2026"
    assert gates.repeated_fact_sentences(REPEATING, 3) == []


def test_repeated_fact_sentences_agrees_with_the_gate_that_fails_the_article():
    assert gates.repeated_fact_tokens(REPEATING, 2) == ["repeated fact: November 5, 2026"]
    assert gates.repeated_fact_sentences(REPEATING, 2)


def test_a_number_in_the_story_name_is_not_a_repeated_fact_for_the_sentence_flags():
    text = "The RTX 5090 ships soon. The RTX 5090 costs more. The RTX 5090 runs hot."
    assert gates.repeated_fact_sentences(text, 2, name_text="GeForce RTX 5090") == []
    assert gates.repeated_fact_sentences(text, 2)


def test_near_duplicate_sentences_returns_each_later_restatement_once():
    text = (
        "The free update is live on PlayStation 5 and Xbox Series X today. "
        "A different sentence covers the new difficulty options and bug fixes. "
        "The update is live on PlayStation 5 and Xbox Series X today."
    )
    flagged = gates.near_duplicate_sentences(text, 0.7)
    assert [f["sentence"] for f in flagged] == ["The update is live on PlayStation 5 and Xbox Series X today."]
    assert flagged[0]["earlier"].startswith("The free update is live")
    assert gates.near_duplicate_sentences(text, 0.99) == []


def test_near_duplicate_sentences_finds_nothing_in_distinct_text():
    assert gates.near_duplicate_sentences("One thing happened. A very different thing followed later.", 0.7) == []


# --- the hero image gate ----------------------------------------------------------------------------


def hero_setup(tmp_path, **hero_overrides):
    import io
    from PIL import Image

    folder = tmp_path / "public" / "images"
    folder.mkdir(parents=True)
    buffer = io.BytesIO()
    Image.new("RGB", (1000, 560), (30, 60, 120)).save(buffer, "WEBP")
    (folder / "nvidia-rtx-5090.webp").write_bytes(buffer.getvalue())
    hero = {"src": "/images/nvidia-rtx-5090.webp", "alt": "A graphics card on a desk", "credit": "NVIDIA",
            "license": "Press kit", "width": 1000, "height": 560}
    hero.update(hero_overrides)
    return hero, tmp_path / "public"


def test_an_article_without_a_hero_passes_the_hero_gate():
    result = run(clean_article(), facts=make_facts())
    assert result.checks["hero"]["failures"] == []


def test_a_complete_hero_passes_the_gate(tmp_path):
    hero, public = hero_setup(tmp_path)
    result = run(clean_article(front_overrides={"hero": hero}), facts=make_facts(), public_dir=public)
    assert result.checks["hero"]["failures"] == []


def test_a_hero_with_a_license_that_is_not_allowed_fails_the_article(tmp_path):
    hero, public = hero_setup(tmp_path, license="Getty Images")
    result = run(clean_article(front_overrides={"hero": hero}), facts=make_facts(), public_dir=public)
    assert not result.passed
    assert any("not an allowed license" in failure for failure in result.failures)


def test_a_hero_whose_file_is_missing_fails_the_article(tmp_path):
    hero, public = hero_setup(tmp_path, src="/images/ghost.webp")
    result = run(clean_article(front_overrides={"hero": hero}), facts=make_facts(), public_dir=public)
    assert any("does not exist" in failure for failure in result.failures)


def test_the_hero_gate_can_be_switched_off(tmp_path):
    hero, public = hero_setup(tmp_path, license="Getty Images")
    result = run(clean_article(front_overrides={"hero": hero}), facts=make_facts(), public_dir=public,
                 config=config_without("check_hero"))
    assert result.checks["hero"]["failures"] == []


# --- the "sale" offer phrase is the word, not any text containing it -----------------------------------------------------------

PHANTOM_BLADE = [
    "S-Game confirmed that Phantom Blade Zero officially sold over 4 million copies worldwide within 72 hours of launch, based on its sales data.",
    "The 4 million figure includes sales made on Steam, PlayStation 5, and the China-exclusive version.",
]


def offer_failures(*sentences, facts=None):
    body = "## What happened\n\n" + filler() + "\n\n" + " ".join(sentences) + "\n"
    extra = {} if facts is None else {"facts": facts}
    return run(make_article(body=body)[0], **extra).checks["time_limited_offers"]["failures"]


def test_the_sentences_from_the_phantom_blade_alert_are_not_mistaken_for_a_discount():
    assert offer_failures(*PHANTOM_BLADE) == []


@pytest.mark.parametrize("sentence", [
    "First-week sales beat the studio's own forecast.",
    "Sales figures were confirmed on Monday.",
    "The wholesale price of the part rose again.",
    "A resale market for the cards has appeared.",
    "The sale of the studio to the publisher was completed.",
    "The sale of 4 million copies is a record for the series.",
    "It is a salesman's dream of a launch.",
])
def test_revenue_and_business_wording_is_not_an_offer(sentence):
    assert offer_failures(sentence) == []


@pytest.mark.parametrize("sentence", [
    "The game is on sale this month.",
    "A Summer Sale starts soon.",
    "The bundle is sale-priced.",
    "The pre-sale bundle is cheaper.",
    "The SALE ends at the weekend.",
    "Steam is hosting a sale.",
    "The sale.",
])
def test_a_real_sale_without_a_date_still_fails(sentence):
    failures = offer_failures(sentence)
    assert len(failures) == 1 and "body offers 'sale' without a date range" in failures[0]


def test_a_sale_with_a_date_from_the_facts_sheet_still_passes():
    facts = make_facts()
    facts["claims"] = [{"claim": "The sale runs from October 7 to October 21.", "value": "October 7 to October 21",
                        "source_url": "https://a.example/1", "confidence": 1.0, "kind": "confirmed"}]
    assert offer_failures("The sale runs from October 7 to October 21.", facts=facts) == []


def test_a_sale_of_up_to_a_percentage_is_still_caught_by_the_percent_off_phrase():
    failures = offer_failures("Steam runs a sale of up to 50% off.")
    assert len(failures) == 1 and "'% off'" in failures[0]


def test_the_other_offer_phrases_still_match_inside_longer_words():
    for sentence, phrase in (("The game is discounted by half.", "discount"), ("Several discounts apply.", "discount"),
                             ("There is a free weekend event.", "free weekend")):
        failures = offer_failures(sentence)
        assert len(failures) == 1 and f"'{phrase}'" in failures[0], sentence


def test_a_sentence_with_both_revenue_and_a_real_sale_is_still_flagged_for_the_sale():
    failures = offer_failures("The game sold well and is on sale now, with strong sales.")
    assert len(failures) == 1 and "'sale'" in failures[0]


def test_offer_matching_treats_only_sale_specially_and_a_custom_phrase_list_keeps_plain_substring_matching():
    assert gates._offer_in("on sale", "sale") and not gates._offer_in("sales", "sale") and not gates._offer_in("the sale of x", "sale")
    assert gates._offer_in("discounted", "discount") and gates._offer_in("50% off", "% off")
    assert gates._offer_in("first-week sales", "sales")                   # a phrase with no pattern is a substring match
    assert set(gates._OFFER_WORD_PATTERNS) == {"sale"}
