"""Tests for turning publish-gate failures into sentence-level repair requests."""

import pytest

import gaterepair
import gates

BODY = (
    "The update arrived on November 5, 2026. It adds New Game Plus and a new difficulty option.\n\n"
    "Players get it free as of November 5, 2026. The free update is live on PlayStation 5 and Xbox Series X today.\n\n"
    "Servers went live on November 5, 2026 worldwide. The update is live on PlayStation 5 and Xbox Series X today.\n"
)
SETTINGS = {"max_repeat_tokens": 2, "near_duplicate_threshold": 0.7, "overlap": {"ngram": 8, "max_ratio": 0.2}}


def requests_for(failures, body=BODY, **kwargs):
    return gaterepair.repair_requests(failures, body, settings=SETTINGS, **kwargs)


def test_a_repeated_date_maps_to_the_sentence_that_repeats_it():
    requests, unfixable = requests_for(["repeated fact: November 5, 2026"])
    assert unfixable == []
    assert [r["clause"] for r in requests] == ["Servers went live on November 5, 2026 worldwide."]
    assert "November 5, 2026" in requests[0]["reason"]


def test_a_near_duplicate_maps_to_the_later_sentence():
    requests, unfixable = requests_for(["near-duplicate sentences: 'a' and 'b'"])
    assert unfixable == []
    assert [r["clause"] for r in requests] == ["The update is live on PlayStation 5 and Xbox Series X today."]


def test_copied_source_wording_maps_to_the_copied_sentences():
    source = "the studio said the new console will launch in march with a price of 449 dollars and two colour options for early buyers"
    body = "Sony confirmed it today.\n\nThe studio said the new console will launch in March with a price of 449 dollars and two colour options for early buyers.\n"
    requests, unfixable = requests_for(["article reuses 22% of source 8-grams (max 20%)"], body, source_texts=[source])
    assert unfixable == []
    assert len(requests) == 1 and requests[0]["clause"].startswith("The studio said")


def test_overlap_without_source_text_cannot_be_repaired():
    requests, unfixable = requests_for(["article reuses 22% of source 8-grams (max 20%)"], source_texts=None)
    assert requests == [] and unfixable == ["article reuses 22% of source 8-grams (max 20%)"]


def test_a_quoted_fragment_maps_to_the_sentence_that_contains_it():
    requests, unfixable = requests_for(["banned phrase 'new difficulty option'"])
    assert unfixable == []
    assert [r["clause"] for r in requests] == ["It adds New Game Plus and a new difficulty option."]
    assert "banned phrase" in requests[0]["reason"] and "keeping every fact" in requests[0]["reason"]


def test_a_double_quoted_fragment_with_an_apostrophe_maps_too():
    body = "Dragon's Dogma 2 adds a collaboration today. Nothing else changed in the game at all.\n"
    requests, unfixable = requests_for(['dangling reference: "Dragon\'s Dogma 2 adds a collaboration today."'], body)
    assert unfixable == [] and [r["clause"] for r in requests] == ["Dragon's Dogma 2 adds a collaboration today."]


def test_markdown_links_do_not_stop_a_quoted_sentence_from_matching():
    body = "Players can read [the patch notes](/news/patch-notes) before they update the game today.\n"
    requests, unfixable = requests_for(["banned phrase 'read the patch notes before they update'"], body)
    assert unfixable == [] and len(requests) == 1


@pytest.mark.parametrize("failure", [
    "thin section: Overview (needs at least 2 sentences and 40 words; has 1 and 60)",
    "body has 61 words (min 200)",
    "entity not mentioned in the body or title: 'PlayStation 5 Pro Max'",
    "sources not present in the facts sheet: ['x']",
    "banned phrase 'text that is nowhere in the article'",
])
def test_failures_no_sentence_is_behind_are_unfixable(failure):
    requests, unfixable = requests_for([failure])
    assert requests == [] and unfixable == [failure]


def test_one_unfixable_failure_is_reported_alongside_the_fixable_ones():
    requests, unfixable = requests_for(["repeated fact: November 5, 2026", "body has 61 words (min 200)"])
    assert len(requests) == 1 and unfixable == ["body has 61 words (min 200)"]


def test_a_sentence_named_by_two_failures_is_requested_once_with_the_first_reason():
    requests, _ = requests_for(["near-duplicate sentences: 'a' and 'b'", "banned phrase 'is live on PlayStation 5 and Xbox Series X today'"])
    sentences = [r["clause"] for r in requests]
    assert sentences.count("The update is live on PlayStation 5 and Xbox Series X today.") == 1
    later = next(r for r in requests if r["clause"] == "The update is live on PlayStation 5 and Xbox Series X today.")
    assert later["reason"] == gaterepair.verify_stage.NEAR_DUPLICATE_REASON


def test_no_failures_means_no_requests():
    assert requests_for([]) == ([], [])


def test_the_default_settings_come_from_the_gate_config():
    requests, unfixable = gaterepair.repair_requests(["repeated fact: November 5, 2026"], BODY)
    assert unfixable == [] and requests


def test_a_quote_longer_than_the_sentence_still_finds_every_sentence_inside_it():
    body = "Sony confirmed the new console today. It launches in March with a price of 449 dollars. A third unrelated sentence closes the piece out.\n"
    failure = "dangling reference: 'Sony confirmed the new console today. It launches in March with a price of 449 dollars.'"
    requests, unfixable = requests_for([failure], body)
    assert unfixable == []
    assert [r["clause"] for r in requests] == [
        "Sony confirmed the new console today.",
        "It launches in March with a price of 449 dollars.",
    ]


# --- thin sections are folded into a neighbour --------------------------------------------------

LONG = ("Nintendo says it will not raise the console's price again, and it has enough memory secured to keep "
        "building units at today's prices for now. The company raised prices earlier this year because memory "
        "costs were climbing across the industry. The new prices took effect on September 1 in four regions.")
SHORT_A = "Nintendo has enough memory to build more consoles. No further increase is planned for now."
SHORT_B = "In February Nintendo said no price rise was imminent. Rivals face the same pressure on memory costs."
MEDIUM = ("Switch 2 shipments came to 2.67 million units from July to September, down 41% from a year earlier, "
          "bringing the total to 26.35 million. Hardware sales have fallen for several quarters since the launch, "
          "and Nintendo expects to sell 16.5 million consoles in the fiscal year ending March 2027.")
PAIR_A = "Sony raised PS5 prices several times already. It says memory volumes for next year are agreed with its suppliers."
PAIR_B = "Xbox raised Series X prices in several rounds. The most recent increase came in August, according to its own announcement on the official blog."


def article(*sections):
    return "".join(f"## {head}\n\n{text}\n\n" for head, text in sections)


def test_a_thin_section_is_folded_into_the_section_before_it():
    body = article(("What happened", LONG), ("Why gamers should care", SHORT_A))
    folded, headings = gaterepair.fold_thin_sections(body)
    assert headings == ["Why gamers should care"]
    assert "## Why gamers should care" not in folded and "## What happened" in folded
    assert folded.index(LONG) < folded.index(SHORT_A)
    assert gates.thin_section_failures(folded) == []


def test_two_thin_sections_in_a_row_are_combined_under_the_first_heading_when_that_is_enough():
    body = article(("What happened", LONG), ("Why gamers should care", PAIR_A), ("Context and comparison", PAIR_B))
    assert len(gates.thin_section_failures(body)) == 2
    folded, headings = gaterepair.fold_thin_sections(body)
    assert headings == ["Context and comparison"]
    assert "## Why gamers should care" in folded and "## Context and comparison" not in folded
    assert folded.index(PAIR_A) < folded.index(PAIR_B)
    assert gates.thin_section_failures(folded) == []


def test_two_short_sections_that_stay_thin_together_end_up_in_the_section_before():
    body = article(("What happened", LONG), ("Why gamers should care", SHORT_A), ("Context and comparison", SHORT_B))
    folded, headings = gaterepair.fold_thin_sections(body)
    assert sorted(headings) == ["Context and comparison", "Why gamers should care"]
    assert folded.index(LONG) < folded.index(SHORT_A) < folded.index(SHORT_B)
    assert folded.count("## ") == 1 and gates.thin_section_failures(folded) == []


def test_a_thin_first_section_becomes_the_introduction():
    body = article(("What happened", SHORT_A), ("Why gamers should care", LONG))
    folded, headings = gaterepair.fold_thin_sections(body)
    assert headings == ["What happened"]
    assert folded.startswith(SHORT_A) and "## Why gamers should care" in folded
    assert gates.thin_section_failures(folded) == []


def test_a_body_with_no_thin_section_comes_back_untouched():
    body = article(("What happened", LONG), ("Why gamers should care", MEDIUM))
    assert gaterepair.fold_thin_sections(body) == (body, [])


def test_h3_subsections_stay_inside_their_section_and_every_sentence_survives():
    body = article(("What happened", LONG)) + "## Why gamers should care\n\nNo hike planned.\n\n### Pricing\n\n- **US:** now $499.99, up from $449.99\n\n"
    folded, headings = gaterepair.fold_thin_sections(body, min_words=10)
    assert "### Pricing" in folded and "$499.99" in folded and "No hike planned." in folded
    assert folded.count("\n\n\n") == 0


def test_folding_keeps_the_front_matter_of_a_draft_byte_for_byte():
    front = "---\ntitle: A title\nentities:\n  - Nintendo\n---\n"
    draft = front + article(("What happened", LONG), ("Why gamers should care", SHORT_A))
    folded, headings = gaterepair.fold_draft(draft)
    assert folded.startswith(front) and headings == ["Why gamers should care"]
    assert gaterepair.fold_draft(front + article(("What happened", LONG)))[1] == []
    assert gaterepair.fold_draft("no front matter\n")[1] == []


def test_the_folded_text_never_has_runs_of_blank_lines():
    body = article(("What happened", LONG), ("Why gamers should care", SHORT_A), ("Context and comparison", SHORT_B))
    assert "\n\n\n" not in gaterepair.fold_thin_sections(body)[0]


ONE_LONG_SENTENCE = ("Nintendo says it has secured enough memory supply to keep Switch 2 prices where they are today, with "
                     "no further price increase planned for now, even as memory costs keep rising across the whole industry worldwide, according to the company's own statement this week.")


def test_a_long_section_with_a_single_sentence_is_still_thin():
    assert gates._count_words(ONE_LONG_SENTENCE) >= 40
    body = article(("What happened", LONG), ("Why gamers should care", ONE_LONG_SENTENCE))
    assert len(gates.thin_section_failures(body)) == 1
    folded, headings = gaterepair.fold_thin_sections(body)
    assert headings == ["Why gamers should care"] and gates.thin_section_failures(folded) == []


def test_folded_text_is_separated_from_the_section_it_joins_by_one_blank_line():
    folded, _ = gaterepair.fold_thin_sections(article(("What happened", LONG), ("Why gamers should care", SHORT_A)))
    assert f"{LONG}\n\n{SHORT_A}" in folded
