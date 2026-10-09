import json

import pytest

import dryrun
import facts
import seo


def test_make_generate_answers_facts_schema():
    generate = dryrun.make_generate("pass")
    generation = generate("fast", "prompt", facts.CLAIM_SCHEMA)
    assert generation.family == "google"
    payload = generation.value
    for claim in payload["claims"]:
        assert claim["source_url"].startswith("https://")


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
    assert ok["sentences"][0]["supported"] is True
    assert ok["unsupported_claims"] == []

    bad = json.loads(dryrun.make_generate("fail-verify")("verifier", "prompt").value)
    assert bad["unsupported_claims"]


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