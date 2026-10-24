import datetime as dt
from types import SimpleNamespace

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
        "check_entity_mentions",
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
