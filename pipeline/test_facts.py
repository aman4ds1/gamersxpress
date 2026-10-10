import datetime as dt
import json
import logging
from pathlib import Path

import pytest

import facts
from providers import Generation, RunState

NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)


def source(link="https://a.example/1", name="Alpha", tier=2, owner="co-a", region="us", text="Some body text about the story.", title=""):
    return {
        "link": link, "title": title, "source_name": name, "tier": tier, "owner": owner,
        "region": region, "status": "ok", "text": text, "error": None,
    }


def gathered(id="story-1", sources=None):
    return {"id": id, "gathered_at": NOW.isoformat(), "sources": sources or [source()]}


def claim(claim="Announcement happened", value="it happened", source_url="https://a.example/1", confidence=0.9, kind="confirmed", origin="test-origin"):
    return {
        "claim": claim, "value": value, "source_url": source_url,
        "confidence": confidence, "origin": origin, "kind": kind,
    }


def make_generate(payload):
    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        if run_state is not None:
            run_state.record(Generation(role=role, provider="mock", model="m", family="f", value=payload))
        return Generation(role=role, provider="mock", model="m", family="f", value=payload, fallbacks=[])

    return generate


# --- confirmation rule -------------------------------------------------------


@pytest.mark.parametrize(
    "sources, claims, allow, expected",
    [
        # one tier-1 source confirms on its own, no origin needed
        ([source(tier=1)], None, False, True),
        ([source(tier=1), source(link="https://a2", name="A2", tier=3)], None, False, True),
        # two independent attributed origins confirm
        (
            [source(), source(link="https://b.example/2", name="Beta", owner="co-b")],
            [
                claim(claim="A", source_url="https://a.example/1", origin="sony"),
                claim(claim="B", source_url="https://b.example/2", origin="microsoft"),
            ],
            False,
            True,
        ),
        # outlets repeating one origin count as one: not confirmed
        (
            [source(), source(link="https://b.example/2", name="Beta", owner="co-b")],
            [
                claim(claim="A", source_url="https://a.example/1", origin="sony"),
                claim(claim="B", source_url="https://b.example/2", origin="sony"),
            ],
            False,
            False,
        ),
        # a single origin needs allow_single_origin_attributed, and a named origin
        ([source()], [claim(source_url="https://a.example/1", origin="sony")], False, False),
        ([source()], [claim(source_url="https://a.example/1", origin="sony")], True, True),
        ([source()], [claim(source_url="https://a.example/1", origin="unstated")], True, False),
        # a source with no claims cannot confirm anything
        ([source()], [], False, False),
        # tier 3 never counts toward confirmation
        ([source(tier=3)], [claim(source_url="https://a.example/1", origin="sony")], False, False),
        (
            [source(tier=3), source(link="https://b.example/2", name="Beta", tier=3, owner="co-b")],
            [
                claim(claim="A", source_url="https://a.example/1", origin="sony"),
                claim(claim="B", source_url="https://b.example/2", origin="microsoft"),
            ],
            False,
            False,
        ),
    ],
)
def test_confirmation_rule(sources, claims, allow, expected):
    result = facts.confirmation_from_sources(
        sources, claims=claims, allow_single_origin_attributed=allow
    )
    assert result["confirmed"] is expected
    assert result["tier3_sources"] == sum(1 for s in sources if s["tier"] == 3)


def test_confirmation_tier3_never_counts():
    assert facts.confirmation_from_sources(
        [source(tier=3), source(link="https://b.example/2", name="Beta", tier=3, owner="co-b")],
        claims=[
            claim(claim="A", source_url="https://a.example/1", origin="sony"),
            claim(claim="B", source_url="https://b.example/2", origin="microsoft"),
        ],
    )["confirmed"] is False


def test_three_outlets_repeating_one_estimate_count_as_one_origin():
    sources = [
        source(link="https://o1.example/1", name="Outlet 1", owner="co-a"),
        source(link="https://o2.example/1", name="Outlet 2", owner="co-b"),
        source(link="https://o3.example/1", name="Outlet 3", owner="co-c"),
    ]
    claims = [
        claim(claim="Game estimated to sell", value="estimated figure", kind="estimate", origin="analyst-firm", source_url="https://o1.example/1"),
        claim(claim="Game estimated to sell", value="estimated figure", kind="estimate", origin="analyst-firm", source_url="https://o2.example/1"),
        claim(claim="Game estimated to sell", value="estimated figure", kind="estimate", origin="analyst-firm", source_url="https://o3.example/1"),
    ]
    result = facts.confirmation_from_sources(sources, claims=claims)
    assert result["origins"] == ["analyst-firm"], "three outlets repeating one origin count as one"
    assert result["confirmed"] is False


def test_three_spellings_of_one_analyst_count_as_one_origin():
    sources = [
        source(link="https://o1.example/1", name="Outlet 1", owner="co-a"),
        source(link="https://o2.example/1", name="Outlet 2", owner="co-b"),
        source(link="https://o3.example/1", name="Outlet 3", owner="co-c"),
    ]
    claims = [
        claim(claim="A", origin="Alinea Analytics", kind="estimate", source_url="https://o1.example/1"),
        claim(claim="B", origin="Alinea", kind="estimate", source_url="https://o2.example/1"),
        claim(claim="C", origin="Rhys Elliott of Alinea Analytics", kind="estimate", source_url="https://o3.example/1"),
    ]
    result = facts.confirmation_from_sources(sources, claims=claims)
    assert result["origins"] == ["alinea analytics"], "different spellings of one analyst count as one"
    assert result["confirmed"] is False


def test_canonicalize_origin_maps_people_titles_and_possessives():
    assert facts.canonicalize_origin("Rhys Elliott of Alinea Analytics") == "alinea analytics"
    assert facts.canonicalize_origin("Alinea") == "alinea analytics"
    assert facts.canonicalize_origin("Alinea Analytics Ltd") == "alinea analytics"
    assert facts.canonicalize_origin("the analyst Alinea Analytics") == "alinea analytics"
    assert facts.canonicalize_origin("Sony's") == "sony"


def test_two_genuinely_different_named_analysts_confirm():
    sources = [
        source(link="https://o1.example/1", name="Outlet 1", owner="co-a"),
        source(link="https://o2.example/1", name="Outlet 2", owner="co-b"),
    ]
    claims = [
        claim(claim="A", origin="Rhys Elliott of Alinea Analytics", kind="estimate", source_url="https://o1.example/1"),
        claim(claim="B", origin="Sensor Tower", kind="estimate", source_url="https://o2.example/1"),
    ]
    result = facts.confirmation_from_sources(sources, claims=claims)
    assert result["origins"] == ["alinea analytics", "sensor tower"]
    assert result["confirmed"] is True


def test_rumor_and_opinion_origins_do_not_confirm():
    sources = [
        source(link="https://o1.example/1", name="Outlet 1", owner="co-a"),
        source(link="https://o2.example/1", name="Outlet 2", owner="co-b"),
    ]
    claims = [
        claim(claim="A", origin="Alinea Analytics", kind="rumor", source_url="https://o1.example/1"),
        claim(claim="B", origin="Sensor Tower", kind="opinion", source_url="https://o2.example/1"),
    ]
    result = facts.confirmation_from_sources(sources, claims=claims)
    assert result["origins"] == []
    assert result["confirmed"] is False


def test_contained_origins_count_as_one():
    sources = [
        source(link="https://o1.example/1", name="Outlet 1", owner="co-a"),
        source(link="https://o2.example/1", name="Outlet 2", owner="co-b"),
    ]
    claims = [
        claim(claim="A", origin="Alinea Analytics", kind="estimate", source_url="https://o1.example/1"),
        claim(claim="B", origin="Alinea Analytics Ltd", kind="estimate", source_url="https://o2.example/1"),
    ]
    assert facts.confirmation_from_sources(sources, claims=claims)["origins"] == ["alinea analytics"]


def test_origins_count_when_source_url_differs_only_by_casing_or_slash():
    sources = [
        source(link="https://o1.example/a", name="Outlet 1", owner="co-a"),
        source(link="https://o2.example/b", name="Outlet 2", owner="co-b"),
    ]
    claims = [
        claim(claim="A", origin="Sony", kind="reported", source_url="https://o1.example/a/"),
        claim(claim="B", origin="Microsoft", kind="reported", source_url="https://O2.example/b"),
    ]
    result = facts.confirmation_from_sources(sources, claims=claims)
    assert result["origins"] == ["microsoft", "sony"]
    assert result["confirmed"] is True


def test_resolve_claim_origins_checks_text_across_url_variants(caplog):
    caplog.set_level(logging.WARNING)
    sources = [source(link="https://o1.example/report", tier=1, text="Alinea Analytics estimates 168,000 sales.")]
    claims = [
        claim(claim="Estimate", kind="estimate", origin="Alinea", source_url="https://www.o1.example/report/"),
        claim(claim="Bogus", kind="estimate", origin="Sensor Tower", source_url="https://o1.example/report#notes"),
    ]
    resolved = facts.resolve_claim_origins(claims, sources)
    assert resolved[0]["origin"] == "alinea analytics"
    assert resolved[1]["origin"] == "unstated"
    assert any("not named in the source text" in record.message for record in caplog.records)



# --- schema validation -------------------------------------------------------


def test_validate_claims_happy_path():
    claims = [claim(), claim(claim="Second", value="two", source_url="https://b.example/2")]
    assert facts.validate_claims({"claims": claims}) == claims


@pytest.mark.parametrize(
    "bad",
    [
        {"claims": [claim(confidence=1.5)]},
        {"claims": [claim(confidence=-0.1)]},
        {"claims": [claim(source_url="ftp://x.example/")]},
        {"claims": [claim(kind="bogus")]},
        {"claims": [claim(claim=1)]},
        {"claims": [{"claim": "missing rest"}]},
        {"claims": [claim(), {"claim": "extra", "value": "v", "source_url": "https://x", "confidence": 0.5, "kind": "confirmed", "bogus": 1}]},
        {"claims": "not a list"},
        {"claims": [claim(), {"no claim": None}]},
        {},
    ],
)
def test_validate_claims_rejects_bad_schema(bad):
    with pytest.raises(facts.FactsError):
        facts.validate_claims(bad)


def test_validate_claims_clips_long_claim_and_value():
    long_claim = "claim " * 100
    long_value = "value " * 100
    claims = facts.validate_claims({"claims": [claim(claim=long_claim, value=long_value)]})
    assert len(claims[0]["claim"]) <= facts.MAX_FIELD_CHARS
    assert len(claims[0]["value"]) <= facts.MAX_FIELD_CHARS
    assert long_claim.startswith(claims[0]["claim"])
    assert long_value.startswith(claims[0]["value"])

    short = facts.validate_claims({"claims": [claim(claim="Short", value="Tiny")]})
    assert short[0]["claim"] == "Short"
    assert short[0]["value"] == "Tiny"


def test_validate_claims_caps_unstated_origin_at_reported():
    validated = facts.validate_claims(
        {"claims": [claim(kind="confirmed", origin="unstated"), claim(claim="Estimate", kind="estimate", origin="UNSTATED")]}
    )
    assert validated[0]["origin"] == "unstated"
    assert validated[0]["kind"] == "reported"
    assert validated[1]["kind"] == "reported"


def test_validate_claims_keeps_kind_for_attributed_origin():
    validated = facts.validate_claims(
        {"claims": [claim(kind="confirmed", origin="Sony"), claim(claim="Estimate", kind="estimate", origin="Sensor Tower")]}
    )
    assert validated[0]["origin"] == "sony"
    assert validated[0]["kind"] == "confirmed"
    assert validated[1]["kind"] == "estimate"


# --- merging duplicate claims across sources ---------------------------------


def test_merge_claims_one_fact_per_duplicate_with_all_source_urls():
    merged = facts.merge_claims(
        [
            claim(claim="Gears sold 168,000 on Steam", value="168,000 copies", source_url="https://a.example/1"),
            claim(claim="Gears sold 168,000 on Steam", value="168,000 copies", source_url="https://b.example/2"),
        ]
    )
    assert len(merged) == 1
    assert merged[0]["source_urls"] == ["https://a.example/1", "https://b.example/2"]


def test_merge_claims_keeps_distinct_facts_separate():
    merged = facts.merge_claims(
        [
            claim(claim="Gears sold 168,000 on Steam", value="168,000", source_url="https://a.example/1"),
            claim(claim="Gears launches on Game Pass", value="Game Pass", source_url="https://a.example/1"),
        ]
    )
    assert len(merged) == 2


def test_estimate_never_marked_confirmed():
    merged = facts.merge_claims(
        [
            claim(claim="Steam units sold", value="sales", kind="confirmed", source_url="https://a.example/1"),
            claim(claim="Steam units sold", value="estimated sales", kind="estimate", source_url="https://b.example/2"),
        ]
    )
    assert merged[0]["kind"] == "estimate"
    assert merged[0]["kind"] != "confirmed"


def test_merge_claims_takes_weakest_kind_when_sources_disagree():
    merged = facts.merge_claims(
        [
            claim(claim="Price", value="price", kind="confirmed", source_url="https://a.example/1"),
            claim(claim="Price", value="reported price", kind="reported", source_url="https://b.example/2"),
        ]
    )
    assert merged[0]["kind"] == "reported"


def test_merge_claims_keeps_hedge_words_in_value():
    merged = facts.merge_claims(
        [
            claim(claim="Copies sold", value="168,000 copies", kind="confirmed", source_url="https://a.example/1"),
            claim(claim="Copies sold", value="estimated 168,000 copies", kind="estimate", source_url="https://b.example/2"),
        ]
    )
    assert "estimated" in merged[0]["value"]

    hedged = facts.merge_claims(
        [
            claim(claim="More sales", value="100,000 copies", kind="confirmed", source_url="https://a.example/1"),
            claim(claim="More sales", value="more than 100,000 copies", kind="reported", source_url="https://b.example/2"),
        ]
    )
    assert "more than" in hedged[0]["value"]


def test_merge_claims_joins_named_origins_and_keeps_lowest_confidence():
    merged = facts.merge_claims(
        [
            claim(claim="Same fact", value="figure", origin="sony", confidence=0.9, source_url="https://a.example/1"),
            claim(claim="Same fact", value="figure", origin="microsoft", confidence=0.6, source_url="https://b.example/2"),
        ]
    )
    assert merged[0]["origin"] == "microsoft; sony"
    assert merged[0]["confidence"] == 0.6


def test_merge_claims_origin_unstated_when_no_source_names_one():
    merged = facts.merge_claims(
        [
            claim(claim="Same fact", value="figure", origin="unstated", source_url="https://a.example/1"),
            claim(claim="Same fact", value="figure", origin="unstated", source_url="https://b.example/2"),
        ]
    )
    assert merged[0]["origin"] == "unstated"


# --- facts() end to end ------------------------------------------------------


def test_facts_writes_sheet_and_records_fast_role(tmp_path):
    data = gathered(sources=[source(tier=1, text="test-origin reported the story.")])
    generation = make_generate({"claims": [claim()]})
    run_state = RunState()
    result = facts.facts(
        data,
        output_dir=tmp_path,
        now=NOW,
        generate=generation,
        run_state=run_state,
    )
    assert result["id"] == "story-1"
    assert result["confirmation"]["confirmed"] is True
    assert result["claims"][0]["kind"] == "confirmed"
    assert result["sources"][0]["source_name"] == "Alpha"

    path = tmp_path / "story-1.json"
    assert path.is_file()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["claims"][0]["source_urls"] == ["https://a.example/1"]
    assert written["claims"][0]["origin"] == "test-origin"
    assert written["extractor"]["family"] == "f"
    assert run_state.generations["fast"].provider == "mock"


def test_facts_rejects_story_with_no_attributed_origin(tmp_path):
    calls = []

    def generate(role, prompt, json_schema=None, **kwargs):
        calls.append(role)
        return Generation(
            role=role, provider="mock", model="m", family="f",
            value={"claims": [claim(kind="reported", origin="unstated")]},
        )

    data = gathered(sources=[source()])  # single tier-2 source, no tier-1
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert calls == ["fast"], "origins only exist after extraction, so the model must run"
    assert not (tmp_path / "story-1.json").exists()


def test_facts_rejects_three_outlets_repeating_one_estimate(tmp_path):
    estimate = claim(
        claim="Game estimated to sell", value="estimated figure",
        kind="estimate", origin="analyst-firm", source_url="https://o1.example/1",
    )
    payloads = [
        {"claims": [{**estimate, "source_url": "https://o1.example/1"}]},
        {"claims": [{**estimate, "source_url": "https://o2.example/1"}]},
        {"claims": [{**estimate, "source_url": "https://o3.example/1"}]},
    ]
    generate, calls = per_source_generate(payloads)
    data = gathered(
        sources=[
            source(link="https://o1.example/1", name="Outlet 1", owner="co-a"),
            source(link="https://o2.example/1", name="Outlet 2", owner="co-b"),
            source(link="https://o3.example/1", name="Outlet 3", owner="co-c"),
        ]
    )
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert calls == ["fast", "fast", "fast"]
    assert not (tmp_path / "story-1.json").exists()


def test_facts_allows_single_attributed_origin_when_enabled(tmp_path):
    data = gathered(sources=[source(text="Sony said the card is coming.")])  # single tier-2 source
    result = facts.facts(
        data,
        output_dir=tmp_path,
        now=NOW,
        allow_single_origin_attributed=True,
        generate=make_generate({"claims": [claim(origin="sony")]}),
    )
    assert result["confirmation"]["rule"] == "single-origin-attributed"
    assert result["confirmation"]["confirmed"] is True


def test_origin_not_in_source_text_is_downgraded(caplog, tmp_path):
    caplog.set_level(logging.WARNING)
    data = gathered(
        sources=[source(tier=1, text="Nvidia confirmed the launch window for its next graphics card line.")]
    )
    result = facts.facts(
        data,
        output_dir=tmp_path,
        now=NOW,
        generate=make_generate(
            {"claims": [claim(claim="Nvidia confirmed the window", origin="Alinea Analytics", kind="estimate")]}
        ),
    )
    assert result["claims"][0]["origin"] == "unstated"
    assert any("not named in the source text" in record.message for record in caplog.records)


def test_origin_named_in_source_text_is_canonicalized(tmp_path):
    data = gathered(sources=[source(tier=1, text="Alinea Analytics estimates 168,000 sales.")])
    result = facts.facts(
        data,
        output_dir=tmp_path,
        now=NOW,
        generate=make_generate({"claims": [claim(claim="Sales figure", origin="Alinea", kind="estimate")]}),
    )
    assert result["claims"][0]["origin"] == "alinea analytics"


def test_facts_lists_only_sources_that_contributed_a_claim(tmp_path):
    data = gathered(
        sources=[
            source(link="https://a.example/1", name="Alpha", tier=1),
            source(link="https://b.example/2", name="Beta", tier=1, owner="co-b"),
        ]
    )
    generate, _calls = per_source_generate(
        [{"claims": [claim()]}, {"claims": []}]
    )
    result = facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert [entry["source_name"] for entry in result["sources"]] == ["Alpha"]


def test_facts_rejects_two_tier2_same_owner(tmp_path):
    data = gathered(sources=[source(), source(link="https://a2", name="Alpha 2", owner="co-a")])
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(data, output_dir=tmp_path, now=NOW, generate=make_generate({"claims": []}))


def test_facts_drops_story_on_invalid_schema(tmp_path):
    data = gathered(sources=[source(tier=1)])
    with pytest.raises(facts.FactsError):
        facts.facts(
            data,
            output_dir=tmp_path,
            now=NOW,
            generate=make_generate({"claims": [claim(confidence=9)]}),
        )
    assert not (tmp_path / "story-1.json").exists()


def test_facts_custom_id_and_output_dir(tmp_path):
    data = gathered(id="run-1", sources=[source(tier=1)])
    facts.facts(
        data,
        id="custom-id",
        output_dir=tmp_path,
        now=NOW,
        generate=make_generate({"claims": [claim()]}),
    )
    assert (tmp_path / "custom-id.json").exists()


def test_facts_reuses_sheet_when_fingerprint_matches(tmp_path):
    calls = []

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        calls.append(role)
        return Generation(role=role, provider="mock", model="m", family="f", value={"claims": [claim()]})

    data = gathered(sources=[source(tier=1)])
    first = facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert calls == ["fast"]

    second = facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert second == first
    assert second["fingerprint"]["value"] == first["fingerprint"]["value"]
    assert calls == ["fast"], "the model must not be called again on a fingerprint match"


def test_facts_regenerates_when_fingerprint_mismatches(tmp_path):
    calls = []

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        calls.append(role)
        return Generation(role=role, provider="mock", model="m", family="f", value={"claims": [claim()]})

    data = gathered(sources=[source(tier=1)])
    facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)

    path = tmp_path / "story-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fingerprint"]["value"] = "stale"
    path.write_text(json.dumps(payload), encoding="utf-8")

    calls.clear()
    facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert calls == ["fast"], "a stale fingerprint must be re-extracted, not reused"
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["fingerprint"]["value"] != "stale"


def test_facts_regenerates_when_file_has_no_fingerprint(tmp_path):
    calls = []

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        calls.append(role)
        return Generation(role=role, provider="mock", model="m", family="f", value={"claims": [claim()]})

    data = gathered(sources=[source(tier=1)])
    facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    path = tmp_path / "story-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["fingerprint"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    calls.clear()
    facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert calls == ["fast"], "a pre-fingerprint sheet must be re-extracted, not reused"


# --- prompt ------------------------------------------------------------------


def test_build_prompt_includes_source_text_and_cap():
    src = source(text="DMZ Cash buys deployables, from air strikes to a tactical nuke.")
    prompt = facts.build_prompt("story-1", src, max_claims=7)
    assert "story-1" in prompt
    assert "Alpha" in prompt
    assert "https://a.example/1" in prompt
    assert "DMZ Cash buys deployables" in prompt
    assert "Extract up to 7 distinct facts" in prompt
    assert "source_url must be exactly https://a.example/1" in prompt
    assert "never appear as a bare dollar figure" in prompt


def test_build_prompt_truncates_long_text():
    long_text = "word " * 6000
    prompt = facts.build_prompt("s", source(text=long_text))
    assert "[truncated]" in prompt
    assert len(prompt) < len(long_text)


def test_build_prompt_includes_headline_and_headline_claim_instruction():
    src = source(title="Gears of War: E-Day Sells 168,000 Copies on Steam")
    prompt = facts.build_prompt("story-1", src)
    assert "TITLE: Gears of War: E-Day Sells 168,000 Copies on Steam" in prompt
    assert "headline's central claim" in prompt


def test_system_instruction_defines_new_kinds_origin_and_relationships():
    instruction = facts.SYSTEM_INSTRUCTION
    for kind in facts.ALLOWED_KINDS:
        assert kind in instruction
    assert "origin" in instruction
    assert "unstated" in instruction
    assert "never label an estimate 'confirmed'" in instruction
    assert "state the exact relationship in words" in instruction
    assert "overlap with players of" in instruction
    for hedge in ("estimated", "reportedly", "more than", "over"):
        assert hedge in instruction


# --- extraction runs per source -----------------------------------------------


DMZ_FEATURE_BULLETS = (
    "DMZ is an open, squad-based multiplayer mode in Call of Duty: Modern Warfare 4.\n"
    "- Base stations: secure and hold territories to capture control of a sector.\n"
    "- Three deployment options: on foot, by tactical vehicle, or from the air.\n"
    "- DMZ Cash: earn it in matches to buy deployables and squad upgrades.\n"
    "- Progression track: climb 70 levels to unlock exclusive blueprints and tags.\n"
    "- Free to play for everyone with a copy of the game, no extra purchase.\n"
)


def per_source_generate(payloads):
    calls = []

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        calls.append(role)
        payload = payloads[len(calls) - 1]
        if run_state is not None:
            run_state.record(Generation(role=role, provider="mock", model="m", family="f", value=payload))
        return Generation(role=role, provider="mock", model="m", family="f", value=payload, fallbacks=[])

    return generate, calls


def test_facts_extracts_once_per_source(tmp_path):
    payloads = [
        {"claims": [claim(claim="First claim", value="one", origin="sony", source_url="https://a.example/1")]},
        {"claims": [claim(claim="Second claim", value="two", origin="microsoft", source_url="https://b.example/2")]},
    ]
    generate, calls = per_source_generate(payloads)
    data = gathered(
        sources=[
            source(link="https://a.example/1", text="Sony announced the first fact."),
            source(link="https://b.example/2", name="Beta", owner="co-b", text="Microsoft announced the second fact."),
        ],
    )
    result = facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert calls == ["fast", "fast"]
    assert len(result["claims"]) == 2
    assert result["confirmation"]["origins"] == ["microsoft", "sony"]
    assert facts.MAX_CLAIMS_PER_SOURCE == 15


def test_facts_extracts_claims_from_a_feature_bullet_source(tmp_path):
    src = source(
        link="https://blog.playstation.com/dmz",
        name="PlayStation Blog",
        tier=1,
        owner="sony",
        text=DMZ_FEATURE_BULLETS,
    )
    payload = {
        "claims": [
            claim(claim="DMZ has base stations that capture territory", value="base stations", source_url="https://blog.playstation.com/dmz"),
            claim(claim="DMZ offers three deployment options", value="on foot, by vehicle, or from the air", source_url="https://blog.playstation.com/dmz"),
            claim(claim="DMZ Cash buys deployables", value="in-game DMZ Cash", source_url="https://blog.playstation.com/dmz"),
        ]
    }
    result = facts.facts(gathered("dmz-1", [src]), output_dir=tmp_path, now=NOW, generate=make_generate(payload))
    assert [c["claim"] for c in result["claims"]] == [
        "DMZ has base stations that capture territory",
        "DMZ offers three deployment options",
        "DMZ Cash buys deployables",
    ]


def test_facts_caps_claims_per_source(tmp_path):
    many = [claim(claim=f"Feature {i}", value=str(i), source_url="https://a.example/1") for i in range(5)]
    result = facts.facts(
        gathered(sources=[source(tier=1)]),
        output_dir=tmp_path,
        now=NOW,
        max_claims_per_source=2,
        generate=make_generate({"claims": many}),
    )
    assert [c["claim"] for c in result["claims"]] == ["Feature 0", "Feature 1"]


def test_facts_logs_per_source_count_and_warns_on_low_yield(caplog, tmp_path):
    caplog.set_level(logging.INFO)
    landlord = source(
        link="https://a.example/1",
        tier=1,
        text="paragraph. " * 200,
    )
    assert len(landlord["text"]) > facts.WARN_LOW_SOURCE_CHARS
    generate, _calls = per_source_generate([{"claims": [claim()]}])
    facts.facts(gathered(sources=[landlord]), output_dir=tmp_path, now=NOW, generate=generate)
    assert any("extracted 1 claim(s)" in record.message and "https://a.example/1" in record.message for record in caplog.records)
    assert any("yielded only 1 claim" in record.message for record in caplog.records)


def test_facts_drops_story_when_no_source_has_text(tmp_path):
    generate, calls = per_source_generate([])
    with pytest.raises(facts.FactsError):
        facts.facts(
            gathered(sources=[source(text=""), source(link="https://b", name="Beta", owner="co-b", text="   ")]),
            output_dir=tmp_path,
            now=NOW,
            generate=generate,
        )
    assert calls == []
    assert not (tmp_path / "story-1.json").exists()


# --- coherence: claims must come from sources about the cluster's entity -------


def test_primary_entities_uses_the_most_supported_headline_entity():
    primary = facts.primary_entities(
        title="Gears of War: E-Day Sold 168,000 Copies on Steam",
        source_titles=[
            "Gears of War: E-Day sales figures are in",
            "Gears of War: E-Day breaks records on Steam",
            "Ace Combat 8 has sold one million copies",
        ],
    )
    assert "gears of war e-day" in primary
    assert "ace combat 8" not in primary


def test_coherence_drops_claims_from_off_topic_sources(tmp_path):
    gathered_sources = [
        source(
            link="https://a.example/gears",
            title="Gears of War: E-Day Sold 168,000 Copies on Steam",
            owner="co-a",
            tier=1,
        ),
        source(
            link="https://b.example/ace",
            title="Ace Combat 8 Has Sold One Million Copies",
            owner="co-b",
            tier=1,
        ),
    ]
    data = gathered("gears-1", gathered_sources)
    on_topic = claim(
        claim="E-Day sold 168,000 copies on Steam",
        value="168,000 copies",
        source_url="https://a.example/gears",
    )
    off_topic = claim(
        claim="Ace Combat 8 sold one million copies",
        value="one million",
        source_url="https://b.example/ace",
    )
    generation = per_source_generate([{"claims": [on_topic]}, {"claims": [off_topic]}])[0]
    result = facts.facts(
        data,
        id="gears-1",
        title="Gears of War: E-Day Sold 168,000 Copies on Steam but Xbox Game Pass Players Generated 130% More Revenue",
        output_dir=tmp_path,
        now=NOW,
        generate=generation,
    )
    assert [claim["claim"] for claim in result["claims"]] == [on_topic["claim"]]
    assert result["coherence"]["checked"] is True
    dropped = result["coherence"]["dropped_claims"]
    assert len(dropped) == 1
    assert dropped[0]["source_url"] == "https://b.example/ace"


def test_coherence_logs_dropped_claims(caplog, tmp_path):
    gathered_sources = [
        source(link="https://a.example/gears", title="Gears of War: E-Day sales", owner="co-a", tier=1),
        source(link="https://b.example/ace", title="Ace Combat 8 sells a million", owner="co-b", tier=1),
    ]
    result = facts.facts(
        gathered("gear", gathered_sources),
        title="Gears of War: E-Day Sold 168,000 Copies on Steam",
        output_dir=tmp_path,
        now=NOW,
        generate=per_source_generate([
            {"claims": []},
            {"claims": [claim(source_url="https://b.example/ace", claim="Ace Combat 8 shipped a million")]},
        ])[0],
    )
    assert result["claims"] == []
    assert any("off-topic" in record.message for record in caplog.records)


def test_coherence_skips_when_no_primary_entity(tmp_path):
    data = gathered(
        sources=[
            source(title="general gadgets digest", tier=1),
            source(link="https://a.example/2", name="Alpha 2", owner="co-b", title="general gadgets digest", tier=1),
        ],
    )
    result = facts.facts(
        data,
        output_dir=tmp_path,
        now=NOW,
        generate=per_source_generate([{"claims": [claim()]}, {"claims": []}])[0],
    )
    assert result["coherence"]["checked"] is False
    assert len(result["claims"]) == 1


def test_coherence_uses_claim_text_when_source_has_no_title(tmp_path):
    data = gathered(
        sources=[
            source(title="", tier=1),
            source(link="https://a.example/2", name="Alpha 2", owner="co-b", title="", tier=1),
        ],
    )
    result = facts.facts(
        data,
        title="Nvidia confirms next graphics card launch window",
        output_dir=tmp_path,
        now=NOW,
        generate=per_source_generate([
            {"claims": [claim(claim="Nvidia confirmed the launch window for its next graphics card line")]},
            {"claims": []},
        ])[0],
    )
    assert len(result["claims"]) == 1
    assert result["coherence"]["checked"] is True