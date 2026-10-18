import json

import pytest

import dryrun
import facts
import gather
import json_schema
import seo
import verify


def test_make_generate_answers_facts_schema():
    generate = dryrun.make_generate("pass")
    generation = generate("fast", "prompt", facts.CLAIM_SCHEMA)
    assert generation.family == "google"
    payload = generation.value
    assert json_schema.schema_errors(payload, facts.CLAIM_SCHEMA) == []
    # The mock answers with sentence ids, and every cited id resolves to a
    # sentence of the sample prose (S0 is empty: the sample has no title).
    numbered = [""] + gather._split_sentences(dryrun._SOURCE_PROSE)
    prose = dryrun._SOURCE_PROSE.lower()
    for claim in payload["claims"]:
        assert claim["source_url"].startswith("https://")
        assert claim["speaker_type"] in facts.SPEAKER_TYPES
        assert claim["quote_ids"]
        for sentence_id in claim["quote_ids"] + claim["attribution_ids"]:
            assert isinstance(sentence_id, int)
            assert 0 <= sentence_id < len(numbered)
        for sentence_id in claim["quote_ids"]:
            assert numbered[sentence_id].lower() in prose


def test_make_generate_answers_seo_schema():
    generation = dryrun.make_generate("pass")("fast", "prompt", seo.SEO_SCHEMA)
    payload = generation.value
    assert payload["slug"] == dryrun.SAMPLE_SLUG
    assert payload["category"] in seo.ALLOWED_CATEGORIES
    assert len(payload["tags"]) == 3


def test_make_generate_writer_body_length_by_scenario():
    pass_body = dryrun.make_generate("pass")("writer", "prompt").value
    assert len(pass_body.split()) >= 300
    short_body = dryrun.make_generate("fail-gate")("writer", "prompt").value
    assert len(short_body.split()) < 300


def test_make_generate_verifier_pass_and_fail():
    ok = json.loads(dryrun.make_generate("pass")("verifier", "prompt").value)
    assert ok["clauses"][0]["match"] == "exact"
    assert ok["clauses"][0]["fact_ids"] == ["F1"]
    assert ok["clauses"][0]["exceeds_facts"] is False
    assert ok["unsupported_claims"] == []

    bad = json.loads(dryrun.make_generate("fail-verify")("verifier", "prompt").value)
    assert bad["unsupported_claims"]
    assert bad["clauses"][0]["exceeds_facts"] is True


def test_make_generate_verifier_payload_is_schema_valid():
    for scenario in dryrun.SCENARIOS:
        payload = json.loads(dryrun.make_generate(scenario)("verifier", "prompt").value)
        assert json_schema.schema_errors(payload, verify.VERIFIER_SCHEMA) == [], scenario


def test_make_generate_answers_the_grouping_schema():
    generate = dryrun.make_generate("pass")
    generation = generate(
        "verifier",
        "- c0 | s0 | Nvidia confirmed the window\n- c1 | s1 | Nvidia confirmed the window",
        facts.GROUPING_SCHEMA,
    )
    assert json_schema.schema_errors(generation.value, facts.GROUPING_SCHEMA) == []
    assert generation.value["groups"] == [{"ids": ["c0", "c1"]}]



def test_unknown_scenario_raises():
    with pytest.raises(ValueError):
        dryrun.make_generate("nope")


def test_sample_pool_has_two_confirmed_tier_one_items():
    pool = dryrun.sample_pool()
    assert len(pool["items"]) == 2
    assert all(item["tier"] == 1 for item in pool["items"])


def test_sample_gathered_has_two_ok_sources():
    gathered = dryrun.sample_gathered("abc")
    assert all(source["status"] == "ok" and source["text"] for source in gathered["sources"])
    assert gathered["id"] == "abc"