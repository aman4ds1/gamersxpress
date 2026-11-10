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


def test_writer_template_bans_causal_connectives():
    template = write_module.load_template()
    assert "No causal or contrast connectives" in template
    for phrase in ("in contrast", "because", "driven by", "thanks to", "as a result"):
        assert phrase in template


def test_writer_template_keeps_overlaps_and_percentages_exact():
    template = write_module.load_template()
    assert "Percentages and overlaps exactly as stated" in template
    assert "came from" in template


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


def test_writer_template_sections_need_two_supported_facts():
    template = write_module.load_template()
    assert "at least two facts for it" in template
    assert '"why it matters" filler section' in template


def test_writer_template_never_refers_to_an_unstated_claim():
    template = write_module.load_template()
    assert "Never refer to a claim, prediction or rumor the article has not stated" in template
    assert "never open with" in template


def test_writer_template_credits_the_person_and_shares_only_the_common_title():
    template = write_module.load_template()
    assert "**Name the person.**" in template
    assert "not their company" in template
    assert "use what they share" in template
    assert "a senior vice president at Nvidia" in template


def test_writer_template_matches_the_verb_to_the_source():
    template = write_module.load_template()
    assert "**Match the verb.**" in template
    assert '"confirmed" only for a `confirmed` fact' in template
    assert "no verb more than twice" in template


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


# --- the editorial voice: facts stated directly, no outlet names ---------------------


def test_the_template_has_no_unconfirmed_section_or_outlet_credit_rules():
    template = write_module.load_template()
    assert "## What is unconfirmed" not in template
    assert "What is unconfirmed" not in template
    for removed in ("attribute it to the outlet", "Attribute a source's angle", "cite every source together"):
        assert removed not in template


def test_the_template_asks_for_the_sites_own_voice_with_facts_stated_directly():
    template = write_module.load_template()
    assert "**Editorial voice, facts stated directly.**" in template
    assert "like an encyclopedia entry" in template
    assert 'State a fact plainly: "The game is delayed to May 20, 2027"' in template
    assert 'never "Eurogamer says the game is delayed"' in template


def test_the_template_bans_outlet_attribution_and_outlet_names():
    template = write_module.load_template()
    for banned in ('"according to <outlet>"', '"<outlet> reports (that)"', '"<outlet> notes"', "claims"):
        assert banned in template
    assert "Never name an outlet or website in the title, description or body" in template
    assert "the front matter `sources` is the only place outlets appear" in template
    assert "A person or company that made a statement or estimate is named as its speaker" in template


def test_the_template_handles_each_kind_without_naming_an_outlet():
    template = write_module.load_template()
    assert "**Treat each `kind` this way.**" in template
    for kind in ("`confirmed`, `reported`", "`estimate`", "`statement`", "`rumor`", "`opinion`"):
        assert kind in template
    assert "state it directly" in template
    assert 'when `origin` is `unstated`, leave the claim out' in template
    assert "**`origin` is a speaker, not an outlet.**" in template
    assert "never fall back to naming an outlet" in template


def test_the_template_keeps_hedges_in_the_same_sentence_without_a_separate_section():
    template = write_module.load_template()
    assert "**Hedge in the same sentence.**" in template
    assert "There is no separate section or heading for unconfirmed material" in template
    assert '"is estimated to have sold"' in template
    assert '"is rumored to"' in template
    assert "Keep hedge words" in template
    for hedge in ("estimated", "reportedly", "more than", "over"):
        assert hedge in template
    # A status tail on the claim itself is fine; an absence claim about a named party
    # ("has not confirmed") needs a claim in the sheet that says so.
    assert '"..., though the figure is unconfirmed"' in template
    assert "vary the wording" in template
    assert 'Never write that a named party "has not confirmed"' in template


def test_the_template_states_absences_in_the_sites_own_voice():
    template = write_module.load_template()
    assert "**Absences in the site's own voice.**" in template
    assert '"Activision has not revealed an exact time"' in template
    assert 'never "Activision says it has not revealed an exact time"' in template


def test_the_template_credits_the_speaker_never_the_relaying_outlet():
    template = write_module.load_template()
    assert "**Credit the speaker, not the relay.**" in template
    assert '"Fisher told Taiwanese media"' in template
    assert "never name the outlet that relayed it" in template


def test_the_template_keeps_its_fact_once_and_aside_rules():
    template = write_module.load_template()
    assert "**State each fact once.**" in template
    assert "never restate it later" in template
    assert "**No asides.**" in template
    assert "context-dependent remark" in template


def test_the_description_and_body_length_rules_match_the_gates():
    template = write_module.load_template()
    assert "one line only, with no outlet names" in template
    assert "200-400 words" in template
    assert "must clear 200 words" in template
    assert "320-450" not in template


def test_writer_template_rules_are_numbered_without_gaps():
    template = write_module.load_template()
    numbers = [int(match) for match in re.findall(r"^(\d+)\. \*\*", template, re.MULTILINE)]
    assert numbers == list(range(1, len(numbers) + 1))
    assert numbers[-1] >= 30


# --- the writer's facts view names no outlet ------------------------------------------

VOICE_SOURCES = [
    {"source_name": "Polygon", "link": "https://polygon.example/pbz"},
    {"source_name": "Eurogamer", "link": "https://eurogamer.example/pbz"},
]


def voice_claim(**overrides):
    claim = {
        "claim": "Concurrent players at launch",
        "value": "attracted over 431,000 concurrent players at launch",
        "kind": "reported", "origin": "unstated", "affiliation": "", "speaker_type": "none",
        "source_urls": ["https://eurogamer.example/pbz", "https://polygon.example/pbz"],
        "versions": [{"source_url": "https://polygon.example/pbz", "value": "833,000 players"}],
    }
    claim.update(overrides)
    return claim


def voice_view(*claims):
    return write_module.writer_view({"sources": VOICE_SOURCES, "claims": list(claims)})


def test_no_claim_in_the_writers_view_lists_the_outlets_that_reported_it():
    view = voice_view(voice_claim())
    assert "sources" not in view["claims"][0]
    assert "Polygon" not in json.dumps(view["claims"]) and "Eurogamer" not in json.dumps(view["claims"])
    # The front matter sources are still there, copied for the writer.
    assert [source["name"] for source in view["sources"]] == ["Polygon", "Eurogamer"]


def test_an_opinion_with_no_named_origin_is_left_out():
    view = voice_view(
        voice_claim(claim="Breathing room", value="the delay may give players breathing room", kind="opinion"),
        voice_claim(),
    )
    assert [claim["claim"] for claim in view["claims"]] == ["Concurrent players at launch"]


def test_an_opinion_from_a_named_person_is_kept():
    view = voice_view(voice_claim(
        claim="Verdict", value="the combat feels fluid", kind="opinion",
        origin="Jane Doe", speaker_type="person",
    ))
    assert [(claim["kind"], claim["origin"]) for claim in view["claims"]] == [("opinion", "Jane Doe")]


@pytest.mark.parametrize("kind", ["opinion", "statement"])
@pytest.mark.parametrize("origin, speaker_type", [("Eurogamer", "outlet"), ("polygon", "none"), ("kotaku.com", "outlet")])
def test_an_opinion_or_statement_that_belongs_to_an_outlet_is_left_out(kind, origin, speaker_type):
    view = voice_view(voice_claim(kind=kind, origin=origin, speaker_type=speaker_type), voice_claim(claim="Other"))
    assert [claim["claim"] for claim in view["claims"]] == ["Other"]


@pytest.mark.parametrize("kind", ["reported", "estimate", "rumor", "confirmed"])
def test_any_other_claim_with_an_outlet_origin_is_stated_without_an_origin(kind):
    view = voice_view(voice_claim(kind=kind, origin="Eurogamer", speaker_type="outlet"))
    assert view["claims"][0]["kind"] == kind
    assert view["claims"][0]["origin"] == "unstated"


def test_an_estimate_keeps_the_firm_that_made_it():
    view = voice_view(voice_claim(
        kind="estimate", value="estimated to have sold over 1 million copies",
        origin="Alinea Analytics", speaker_type="organization",
    ))
    assert view["claims"][0]["origin"] == "Alinea Analytics"


def test_a_statement_from_a_named_speaker_is_kept():
    view = voice_view(voice_claim(
        kind="statement", value="We are doing great", origin="Marty Stratton", speaker_type="person",
    ))
    assert [(claim["kind"], claim["origin"]) for claim in view["claims"]] == [("statement", "Marty Stratton")]


def test_a_single_origin_story_tells_the_writer_to_name_the_speaker_and_no_outlet():
    note = write_module.writer_view(single_origin_sheet())["single_origin_note"]
    assert "Romain Toutain" in note
    assert "Open by naming Romain Toutain" in note
    assert "Do not name any outlet" in note
    assert "never state a claim in the publication's own voice" in note
    assert "Do not call it confirmed" in note
    assert "Eurogamer" not in note and "Xbox Wire" not in note


# --- the repair prompt: keep the speaker, drop the outlet ------------------------------------


def repair_template():
    return Path(write_module.DEFAULT_REPAIR_PROMPT_PATH).read_text(encoding="utf-8")


def test_the_repair_prompt_keeps_speakers_and_drops_outlet_names():
    template = repair_template()
    assert "**Keep every speaker, drop every outlet name.**" in template
    assert 'a replacement must still attribute the statement to that same speaker' in template
    assert 'state the fact directly with no outlet name' in template
    assert "the front matter `sources` is the only place outlets appear" in template
    assert "Keep every hedge" in template
    assert "Keep every attribution" not in template


def test_the_repair_prompt_still_has_its_placeholders_and_numbered_rules():
    template = repair_template()
    for token in ("{{unsupported_clauses}}", "{{article}}", "{{facts_sheet}}"):
        assert token in template
    numbers = [int(match) for match in re.findall(r"^(\d+)\. ", template, re.MULTILINE)]
    assert numbers == list(range(1, len(numbers) + 1))


def test_the_writer_prompt_tells_the_model_to_fold_a_short_section_instead_of_writing_it():
    template = Path(write_module.DEFAULT_PROMPT_PATH).read_text(encoding="utf-8")
    assert "Every `##` section needs at least two sentences and 40 words" in template
    assert "fold what you have into another section instead of writing a short one" in template


# --- deleting flagged sentences in code ---------------------------------------------------------------------------------------

SAMPLE = """---
title: A sample story
description: A sample description of the story.
tags:
  - one
  - two
  - three
---

## What happened

The studio shipped an update on November 6. It broke the install size.

## Context

A rumor sentence about a mobile game. Another rumor sentence about costs. A supported sentence about the studio. A second supported sentence about the update.

## Extra

Only a rumor lives here. This follows the rumor and leans on it.

## Notes

- A bullet that is supported
- A bullet that is a rumor
- Another supported bullet
"""
RUMOR_1 = "A rumor sentence about a mobile game"
RUMOR_2 = "Another rumor sentence about costs"
RUMOR_3 = "Only a rumor lives here"
RUMOR_BULLET = "A bullet that is a rumor"


def test_flagged_sentences_are_removed_and_every_other_sentence_keeps_its_wording_and_order():
    result, removed = write_module.delete_flagged_sentences(SAMPLE, [RUMOR_1, RUMOR_2])
    assert removed == ["A rumor sentence about a mobile game.", "Another rumor sentence about costs."]
    assert "A supported sentence about the studio. A second supported sentence about the update." in result
    assert RUMOR_1 not in result and RUMOR_2 not in result
    assert result.startswith(SAMPLE[: SAMPLE.index("## What happened")])          # the front matter is byte for byte the same
    assert "The studio shipped an update on November 6. It broke the install size." in result


def test_the_result_is_always_a_valid_delete_only_repair_of_its_draft():
    for clauses in ([RUMOR_1], [RUMOR_1, RUMOR_2], [RUMOR_3], [RUMOR_BULLET], [RUMOR_1, RUMOR_BULLET]):
        result, removed = write_module.delete_flagged_sentences(SAMPLE, clauses)
        assert removed, clauses
        assert write_module.validate_repair(SAMPLE, result, clauses) == [], clauses


def test_a_sentence_leaning_on_a_removed_one_goes_with_it_and_an_emptied_section_loses_its_heading():
    result, removed = write_module.delete_flagged_sentences(SAMPLE, [RUMOR_3])
    assert removed == ["Only a rumor lives here.", "This follows the rumor and leans on it."]
    assert "## Extra" not in result and "## Notes" in result and "## Context" in result


def test_a_list_item_is_removed_and_the_list_stays_one_item_per_line():
    result, removed = write_module.delete_flagged_sentences(SAMPLE, [RUMOR_BULLET])
    assert removed == ["- A bullet that is a rumor"]
    assert "- A bullet that is supported\n- Another supported bullet" in result


def test_a_clause_without_its_full_stop_or_in_another_case_still_finds_its_sentence():
    result, removed = write_module.delete_flagged_sentences(SAMPLE, ["another RUMOR sentence about costs.", "A rumor sentence about a mobile"])
    assert len(removed) == 2 or len(removed) == 1
    assert "Another rumor sentence about costs" not in result


def test_nothing_is_changed_when_no_sentence_matches():
    assert write_module.delete_flagged_sentences(SAMPLE, ["A sentence that is nowhere in the article"]) == (SAMPLE, [])
    assert write_module.delete_flagged_sentences(SAMPLE, []) == (SAMPLE, [])
    assert write_module.delete_flagged_sentences("", [RUMOR_1]) == ("", [])


def test_the_lead_is_never_deleted_because_then_the_story_itself_is_in_question():
    assert write_module.delete_flagged_sentences(SAMPLE, ["The studio shipped an update on November 6"]) == (SAMPLE, [])
    assert write_module.delete_flagged_sentences(SAMPLE, ["The studio shipped an update on November 6", RUMOR_1]) == (SAMPLE, [])


def test_an_article_is_not_gutted_by_deletion():
    many = [RUMOR_1, RUMOR_2, RUMOR_3, RUMOR_BULLET, "It broke the install size"]
    assert write_module.delete_flagged_sentences(SAMPLE, many) == (SAMPLE, [])
    assert write_module.delete_flagged_sentences(SAMPLE, many, max_share=0.9)[1]
    assert write_module.delete_flagged_sentences(SAMPLE, many, max_share=1.0)[0] != SAMPLE      # only the lead is kept, never zero


def test_a_heading_is_never_deleted_by_matching_its_text():
    assert write_module.delete_flagged_sentences(SAMPLE, ["What happened", "Context"]) == (SAMPLE, [])


def test_deleting_twice_changes_nothing_more_and_a_headline_free_article_works():
    once, removed = write_module.delete_flagged_sentences(SAMPLE, [RUMOR_1])
    assert write_module.delete_flagged_sentences(once, [RUMOR_1]) == (once, [])
    plain = "Lead sentence here. A rumor about something. Another kept sentence. A fourth kept sentence. A fifth kept sentence. A sixth kept one."
    assert write_module.delete_flagged_sentences(plain, ["A rumor about something"])[0] == (
        "Lead sentence here. Another kept sentence. A fourth kept sentence. A fifth kept sentence. A sixth kept one.")


def test_a_trailing_newline_and_the_blank_line_after_the_front_matter_are_kept():
    result, _removed = write_module.delete_flagged_sentences(SAMPLE, [RUMOR_1])
    assert result.endswith("\n") and not result.endswith("\n\n") and "---\n\n## What happened" in result
    result, _removed = write_module.delete_flagged_sentences(SAMPLE.rstrip("\n"), [RUMOR_1])
    assert not result.endswith("\n")


def test_the_real_nier_draft_loses_only_its_two_rumor_sentences():
    clauses = ["One cancelled project in 2024 may have been a Tencent-backed Nier mobile game",
               "The mobile game reportedly struggled to find monetization to offset high costs"]
    result, removed = write_module.delete_flagged_sentences(NIER_ORIGINAL, clauses)
    assert removed == ["One cancelled project in 2024 may have been a Tencent-backed Nier mobile game.",
                       "The mobile game reportedly struggled to find monetization to offset high costs."]
    assert "Tencent" not in result and "monetization" not in result
    assert "## Context and comparison" in result and "Most projects the developer was working on were cancelled" in result
    assert "unchecking all eight DLC" in result and "366GB" in result
    assert write_module.validate_repair(NIER_ORIGINAL, result, clauses) == []
    assert len(result.split()) == len(NIER_ORIGINAL.split()) - 25      # 13 + 12 words


def test_the_repair_prompt_forbids_copying_and_tells_the_writer_what_to_do_with_a_rumor():
    template = write_module.load_template(write_module.DEFAULT_REPAIR_PROMPT_PATH)
    assert "**Never copy.**" in template and "eight or more words" in template
    assert "rests on a rumor, delete the sentence unless the rumor is what the whole article is about" in template


NIER_ORIGINAL = '---\ntitle: Nier: Automata Update Inflates PC File Size to 366GB\ndescription: Square Enix is investigating a Steam update bug for Nier: Automata that drastically increased required storage space to 366GB.\npubDate: 2026-11-10\ncategory: pc\ntags:\n  - nier automata\n  - square enix\n  - steam\n  - pc\nentities:\n  - Nier: Automata\n  - PC\n  - Steam\nsources:\n  - name: PC Gamer\n    url: https://pcgamer.com/games/rpg/nier-automata-developer-is-currently-investigating-a-bizarre-update-bug-that-drastically-inflated-the-games-file-size-from-50gb-to-366gb/\n  - name: Eurogamer\n    url: https://eurogamer.net/square-enix-nier-automata-update-required-file-size-366gb\n  - name: Dexerto\n    url: https://dexerto.com/gaming/square-enix-responds-after-nier-automata-update-triggers-massive-366gb-storage-requirement-3425350/\n---\n\n## What happened\n\nSquare Enix is investigating an update bug on PC that inflated the required installation file size for Nier: Automata from 50GB to 366GB. The update on November 6 added language support for Simplified Chinese, Traditional Chinese, and Korean. The update on Steam is not optional.\n\nSquare Enix confirmed an issue where the installation process displays a storage requirement higher than expected. Square Enix posted an apology on the official Japanese Nier Automata account. Square Enix stated it is investigating the cause and working on a solution.\n\n## Why gamers should care\n\nThe inflated file size is true for the initial download and installation. Steam demanded 366GB of free space to install the game. Players reported that Steam incorrectly counts the full game file size for each DLC in the YoRHa Edition. The download reportedly stacks all eight DLC concurrently. Once installed, the file reportedly reverts to a size of roughly 50GB.\n\nSquare Enix recommended a temporary workaround. Players can download the base game without the inflated storage requirement by opening Steam properties and unchecking all eight DLC.\n\n## Context and comparison\n\nInformation from earlier in 2026 indicates Nier Automata will continue. Most projects the developer was working on were cancelled, as learned in 2025.\n\nOne cancelled project in 2024 may have been a Tencent-backed Nier mobile game. The mobile game reportedly struggled to find monetization to offset high costs.'


def test_an_article_with_nothing_to_delete_comes_back_exactly_as_it_was_even_with_irregular_whitespace():
    messy = "---\ntitle: T\n---\n\n\n## Heading\n\nA soft-wrapped paragraph that\ncontinues here. A second sentence.   Third one.\n\n\n\nAnother paragraph.\n\n- item one\n- item two"
    assert write_module.delete_flagged_sentences(messy, ["Nothing like this sentence appears"]) == (messy, [])
    assert write_module.delete_flagged_sentences(messy, ["A heading that is not there"]) == (messy, [])
