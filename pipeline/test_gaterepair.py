"""Tests for turning publish-gate failures into sentence-level repair requests."""

import pytest

import gaterepair

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
