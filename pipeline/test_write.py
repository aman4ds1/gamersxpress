import json
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


def test_repair_prompt_uses_the_compact_view():
    sheet = {**facts_sheet(), "grounding": [{"claim": "x", "reason": "y"}]}
    prompt = write_module.build_repair_prompt("## Body\nA sentence.", ["A sentence"], sheet)
    assert "F1: 'A game was announced'" in prompt
    assert "- A sentence" in prompt
    assert "grounding" not in prompt


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