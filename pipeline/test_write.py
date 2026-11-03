import json
import re
from pathlib import Path

import pytest

import providers
import write as write_module
from providers import Generation, RunState


def facts_sheet():
    return {
        "id": "story-1",
        "generated_at": "2026-10-09T12:00:00+00:00",
        "confirmation": {"tier1_sources": 1, "tier2_owners": [], "confirmed": True},
        "sources": [
            {"source_name": "Xbox Wire", "link": "https://news.xbox.com/a", "tier": 1, "owner": "microsoft", "region": "global"},
            {"source_name": "Eurogamer", "link": "https://eurogamer.net/b", "tier": 2, "owner": "ign-entertainment", "region": "uk"},
        ],
        "claims": [
            {"claim": "A game was announced", "value": "Game X", "source_url": "https://news.xbox.com/a", "confidence": 0.95, "kind": "confirmed"},
        ],
    }


def test_build_prompt_substitutes_facts_sheet():
    prompt = write_module.build_prompt(facts_sheet())
    assert write_module.FACTS_SHEET_TOKEN not in prompt
    assert "A game was announced" in prompt
    assert "https://news.xbox.com/a" in prompt


def test_default_template_exists_and_has_placeholder():
    template = write_module.load_template()
    assert write_module.FACTS_SHEET_TOKEN in template


@pytest.mark.parametrize(
    "required",
    [
        "title:", "description:", "pubDate:", "category:", "tags:", "entities:", "sources:",
        "## What happened", "## Why gamers should care", "## Context and comparison",
        "## What is unconfirmed",
    ],
)
def test_default_template_names_each_required_field_and_section(required):
    assert required in write_module.load_template()


def test_default_template_forbids_a_body_sources_section():
    template = write_module.load_template()
    assert "Do not write a `## Sources`" in template
    assert "front matter `sources` field is the only place sources live" in template


def test_writer_template_every_sentence_supported_and_no_filler():
    template = write_module.load_template()
    assert "Every sentence must be supported by a claim" in template
    assert "unlocks additional gear and opportunities" in template
    assert "a shorter article is better than a padded one" in template


def test_writer_template_costs_are_paid_not_received():
    template = write_module.load_template()
    assert "Costs are paid, not received" in template
    assert "as something players receive" in template


def test_writer_template_forbids_invented_labels_and_confidence_talk():
    template = write_module.load_template()
    assert "mid-tier" in template
    assert "No invented labels or confidence talk" in template
    assert "low confidence" in template


def test_writer_template_bans_internal_terms_in_reader_text():
    template = write_module.load_template()
    assert "No internal terms in reader-facing text" in template
    assert "no other details are confirmed" in template


def test_writer_template_names_in_game_currencies_everywhere():
    template = write_module.load_template()
    assert "Name in-game currencies everywhere" in template
    assert "100,000 in-game DMZ Cash" in template


def test_writer_template_modes_only_as_sources_call_them():
    template = write_module.load_template()
    assert "Modes only as the sources call them" in template


def test_writer_template_attributes_source_angle():
    template = write_module.load_template()
    assert "Attribute a source's angle" in template
    assert "Eurogamer reports" in template


def test_writer_template_names_claim_kinds_and_origin():
    template = write_module.load_template()
    assert "Attribute by `kind`" in template
    assert "`origin` and attribution" in template
    for kind in ("confirmed", "reported", "estimate", "rumor", "opinion"):
        assert kind in template


def test_writer_template_attributes_estimates_with_hedges():
    template = write_module.load_template()
    assert "according to estimates from" in template
    assert "Keep hedge words" in template
    for hedge in ("estimated", "reportedly", "more than", "over"):
        assert hedge in template


def test_writer_template_attributes_unstated_origin_to_the_outlet():
    template = write_module.load_template()
    assert "unstated" in template
    assert "attribute the claim to the outlet that reported it" in template


def test_writer_template_bans_causal_connectives():
    template = write_module.load_template()
    assert "No causal or contrast connectives" in template
    for phrase in ("in contrast", "because", "driven by", "thanks to", "as a result"):
        assert phrase in template


def test_writer_template_keeps_overlaps_and_percentages_exact():
    template = write_module.load_template()
    assert "Percentages and overlaps exactly as stated" in template
    assert "came from" in template


def test_writer_template_groups_estimates_and_rumors_in_one_place():
    template = write_module.load_template()
    assert "One place for the unconfirmed" in template
    assert "Group every estimate and rumor" in template


def test_writer_template_states_each_fact_once():
    template = write_module.load_template()
    assert "state each fact once" in template


def test_writer_template_names_person_or_firm_for_opinions():
    template = write_module.load_template()
    assert "Name the person or firm" in template


def test_writer_template_bans_all_internal_terms():
    template = write_module.load_template()
    for term in ("facts sheet", "confidence", "kind", "origin"):
        assert term in template


def test_writer_template_uses_calendar_dates_not_relative_time():
    template = write_module.load_template()
    assert "Calendar dates only" in template
    for word in ("today", "tonight", "now", "currently", "this week", "this weekend",
                 "yesterday", "tomorrow", "recently", "soon", "next week"):
        assert word in template


def test_writer_template_attributes_statements_to_the_named_speaker():
    template = write_module.load_template()
    assert "Attribute statements to the named speaker" in template
    assert "the narrative team" in template


def test_writer_template_requires_a_date_for_limited_time_offers():
    template = write_module.load_template()
    assert "Limited-time offers need a date" in template
    assert "date range or start date" in template


def test_writer_template_omits_empty_sections_and_filler():
    template = write_module.load_template()
    assert "Sections earn their place" in template
    assert "no other details are confirmed" in template


def test_writer_template_names_title_entities():
    template = write_module.load_template()
    assert "Entities from the headline" in template
    assert "every named character or product in the title" in template


def test_writer_template_bans_full_and_complete_hype():
    template = write_module.load_template()
    assert 'No "full" or "complete" hype' in template


def test_build_prompt_uses_custom_template(tmp_path):
    path = tmp_path / "writer.md"
    path.write_text("INSTRUCTIONS\n\n{{facts_sheet}}\n", encoding="utf-8")
    prompt = write_module.build_prompt({"claims": []}, template_path=path)
    assert prompt.startswith("INSTRUCTIONS")
    assert '"claims": []' in prompt


def test_build_prompt_requires_placeholder(tmp_path):
    path = tmp_path / "writer.md"
    path.write_text("no token here", encoding="utf-8")
    with pytest.raises(ValueError):
        write_module.build_prompt({"claims": []}, template_path=path)


def test_missing_template_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        write_module.build_prompt({"claims": []}, template_path=tmp_path / "nope.md")


@pytest.mark.parametrize(
    "prose",
    [
        {"text": "full source article body"},
        {"gathered_at": "2026-10-09T12:00:00Z", "sources": []},
        {"sources": [{"source_name": "X", "link": "https://x", "text": "prose"}]},
        {"claims": [{"claim": "c", "value": "v", "text": "leak"}]},
    ],
)
def test_build_prompt_rejects_source_prose(prose):
    with pytest.raises(write_module.WriterInputError):
        write_module.build_prompt(prose)


def test_build_prompt_allows_a_facts_sheet_without_prose():
    write_module.build_prompt(facts_sheet())


def test_write_calls_writer_role_with_run_state():
    seen = {}

    def fake_generate(role, prompt, *, run_state, **kwargs):
        seen["role"] = role
        seen["prompt"] = prompt
        seen["run_state"] = run_state
        return Generation(role=role, provider="gemini", model="m", family="google", value="DRAFT")

    run_state = RunState()
    generation = write_module.write(facts_sheet(), run_state=run_state, generate=fake_generate)

    assert seen["role"] == "writer"
    assert seen["run_state"] is run_state
    assert "A game was announced" in seen["prompt"]
    assert generation.value == "DRAFT"


def test_write_accepts_an_explicit_prompt_and_skips_build():
    seen = {}

    def fake_generate(role, prompt, *, run_state, **kwargs):
        seen["prompt"] = prompt
        return Generation(role=role, provider="mock", model="m", family="google", value="D")

    write_module.write({"text": "prose"}, run_state=RunState(), prompt="EXPLICIT", generate=fake_generate)
    assert seen["prompt"] == "EXPLICIT"


def test_write_string_facts_are_inserted_verbatim(tmp_path):
    path = tmp_path / "writer.md"
    path.write_text("{{facts_sheet}}", encoding="utf-8")
    prompt = write_module.build_prompt('{"claims": []}', template_path=path)
    assert prompt == '{"claims": []}'


# --- repair pass -------------------------------------------------------------


def test_repair_template_exists_and_has_every_placeholder():
    template = write_module.load_template(write_module.DEFAULT_REPAIR_PROMPT_PATH)
    assert write_module.FACTS_SHEET_TOKEN in template
    assert write_module.ARTICLE_TOKEN in template
    assert write_module.UNSUPPORTED_CLAUSES_TOKEN in template


def test_repair_template_instructs_delete_or_reground_and_no_new_claims():
    template = write_module.load_template(write_module.DEFAULT_REPAIR_PROMPT_PATH)
    assert "Delete or re-ground each flagged clause" in template
    assert "Add no new claims" in template
    assert "Change nothing else" in template


def test_build_repair_prompt_includes_article_clauses_and_numbered_facts():
    prompt = write_module.build_repair_prompt(
        "## Body\nAn unsupported sentence.",
        ["An unsupported sentence"],
        facts_sheet(),
    )
    assert "## Body\nAn unsupported sentence." in prompt
    assert "- An unsupported sentence" in prompt
    assert "F1: 'A game was announced'" in prompt
    assert write_module.UNSUPPORTED_CLAUSES_TOKEN not in prompt
    assert write_module.ARTICLE_TOKEN not in prompt


def test_build_repair_prompt_uses_custom_template(tmp_path):
    path = tmp_path / "writer-repair.md"
    path.write_text("{{unsupported_clauses}}|{{article}}|{{facts_sheet}}", encoding="utf-8")
    prompt = write_module.build_repair_prompt("BODY", ["CLAUSE"], {"claims": []}, template_path=path)
    assert prompt.startswith("- CLAUSE|BODY|")


def test_build_repair_prompt_requires_every_placeholder(tmp_path):
    path = tmp_path / "writer-repair.md"
    path.write_text("{{article}} {{facts_sheet}}", encoding="utf-8")
    with pytest.raises(ValueError):
        write_module.build_repair_prompt("a", ["x"], facts_sheet(), template_path=path)


def test_repair_rejects_source_prose():
    with pytest.raises(write_module.WriterInputError):
        write_module.build_repair_prompt("a", ["x"], {"text": "source prose"})


def test_repair_calls_writer_role_with_run_state():
    seen = {}

    def fake_generate(role, prompt, *, run_state, **kwargs):
        seen["role"] = role
        seen["prompt"] = prompt
        seen["run_state"] = run_state
        return Generation(role=role, provider="gemini", model="m", family="google", value="FIXED")

    run_state = RunState()
    generation = write_module.repair(
        "draft", ["An unsupported sentence"], facts_sheet(),
        run_state=run_state, generate=fake_generate,
    )

    assert seen["role"] == "writer"
    assert seen["run_state"] is run_state
    assert "An unsupported sentence" in seen["prompt"]
    assert generation.value == "FIXED"


def test_repair_accepts_an_explicit_prompt_and_skips_build():
    seen = {}

    def fake_generate(role, prompt, *, run_state, **kwargs):
        seen["prompt"] = prompt
        return Generation(role=role, provider="mock", model="m", family="google", value="D")

    write_module.repair(
        "a", ["b"], {"text": "prose"}, run_state=RunState(),
        prompt="EXPLICIT", generate=fake_generate,
    )
    assert seen["prompt"] == "EXPLICIT"


# --- repair validation --------------------------------------------------------


REPAIR_FRONT = (
    "---\n"
    "title: Synthetic title for the repair validation tests\n"
    "description: A synthetic description written only for the repair tests here.\n"
    "---\n"
)
OVERVIEW_SENTENCE = "The glasses ship in two colors worldwide."
UNATTRIBUTED_SENTENCE = "The card ships with a two-year warranty worldwide."
ATTRIBUTED_SENTENCE = "Acme states that the glasses use a 240 Hz display panel."
UNATTRIBUTED_REPLACEMENT = "The glasses use a 240 Hz display panel worldwide."
ATTRIBUTED_REPLACEMENT = "Acme says the glasses render at 240 Hz refresh rate worldwide."


def repair_article(*sentences):
    """A synthetic draft: front matter, one heading and one line per sentence."""
    return REPAIR_FRONT + "## Overview\n\n" + "\n".join(sentences)


def test_validate_repair_accepts_an_unchanged_draft():
    draft = repair_article(ATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    assert write_module.validate_repair(draft, draft, ["a clause not in the text"]) == []


def test_validate_repair_accepts_deleting_a_flagged_sentence():
    original = repair_article(UNATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(OVERVIEW_SENTENCE)
    assert write_module.validate_repair(original, repaired, [UNATTRIBUTED_SENTENCE]) == []


def test_validate_repair_accepts_deleting_an_attributed_flagged_sentence():
    original = repair_article(ATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(OVERVIEW_SENTENCE)
    assert write_module.validate_repair(original, repaired, [ATTRIBUTED_SENTENCE]) == []


def test_validate_repair_accepts_a_replacement_that_keeps_the_attribution():
    original = repair_article(ATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(ATTRIBUTED_REPLACEMENT, OVERVIEW_SENTENCE)
    assert write_module.validate_repair(original, repaired, [ATTRIBUTED_SENTENCE]) == []


def test_validate_repair_rejects_a_replacement_that_drops_the_attribution():
    original = repair_article(ATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(UNATTRIBUTED_REPLACEMENT, OVERVIEW_SENTENCE)
    problems = write_module.validate_repair(original, repaired, [ATTRIBUTED_SENTENCE])
    assert any("drops the attribution to acme" in problem for problem in problems), problems


def test_validate_repair_rejects_a_fragment_left_by_a_replacement():
    original = repair_article(UNATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article("The two-year warranty.", OVERVIEW_SENTENCE)
    problems = write_module.validate_repair(original, repaired, [UNATTRIBUTED_SENTENCE])
    assert any("fewer than 6 words" in problem for problem in problems), problems


def test_validate_repair_rejects_truncating_an_unflagged_sentence():
    original = repair_article(UNATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(UNATTRIBUTED_SENTENCE, "The glasses ship in two colors.")
    problems = write_module.validate_repair(original, repaired, [UNATTRIBUTED_SENTENCE])
    assert any("unflagged sentence was changed or dropped" in problem for problem in problems), problems


def test_validate_repair_rejects_any_wording_change_to_an_unflagged_sentence():
    original = repair_article(UNATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(UNATTRIBUTED_SENTENCE, "The glasses ship in three colors worldwide.")
    problems = write_module.validate_repair(original, repaired, [UNATTRIBUTED_SENTENCE])
    assert any("unflagged sentence was changed or dropped" in problem for problem in problems), problems


CASE_A_SENTENCE = (
    "Satoshi Aoyagi of the color, material, and finish design team stated that the "
    "high-gloss clear finish extends across the back of the controller, offering a look "
    "at the technology that powers the DualSense controller while maintaining a sleek, "
    "monochromatic look."
)
CASE_A_CLAUSE = (
    "Satoshi Aoyagi stated the high-gloss clear finish extends across the back of the "
    "controller, offering a look at the technology that powers the DualSense controller "
    "while maintaining a sleek, monochromatic look"
)
CASE_A_REPAIRED = (
    "Satoshi Aoyagi stated that the high-gloss clear finish extends across the back of "
    "the controller, offering a look at the technology that powers the DualSense "
    "controller while maintaining a sleek, monochromatic look."
)
CASE_B_SENTENCE = (
    "Revealed on October 28, 2026, the new Prism Collection includes both controllers "
    "and console covers, allowing PlayStation 5 owners to customize their setups with "
    "matching accessories."
)
CASE_B_CLAUSE = "The new Prism Collection was revealed on October 28, 2026"
CASE_B_REPAIRED = (
    "The new Prism Collection includes both controllers and console covers, allowing "
    "PlayStation 5 owners to customize their setups with matching accessories."
)


def test_validate_repair_accepts_a_paraphrased_flagged_clause_case_a():
    original = repair_article(CASE_A_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(CASE_A_REPAIRED, OVERVIEW_SENTENCE)
    assert write_module.validate_repair(original, repaired, [CASE_A_CLAUSE]) == []


def test_validate_repair_accepts_a_trimmed_flagged_clause_case_b():
    original = repair_article(CASE_B_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(CASE_B_REPAIRED, OVERVIEW_SENTENCE)
    assert write_module.validate_repair(original, repaired, [CASE_B_CLAUSE]) == []


def test_validate_repair_still_rejects_an_unrelated_sentence_when_a_clause_matches():
    original = repair_article(CASE_B_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(CASE_B_REPAIRED, "The glasses ship in three colors worldwide.")
    problems = write_module.validate_repair(original, repaired, [CASE_B_CLAUSE])
    assert any("unflagged sentence was changed or dropped" in problem for problem in problems), problems


def test_flagged_flags_matches_nothing_for_a_clause_no_sentence_resembles():
    clause = "A completely unrelated clause about bananas and oranges"
    assert write_module._flagged_flags(["The glasses ship in two colors."], [clause]) == [False]


def test_validate_repair_flags_nothing_for_a_clause_no_sentence_resembles():
    clause = "A completely unrelated clause about bananas and oranges"
    original = repair_article(CASE_B_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(CASE_B_SENTENCE, "The glasses ship in three colors worldwide.")
    problems = write_module.validate_repair(original, repaired, [clause])
    assert any("unflagged sentence was changed or dropped" in problem for problem in problems), problems


def test_validate_repair_flags_only_the_best_matching_sentence():
    overlapping = "The Prism Collection was revealed in 2026 with new controllers."
    original = repair_article(CASE_B_SENTENCE, overlapping, OVERVIEW_SENTENCE)
    repaired = repair_article(
        CASE_B_REPAIRED,
        "The Prism Collection arrived in 2026 with new controllers.",
        OVERVIEW_SENTENCE,
    )
    problems = write_module.validate_repair(original, repaired, [CASE_B_CLAUSE])
    assert any("unflagged sentence was changed or dropped" in problem for problem in problems), problems


def test_validate_repair_rejects_a_changed_front_matter_field():
    original = repair_article(OVERVIEW_SENTENCE)
    repaired = original.replace("title: Synthetic title for the repair validation tests",
                                "title: A rewritten title")
    problems = write_module.validate_repair(original, repaired, ["absent clause"])
    assert any("unflagged sentence was changed or dropped" in problem for problem in problems), problems


def test_validate_repair_rejects_an_appended_facts_sheet_block():
    original = repair_article(OVERVIEW_SENTENCE)
    repaired = original + "\n\n## Facts sheet\n\nF1: 'A game was announced'\n"
    problems = write_module.validate_repair(original, repaired, ["absent clause"])
    assert any("leaks pipeline detail" in problem for problem in problems), problems
    assert any("heading or list item" in problem for problem in problems), problems


def test_validate_repair_rejects_a_facts_sheet_json_key():
    original = repair_article(OVERVIEW_SENTENCE)
    repaired = original + '\n\nA sheet row reads "source_urls": ["https://a.example/1"] here.\n'
    problems = write_module.validate_repair(original, repaired, ["absent clause"])
    assert any("JSON key" in problem for problem in problems), problems


def test_validate_repair_rejects_a_word_count_beyond_the_margin():
    original = repair_article(UNATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    additions = (
        "Acme explained that the shipping window covers every region where the glasses "
        "are sold, that the packaging includes a cleaning cloth, and that the charging "
        "cable ships inside the box with the headset.",
        "The company also said the accessory lineup arrives later this year with a travel "
        "case, a replacement strap, a lens cloth, and a printed manual written in six "
        "separate languages worldwide.",
    )
    repaired = repair_article(UNATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE, *additions)
    assert (
        len(repaired.split()) - len(original.split())
    ) > write_module.REPAIR_MAX_NET_ADDED_WORDS
    problems = write_module.validate_repair(original, repaired, [UNATTRIBUTED_SENTENCE])
    assert any("grows the article" in problem for problem in problems), problems


def test_validate_repair_ignores_attribution_rules_for_plain_sentences():
    original = repair_article(UNATTRIBUTED_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(
        "The card ships with a two-year warranty worldwide.",
        OVERVIEW_SENTENCE,
        "The warranty covers repairs at service centers in nine countries worldwide.",
    )
    assert write_module.validate_repair(original, repaired, [UNATTRIBUTED_SENTENCE]) == []


def test_build_repair_prompt_shows_the_verifier_reason_for_each_clause():
    prompt = write_module.build_repair_prompt(
        "## Body\nAn unsupported sentence.",
        [{"clause": "An unsupported sentence", "reason": "the sheet does not state a ship date"}],
        facts_sheet(),
    )
    assert "- An unsupported sentence (flagged because: the sheet does not state a ship date)" in prompt


# --- compact writer view ------------------------------------------------------


def test_writer_view_keeps_value_kind_and_central_for_every_claim():
    sheet = {
        "generated_at": "2026-10-09T12:00:00+00:00",
        "sources": [{"source_name": "Xbox Wire", "link": "https://news.xbox.com/a"}],
        "claims": [
            {"claim": "a", "value": "one", "kind": "confirmed", "central": True},
            {"claim": "b", "value": "two", "kind": "reported"},
            {"claim": "c", "value": "three", "kind": "estimate", "central": False},
        ],
    }
    view = write_module.writer_view(sheet)
    assert [claim["value"] for claim in view["claims"]] == ["one", "two", "three"]
    assert [claim["kind"] for claim in view["claims"]] == ["confirmed", "reported", "estimate"]
    assert [claim["central"] for claim in view["claims"]] == [True, False, False]


def test_writer_view_drops_gathered_text_and_prose_fields():
    view = write_module.writer_view(
        {
            "generated_at": "2026-10-09T12:00:00+00:00",
            "text": "gathered article prose",
            "gathered_at": "2026-10-09T12:00:00+00:00",
            "sources": [
                {"source_name": "X", "link": "https://x", "text": "source prose", "gathered_at": "t"}
            ],
            "claims": [{"claim": "c", "value": "v", "text": "leaked body"}],
        }
    )
    blob = json.dumps(view)
    for leaked in ("gathered prose", "source prose", "body", "leak"):
        assert leaked not in blob
    assert '"text"' not in blob
    assert '"gathered_at"' not in blob


def test_build_prompt_uses_the_compact_view():
    sheet = {
        **facts_sheet(),
        "coherence": {"checked": True, "entities": []},
        "grounding": [{"claim": "x", "reason": "y"}],
        "origins_downgraded": [{"source": "z"}],
        "extractor": {"model": "m", "prompt_version": "facts-v7"},
        "fingerprint": "deadbeef",
    }
    prompt = write_module.build_prompt(sheet)
    for internal in ("coherence", "grounding", "origins_downgraded", "extractor", "fingerprint"):
        assert internal not in prompt
    assert "A game was announced" in prompt


def test_writer_template_states_absences_in_the_outlets_voice():
    template = write_module.load_template()
    assert "Absences belong to the outlet" in template
    assert "Never attribute an absence" in template
    assert "PC Gamer notes that Activision has not revealed an exact time" in template


FLAGGED_SENTENCE = "The card ships with a two-year warranty worldwide."
DEPENDENT_SENTENCE = "Beside this prediction, the schedule stays unclear worldwide."


def test_validate_repair_accepts_deleting_a_flagged_sentence_and_its_dependent():
    original = repair_article(FLAGGED_SENTENCE, DEPENDENT_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(OVERVIEW_SENTENCE)
    assert write_module.validate_repair(original, repaired, [FLAGGED_SENTENCE]) == []


def test_validate_repair_rejects_keeping_a_dependent_sentence():
    original = repair_article(FLAGGED_SENTENCE, DEPENDENT_SENTENCE, OVERVIEW_SENTENCE)
    repaired = repair_article(DEPENDENT_SENTENCE, OVERVIEW_SENTENCE)
    problems = write_module.validate_repair(original, repaired, [FLAGGED_SENTENCE])
    assert any("dependent sentence must be rewritten or deleted" in problem for problem in problems), problems


def test_build_repair_prompt_flags_dependent_sentences():
    prompt = write_module.build_repair_prompt(
        "## Body\n" + FLAGGED_SENTENCE + "\n" + DEPENDENT_SENTENCE,
        [FLAGGED_SENTENCE],
        {"claims": []},
    )
    assert DEPENDENT_SENTENCE in prompt
    assert write_module.DEPENDENT_CLAUSE_REASON in prompt


def test_validate_repair_accepts_removing_an_emptied_paragraph():
    original = REPAIR_FRONT + "## Overview\n\n" + FLAGGED_SENTENCE + "\n" + DEPENDENT_SENTENCE + "\n\n" + OVERVIEW_SENTENCE + "\n"
    repaired = REPAIR_FRONT + "## Overview\n\n" + OVERVIEW_SENTENCE + "\n"
    assert write_module.validate_repair(original, repaired, [FLAGGED_SENTENCE]) == []


def test_validate_repair_accepts_removing_an_emptied_section():
    original = (
        REPAIR_FRONT
        + "## Overview\n\n"
        + OVERVIEW_SENTENCE
        + "\n\n## Extra\n\n"
        + FLAGGED_SENTENCE
        + "\n"
        + DEPENDENT_SENTENCE
        + "\n"
    )
    repaired = REPAIR_FRONT + "## Overview\n\n" + OVERVIEW_SENTENCE + "\n"
    assert write_module.validate_repair(original, repaired, [FLAGGED_SENTENCE]) == []


def test_repair_prompt_uses_the_compact_view():
    sheet = {**facts_sheet(), "grounding": [{"claim": "x", "reason": "y"}]}
    prompt = write_module.build_repair_prompt("## Body\nA sentence.", ["A sentence"], sheet)
    assert "F1: 'A game was announced'" in prompt
    assert "- A sentence" in prompt
    assert "grounding" not in prompt


def big_sheet(count=40, *, central_kind="confirmed"):
    claims = []
    for index in range(count):
        kind = central_kind if index == 0 else ("rumor" if index % 3 == 0 else "reported")
        claims.append({
            "claim": f"Claim {index}",
            "value": f"token{index} " + ("detail " * 80),
            "source_url": "https://news.xbox.com/a",
            "confidence": 0.9,
            "kind": kind,
            "central": index == 0,
        })
    return {**facts_sheet(), "claims": claims}


def test_a_small_prompt_needs_no_fallback():
    primary = write_module.build_prompt(facts_sheet())
    assert write_module.build_fallback_prompt(facts_sheet(), primary=primary) == primary


def test_the_fallback_prompt_fits_the_budget_and_keeps_central_claims():
    sheet = big_sheet()
    primary = write_module.build_prompt(sheet)
    fallback = write_module.build_fallback_prompt(sheet, primary=primary)
    assert providers.estimate_prompt_tokens(primary) > write_module.WRITER_FALLBACK_TOKEN_BUDGET
    assert providers.estimate_prompt_tokens(fallback) <= write_module.WRITER_FALLBACK_TOKEN_BUDGET
    assert "token0" in fallback
    assert "https://news.xbox.com/a" in fallback


def test_the_fallback_prompt_never_drops_a_central_claim():
    sheet = big_sheet(count=1)
    sheet["claims"][0]["value"] = "centraltoken " + ("detail " * 2000)
    sheet["claims"].extend({
        "claim": f"Extra {index}",
        "value": f"extra{index} " + ("detail " * 80),
        "source_url": "https://news.xbox.com/a",
        "confidence": 0.9,
        "kind": "reported",
        "central": False,
    } for index in range(5))
    fallback = write_module.build_fallback_prompt(sheet, primary=write_module.build_prompt(sheet))
    assert "centraltoken" in fallback


def test_the_fallback_prompt_drops_rumors_before_confirmed_claims():
    sheet = big_sheet()
    fallback = write_module.build_fallback_prompt(sheet, primary=write_module.build_prompt(sheet))
    assert "token0" in fallback
    assert "token3" not in fallback


def test_the_primary_prompt_is_never_changed_by_the_fallback_builder():
    sheet = big_sheet()
    primary = write_module.build_prompt(sheet)
    write_module.build_fallback_prompt(sheet, primary=primary)
    assert write_module.build_prompt(sheet) == primary


def test_the_fallback_prompt_keeps_the_single_origin_note():
    sheet = {
        **big_sheet(),
        "confirmation": {
            "rule": "single-origin",
            "single_origin": {"speaker": "Romain Toutain", "outlets": ["Eurogamer"]},
        },
    }
    fallback = write_module.build_fallback_prompt(sheet, primary=write_module.build_prompt(sheet))
    assert "rests on one named source" in fallback


def test_write_passes_the_trimmed_prompt_only_when_the_primary_is_too_large():
    seen = {}

    def fake_generate(role, prompt, *, run_state, **kwargs):
        seen["prompt"] = prompt
        seen["kwargs"] = kwargs
        return Generation(role=role, provider="mock", model="m", family="google", value="D")

    write_module.write(big_sheet(), run_state=RunState(), generate=fake_generate)
    assert "fallback_prompt" in seen["kwargs"]
    assert len(seen["kwargs"]["fallback_prompt"]) < len(seen["prompt"])
    assert seen["prompt"] == write_module.build_prompt(big_sheet())

    write_module.write(facts_sheet(), run_state=RunState(), generate=fake_generate)
    assert "fallback_prompt" not in seen["kwargs"]


def test_saved_sheet_writer_prompt_fits_groq_after_compaction():
    sheet_path = Path(write_module.PROJECT_ROOT) / "data" / "facts" / "b70cf3951d15.json"
    if not sheet_path.is_file():
        pytest.skip("saved facts sheet data/facts/b70cf3951d15.json is not present")
    sheet = json.loads(sheet_path.read_text(encoding="utf-8"))

    prompt = write_module.build_prompt(sheet)
    estimate = providers.estimate_prompt_tokens(prompt)

    limit = providers._tpm_limit("groq", "openai/gpt-oss-120b", {"groq": {"openai/gpt-oss-120b": 8000}})
    assert limit == 8000
    cap = int(0.9 * limit - estimate)

    assert estimate <= 5000, f"compact writer prompt is {estimate:.0f} tokens"
    assert cap >= providers.DEFAULT_MIN_OUTPUT_TOKENS["writer"], (
        f"computed Groq cap {cap} is below the writer minimum "
        f"{providers.DEFAULT_MIN_OUTPUT_TOKENS['writer']}; the sheet would be skipped"
    )


def test_writer_template_cites_every_source_together():
    template = write_module.load_template()
    assert "cite every source together" in template
    assert "according to Xbox Wire and PC Gamer" in template


def test_writer_template_limits_outlet_sentence_openers():
    template = write_module.load_template()
    assert "Never open more than two consecutive sentences with the same outlet name" in template


def test_writer_template_sections_need_two_supported_facts():
    template = write_module.load_template()
    assert "at least two facts for it" in template
    assert '"why it matters" filler section' in template


def test_writer_template_unconfirmed_section_only_with_an_unconfirmed_claim():
    template = write_module.load_template()
    assert "only when the sheet holds an unconfirmed claim" in template


def test_writer_template_never_refers_to_an_unstated_claim():
    template = write_module.load_template()
    assert "Never refer to a claim, prediction or rumor the article has not stated" in template
    assert "never open with" in template


def test_writer_template_merges_fact_lines_that_make_one_point():
    template = write_module.load_template()
    assert "Fact lines making one point" in template
    assert "never restate it later" in template
    # The phrases earlier tests rely on are untouched.
    assert "cite every source together" in template
    assert "according to Xbox Wire and PC Gamer" in template


def test_writer_template_credits_the_person_and_shares_only_the_common_title():
    template = write_module.load_template()
    assert "**Name the person.**" in template
    assert "not their company" in template
    assert "use what they share" in template
    assert "a senior vice president at Nvidia" in template


def test_writer_template_credits_the_relaying_outlet():
    template = write_module.load_template()
    assert "**Credit the relay.**" in template
    assert "second-hand or translated" in template
    assert "according to TechPowerUp News, Fisher told Taiwanese media" in template


def test_writer_template_matches_the_verb_to_the_source():
    template = write_module.load_template()
    assert "**Match the verb.**" in template
    assert '"confirmed" only for a `confirmed` fact' in template
    assert "no verb more than twice" in template


def test_writer_template_leaves_out_asides_and_keeps_rumor_support_with_the_rumor():
    template = write_module.load_template()
    assert "**No asides.**" in template
    assert "context-dependent remark" in template
    assert 'goes under "What is unconfirmed"' in template


def test_writer_template_rules_are_numbered_without_gaps():
    template = write_module.load_template()
    numbers = [int(match) for match in re.findall(r"^(\d+)\. \*\*", template, re.MULTILINE)]
    assert numbers == list(range(1, len(numbers) + 1))
    assert numbers[-1] >= 36


def test_writer_template_stays_within_its_token_ceiling():
    # The saved-sheet Groq test only runs where that sheet exists; this one
    # always runs and stops the rules from growing past what the budget allows.
    tokens = providers.estimate_prompt_tokens(write_module.load_template())
    assert tokens <= 3700, (
        f"writer template is {tokens:.0f} tokens; the Groq fallback needs the compact "
        "prompt under 5000, so make room before adding a rule"
    )


# --- a single-origin story tells the writer to attribute everything ------------------


def single_origin_sheet(**origin):
    sheet = facts_sheet()
    sheet["confirmation"] = {
        "rule": "single-origin",
        "confirmed": True,
        "single_origin": {
            "origin": "romain toutain", "speaker": "Romain Toutain",
            "owners": ["a", "b"], "outlets": ["Eurogamer", "Xbox Wire"], **origin,
        },
    }
    return sheet


def test_a_single_origin_story_adds_a_note_to_the_writers_facts():
    view = write_module.writer_view(single_origin_sheet())
    note = view["single_origin_note"]
    assert "Romain Toutain" in note
    assert "Eurogamer, Xbox Wire" in note
    assert "never state a claim in the publication's own voice" in note
    assert "Do not call it confirmed" in note


@pytest.mark.parametrize("confirmation", [
    {"rule": "first-party", "confirmed": True},
    {"rule": "two-independent-origins", "confirmed": True},
    {"rule": "tier1", "confirmed": True},
    {"rule": "unconfirmed", "confirmed": False},
    {"tier1_sources": 1, "confirmed": True},
    None,
    "single-origin",
])
def test_any_other_story_gets_no_note(confirmation):
    sheet = facts_sheet()
    sheet["confirmation"] = confirmation
    assert "single_origin_note" not in write_module.writer_view(sheet)


@pytest.mark.parametrize("rule", ["first-party", "two-independent-origins", "tier1", "unconfirmed", None])
def test_a_stale_single_origin_block_under_another_rule_gets_no_note(rule):
    # Only the rule decides; a leftover block of speaker details must not.
    sheet = single_origin_sheet()
    sheet["confirmation"]["rule"] = rule
    assert write_module.single_origin_note(sheet) == ""
    assert "single_origin_note" not in write_module.writer_view(sheet)


def test_a_single_origin_sheet_without_a_named_speaker_gets_no_note():
    assert write_module.single_origin_note(single_origin_sheet(speaker="")) == ""
    assert write_module.single_origin_note(single_origin_sheet(speaker=None)) == ""


def test_the_note_names_no_outlets_when_none_are_recorded():
    note = write_module.single_origin_note(single_origin_sheet(outlets=[]))
    assert "Name the outlets" not in note
    assert "Romain Toutain" in note


def test_the_prompt_carries_the_note_only_for_a_single_origin_story():
    assert "single_origin_note" in write_module.build_prompt(single_origin_sheet())
    assert "rests on one named source" in write_module.build_prompt(single_origin_sheet())
    assert "single_origin_note" not in write_module.build_prompt(facts_sheet())


def test_the_static_template_stays_free_of_the_single_origin_text():
    # The note rides in the facts, so the template keeps its Groq size budget.
    template = write_module.load_template()
    assert "single-origin" not in template and "rests on one named source" not in template


def test_a_non_dict_sheet_is_passed_through_without_a_note():
    assert write_module.single_origin_note("a pre-rendered sheet") == ""


# --- credit a figure only to the outlets that report it ---------------------------

PBZ_SOURCES = [
    {"source_name": "Polygon", "link": "https://polygon.example/pbz"},
    {"source_name": "Eurogamer", "link": "https://eurogamer.example/pbz"},
]


def pbz_claim(**overrides):
    claim = {
        "claim": "Concurrent players at launch",
        "value": "attracted over 431,000 concurrent players at launch",
        "kind": "reported",
        "source_urls": ["https://eurogamer.example/pbz", "https://polygon.example/pbz"],
        "versions": [
            {"source_url": "https://polygon.example/pbz",
             "value": "833,000 concurrent players within hours of launch"},
            {"source_url": "https://eurogamer.example/pbz",
             "value": "attracted over 431,000 concurrent players at launch"},
        ],
    }
    claim.update(overrides)
    return claim


def view_sources(claim):
    view = write_module.writer_view({"sources": PBZ_SOURCES, "claims": [claim]})
    return view["claims"][0].get("sources")


def test_a_figure_is_credited_only_to_the_outlets_that_report_it():
    assert view_sources(pbz_claim()) == ["Eurogamer"]


def test_every_outlet_with_the_figure_keeps_its_credit():
    claim = pbz_claim()
    claim["versions"][0]["value"] = "over 431,000 people played at launch"
    assert view_sources(claim) == ["Eurogamer", "Polygon"]


def test_an_outlets_evidence_counts_as_its_report():
    claim = pbz_claim(evidence=[{
        "source_url": "https://polygon.example/pbz", "quote": "It drew 431,000 players at launch.",
    }])
    assert view_sources(claim) == ["Eurogamer", "Polygon"]


@pytest.mark.parametrize("overrides", [
    # No figures: nothing to narrow.
    {"value": "attracted a large crowd at launch"},
    # No outlet holds the figure: the full list stays and the verifier decides.
    {"value": "attracted 999 players at launch"},
    # No per-outlet versions: the merged value is every outlet's.
    {"versions": []},
])
def test_the_full_source_list_stays_when_there_is_nothing_to_narrow(overrides):
    assert view_sources(pbz_claim(**overrides)) == ["Eurogamer", "Polygon"]


def test_a_single_source_claim_is_unchanged():
    claim = pbz_claim(source_urls=["https://polygon.example/pbz"])
    assert view_sources(claim) == ["Polygon"]


def test_a_merged_value_does_not_count_for_an_outlet_without_its_own_version():
    # Eurogamer and Polygon both covered it, but only Eurogamer has a version.
    claim = pbz_claim()
    claim["versions"] = claim["versions"][1:]
    assert view_sources(claim) == ["Eurogamer"]


def test_an_outlet_giving_the_date_without_the_year_keeps_its_credit():
    claim = pbz_claim(value="launches on October 29, 2026")
    claim["versions"] = [
        {"source_url": "https://polygon.example/pbz", "value": "out October 29"},
        {"source_url": "https://eurogamer.example/pbz", "value": "launches on October 29, 2026"},
    ]
    assert view_sources(claim) == ["Eurogamer", "Polygon"]
