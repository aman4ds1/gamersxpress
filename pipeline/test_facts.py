import datetime as dt
import json
import logging
from pathlib import Path

import pytest

import facts
from providers import Generation, RunState

NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)


def source(link="https://a.example/1", name="Alpha", tier=2, owner="co-a", region="us", text="Some body text about the story.", title="", published=None, url_date=None):
    return {
        "link": link, "title": title, "source_name": name, "tier": tier, "owner": owner,
        "region": region, "status": "ok", "text": text, "error": None,
        "published": published, "url_date": url_date,
    }


def gathered(id="story-1", sources=None):
    return {"id": id, "gathered_at": NOW.isoformat(), "sources": sources or [source()]}


def claim(claim="Announcement happened", value="it happened", source_url="https://a.example/1", confidence=0.9, kind="confirmed", origin="test-origin", affiliation="", quote="", attribution_quote="", speaker="", speaker_type="none", quote_ids=(), attribution_ids=()):
    return {
        "claim": claim, "value": value, "source_url": source_url,
        "confidence": confidence, "origin": origin, "kind": kind,
        "affiliation": affiliation,
        "quote": quote, "attribution_quote": attribution_quote,
        "quote_ids": list(quote_ids), "attribution_ids": list(attribution_ids),
        "speaker": speaker, "speaker_type": speaker_type,
    }


def is_grouping_call(json_schema):
    """True when a generate call is the fact-grouping call, not extraction."""
    return isinstance(json_schema, dict) and "groups" in (json_schema.get("properties") or {})


def make_generate(payload):
    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        value = {"groups": []} if is_grouping_call(json_schema) else payload
        if run_state is not None:
            run_state.record(Generation(role=role, provider="mock", model="m", family="f", value=value))
        return Generation(role=role, provider="mock", model="m", family="f", value=value, fallbacks=[])

    return generate


# --- confirmation rule -------------------------------------------------------


@pytest.mark.parametrize(
    "sources, claims, allow, expected",
    [
        # one tier-1 source confirms on its own, no origin needed
        ([source(tier=1)], None, False, True),
        ([source(tier=1), source(link="https://a2", name="A2", tier=3)], None, False, True),
        # two independent attributed origins, both supporting the same claim,
        # confirm
        (
            [source(), source(link="https://b.example/2", name="Beta", owner="co-b")],
            [
                claim(claim="A", source_url="https://a.example/1", origin="sony"),
                claim(claim="A", source_url="https://b.example/2", origin="microsoft"),
            ],
            False,
            True,
        ),
        # outlets repeating one origin count as one: not confirmed
        (
            [source(), source(link="https://b.example/2", name="Beta", owner="co-b")],
            [
                claim(claim="A", source_url="https://a.example/1", origin="sony"),
                claim(claim="A", source_url="https://b.example/2", origin="sony"),
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
                claim(claim="A", source_url="https://b.example/2", origin="microsoft"),
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
            claim(claim="A", source_url="https://b.example/2", origin="microsoft"),
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
        claim(claim="A", origin="Sensor Tower", kind="estimate", source_url="https://o2.example/1"),
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


# --- first-party announcements -----------------------------------------------


def _first_party_sources():
    return [
        source(
            link="https://a.example/1", name="Alpha", owner="co-a",
            text="Epic Games announced in a blog post that the game is coming to Linux.",
        ),
        source(
            link="https://b.example/2", name="Beta", owner="co-b",
            text="Epic Games confirmed in a blog post that the game is coming to Linux.",
        ),
    ]


def test_first_party_announcement_confirms_with_two_independent_sources():
    sources = _first_party_sources()
    claims = [
        claim(claim="The game is coming to Linux", origin="Epic Games", source_url="https://a.example/1"),
        claim(claim="The game is coming to Linux", origin="Epic Games", source_url="https://b.example/2"),
    ]
    resolved = facts.resolve_claim_origins(claims, sources, entities=["Epic"])
    result = facts.confirmation_from_sources(sources, claims=resolved, entities=["Epic"])
    assert result["confirmed"] is True
    assert result["rule"] == "first-party"
    assert result["first_party_organizations"] == ["epic games"]
    assert result["first_party_sources"] == 2


def test_first_party_announcement_needs_two_independent_sources():
    sources = [
        source(
            link="https://a.example/1", name="Alpha", owner="co-a",
            text="Epic Games announced in a blog post that the game is coming to Linux.",
        )
    ]
    claims = [
        claim(claim="The game is coming to Linux", origin="Epic Games", source_url="https://a.example/1")
    ]
    resolved = facts.resolve_claim_origins(claims, sources, entities=["Epic"])
    result = facts.confirmation_from_sources(sources, claims=resolved, entities=["Epic"])
    assert result["confirmed"] is False
    assert result["rule"] == "unconfirmed"
    assert result["first_party_sources"] == 1


def test_outlet_reporting_about_a_company_is_not_first_party():
    sources = [
        source(
            link="https://a.example/1", name="GamesBeat", owner="co-a",
            text="Epic Games announced in a blog post that the game is coming to Linux.",
        ),
        source(
            link="https://b.example/2", name="GamesBeat", owner="co-b",
            text="Epic Games announced in a blog post that the game is coming to Linux.",
        ),
    ]
    claims = [
        claim(claim="The game is coming to Linux", origin="GamesBeat", source_url="https://a.example/1"),
        claim(claim="The game is coming to Linux", origin="GamesBeat", source_url="https://b.example/2"),
    ]
    result = facts.confirmation_from_sources(sources, claims=claims, entities=["Epic"])
    assert result["rule"] != "first-party"
    assert result["confirmed"] is False


def test_inverted_headline_attributes_an_organization():
    text = "Fortnite is coming to Linux, confirms Epic Games."
    src = source(link="https://a.example/1", name="Alpha", text=text)
    claims = [
        claim(claim="Fortnite is coming to Linux", origin="Epic Games", source_url="https://a.example/1")
    ]
    resolved = facts.resolve_claim_origins(claims, [src], entities=["Fortnite"])
    assert resolved[0]["origin"] == "epic games"


def test_organizations_are_not_rejected_as_products():
    text = "Epic Games announced that Fortnite is coming to Linux."
    src = source(link="https://a.example/1", name="Alpha", text=text)
    entities = ["Epic", "Fortnite", "Easy Anti-Cheat"]
    claims = [
        claim(claim="A", origin="Epic Games", source_url="https://a.example/1"),
        claim(claim="B", origin="Fortnite", source_url="https://a.example/1"),
        claim(claim="C", origin="Easy Anti-Cheat", source_url="https://a.example/1"),
    ]
    resolved = facts.resolve_claim_origins(claims, [src], entities=entities)
    assert resolved[0]["origin"] == "epic games", "an organization is never a product"
    assert resolved[1]["origin"] == facts.UNSTATED_ORIGIN, "a game title is still not an origin"
    assert resolved[2]["origin"] == facts.UNSTATED_ORIGIN


def test_organization_recognition_rules():
    assert facts._is_organization("Epic Games") is True
    assert facts._is_organization("Some New Studio") is True
    assert facts._is_organization("sources") is False
    text = "Dan Houser says the game is coming."
    assert facts._is_organization("Dan Houser", text, {"claim": "The game is coming", "value": "coming"}) is False


# --- source cap and model-call budget ----------------------------------------


def test_select_sources_prefers_tier_then_distinct_owners():
    sources = [
        source(link="https://a/1", name="A", tier=2, owner="co-a", text="aaaa"),
        source(link="https://b/2", name="B", tier=2, owner="co-b", text="bbbbbb"),
        source(link="https://c/3", name="C", tier=1, owner="co-c", text="cc"),
        source(link="https://d/4", name="D", tier=2, owner="co-a", text="dddddddd"),
    ]
    chosen = facts._select_sources(sources, 3)
    assert [s["link"] for s in chosen] == ["https://b/2", "https://c/3", "https://d/4"]
    assert [s["link"] for s in facts._select_sources(sources, 4)] == [s["link"] for s in sources]


def test_facts_stops_at_model_call_budget(tmp_path):
    sources = [
        source(link="https://a/1", name="A", tier=1, owner="co-a", text="Sony said the first fact."),
        source(link="https://b/2", name="B", tier=1, owner="co-b", text="Microsoft said the second fact."),
    ]
    generate, calls = per_source_generate(
        [{"claims": [claim(claim="First", origin="sony", source_url="https://a/1")]}]
    )
    run_state = RunState()
    result = facts.facts(
        gathered(sources=sources),
        output_dir=tmp_path,
        now=NOW,
        generate=generate,
        run_state=run_state,
        model_call_budget=1,
    )
    assert calls == ["fast"], "the budget is checked before each source, so the second is skipped"
    assert result["id"] == "story-1"


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


def test_product_name_is_not_an_origin(caplog):
    caplog.set_level(logging.WARNING)
    text = "Marvel Tōkon: Fighting Souls reveals its Year 1 DLC roster."
    src = source(link="https://e.example/tokon", name="Eurogamer", text=text)
    entities = ["Marvel Tōkon: Fighting Souls", "Phoenix Cyclops"]
    claims = [
        claim(
            claim="Roster revealed",
            value="Year 1 DLC roster",
            origin="Marvel Tōkon: Fighting Souls",
            source_url="https://e.example/tokon",
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src], entities=entities)
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN
    assert any("product or title" in record.message for record in caplog.records)


def test_origin_containing_a_product_title_is_downgraded():
    text = "The Marvel Tōkon: Fighting Souls game reveals its Year 1 DLC roster."
    src = source(link="https://e.example/tokon", name="Eurogamer", text=text)
    entities = ["Marvel Tōkon: Fighting Souls"]
    claims = [
        claim(
            claim="Roster revealed",
            value="Year 1 DLC roster",
            origin="the Marvel Tōkon: Fighting Souls game",
            source_url="https://e.example/tokon",
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src], entities=entities)
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN


def test_company_origin_is_kept_even_when_a_product_exists():
    text = "Arc System Works reveals the Marvel Tōkon: Fighting Souls Year 1 DLC."
    src = source(link="https://e.example/tokon", name="Eurogamer", text=text)
    entities = ["Marvel Tōkon: Fighting Souls"]
    claims = [
        claim(
            claim="Studio speaks",
            value="Year 1 DLC",
            origin="Arc System Works",
            source_url="https://e.example/tokon",
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src], entities=entities)
    assert resolved[0]["origin"] == "arc system works"


def test_a_company_and_its_own_game_are_not_two_independent_origins():
    sources = [
        source(
            link="https://a.example/1",
            name="Alpha",
            text="Arc System Works reveals the Year 1 DLC roster.",
        ),
        source(
            link="https://b.example/2",
            name="Beta",
            owner="co-b",
            text="Marvel Tōkon: Fighting Souls reveals its Year 1 DLC roster.",
        ),
    ]
    entities = ["Marvel Tōkon: Fighting Souls"]
    claims = [
        claim(
            claim="Year 1 DLC revealed",
            value="Year 1 DLC",
            origin="Arc System Works",
            source_url="https://a.example/1",
        ),
        claim(
            claim="Year 1 DLC revealed",
            value="Year 1 DLC",
            origin="Marvel Tōkon: Fighting Souls",
            source_url="https://b.example/2",
        ),
    ]
    resolved = facts.resolve_claim_origins(claims, sources, entities=entities)
    result = facts.confirmation_from_sources(sources, claims=resolved)
    assert result["origins"] == ["arc system works"]
    assert result["confirmed"] is False



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


def test_merge_claims_never_merges_two_claims_from_the_same_source_with_different_dates():
    # One article is never two versions of its own fact: even an identical label
    # whose value states a different date stays its own claim, so a single source
    # cannot fold a range of dates into one fact.
    merged = facts.merge_claims(
        [
            claim(claim="Modern Warfare 4 full release date", value="October 23, 2026", source_url="https://a.example/1"),
            claim(claim="Modern Warfare 4 full release date", value="October 20, 2026", source_url="https://a.example/1"),
        ]
    )
    assert len(merged) == 2, "claims from one source never merge with each other"


def test_merge_claims_two_sources_same_fact_same_date_merge_into_one_with_two_versions():
    merged = facts.merge_claims(
        [
            claim(claim="Modern Warfare 4 campaign early access begins", value="October 16, 2026", source_url="https://news.xbox.com/mw4"),
            claim(claim="Modern Warfare 4 campaign early access begins", value="October 16, 2026", source_url="https://pcgamer.com/mw4"),
        ],
        entities=["Modern Warfare 4"],
    )
    assert len(merged) == 1
    assert merged[0]["source_urls"] == ["https://news.xbox.com/mw4", "https://pcgamer.com/mw4"]
    assert len(merged[0]["versions"]) == 2, "one version per source, the same fact's value"


def test_merge_claims_keeps_control_resonant_and_alan_wake_2_sales_separate():
    # Two different titles' sales milestones share the same figure; they must not
    # merge onto one claim just because the wording and the number overlap.
    merged = facts.merge_claims(
        [
            claim(
                claim="Control Resonant sold one million copies in three weeks",
                value="1,000,000 copies in three weeks",
                source_url="https://a.example/control-resonant",
            ),
            claim(
                claim="Alan Wake 2 took two months to sell one million copies",
                value="1,000,000 copies in two months",
                source_url="https://b.example/alan-wake-2",
            ),
        ],
        entities=["Control Resonant", "Alan Wake 2"],
    )
    assert len(merged) == 2, "different titles never merge into one fact"


def test_merge_claims_mw4_release_sheet_keeps_at_least_eight_claims():
    # The real run's 12 raw claims (Xbox Wire 4, PC Gamer 8) about one game's
    # release schedule collapsed to 4, fusing campaign early access, DMZ early
    # access and the full release -- three different dates -- into one fact.
    xbox = "https://news.xbox.com/en-us/2026/10/16/modern-warfare-4-campaign-early-access-now-live/"
    pcgamer = "https://pcgamer.com/games/call-of-duty/modern-warfare-4-release-date-launch-times-early-access/"
    merged = facts.merge_claims(
        [
            claim(claim="Modern Warfare 4 campaign early access availability", value="Modern Warfare 4: Campaign Early Access is live as of October 16, 2026", source_url=xbox),
            claim(claim="Modern Warfare 4 campaign early access launch date", value="October 16, 2026", source_url=xbox),
            claim(claim="DMZ early access start date", value="October 20, 2026", source_url=xbox),
            claim(claim="Modern Warfare 4 standalone release date", value="October 23, 2026", source_url=xbox),
            claim(claim="Modern Warfare 4 release times and early access explained", value="Modern Warfare 4 release times and early access explained", source_url=pcgamer),
            claim(claim="Early access to the campaign starts October 16", value="October 16, 2026", source_url=pcgamer),
            claim(claim="Early access to DMZ starts October 20", value="October 20, 2026", source_url=pcgamer),
            claim(claim="The full release is on October 23 at midnight local time", value="October 23, 2026 at midnight local time", source_url=pcgamer),
            claim(claim="PC preload is already live", value="already live", source_url=pcgamer),
            claim(claim="Activision has not revealed an exact time for the campaign early access unlock on the 16th", value="exact time not revealed for October 16, 2026", source_url=pcgamer),
            claim(claim="Some sources predict DMZ early access release time will be 10 am PT", value="10 am PT (1 pm ET/6 pm BST)", source_url=pcgamer),
            claim(claim="You will need 115GB for the game's storage", value="115GB", source_url=pcgamer),
        ],
        entities=["Modern Warfare 4", "DMZ"],
    )
    assert len(merged) >= 8, "distinct release dates must not collapse into one claim"
    for entry in merged:
        values = {version["value"] for version in entry["versions"]}
        assert not ({"October 20, 2026", "October 23, 2026"} <= values), "different dates never share a claim"


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


def test_facts_allows_single_attributed_origin_when_enabled(monkeypatch, tmp_path):
    use_mode(monkeypatch, "legacy")
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


def test_facts_single_attributed_origin_does_not_confirm_under_evidence(monkeypatch, tmp_path):
    # Evidence-mode twin: the same story, but the evidence rule refuses a lone
    # attributed origin even with allow_single_origin_attributed enabled. The
    # legacy reason is "a single attributed origin supports the headline claim
    # and single-origin confirmation is enabled"; the evidence reason is
    # "the central fact is not backed by enough verified cross-source evidence".
    use_mode(monkeypatch, "evidence")
    data = gathered(sources=[source(text="Sony said the card is coming.")])
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            data,
            output_dir=tmp_path,
            now=NOW,
            allow_single_origin_attributed=True,
            generate=make_generate({"claims": [claim(origin="sony")]}),
        )
    sheet = json.loads((tmp_path / "unconfirmed" / "story-1.json").read_text(encoding="utf-8"))
    assert sheet["confirmation"]["confirmed"] is False
    assert sheet["confirmation"]["rule"] == "unconfirmed"
    assert sheet["confirmation"]["rule_source"] == "evidence"
    assert sheet["confirmation"]["reason"] == (
        "the central fact is not backed by enough verified cross-source evidence"
    )
    assert not (tmp_path / "story-1.json").exists()


def test_evidence_mode_single_verified_origin_does_not_confirm(monkeypatch, tmp_path):
    # A single claim that passes verify_evidence from one owner, with the headline
    # title so it is the central fact, still does not confirm under the evidence
    # rule even with allow_single_origin_attributed enabled: the rule that
    # produces the outcome is "unconfirmed" (rule_source "evidence",
    # verified_claims 1, single_origin_attributed True).
    use_mode(monkeypatch, "evidence")
    src = source(link="https://a.example/1", name="Alpha", owner="co-a", tier=2,
                 title=ACME_TITLE, text=ACME_TEXT)
    generate = make_generate({"claims": [acme_claim("https://a.example/1")]})
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-one", [src]), id="acme-one", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW,
            allow_single_origin_attributed=True, generate=generate,
        )
    sheet = json.loads((tmp_path / "unconfirmed" / "acme-one.json").read_text(encoding="utf-8"))
    assert sheet["confirmation"]["confirmed"] is False
    assert sheet["confirmation"]["rule"] == "unconfirmed"
    assert sheet["confirmation"]["rule_source"] == "evidence"
    assert sheet["confirmation"]["verified_claims"] == 1
    assert sheet["confirmation"]["single_origin_attributed"] is True
    assert sheet["confirmation"]["reason"] == (
        "the central fact is not backed by enough verified cross-source evidence"
    )
    assert not (tmp_path / "acme-one.json").exists()


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
    assert len(prompt) < len(long_text)
    # The text is cut to its character cap before it is numbered, so no
    # "[truncated]" marker appears inside the numbered block a quote is cited
    # from.
    assert "[truncated]" not in prompt


def test_build_prompt_numbers_the_sentences_it_offers_for_citation():
    text = " ".join(f"Fact {index}." for index in range(1, 121))
    prompt = facts.build_prompt("s", source(text=text))
    assert "S1: Fact 1." in prompt
    assert "S80:" in prompt
    assert "S81:" not in prompt


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
        if is_grouping_call(json_schema):
            value = {"groups": []}
            if run_state is not None:
                run_state.record(Generation(role=role, provider="mock", model="m", family="f", value=value))
            return Generation(role=role, provider="mock", model="m", family="f", value=value, fallbacks=[])
        calls.append(role)
        payload = payloads[len(calls) - 1]
        if run_state is not None:
            run_state.record(Generation(role=role, provider="mock", model="m", family="f", value=payload))
        return Generation(role=role, provider="mock", model="m", family="f", value=payload, fallbacks=[])

    return generate, calls


def test_facts_extracts_once_per_source(monkeypatch, tmp_path):
    use_mode(monkeypatch, "legacy")
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


def test_facts_extracts_once_per_source_under_evidence(monkeypatch, tmp_path):
    # Evidence-mode twin: the same story and the same one-extraction-per-source
    # billing. The legacy rule reached "two-independent-origins" from the claims'
    # ``origin`` fields; the evidence rule judges only verified evidence, and
    # these synthetic claims carry no verbatim quote or named speaker, so the
    # confirmation it gives for this story is "unconfirmed" (verified_claims 0).
    use_mode(monkeypatch, "evidence")
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
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert calls == ["fast", "fast"], "one extraction call per source"
    sheet = json.loads((tmp_path / "unconfirmed" / "story-1.json").read_text(encoding="utf-8"))
    assert sheet["confirmation"]["confirmed"] is False
    assert sheet["confirmation"]["rule"] == "unconfirmed"
    assert sheet["confirmation"]["rule_source"] == "evidence"
    assert sheet["confirmation"]["verified_claims"] == 0


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
        gathered(sources=[source(tier=1, text="Feature figures: 0 1 2 3 4.")]),
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
    assert "gears of war: e-day" in primary
    assert "gears of war" in primary
    assert "e day" in primary
    assert "ace combat 8" not in primary


def test_coherence_drops_claims_from_off_topic_sources(tmp_path):
    gathered_sources = [
        source(
            link="https://a.example/gears",
            title="Gears of War: E-Day Sold 168,000 Copies on Steam",
            owner="co-a",
            tier=1,
            text="Gears of War: E-Day sold 168,000 copies on Steam.",
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
        source(link="https://a.example/gears", title="Gears of War: E-Day sales", owner="co-a", tier=1, text="Embark said Gears of War: E-Day Sold 168,000 Copies on Steam."),
        source(link="https://b.example/ace", title="Ace Combat 8 sells a million", owner="co-b", tier=1),
    ]
    result = facts.facts(
        gathered("gear", gathered_sources),
        title="Gears of War: E-Day Sold 168,000 Copies on Steam",
        output_dir=tmp_path,
        now=NOW,
        generate=per_source_generate([
            {"claims": [claim(claim="Gears of War: E-Day Sold 168,000 Copies on Steam", value="168,000 copies", source_url="https://a.example/gears", origin="embark", kind="reported")]},
            {"claims": [claim(source_url="https://b.example/ace", claim="Ace Combat 8 shipped a million")]},
        ])[0],
    )
    assert len(result["claims"]) == 1
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


COD_DMZ_SPLIT_TITLE = (
    "DMZ for Call of Duty: Modern Warfare 4 being released early, but "
    "Infinity Ward shoots down PvE hopes"
)
COD_DMZ_BLOG_TITLE = (
    "Everything you need to know about DMZ mode in Call of Duty: Modern Warfare 4"
)


def test_coherence_keeps_claims_that_name_a_sub_entity():
    # The cluster title binds DMZ and the game into one long phrase; a claim that
    # names only "Call of Duty: Modern Warfare 4", or only "DMZ", is still about
    # the story and must be kept. A claim from an unrelated game's source, whose
    # headline is not about the cluster either, is still dropped.
    sources = [
        source(link="https://eurogamer.net/dmz", name="Eurogamer", owner="ign", tier=2, title=COD_DMZ_SPLIT_TITLE),
        source(link="https://blog.playstation.com/dmz", name="PlayStation Blog", owner="sony", tier=1, title=COD_DMZ_BLOG_TITLE),
        source(link="https://b.example/ace", name="Beta", owner="co-b", tier=1, title="Ace Combat 8 Has Sold One Million Copies"),
    ]
    claims = [
        claim(claim="Release date of Call of Duty: Modern Warfare 4", value="October 23", source_url="https://blog.playstation.com/dmz"),
        claim(claim="Forward Operating Base station count", value="over a dozen stations", source_url="https://blog.playstation.com/dmz"),
        claim(claim="DMZ Progression level range", value="70 levels", source_url="https://blog.playstation.com/dmz"),
        claim(claim="Ace Combat 8 sold one million copies", value="one million", source_url="https://b.example/ace"),
    ]
    kept, dropped, primary = facts.coherence_check(
        claims, sources, title=COD_DMZ_SPLIT_TITLE, story_id="dmz"
    )
    assert [entry["claim"] for entry in kept] == [
        "Release date of Call of Duty: Modern Warfare 4",
        "Forward Operating Base station count",
        "DMZ Progression level range",
    ]
    assert [entry["claim"] for entry in dropped] == ["Ace Combat 8 sold one million copies"]
    assert "call of duty: modern warfare 4" in primary
    assert "call of duty" in primary
    assert "dmz" in primary


def test_coherence_warns_when_most_of_one_source_is_dropped(caplog):
    caplog.set_level(logging.WARNING)
    sources = [
        source(link="https://blog.playstation.com/dmz", name="PlayStation Blog", owner="sony", tier=1, title=COD_DMZ_BLOG_TITLE),
        source(link="https://b.example/ace", name="Beta", owner="co-b", tier=1, title="Ace Combat 8 Has Sold One Million Copies"),
    ]
    claims = [
        claim(claim="Ace Combat 8 sold one million copies", value="one million", source_url="https://b.example/ace"),
        claim(claim="Ace Combat 8 review score", value="9/10", source_url="https://b.example/ace"),
    ]
    kept, dropped, _ = facts.coherence_check(
        claims, sources, title=COD_DMZ_SPLIT_TITLE, story_id="dmz"
    )
    assert kept == []
    assert len(dropped) == 2
    assert any(
        "dropped 2 of 2 claims from Beta" in record.message for record in caplog.records
    )


# --- old-format fixture: only the central claim's origins may confirm ---------


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
ARC_FIXTURE_PATH = FIXTURE_DIR / "arc-raiders-7b1884edc474.json"
ARC_TITLE = "Arc Raiders could eventually be split into PvE and PvP, says executive producer"
ARC_CENTRAL_CLAIM = "Arc Raiders could eventually be split into PvE and PvP modes"
ARC_EUROGAMER = "https://eurogamer.net/arc-raiders-pve-pvp-split-possible-executive-producer"
ARC_TECHPOWERUP = (
    "https://techpowerup.com/353541/arc-raiders-jumps-to-six-month-high-player-count-"
    "with-2-0-update-and-free-weekend"
)

# Minimal source text so each fixture origin is named in its own source (the
# current code verifies an origin against the text of the claim's own source).
ARC_SOURCE_TEXT = {
    ARC_EUROGAMER: (
        "Embark executive producer Aleksander Grøndal said Arc Raiders could "
        "eventually be split into PvE and PvP modes. Embark said the limited PvE "
        "test runs from 13th to 20th October."
    ),
    "https://tomshardware.com/video-games/pc-gaming/arc-raiders-developer-uses-ai-to-detect-cheating-embark-cracks-down-on-cheaters-deploys-harsh-penalties-including-permaban-on-first-time-offenders": (
        "Embark Studios said it is developing in-house AI models to detect "
        "cheating in Arc Raiders."
    ),
    ARC_TECHPOWERUP: (
        "Arc Raiders hit a six-month high player count. SteamDB tracked 131,388 "
        "peak concurrent players. Embark Studios said the co-op test starts October 13."
    ),
}


def arc_fixture_sources_claims(fixture):
    """The fixture's sources and claims adapted to the current input shape.

    The fixture is an old facts sheet: one origin string per claim, no central or
    affiliation fields, and each claim lists its source under ``source_urls``.
    Current functions read a claim's ``source_url`` (singular) and confirmation
    runs over the pre-merge central group, so the helper rewrites every claim to
    that shape (dropping the list) and returns ``(sources, claims)``.
    """
    claims = [
        {
            "claim": raw["claim"],
            "value": raw["value"],
            "source_url": raw["source_urls"][0],
            "confidence": raw["confidence"],
            "origin": raw["origin"],
            "kind": raw["kind"],
        }
        for raw in fixture["claims"]
    ]
    return fixture["sources"], claims


def load_arc_fixture():
    """Load the old-format fixture and replay it through the current code.

    Returns ``(fixture, gathered, generate)``. ``gathered`` gives each source the
    text the current pipeline needs, and ``generate`` replays that source's own
    fixture claims (one model call per source, as extraction does).
    """
    fixture = json.loads(ARC_FIXTURE_PATH.read_text(encoding="utf-8"))
    by_url: dict[str, list[dict]] = {}
    for raw in fixture["claims"]:
        url = raw["source_urls"][0]
        by_url.setdefault(url, []).append(
            {
                "claim": raw["claim"],
                "value": raw["value"],
                "source_url": url,
                "confidence": raw["confidence"],
                "origin": raw["origin"],
                "kind": raw["kind"],
            }
        )
    sources = []
    payloads = []
    for src in fixture["sources"]:
        sources.append({**src, "status": "ok", "error": None, "text": ARC_SOURCE_TEXT[src["link"]]})
        payloads.append({"claims": by_url.get(src["link"], [])})
    gathered = {"id": fixture["id"], "gathered_at": NOW.isoformat(), "sources": sources}
    generate = per_source_generate(payloads)[0]
    return fixture, gathered, generate


def test_arc_raiders_fixture_is_not_confirmed(tmp_path):
    fixture, gathered, generate = load_arc_fixture()

    # End to end the current logic rejects the story: only the central claim's
    # single origin (the executive producer) counts toward confirmation, so the
    # unrelated anti-cheat/player-count origins never confirm it.
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered,
            id=fixture["id"],
            title=ARC_TITLE,
            output_dir=tmp_path,
            now=NOW,
            generate=generate,
        )

    sources, claims = arc_fixture_sources_claims(fixture)
    central = facts.central_claim(claims, title=ARC_TITLE)
    assert central["claim"] == ARC_CENTRAL_CLAIM

    groups = facts._claim_groups(claims)
    central_group = groups[claims.index(central)]
    confirmation = facts.confirmation_from_sources(sources, claims=central_group)
    assert confirmation["origins"] == ["aleksander grøndal"]
    assert confirmation["confirmed"] is False


def test_two_outlets_quoting_one_executive_count_as_one_origin():
    sources = [
        source(
            link="https://a.example/pve",
            name="Alpha",
            text="Embark executive producer Aleksander Grøndal said the modes could split.",
        ),
        source(
            link="https://b.example/pve",
            name="Beta",
            owner="co-b",
            text="Arc Raiders executive producer Aleksander Grøndal works at Embark.",
        ),
    ]
    claims = [
        claim(
            claim="Arc Raiders could be split into PvE and PvP",
            value="split possible",
            kind="confirmed",
            origin="Aleksander Grøndal",
            affiliation="Embark",
            source_url="https://a.example/pve",
        ),
        claim(
            claim="Arc Raiders could be split into PvE and PvP",
            value="split possible",
            kind="confirmed",
            origin="Embark executive producer Aleksander Grøndal",
            affiliation="Embark",
            source_url="https://b.example/pve",
        ),
    ]
    resolved = facts.resolve_claim_origins(claims, sources)
    result = facts.confirmation_from_sources(sources, claims=resolved)
    assert result["origins"] == ["embark"], "the person and their studio are one origin"
    assert result["confirmed"] is False


def test_statement_kind_never_confirmed_even_after_merging_with_confirmed():
    merged = facts.merge_claims(
        [
            claim(
                claim="Arc Raiders could be split into PvE and PvP",
                value="could split",
                kind="confirmed",
                origin="embark",
                source_url="https://a.example/1",
            ),
            claim(
                claim="Arc Raiders could be split into PvE and PvP",
                value="could split",
                kind="statement",
                origin="embark",
                source_url="https://b.example/2",
            ),
        ]
    )
    assert len(merged) == 1
    assert merged[0]["kind"] == "statement"
    assert merged[0]["kind"] != "confirmed"

    sources = [
        source(link="https://a.example/1"),
        source(link="https://b.example/2", name="Beta", owner="co-b"),
    ]
    result = facts.confirmation_from_sources(
        sources,
        claims=[
            claim(claim="Modes could split", value="could", kind="statement", origin="embark", source_url="https://a.example/1"),
            claim(claim="Modes could split", value="could", kind="statement", origin="square", source_url="https://b.example/2"),
        ],
    )
    assert result["confirmed"] is False


def test_fixture_test_date_claims_merge_into_one_claim_with_both_source_urls():
    fixture, _gathered, _generate = load_arc_fixture()
    _sources, claims = arc_fixture_sources_claims(fixture)
    merged = facts.merge_claims(claims)
    combined = [
        entry
        for entry in merged
        if ARC_EUROGAMER in entry["source_urls"] and ARC_TECHPOWERUP in entry["source_urls"]
    ]
    assert len(combined) == 1, "the same test written two ways must merge into one claim"
    assert combined[0]["source_urls"] == [ARC_EUROGAMER, ARC_TECHPOWERUP]
    assert "13" in combined[0]["value"] and "october" in combined[0]["value"].lower()


# --- relative time, grounding, bylines and merged confirmation (Tōkon fixes) ---


def test_system_instruction_states_relative_time_grounding_and_byline_rules():
    instruction = facts.SYSTEM_INSTRUCTION
    assert "absolute calendar date" in instruction
    assert "never add words of your own" in instruction
    assert "and not" in instruction
    assert "affiliation to the publishing outlet" in instruction
    assert "design decision" in instruction


def test_absolutize_relative_time_uses_the_source_date():
    assert facts.absolutize_relative_time("begins today", dt.date(2026, 10, 7)) == "begins on October 7, 2026"
    assert facts.absolutize_relative_time("out now", "2026-10-07") == "available as of October 7, 2026"
    assert facts.absolutize_relative_time("ended yesterday", dt.date(2026, 10, 7)) == "ended on October 6, 2026"
    assert facts.absolutize_relative_time("arrives tomorrow", dt.date(2026, 10, 7)) == "arrives on October 8, 2026"
    assert facts.absolutize_relative_time("no dates here", None) == "no dates here"
    assert facts.absolutize_relative_time("begins today", None) is None


def test_absolutize_relative_time_uses_grammatical_forms_never_a_bare_date():
    assert facts.absolutize_relative_time("begins today", dt.date(2026, 10, 7)) == "begins on October 7, 2026"
    assert (
        facts.absolutize_relative_time("is out now on PS5 and PC", dt.date(2026, 10, 7))
        == "is available on PS5 and PC as of October 7, 2026"
    )
    assert facts.absolutize_relative_time("now", dt.date(2026, 10, 7)) == "as of October 7, 2026"
    assert facts.absolutize_relative_time("currently", dt.date(2026, 10, 7)) == "as of October 7, 2026"
    assert facts.absolutize_relative_time("live now on Xbox", dt.date(2026, 10, 7)) == "live as of October 7, 2026 on Xbox"
    assert facts.absolutize_relative_time("from today", dt.date(2026, 10, 7)) == "from October 7, 2026"


def test_facts_absolutizes_relative_time_from_the_source_url_date(tmp_path):
    src = source(
        link="https://blog.example/2026/10/07/cyclops",
        name="Official Blog",
        tier=1,
        owner="co-a",
        text="Phoenix Cyclops arrives today.",
    )
    payload = {
        "claims": [
            claim(claim="Phoenix Cyclops release", value="arrives today", source_url=src["link"])
        ]
    }
    result = facts.facts(
        gathered(sources=[src]), output_dir=tmp_path, now=NOW, generate=make_generate(payload)
    )
    assert result["claims"][0]["value"] == "arrives on October 7, 2026"


def test_url_date_wins_over_a_feed_date_one_day_later_and_is_noted(tmp_path):
    # The URL date (2026-10-07) and the feed date (2026-10-08) differ by one day,
    # so the URL date is used and the disagreement is recorded in the sheet.
    src = source(
        link="https://blog.example/2026/10/07/cyclops",
        url_date="2026-10-07",
        published="2026-10-08T02:06:00Z",
        name="Official Blog",
        tier=1,
        owner="co-a",
        text="Phoenix Cyclops arrives today.",
    )
    payload = {
        "claims": [
            claim(claim="Phoenix Cyclops release", value="arrives today", source_url=src["link"])
        ]
    }
    result = facts.facts(
        gathered(sources=[src]), output_dir=tmp_path, now=NOW, generate=make_generate(payload)
    )
    assert result["claims"][0]["value"] == "arrives on October 7, 2026"
    notes = result["date_anchors"]
    assert len(notes) == 1
    note = notes[0]
    assert note["used"] == "url"
    assert note["differs"] is True
    assert note["gap_days"] == 1
    assert note["anchors"] == {"url": "2026-10-07", "feed": "2026-10-08T02:06:00Z"}


def test_anchors_that_agree_need_no_note(tmp_path):
    src = source(
        link="https://blog.example/2026/10/07/cyclops",
        url_date="2026-10-07",
        published="2026-10-07T17:00:00+00:00",
        name="Official Blog",
        tier=1,
        owner="co-a",
        text="Phoenix Cyclops arrives today.",
    )
    payload = {
        "claims": [
            claim(claim="Phoenix Cyclops release", value="arrives today", source_url=src["link"])
        ]
    }
    result = facts.facts(
        gathered(sources=[src]), output_dir=tmp_path, now=NOW, generate=make_generate(payload)
    )
    assert result["claims"][0]["value"] == "arrives on October 7, 2026"
    assert result["date_anchors"] == []


def test_no_url_date_falls_back_to_the_feed_date(tmp_path):
    src = source(
        link="https://blog.example/cyclops",
        published="2026-10-07T17:00:00+00:00",
        name="Official Blog",
        tier=1,
        owner="co-a",
        text="Phoenix Cyclops arrives today.",
    )
    payload = {
        "claims": [
            claim(claim="Phoenix Cyclops release", value="arrives today", source_url=src["link"])
        ]
    }
    result = facts.facts(
        gathered(sources=[src]), output_dir=tmp_path, now=NOW, generate=make_generate(payload)
    )
    assert result["claims"][0]["value"] == "arrives on October 7, 2026"
    assert result["date_anchors"][0]["used"] == "feed"
    assert result["date_anchors"][0]["anchors"]["url"] is None


def test_anchors_more_than_a_day_apart_drop_relative_claims_only(tmp_path):
    # A two-week gap between the URL date and the feed date is too ambiguous to
    # date from: the relative-time claim is dropped with a reason, while the
    # other claim is kept unchanged.
    src = source(
        link="https://blog.example/2026/09/20/cyclops",
        url_date="2026-09-20",
        published="2026-10-07T17:00:00+00:00",
        name="Official Blog",
        tier=1,
        owner="co-a",
        text="Phoenix Cyclops arrives today. It is available on PC.",
    )
    payload = {
        "claims": [
            claim(claim="Phoenix Cyclops release", value="arrives today", source_url=src["link"]),
            claim(claim="Phoenix Cyclops platforms", value="available on PC", source_url=src["link"]),
        ]
    }
    result = facts.facts(
        gathered(sources=[src]), output_dir=tmp_path, now=NOW, generate=make_generate(payload)
    )
    values = [claim_["value"] for claim_ in result["claims"]]
    assert values == ["available on PC"]
    grounding = result["grounding"]
    assert grounding and grounding[0]["reason"] == "ambiguous publication date"
    assert grounding[0]["anchors"] == {"url": "2026-09-20", "feed": "2026-10-07T17:00:00+00:00"}
    assert result["date_anchors"][0]["reason"] == "ambiguous publication date"


def test_a_date_is_grounded_by_either_anchor():
    value = claim(claim="Release", value="ships 2026-10-07")
    body = "The game ships later this year."
    assert facts.grounding_failure(
        value, body, anchors=[dt.date(2026, 10, 1), dt.date(2026, 10, 7)]
    ) is None
    # With only the other anchor the URL/feed date is not grounded.
    assert facts.grounding_failure(value, body, anchors=[dt.date(2026, 10, 1)]) is not None


def test_grounding_drops_a_value_that_adds_a_qualifier_the_source_never_states():
    source_text = "Phoenix Cyclops joins the Year 1 DLC roster on November 2."
    unsupported = claim(claim="Roster revealed", value="full Year 1 DLC roster", source_url="https://x")
    assert facts.grounding_failure(unsupported, source_text) is not None
    supported = claim(claim="Roster revealed", value="Year 1 DLC roster", source_url="https://x")
    assert facts.grounding_failure(supported, source_text) is None


def test_grounding_rejects_a_label_that_adds_an_unsupported_contrast():
    source_text = "The narrative team chose the Phoenix version for fresher visuals."
    bad = claim(
        claim="Reason why Phoenix Cyclops is DLC and not a day-one fighter",
        value="fresher visuals",
        source_url="https://x",
    )
    reason = facts.grounding_failure(bad, source_text)
    assert reason is not None and "and not" in reason

    good = claim(claim="Reason for the Phoenix version", value="fresher visuals", source_url="https://x")
    assert facts.grounding_failure(good, source_text) is None


def test_byline_org_replaces_the_publication_affiliation():
    src = source(
        link="https://blog.playstation.com/2026/10/07/cyclops",
        name="PlayStation Blog",
        owner="sony",
        tier=1,
        text=(
            "Hello everyone! I'm Takeshi Yamanaka, Producer at Arc System Works. "
            "The narrative team chose the Phoenix version for fresher visuals."
        ),
    )
    claims = [
        claim(
            claim="Reason for the Phoenix version",
            value="fresher visuals",
            origin="takeshi yamanaka",
            affiliation="playstation blog",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == "takeshi yamanaka"
    assert resolved[0]["affiliation"] == "arc system works"


def test_merged_claim_is_not_confirmed_by_a_tier1_source_missing_a_specific():
    sources = [
        source(link="https://a.example/1", name="Official", tier=1, owner="co-a"),
        source(link="https://b.example/2", name="Outlet", tier=2, owner="co-b"),
    ]
    claims = [
        claim(claim="Year 1 roster", value="Year 1 DLC roster", kind="reported", origin="sony", source_url="https://a.example/1"),
        claim(claim="Year 1 roster", value="full Year 1 DLC roster", kind="reported", origin="sony", source_url="https://b.example/2"),
    ]
    merged = facts._merge_group(claims)
    assert merged["value"] == "full Year 1 DLC roster", "the richer value merges forward"
    result = facts.confirmation_from_sources(sources, claims=claims, merged=merged)
    assert result["tier1_sources"] == 0, "the tier-1 source never wrote 'full'"
    assert result["confirmed"] is False


# --- Tōkon spellings, feed-date grounding and the tier-1 central claim --------


def test_tokon_spelling_variants_merge_into_one_canonical_entity():
    # The same game is spelled "MARVEL Tōkon" once and "Marvel Tōkon" twice; with
    # exact counting each spelling falls short of "Phoenix Cyclops" (3) and the
    # game is lost. Folding case and diacritics merges them into one surviving
    # canonical entity, and its parts stay aliases used only for matching.
    headlines = [
        "MARVEL Tōkon: Fighting Souls adds Phoenix Cyclops",
        "Marvel Tōkon: Fighting Souls adds Phoenix Cyclops",
        "Marvel Tōkon: Fighting Souls adds Phoenix Cyclops",
    ]
    canonical = facts.canonical_entities(title=headlines[0], source_titles=headlines[1:])
    assert canonical == ["Marvel Tōkon: Fighting Souls", "Phoenix Cyclops"]
    aliases = facts.entity_aliases("Marvel Tōkon: Fighting Souls")
    assert aliases == ["marvel tōkon", "fighting souls"]
    assert all(alias not in canonical for alias in aliases)

    # The aliases are still the matching surface, so a claim naming only a part
    # of the title is recognized as on-topic.
    primary = facts.primary_entities(title=headlines[0], source_titles=headlines[1:])
    assert "phoenix cyclops" in primary
    assert "marvel tōkon: fighting souls" in primary
    assert "marvel tokon" in primary
    assert "fighting souls" in primary


def test_best_display_entity_prefers_proper_casing_and_diacritics():
    variants = [
        "MARVEL Tōkon: Fighting Souls",
        "Marvel Tokon: Fighting Souls",
        "Marvel Tōkon: Fighting Souls",
    ]
    assert (
        facts._best_display_entity(variants, {"Marvel Tōkon: Fighting Souls": 2})
        == "Marvel Tōkon: Fighting Souls"
    )


def test_grounding_keeps_a_date_the_feed_publishes_but_the_body_never_states():
    body = (
        "Marvel Tōkon: Fighting Souls unveils its Year 1 DLC roster. "
        "Phoenix Cyclops arrives on November 2."
    )
    claim_ = claim(
        claim="Phoenix Cyclops joins Marvel Tōkon: Fighting Souls on November 2, 2026",
        value="Phoenix Cyclops joins Marvel Tōkon: Fighting Souls on November 2, 2026",
        source_url="https://blog.playstation.com/tokon-year-1-roster",
    )
    src = source(
        link=claim_["source_url"],
        name="PlayStation Blog",
        tier=1,
        owner="sony",
        published="2026-10-07T17:00:00+00:00",
        text=body,
    )
    kept, dropped = facts.ground_claims([claim_], [src])
    assert kept == [claim_] and dropped == []
    assert facts.grounding_failure(claim_, body, anchor=dt.date(2026, 10, 7)) is None


def test_grounding_still_drops_a_number_in_neither_body_nor_anchor():
    body = "Marvel Tōkon: Fighting Souls Year 1 DLC details arrive in November."
    claim_ = claim(
        claim="Roster demand",
        value="368,000 pre-orders recorded",
        source_url="https://blog.playstation.com/tokon-year-1-roster",
    )
    reason = facts.grounding_failure(claim_, body, anchor=dt.date(2026, 10, 7))
    assert reason is not None
    assert "368000" in reason


def test_grounding_reads_an_iso_date_as_the_same_calendar_date():
    body = "Phoenix Cyclops arrives on November 2."
    claim_ = claim(
        claim="Release date",
        value="Phoenix Cyclops arrives on 2026-11-02",
        source_url="https://a.example/1",
    )
    assert facts.grounding_failure(claim_, body) is None


def test_grounding_reads_an_ordinal_date_as_the_same_calendar_date():
    body = "Phoenix Cyclops arrives on November 2."
    claim_ = claim(
        claim="Release date",
        value="Phoenix Cyclops arrives on 2nd November",
        source_url="https://a.example/1",
    )
    assert facts.grounding_failure(claim_, body) is None


def test_grounding_drops_a_date_on_a_different_day():
    body = "Phoenix Cyclops arrives on November 2."
    claim_ = claim(
        claim="Release date",
        value="Phoenix Cyclops arrives on November 3",
        source_url="https://a.example/1",
    )
    reason = facts.grounding_failure(claim_, body)
    assert reason is not None
    assert "November 3" in reason


def test_grounding_accepts_a_date_equal_to_the_feed_anchor():
    body = "Phoenix Cyclops arrives soon."
    claim_ = claim(
        claim="Release date",
        value="Phoenix Cyclops arrives on October 11, 2026",
        source_url="https://a.example/1",
    )
    assert facts.grounding_failure(claim_, body, anchor=dt.date(2026, 10, 11)) is None


# --- possessive names ground against the source's bare spelling --------------


def test_grounding_possessive_name_is_grounded_by_a_bare_source():
    body = "A Dawnwalker sequel was announced today."
    value = claim(claim="Sequel", value="Sequel to Dawnwalker's remake announced")
    assert facts.grounding_failure(value, body) is None


def test_grounding_possessive_matches_bare_and_curly_apostrophe_spellings():
    body = "A Dawnwalker sequel was announced today."
    curly_body = "The Dawnwalker\u2019s team confirmed it."
    straight = claim(claim="Sequel", value="Sequel to Dawnwalker's remake announced")
    curly = claim(claim="Sequel", value="Sequel to Dawnwalker\u2019s remake announced")
    for value in (straight, curly):
        assert facts.grounding_failure(value, body) is None
        assert facts.grounding_failure(value, curly_body) is None


def test_grounding_ubisofts_possessive_is_grounded_by_ubisoft():
    body = "Ubisoft confirmed the port today."
    value = claim(claim="Port", value="Port of Ubisoft's remake confirmed")
    assert facts.grounding_failure(value, body) is None


def test_grounding_reds_possessive_is_grounded_by_red():
    body = "Red returns in a new saga."
    value = claim(claim="Chapter", value="Chapter of Red's saga announced")
    assert facts.grounding_failure(value, body) is None


def test_grounding_folds_non_nfkd_letters_on_both_sides():
    michal = claim(claim="Studio", value="Sequel to Michal's studio announced")
    michal_diacritic = claim(claim="Studio", value="Sequel to Micha\u0142's studio announced")
    assert facts.grounding_failure(michal, "Micha\u0142 confirmed the plan today.") is None
    assert facts.grounding_failure(michal_diacritic, "Michal confirmed the plan today.") is None
    ligature = claim(claim="Studio", value="Sequel to \u0152uvre Studio's remake announced")
    assert facts.grounding_failure(ligature, "Oeuvre Studio confirmed a sequel today.") is None
    slashed = claim(claim="Port", value="Port of Gr\u00f8ndal's studio confirmed")
    assert facts.grounding_failure(slashed, "Grondal confirmed a port today.") is None


def test_grounding_title_can_ground_a_possessive_term_the_body_never_states():
    body = "A sequel was announced today."
    value = claim(claim="Sequel", value="Sequel to Dawnwalker's remake announced")
    assert facts.grounding_failure(value, body, title="Dawnwalker hits 2027") is None


def test_grounding_still_drops_a_capitalized_term_the_source_never_mentions():
    body = "Dawnwalker announced a sequel today."
    value = claim(claim="Sequel", value="Sequel to Interplay's remake announced")
    reason = facts.grounding_failure(value, body)
    assert reason is not None
    assert "interplay" in reason


def test_grounding_dates_and_numbers_still_drop_when_unsupported():
    body = "Dawnwalker announced a sequel today."
    number_value = claim(claim="Pre-orders", value="Dawnwalker sequel reaches 368,000 pre-orders")
    number_reason = facts.grounding_failure(number_value, body)
    assert number_reason is not None and "368000" in number_reason
    date_value = claim(claim="Release date", value="Dawnwalker sequel arrives on November 3")
    date_reason = facts.grounding_failure(date_value, body)
    assert date_reason is not None and "November 3" in date_reason


TOKON_TITLE = (
    "Marvel Tōkon: Fighting Souls reveals its Year 1 DLC roster, "
    "starting with Phoenix Cyclops on 2nd November"
)
TOKON_PS_URL = "https://blog.playstation.com/2026/10/07/tokon-year-1-roster"
TOKON_EU_URL = (
    "https://eurogamer.net/marvel-tokon-fighting-souls-year-1-dlc-roster-phoenix-cyclops"
)


def test_tokon_sheet_takes_its_central_claim_from_the_tier1_source(tmp_path):
    ps_blog = source(
        link=TOKON_PS_URL,
        name="PlayStation Blog",
        tier=1,
        owner="sony",
        published="2026-10-07T17:00:00+00:00",
        title="MARVEL Tōkon: Fighting Souls unveils Year 1 DLC with Phoenix Cyclops",
        text=(
            "I'm Takeshi Yamanaka, Producer at Arc System Works. "
            "Marvel Tōkon: Fighting Souls unveils its Year 1 DLC roster. "
            "Phoenix Cyclops arrives on November 2."
        ),
    )
    eurogamer = source(
        link=TOKON_EU_URL,
        name="Eurogamer",
        tier=2,
        owner="ign-entertainment",
        region="uk",
        title="Marvel Tōkon: Fighting Souls Year 1 DLC roster starts with Phoenix Cyclops",
        text=(
            "Marvel Tōkon: Fighting Souls Year 1 DLC roster begins with "
            "Phoenix Cyclops on November 2."
        ),
    )
    ps_claim = claim(
        claim="Roster starts with Phoenix Cyclops on November 2",
        value="Year 1 DLC roster starting with Phoenix Cyclops on November 2, 2026",
        kind="confirmed",
        origin="takeshi yamanaka",
        source_url=TOKON_PS_URL,
    )
    # The outlet's own claim restates the headline more closely than the tier-1
    # source's, so the old rule would pick it; the tier-1 rule must not.
    eu_claim = claim(
        claim=(
            "Marvel Tōkon: Fighting Souls reveals its Year 1 DLC roster, "
            "starting with Phoenix Cyclops on 2nd November"
        ),
        value="the roster starts with Phoenix Cyclops",
        kind="reported",
        origin="unstated",
        source_url=TOKON_EU_URL,
    )
    generate = per_source_generate([{"claims": [ps_claim]}, {"claims": [eu_claim]}])[0]
    result = facts.facts(
        gathered("tokon-1", [ps_blog, eurogamer]),
        id="tokon-1",
        title=TOKON_TITLE,
        output_dir=tmp_path,
        now=NOW,
        generate=generate,
    )
    assert result["confirmation"]["confirmed"] is True
    assert result["confirmation"]["rule"] == "tier1"
    assert result["confirmation"]["tier1_sources"] == 1
    central = next(c for c in result["claims"] if c["central"])
    assert central["source_urls"] == [TOKON_PS_URL]
    assert central["value"] == "Year 1 DLC roster starting with Phoenix Cyclops on November 2, 2026"
    corroboration = [c for c in result["claims"] if not c["central"]]
    assert [c["source_urls"] for c in corroboration] == [[TOKON_EU_URL]]


def test_no_tier1_source_keeps_the_old_headline_overlap_central_rule():
    title = "Gears of War: E-Day Sold 1 Million on Steam"
    claims = [
        claim(
            claim="Gears of War: E-Day sales reach 1 million on Steam",
            value="1 million",
            source_url="https://a.example/1",
        ),
        claim(claim="Unrelated footnote", value="a detail", source_url="https://b.example/2"),
    ]
    groups = facts._claim_groups(claims)
    sources = [source(link=c["source_url"], tier=2) for c in claims]
    assert facts._preferred_central_index(groups, sources, title) is None
    merged = [facts._merge_group(group) for group in groups]
    central = facts.central_claim(merged, title=title)
    assert central is merged[0]


# --- entity stop-list, headline entities and the central-overlap guard --------


def test_months_weekdays_and_generic_phrases_are_never_entities():
    title = (
        "Marvel Tōkon: Fighting Souls reveals its Year 1 DLC roster, "
        "starting with Phoenix Cyclops on 2nd November"
    )
    source_titles = [
        "Marvel Tōkon: Fighting Souls Year 1 DLC roster arrives in November",
        "Phoenix Cyclops joins Marvel Tōkon: Fighting Souls on November 2",
    ]
    canonical = facts.canonical_entities(title=title, source_titles=source_titles)
    assert canonical == ["Marvel Tōkon: Fighting Souls", "Phoenix Cyclops"]
    assert not any("november" in entity.lower() for entity in canonical)
    assert not any("dlc" in entity.lower() for entity in canonical)
    assert not any("year" in entity.lower() for entity in canonical)


def test_headline_entity_mentioned_only_once_still_survives():
    # The seed headline names "Acme Hero" and no other headline repeats it; a
    # headline entity is kept even though its support count is one.
    primary = facts.primary_entities(
        title="Acme Hero launches today",
        source_titles=["the studio announced a new game"],
    )
    assert "acme hero" in primary


def test_seo_canonical_entities_contain_no_alias_only_entries():
    canonical = facts.canonical_entities(
        title="Marvel Tōkon: Fighting Souls adds Phoenix Cyclops",
        source_titles=["Phoenix Cyclops joins Marvel Tōkon: Fighting Souls"],
    )
    aliases: set[str] = set()
    for entity in canonical:
        aliases.update(facts.entity_aliases(entity))
    assert not (set(canonical) & aliases), "an alias must never be its own entity"
    assert "marvel tōkon" not in canonical
    assert "fighting souls" not in canonical


def test_tier1_passing_mention_does_not_confirm_the_story(monkeypatch, tmp_path):
    use_mode(monkeypatch, "legacy")
    # A tier-1 source's only claim is about something else; it names the game in
    # its headline but never states the headline's fact. Two outlets agreeing on
    # that passing claim must not confirm the story.
    official = source(
        link="https://blog.playstation.com/tokon",
        name="PlayStation Blog",
        tier=1,
        owner="sony",
        title="Marvel Tōkon: Fighting Souls news roundup",
        text="Marvel Tōkon: Fighting Souls news roundup. The blog covers many topics.",
    )
    outlet = source(
        link="https://eurogamer.net/tokon",
        name="Eurogamer",
        tier=2,
        owner="ign-entertainment",
        title="Marvel Tōkon: Fighting Souls news roundup",
        text="Marvel Tōkon: Fighting Souls news roundup. The blog covers many topics.",
    )
    generate = per_source_generate([
        {"claims": [claim(claim="The blog covers many topics", value="a roundup", source_url=official["link"], origin="sony")]},
        {"claims": [claim(claim="The blog covers many topics", value="a roundup", source_url=outlet["link"], origin="unstated")]},
    ])[0]
    with pytest.raises(facts.UnconfirmedStory) as excinfo:
        facts.facts(
            gathered("tokon-passing", [official, outlet]),
            id="tokon-passing",
            title="Marvel Tōkon: Fighting Souls reveals Phoenix Cyclops",
            output_dir=tmp_path,
            now=NOW,
            generate=generate,
        )
    assert "tier-1 source does not state the headline claim" in str(excinfo.value)
    assert not (tmp_path / "tokon-passing.json").exists()


def test_tier1_passing_mention_does_not_confirm_the_story_under_evidence(monkeypatch, tmp_path):
    # Evidence-mode twin: the same story, and the same safety outcome -- a tier-1
    # source whose only claim is a passing mention does not confirm the story.
    # The legacy reason is "tier-1 source does not state the headline claim";
    # the evidence reason is "the central fact is not backed by enough verified
    # cross-source evidence".
    use_mode(monkeypatch, "evidence")
    official = source(
        link="https://blog.playstation.com/tokon",
        name="PlayStation Blog",
        tier=1,
        owner="sony",
        title="Marvel Tōkon: Fighting Souls news roundup",
        text="Marvel Tōkon: Fighting Souls news roundup. The blog covers many topics.",
    )
    outlet = source(
        link="https://eurogamer.net/tokon",
        name="Eurogamer",
        tier=2,
        owner="ign-entertainment",
        title="Marvel Tōkon: Fighting Souls news roundup",
        text="Marvel Tōkon: Fighting Souls news roundup. The blog covers many topics.",
    )
    generate = per_source_generate([
        {"claims": [claim(claim="The blog covers many topics", value="a roundup", source_url=official["link"], origin="sony")]},
        {"claims": [claim(claim="The blog covers many topics", value="a roundup", source_url=outlet["link"], origin="unstated")]},
    ])[0]
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("tokon-passing", [official, outlet]),
            id="tokon-passing",
            title="Marvel Tōkon: Fighting Souls reveals Phoenix Cyclops",
            output_dir=tmp_path,
            now=NOW,
            generate=generate,
        )
    sheet = json.loads((tmp_path / "unconfirmed" / "tokon-passing.json").read_text(encoding="utf-8"))
    assert sheet["confirmation"]["confirmed"] is False
    assert sheet["confirmation"]["rule"] == "unconfirmed"
    assert sheet["confirmation"]["rule_source"] == "evidence"
    assert sheet["confirmation"]["reason"] == (
        "the central fact is not backed by enough verified cross-source evidence"
    )
    assert not (tmp_path / "tokon-passing.json").exists()


def test_tier1_passing_mention_with_verified_claims_does_not_confirm_under_evidence(monkeypatch, tmp_path):
    # Evidence-mode passing-mention test whose claims pass verify_evidence. The
    # tier-1 source's title and central claim are about something else (it ships
    # three games; the game is named only in passing) and a second outlet states
    # the headline claim from a different owner. One verified outlet is a single
    # origin, so no evidence rule is met and the rule that produces the outcome
    # is "unconfirmed" (rule_source "evidence", verified_claims 1): the tier-1
    # passing mention does not confirm the story.
    use_mode(monkeypatch, "evidence")
    official = source(
        link="https://official.example/review",
        name="Official Blog",
        tier=1,
        owner="acme games",
        title="Acme Games year in review",
        text=(
            "Acme Games year in review. Acme Games said it shipped three games this year. "
            "Acme Arena fans await more."
        ),
    )
    outlet = source(
        link="https://outlet1.example/acme", name="Outlet 1", tier=2, owner="co-b",
        title=ACME_TITLE, text=ACME_TEXT,
    )
    payloads = [
        {"claims": [acme_claim(
            official["link"], claim="The company shipped three games this year", value="three games",
            origin="Acme Games", kind="reported", quote="shipped three games this year",
            attribution_quote="Acme Games said",
        )]},
        {"claims": [acme_claim(outlet["link"])]},
    ]
    generate, calls = grouping_generate(payloads, [["c0"], ["c1"]])
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-passing", [official, outlet]), id="acme-passing", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
        )
    assert calls == ["fast", "fast", "group"]
    sheet = json.loads((tmp_path / "unconfirmed" / "acme-passing.json").read_text(encoding="utf-8"))
    assert sheet["confirmation"]["confirmed"] is False
    assert sheet["confirmation"]["rule"] == "unconfirmed"
    assert sheet["confirmation"]["rule_source"] == "evidence"
    assert sheet["confirmation"]["verified_claims"] == 1
    assert sheet["confirmation"]["tier1_sources"] == 0
    assert sheet["confirmation"]["reason"] == (
        "the central fact is not backed by enough verified cross-source evidence"
    )
    assert not (tmp_path / "acme-passing.json").exists()


def test_tier1_claim_naming_only_the_company_does_not_state_the_headline(monkeypatch, tmp_path):
    # Regression: a tier-1 claim that names the story's company ("Acme Games
    # shipped three games") but never states the headline fact. The company name
    # alone used to clear facts.min_central_overlap -- "Acme Games" matches one of
    # the two headline entities -- and confirm the story as "tier1". Naming the
    # company must not stand in for the headline, in either mode.
    official = source(
        link="https://news.acme.example/1",
        name="Acme News",
        tier=1,
        owner="acme games",
        title="Acme Games shipped three games",
        text="Acme Games said it shipped three games this year.",
    )
    payloads = [{"claims": [acme_claim(
        official["link"],
        claim="Acme Games shipped three games",
        value="three games",
        quote="shipped three games",
        attribution_quote="Acme Games said",
    )]}]

    use_mode(monkeypatch, "legacy")
    with pytest.raises(facts.UnconfirmedStory) as excinfo:
        facts.facts(
            gathered("acme-company", [official]), id="acme-company", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path / "legacy", now=NOW,
            generate=per_source_generate(payloads)[0],
        )
    assert "tier-1 source does not state the headline claim" in str(excinfo.value)
    legacy = json.loads(
        (tmp_path / "legacy" / "unconfirmed" / "acme-company.json").read_text(encoding="utf-8")
    )
    assert legacy["confirmation"]["confirmed"] is False
    assert legacy["confirmation"]["rule"] == "unconfirmed"
    assert legacy["confirmation"]["reason"] == "tier-1 source does not state the headline claim"

    use_mode(monkeypatch, "evidence")
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-company", [official]), id="acme-company", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path / "evidence", now=NOW,
            generate=per_source_generate(payloads)[0],
        )
    evidence = json.loads(
        (tmp_path / "evidence" / "unconfirmed" / "acme-company.json").read_text(encoding="utf-8")
    )
    assert evidence["confirmation"]["confirmed"] is False
    assert evidence["confirmation"]["rule"] != "tier1"
    assert evidence["confirmation"]["rule_source"] == "evidence"
    assert evidence["confirmation"]["tier1_sources"] == 0
    assert evidence["confirmation"]["reason"] == (
        "the central fact is not backed by enough verified cross-source evidence"
    )


def test_preferred_central_keeps_a_tier1_claim_that_states_the_headline():
    url = "https://acme.example/1"
    groups = facts._claim_groups([acme_claim(url)], entities=ACME_ENTITIES)
    sources = [source(link=url, tier=1)]
    preferred = facts._preferred_central_index(
        groups, sources, ACME_TITLE, canonical=ACME_ENTITIES
    )
    assert preferred is not None
    assert preferred[1] == facts._source_url_key(url)


def test_tier1_claim_stating_the_headline_confirms_as_tier1(monkeypatch, tmp_path):
    use_mode(monkeypatch, "legacy")
    official = source(
        link="https://news.acme.example/1", name="Acme News", tier=1, owner="acme games",
        title=ACME_TITLE, text=ACME_TEXT,
    )
    payloads = [{"claims": [acme_claim(official["link"])]}]
    result = facts.facts(
        gathered("acme-stated", [official]), id="acme-stated", title=ACME_TITLE,
        entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW,
        generate=per_source_generate(payloads)[0],
    )
    assert result["confirmation"]["confirmed"] is True
    assert result["confirmation"]["rule"] == "tier1"
    assert result["confirmation"]["tier1_sources"] == 1


def test_tier1_claim_naming_only_the_game_does_not_state_the_headline():
    url = "https://acme.example/1"
    claims = [acme_claim(
        url,
        claim="Acme Arena shipped three games",
        value="three games",
        quote="shipped three games",
        attribution_quote="Acme said",
    )]
    groups = facts._claim_groups(claims, entities=ACME_ENTITIES)
    sources = [source(link=url, tier=1)]
    assert facts._preferred_central_index(
        groups, sources, ACME_TITLE, canonical=ACME_ENTITIES
    ) is None


def test_official_announcement_headline_claim_still_confirms_as_tier1(monkeypatch, tmp_path):
    use_mode(monkeypatch, "legacy")
    title = "Acme Games announces Acme Arena launch date"
    official = source(
        link="https://news.acme.example/1", name="Acme News", tier=1, owner="acme games",
        title=title, text="Acme Games announces the Acme Arena launch date today.",
    )
    payloads = [{"claims": [acme_claim(
        official["link"], claim=title, value="Acme Arena launch date",
        quote="announces the Acme Arena launch date", attribution_quote="Acme Games announces",
    )]}]
    result = facts.facts(
        gathered("acme-announce", [official]), id="acme-announce", title=title,
        entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW,
        generate=per_source_generate(payloads)[0],
    )
    assert result["confirmation"]["confirmed"] is True
    assert result["confirmation"]["rule"] == "tier1"


# --- unconfirmed sheets and stored grounding drops ---------------------------


def test_ground_claims_details_returns_reasons():
    src = source(tier=1, text="Nvidia confirmed the launch window for its next graphics card line.")
    bad = claim(claim="Pre-order figure", value="368,000 pre-orders", source_url="https://a.example/1")
    good = claim(claim="Nvidia confirmed the window", value="the launch window", source_url="https://a.example/1")
    kept, dropped = facts.ground_claims_details([good, bad], [src])
    assert kept == [good]
    assert len(dropped) == 1
    assert dropped[0]["claim"] is bad
    assert "368000" in dropped[0]["reason"]


def test_unconfirmed_story_writes_sheet_under_unconfirmed_dir(tmp_path):
    data = gathered(sources=[source()])  # single tier-2 source, no attributed origin
    generate = make_generate({"claims": [claim(kind="reported", origin="unstated")]})
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert not (tmp_path / "story-1.json").exists(), "no top-level confirmed sheet for a rejected story"
    path = tmp_path / "unconfirmed" / "story-1.json"
    assert path.is_file()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["id"] == "story-1"
    assert written["confirmation"]["confirmed"] is False
    assert isinstance(written["origins"], list)
    assert "claims" in written and "sources" in written
    assert written["kinds"] == ["reported"]


def test_confirmed_sheet_includes_grounding_block(tmp_path):
    data = gathered(sources=[source(tier=1, text="test-origin reported the story.")])
    result = facts.facts(
        data, output_dir=tmp_path, now=NOW,
        generate=make_generate({"claims": [claim()]}),
    )
    assert result["grounding"] == []
    written = json.loads((tmp_path / "story-1.json").read_text(encoding="utf-8"))
    assert "grounding" in written

# --- attribution-linked origins (conflation guard) ---------------------------


GTA6_CONFLATION_TEXT = (
    "Per Twitch CEO Dan Clancy, the new version of GTA Online is lined up for a "
    "2027 release. GamesBeat has confirmed that Rockstar plans a PC release of "
    "GTA 6 in the first quarter of 2027, according to four industry sources."
)


def test_origin_attributed_by_another_sentence_is_kept():
    src = source(link="https://gb.example/gta6", name="GamesBeat", tier=2, text=GTA6_CONFLATION_TEXT)
    claims = [
        claim(
            claim="GTA 6 is coming to PC in Q1 2027",
            value="PC release",
            kind="reported",
            origin="dan clancy; gamesbeat",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == "gamesbeat", "only the attributed origin survives"


def test_origin_named_but_not_attributed_is_downgraded(caplog):
    caplog.set_level(logging.WARNING)
    src = source(link="https://gb.example/gta6", name="GamesBeat", tier=2, text=GTA6_CONFLATION_TEXT)
    claims = [
        claim(
            claim="GTA 6 is coming to PC in Q1 2027",
            value="PC release",
            kind="reported",
            origin="dan clancy",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN
    assert any("not attributed" in record.message for record in caplog.records)


def test_each_origin_token_is_verified_separately(caplog):
    caplog.set_level(logging.WARNING)
    src = source(link="https://gb.example/gta6", name="GamesBeat", tier=2, text=GTA6_CONFLATION_TEXT)
    claims = [
        claim(
            claim="GTA 6 is coming to PC in Q1 2027",
            value="PC release",
            kind="reported",
            origin="alpha; beta",
            source_url=src["link"],
        )
    ]
    downgrades = []
    resolved = facts.resolve_claim_origins(claims, [src], downgrades=downgrades)
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN
    # Each ";"-separated token is verified on its own: both fail on their own
    # merits and each keeps its own verdict, rather than the pair being judged
    # once.
    verdicts = {entry["origin"]: entry["reason"] for entry in downgrades}
    assert verdicts == {
        "alpha": "not named in the source text",
        "beta": "not named in the source text",
    }
    # One summary line per source, not one warning per token.
    assert sum("downgraded for" in record.message for record in caplog.records) == 1


def test_first_person_byline_attributes_itself_without_a_verb():
    src = source(
        link="https://blog.example/cyclops",
        name="Studio Blog",
        tier=1,
        text=(
            "Hello everyone! I'm Takeshi Yamanaka, Producer at Arc System Works. "
            "The narrative team chose the Phoenix version for fresher visuals."
        ),
    )
    claims = [
        claim(
            claim="Reason for the Phoenix version",
            value="fresher visuals",
            origin="takeshi yamanaka",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == "takeshi yamanaka"


# --- cue carry-forward within a paragraph ------------------------------------


def test_a_cue_carries_forward_until_a_later_cue_names_a_different_source():
    src = source(
        link="https://b.example/crunch",
        name="Beat",
        tier=2,
        text=(
            "According to Bloomberg, Rockstar crunched for months. "
            "The studio lengthened shifts. Staff were exhausted."
        ),
    )
    claims = [
        claim(
            claim="Staff were exhausted",
            value="exhausted staff",
            kind="reported",
            origin="bloomberg",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == "bloomberg", "the cue carries forward two sentences"
    assert facts._attribution_winner(src["text"], claims[0]) == "Bloomberg"


def test_a_later_cue_naming_a_different_source_supersedes_an_earlier_one():
    src = source(
        link="https://b.example/delays",
        name="Beat",
        tier=2,
        text=(
            "According to Bloomberg, Rockstar crunched for months. "
            "Rockstar said the delays would continue for a while."
        ),
    )
    claims = [
        claim(
            claim="The delays would continue for a while",
            value="delays continue",
            kind="reported",
            origin="bloomberg",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN, "the later cue supersedes Bloomberg"


# --- a company's own bylined post vs an outlet using first person ------------


def test_an_outlet_using_we_does_not_self_attribute_byline():
    src = source(
        link="https://o.example/comment",
        name="Eurogamer",
        owner="gamer",
        tier=2,
        text="We asked Rockstar for comment about the delay. Rockstar has not yet replied.",
    )
    claims = [
        claim(
            claim="Rockstar has not replied",
            value="no reply",
            kind="reported",
            origin="unstated",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN


def test_an_outlet_using_i_does_not_self_attribute_byline():
    src = source(
        link="https://o.example/played",
        name="Eurogamer",
        owner="gamer",
        tier=2,
        text="I played the new build and it felt smooth. The frame rate stayed stable.",
    )
    claims = [
        claim(
            claim="The frame rate stayed stable",
            value="stable frame rate",
            kind="opinion",
            origin="unstated",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN


def test_a_tier1_own_site_with_a_byline_self_attributes():
    src = source(
        link="https://blog.example/cyclops",
        name="Studio Blog",
        owner="arc system works",
        tier=1,
        text=(
            "Hello! I am Takeshi Yamanaka, Producer at Arc System Works. "
            "The team chose the Phoenix version for fresher visuals."
        ),
    )
    claims = [
        claim(
            claim="The team chose the Phoenix version",
            value="fresher visuals",
            kind="statement",
            origin="unstated",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == "arc system works"


# --- speech verbs vs announcement verbs --------------------------------------


def test_a_person_speaking_stays_a_speaker_even_when_a_headline_entity():
    src = source(
        link="https://d.example/houser",
        name="Outlet",
        tier=2,
        text="Dan Houser says he failed with the game.",
    )
    claims = [
        claim(
            claim="Dan Houser failed with the game",
            value="failure",
            kind="statement",
            origin="dan houser",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src], entities=["Dan Houser"])
    assert resolved[0]["origin"] == "dan houser"


def test_an_announcement_verb_keeps_an_organization_as_its_origin():
    src = source(
        link="https://d.example/delay",
        name="Outlet",
        tier=2,
        text="Rockstar announced a delay to the next update.",
    )
    claims = [
        claim(
            claim="The next update is delayed",
            value="delay",
            kind="confirmed",
            origin="rockstar",
            source_url=src["link"],
        )
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == "rockstar games"


# --- coherence with a person in the seed headline ----------------------------


ASHA_TITLE = (
    "Hundreds of layoffs and many cancelled projects later, Xbox CEO Asha Sharma "
    "reportedly says 'we will not survive' without new IPs"
)


def test_coherence_keeps_a_claim_naming_the_person_in_the_headline():
    claims = [
        claim(
            claim="Asha Sharma tells rattled Xbox employees about invention on new IP",
            value="new IP",
            source_url="https://a.example/1",
        )
    ]
    sources = [source(link="https://a.example/1", title="Xbox CEO Asha Sharma addresses employees")]
    kept, dropped, _ = facts.coherence_check(claims, sources, title=ASHA_TITLE)
    assert len(kept) == 1
    assert dropped == []


def test_coherence_drops_a_claim_about_an_unrelated_game():
    claims = [
        claim(
            claim="Mario Kart is getting a new cup",
            value="Mario Kart",
            source_url="https://b.example/2",
        )
    ]
    sources = [source(link="https://b.example/2", name="Beta", title="Sonic Racing adds tracks")]
    kept, dropped, _ = facts.coherence_check(claims, sources, title=ASHA_TITLE)
    assert kept == []
    assert len(dropped) == 1


def test_coherence_drops_a_quantity_claim_for_the_layoffs_story():
    # "Hundreds" is no entity, so a claim that names only it is off-topic even
    # though the headline also opens with it.
    claims = [
        claim(
            claim="Hundreds of players joined the free weekend",
            value="hundreds of players",
            source_url="https://a.example/1",
        )
    ]
    sources = [source(link="https://a.example/1", name="Alpha", title="Alpha weekly digest")]
    kept, dropped, primary = facts.coherence_check(claims, sources, title=ASHA_TITLE)
    assert kept == []
    assert len(dropped) == 1
    assert {"asha sharma", "xbox"} <= set(primary)
    assert "hundreds" not in primary
    assert "ips" not in primary


def test_coherence_keeps_the_asha_sharma_claim_from_the_layoffs_story():
    # The generic/quantity cleanup must not drop the person the headline names.
    claims = [
        claim(
            claim="Asha Sharma tells Xbox staff the company needs new IP",
            value="new IP",
            source_url="https://a.example/1",
        )
    ]
    sources = [source(link="https://a.example/1", name="Alpha", title="Alpha weekly digest")]
    kept, dropped, primary = facts.coherence_check(claims, sources, title=ASHA_TITLE)
    assert len(kept) == 1
    assert dropped == []
    assert "asha sharma" in primary


GTA_TITLE = "GTA 6 Reportedly Coming to PC in Q1 2027"


def test_coherence_generic_only_claim_needs_an_on_topic_source():
    claims = [
        claim(claim="PC gaming is growing fast", value="PC", source_url="https://a.example/1"),
    ]
    off_topic = [source(link="https://a.example/1", name="Alpha", title="Alpha weekly digest")]
    kept, dropped, primary = facts.coherence_check(claims, off_topic, title=GTA_TITLE)
    assert "pc" not in primary
    assert kept == []
    assert len(dropped) == 1

    on_topic = [
        source(
            link="https://a.example/1",
            name="Alpha",
            title="GTA 6 PC version arrives in Q1 2027",
        )
    ]
    kept, dropped, _ = facts.coherence_check(claims, on_topic, title=GTA_TITLE)
    assert len(kept) == 1
    assert dropped == []


def test_central_claim_of_kind_opinion_is_not_confirmable():
    result = facts.confirmation_from_sources(
        [source(tier=1)],
        claims=[
            claim(claim="Xbox needs new IP", kind="opinion", source_url="https://a.example/1")
        ],
        merged={"kind": "opinion"},
    )
    assert result["confirmed"] is False
    assert result["rule"] == "unconfirmed"
    assert result["reason"] == "the central claim is an opinion"


# --- unconfirmed sheets keep the full diagnostics ----------------------------


def test_unconfirmed_sheet_records_coherence_grounding_and_reason(tmp_path):
    data = gathered(
        sources=[
            source(
                link="https://a.example/1",
                name="Alpha",
                tier=2,
                title="Xbox CEO Asha Sharma addresses employees",
                text="Xbox CEO Asha Sharma said the company needs new IP.",
            )
        ]
    )
    generate = make_generate({"claims": [claim(kind="reported", origin="unstated")]})
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            data,
            id="story-1",
            title=ASHA_TITLE,
            output_dir=tmp_path,
            now=NOW,
            generate=generate,
        )
    written = json.loads((tmp_path / "unconfirmed" / "story-1.json").read_text(encoding="utf-8"))
    assert written["confirmation_reason"], "the sheet states why it was not confirmed"
    assert isinstance(written["grounding"], list)
    coherence = written["coherence"]
    assert coherence["checked"] is True
    assert "asha sharma" in coherence["primary_entities"]
    assert isinstance(coherence["dropped_claims"], list)


# --- anonymous origins never count as a source -------------------------------


@pytest.mark.parametrize(
    "text, origin",
    [
        (
            "According to sources who requested anonymity, the studio delayed the game.",
            "sources",
        ),
        ("Unnamed sources say the studio delayed the game.", "unnamed sources"),
        ("Insiders say the studio delayed the game.", "insiders"),
        ("Officials say the studio delayed the game.", "officials"),
        (
            "People familiar with the matter say the studio delayed the game.",
            "people familiar with the matter",
        ),
        (
            "A source close to the project says the studio delayed the game.",
            "source close to the project",
        ),
        (
            "Sources familiar with the studio's plans say the game slipped.",
            "sources",
        ),
    ],
)
def test_anonymous_cue_origin_is_never_kept(text, origin):
    # Case (1): an anonymous cue ("according to sources", "insiders",
    # "people familiar with the matter") names no real source, so the extracted
    # origin is never kept when it is the only attribution.
    src = source(text=text)
    resolved = facts.resolve_claim_origins([claim(origin=origin)], [src])
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN, (
        f"anonymous origin {origin!r} must not be kept"
    )


def test_nearest_cue_wins_over_an_earlier_anonymous_cue():
    # Case (2): the later named cue supersedes the earlier anonymous one, and the
    # first claim attaches to neither the anonymous cue nor the named one.
    src = source(
        text=(
            "According to sources who requested anonymity, the game slipped. "
            "Bloomberg reports the delay is internal."
        )
    )
    claims = [
        claim(claim="the game slipped", value="the game slipped", origin="sources"),
        claim(claim="the delay is internal", value="the delay is internal", origin="bloomberg"),
    ]
    resolved = facts.resolve_claim_origins(claims, [src])
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN
    assert resolved[0]["origin"] != "bloomberg"
    assert resolved[1]["origin"] == "bloomberg"


def test_named_outlet_citing_anonymous_sources_is_the_origin():
    # Case (3): a named outlet that merely cites anonymous sources is itself the
    # origin, regardless of the anonymous phrase in the sentence.
    src = source(
        name="GamesBeat",
        text=(
            "GamesBeat reports, citing four industry sources, that the PC version "
            "arrives in the first quarter."
        ),
    )
    resolved = facts.resolve_claim_origins([claim(origin="gamesbeat", kind="reported")], [src])
    assert resolved[0]["origin"] == "gamesbeat"


@pytest.mark.parametrize("origin", ["anonymous sources", "insiders"])
def test_extractor_anonymous_origin_present_in_text_is_downgraded(origin):
    # Case (4): the extractor returning an anonymous source (even one whose words
    # appear in the text) is rejected as anonymous and downgraded to unstated.
    src = source(text="The report cites anonymous sources and insiders about the delay.")
    resolved = facts.resolve_claim_origins([claim(origin=origin)], [src])
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN


def test_two_anonymous_origins_never_confirm():
    # Case (5): two anonymous origins are not two independent origins, so they
    # never contribute to confirmation.
    sources = [
        source(
            link="https://a.example/1",
            name="Alpha",
            text="Sources say the delay is internal.",
        ),
        source(
            link="https://b.example/2",
            name="Beta",
            owner="co-b",
            text="Insiders say the delay is internal.",
        ),
    ]
    claims = [
        claim(
            claim="The delay is internal",
            value="the delay is internal",
            kind="reported",
            origin="sources",
            source_url="https://a.example/1",
        ),
        claim(
            claim="The delay is internal",
            value="the delay is internal",
            kind="reported",
            origin="insiders",
            source_url="https://b.example/2",
        ),
    ]
    result = facts.confirmation_from_sources(sources, claims=claims)
    assert result["origins"] == []
    assert result["rule"] == "unconfirmed"
    assert result["confirmed"] is False


def test_named_person_with_a_speech_cue_is_still_accepted():
    # Case (6): the anonymous rule must not over-reject a named, cue-attributed
    # speaker.
    src = source(text="Dan Houser says the project failed.")
    resolved = facts.resolve_claim_origins([claim(origin="dan houser")], [src])
    assert resolved[0]["origin"] == "dan houser"


def test_insider_gaming_reports_is_a_named_outlet_origin():
    # A named outlet that contains a descriptor-like word ("Insider") is still a
    # name, so it is kept as the origin instead of being downgraded to unstated.
    src = source(text="Insider Gaming reports the studio delayed the game.")
    resolved = facts.resolve_claim_origins(
        [
            claim(
                claim="the studio delayed the game",
                value="the studio delayed the game",
                kind="reported",
                origin="Insider Gaming",
            )
        ],
        [src],
    )
    assert resolved[0]["origin"] == "insider gaming"


@pytest.mark.parametrize(
    "origin",
    ["Insider Gaming", "Source Engine", "People Can Fly", "Official Xbox Magazine"],
)
def test_outlet_and_company_names_are_not_anonymous(origin):
    # A phrase is anonymous only when EVERY token is a descriptor. One name-like
    # token ("Gaming", "Engine", "Fly", "Xbox", "Magazine") makes it a name.
    assert facts._is_anonymous_origin(origin, facts.load_origin_aliases()) is False


@pytest.mark.parametrize(
    "origin",
    ["four industry sources", "sources familiar with the studio's plans"],
)
def test_descriptor_only_phrases_are_anonymous(origin):
    # Every token is in the vocabulary, so the phrase names no real source.
    assert facts._is_anonymous_origin(origin, facts.load_origin_aliases()) is True


# --- a cue names a source, not a document, product or back-reference ----------


def test_document_noun_after_a_lead_resolves_to_the_organization():
    # "According to an Epic blog post" names Epic, never the "blog post": the
    # lead filler is lowercase-only, so the capture keeps "Epic blog post", and
    # the alias resolves it to Epic Games.
    src = source(
        text=(
            "Fortnite will launch via the Epic Games Launcher. "
            "According to an Epic blog post, Easy Anti-Cheat is being rebuilt."
        )
    )
    resolved = facts.resolve_claim_origins(
        [claim(claim="Easy Anti-Cheat is being rebuilt", origin="epic games")], [src]
    )
    assert resolved[0]["origin"] == "epic games"


def test_a_product_or_software_noun_is_never_the_source():
    # "via Wine" and "the Epic Games Launcher" place a product next to a claim;
    # the attribution belongs to the organization that spoke, Epic.
    src = source(text="It runs via Wine. Epic says the client supports Linux.")
    resolved = facts.resolve_claim_origins(
        [claim(claim="the client supports Linux", origin="wine")], [src]
    )
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN
    kept = facts.resolve_claim_origins(
        [claim(claim="the client supports Linux", origin="epic")], [src]
    )
    assert kept[0]["origin"] == "epic games"


def test_a_clause_subject_attributes_a_later_clause():
    # A headline names its source once and then attributes a later clause to it:
    # "Epic Games vibe codes ..., claims ..." must credit Epic Games.
    src = source(
        text="Epic Games vibe codes a new launcher, claims it starts 46 percent faster."
    )
    assert facts._attribution_winner(src["text"], claim(claim="it starts 46 percent faster")) == (
        "epic games"
    )
    assert facts.resolve_claim_origins(
        [claim(claim="it starts 46 percent faster", origin="epic games")], [src]
    )[0]["origin"] == "epic games"


def test_a_person_talking_to_an_outlet_is_the_speaker_not_the_outlet():
    # "told IGN" names where the person spoke, not who produced the claim.
    src = source(
        text=(
            "After a Bloomberg report, Martin Keely, vice president at Epic Games, "
            "told IGN that AI is highly visible."
        )
    )
    resolved = facts.resolve_claim_origins(
        [claim(claim="AI is highly visible", origin="martin keely", affiliation="epic games")], [src]
    )
    assert resolved[0]["origin"] == "martin keely"
    assert resolved[0]["affiliation"] == "epic games"
    spoken = facts.resolve_claim_origins(
        [claim(claim="AI is highly visible", origin="ign")], [src]
    )
    assert spoken[0]["origin"] == facts.UNSTATED_ORIGIN


def test_coreference_phrase_resolves_to_the_prior_organization():
    # "The company" stands for the organization the paragraph already named.
    src = source(
        text="Epic Games announced a preview. The company says it vibe coded the launcher."
    )
    resolved = facts.resolve_claim_origins(
        [claim(claim="it vibe coded the launcher", origin="epic games")], [src]
    )
    assert resolved[0]["origin"] == "epic games"


def test_coreference_phrase_without_a_prior_organization_is_unstated():
    # With no organization before it, "the company" names nothing: the claim is
    # unstated, and the phrase is not treated as an anonymous descriptor either.
    src = source(text="The company says it is cheaper.")
    assert facts._is_anonymous_origin("the company", facts.load_origin_aliases()) is False
    resolved = facts.resolve_claim_origins(
        [claim(claim="it is cheaper", origin="epic games")], [src]
    )
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN


def test_an_industry_quantity_phrase_is_still_anonymous():
    # The coreference fix must not turn a quantity descriptor into a source.
    assert facts._is_anonymous_origin("four industry sources", facts.load_origin_aliases()) is True


def test_an_unattributed_origin_leaves_a_diagnostic_trace():
    # A downgraded "not attributed" origin must explain itself in the sheet.
    src = source(
        text=(
            "According to Bloomberg, Rockstar crunched for months. "
            "The studio lengthened shifts. Staff were exhausted."
        )
    )
    traces: list[dict] = []
    resolved = facts.resolve_claim_origins(
        [claim(claim="Staff were exhausted", origin="rockstar")],
        [src],
        attribution_traces=traces,
    )
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN
    assert traces and traces[0]["origin"] == "rockstar"
    assert traces[0]["winner"] == "Bloomberg"
    assert any("bloomberg" in entry for entry in traces[0]["cue_sources"])


# --- canonical organization names and aliases (stage 6) ----------------------


def test_canonicalize_origin_resolves_organization_aliases():
    assert facts.canonicalize_origin("Epic") == "epic games"
    assert facts.canonicalize_origin("Rockstar") == "rockstar games"
    assert facts.canonicalize_origin("Take-Two") == "take-two interactive"
    assert facts.canonicalize_origin("Xbox Game Studios") == "microsoft"
    # The canonical name itself is unchanged, and origins.json aliases still win.
    assert facts.canonicalize_origin("Epic Games") == "epic games"
    assert facts.canonicalize_origin("Alinea") == "alinea analytics"


def test_organization_aliases_are_loaded_from_orgs_json():
    aliases = facts.load_organization_aliases()
    assert aliases["epic"] == "epic games"
    assert aliases["rockstar"] == "rockstar games"
    assert facts.load_origin_aliases()["epic"] == "epic games"


def test_contained_origins_keep_a_known_organization_name():
    # A known organization is never dropped for a shorter, unknown form.
    assert facts._collapse_contained_origins({"embark", "embark studios"}) == ["embark studios"]
    # Non-organizations keep the shorter, more general name as before.
    assert facts._collapse_contained_origins({"alinea analytics", "alinea analytics ltd"}) == [
        "alinea analytics"
    ]


# --- corroboration of the central fact (stage 6) -----------------------------


LINUX_FACT = "Fortnite is coming to Linux once Epic improves Easy Anti-Cheat"


def test_corroboration_accepts_restatements_of_the_central_fact():
    central = claim(claim="Fortnite is coming to Linux", value=LINUX_FACT)
    epic = claim(claim="Epic Games confirms Fortnite for Linux", value=LINUX_FACT)
    plans = claim(
        claim="Fortnite will come to Linux once Epic improves Easy Anti-Cheat",
        value=LINUX_FACT,
    )
    assert facts.corroboration_score(epic, central, entities=["Fortnite"]) > 0
    assert facts.corroboration_score(plans, central, entities=["Fortnite"]) > 0


def test_corroboration_rejects_a_different_fact():
    central = claim(claim="Fortnite is coming to Linux", value=LINUX_FACT)
    skin = claim(claim="Fortnite gets a new winter skin", value="a new winter skin")
    assert facts.corroboration_score(skin, central, entities=["Fortnite"]) == 0.0


def test_corroboration_rejects_conflicting_figures():
    lobby = claim(claim="The mode supports a large lobby", value="the mode supports 20 players")
    other = claim(claim="The mode supports a large lobby", value="the mode supports 10 players")
    assert facts._overlap_score(other, lobby) >= facts.DEFAULT_CORROBORATION_OVERLAP
    assert facts.corroboration_score(other, lobby) == 0.0


def test_corroboration_requires_a_primary_entity():
    central = claim(
        claim="The game is coming to a new platform",
        value="the game arrives on a new platform",
    )
    report = claim(
        claim="A report confirms the release",
        value="the game arrives on a new platform",
    )
    assert facts.corroboration_score(report, central) > 0
    assert facts.corroboration_score(report, central, entities=["Fortnite"]) == 0.0


def test_corroboration_accepts_a_longer_restatement_of_the_same_fact():
    # The extra words dilute the Jaccard overlap, but the shorter claim's words
    # are all contained in the longer version, so it is the same fact.
    central = claim(claim="Fortnite is coming to Linux", value="Fortnite is coming to Linux")
    longer = claim(
        claim="Fortnite will come to Linux once Epic improves Easy Anti-Cheat",
        value="Fortnite is coming to Linux once Epic improves Easy Anti-Cheat",
    )
    assert facts._overlap_score(longer, central) < facts.DEFAULT_CORROBORATION_OVERLAP
    assert facts.corroboration_score(longer, central, entities=["Fortnite"]) > 0


def test_corroboration_rejects_a_different_route_to_the_same_entity():
    # "distributed through the Epic Games Launcher" is a different fact from
    # "coming to Linux", however many words the two share.
    central = claim(claim="Fortnite is coming to Linux", value="Fortnite is coming to Linux")
    launcher = claim(
        claim="Fortnite will be distributed through the Epic Games Launcher",
        value="Fortnite will be distributed through the Epic Games Launcher",
    )
    assert facts.corroboration_score(launcher, central, entities=["Fortnite"]) == 0.0


def test_corroboration_rejects_a_central_date_the_candidate_omits():
    # "coming to Linux in March" is not restated by "coming to Linux": the
    # candidate never states the month the central claim does.
    march = claim(claim="Fortnite is coming to Linux in March", value="Fortnite is coming to Linux in March")
    plain = claim(claim="Fortnite is coming to Linux", value="Fortnite is coming to Linux")
    assert facts.corroboration_score(plain, march, entities=["Fortnite"]) == 0.0


def test_central_claim_prefers_a_confirmed_named_claim_over_a_longer_unstated_one():
    title = "Fortnite is coming to Linux"
    unstated = claim(
        claim="Fortnite is coming to Linux and it will be amazing",
        origin="unstated",
        kind="reported",
    )
    confirmed = claim(claim="Fortnite is coming to Linux", origin="epic games", kind="confirmed")
    assert facts.central_claim([unstated, confirmed], title=title)["origin"] == "epic games"


def test_corroborating_claims_count_toward_two_independent_origins():
    sources = [
        source(link="https://a.example/1", text="Sony said the thing."),
        source(link="https://b.example/2", name="Beta", owner="co-b", text="Microsoft said the thing."),
    ]
    central = claim(claim="The thing", origin="sony", source_url="https://a.example/1")
    alone = facts.confirmation_from_sources(sources, claims=[central])
    assert alone["confirmed"] is False
    assert alone["origins"] == ["sony"]

    together = facts.confirmation_from_sources(
        sources,
        claims=[central],
        corroborating=[claim(claim="The thing", origin="microsoft", source_url="https://b.example/2")],
    )
    assert together["origins"] == ["microsoft", "sony"]
    assert together["confirmed"] is True
    assert together["rule"] == "two-independent-origins"


def test_corroborating_sources_satisfy_the_first_party_rule():
    sources = [
        source(link="https://a.example/1", text="Epic Games announced Fortnite is coming to Linux."),
        source(
            link="https://b.example/2", name="Beta", owner="co-b",
            text="Epic Games said Fortnite is coming to Linux.",
        ),
        source(
            link="https://c.example/3", name="Gamma", owner="co-c",
            text="Epic Games confirmed Fortnite is coming to Linux.",
        ),
    ]
    central = claim(
        claim="Fortnite is coming to Linux", value=LINUX_FACT,
        origin="epic games", kind="confirmed", source_url="https://a.example/1",
    )
    others = [
        claim(
            claim="Fortnite is coming to Linux", value=LINUX_FACT, origin="epic games",
            kind="reported", source_url="https://b.example/2",
        ),
        claim(
            claim="Fortnite is coming to Linux", value=LINUX_FACT, origin="epic games",
            kind="reported", source_url="https://c.example/3",
        ),
    ]
    without = facts.confirmation_from_sources(sources, claims=[central], entities=["Fortnite"])
    assert without["confirmed"] is False

    with_others = facts.confirmation_from_sources(
        sources, claims=[central], entities=["Fortnite"], corroborating=others
    )
    assert with_others["confirmed"] is True
    assert with_others["rule"] == "first-party"
    assert with_others["first_party_sources"] == 3


def test_corroboration_never_counts_toward_tier1():
    sources = [source(link="https://a.example/1", text="The claim.")]
    central = claim(claim="The claim", source_url="https://a.example/1", origin="sony")
    result = facts.confirmation_from_sources(
        sources,
        claims=[central],
        corroborating=[claim(claim="The claim", source_url="https://b.example/2", origin="microsoft")],
    )
    # The corroborating source is not in the central claim's own source list, so
    # it can never make the tier-1 count non-zero.
    assert result["tier1_sources"] == 0


# --- unconfirmed sheet diagnostics (stage 6) ---------------------------------


def test_claims_by_source_groups_and_shapes():
    claims = [
        claim(claim="One", kind="confirmed", origin="sony", source_url="https://a.example/1"),
        claim(claim="Two", kind="reported", origin="microsoft", source_url="https://a.example/1"),
        claim(claim="Three", kind="statement", origin="sony", source_url="https://b.example/2"),
    ]
    by_source = facts._claims_by_source(claims)
    assert [entry["source_url"] for entry in by_source] == [
        "https://a.example/1",
        "https://b.example/2",
    ]
    assert [entry["claim"] for entry in by_source[0]["claims"]] == ["One", "Two"]
    assert by_source[0]["claims"][0] == {"claim": "One", "kind": "confirmed", "origin": "sony"}


def test_corroboration_diagnostics_reports_best_overlap_per_other_source():
    central = {
        "claim": "Fortnite is coming to Linux",
        "value": LINUX_FACT,
        "source_urls": ["https://a.example/1"],
    }
    claims = [
        claim(claim="Fortnite is coming to Linux", value=LINUX_FACT, source_url="https://a.example/1"),
        claim(
            claim="Epic Games confirms Fortnite for Linux", value=LINUX_FACT,
            source_url="https://b.example/2",
        ),
        claim(claim="Fortnite gets a new winter skin", value="new winter skin", source_url="https://c.example/3"),
    ]
    diagnostics = facts._corroboration_diagnostics(claims, central, entities=["Fortnite"])
    by_url = {entry["source_url"]: entry for entry in diagnostics}
    assert "https://a.example/1" not in by_url, "the central claim's own source is skipped"
    assert by_url["https://b.example/2"]["matched_claim"] == "Epic Games confirms Fortnite for Linux"
    assert by_url["https://b.example/2"]["score"] >= facts.DEFAULT_CORROBORATION_OVERLAP
    assert by_url["https://c.example/3"]["matched_claim"] is None


def test_unconfirmed_sheet_carries_diagnostics_and_corroborated_versions(tmp_path):
    central = {
        "claim": "Fortnite is coming to Linux", "value": LINUX_FACT,
        "source_urls": ["https://a.example/1"], "central": True,
        "versions": [{"source_url": "https://a.example/1", "value": LINUX_FACT}],
    }
    kept = [
        claim(claim="Fortnite is coming to Linux", value=LINUX_FACT, source_url="https://a.example/1"),
        claim(claim="Epic Games confirms Fortnite for Linux", value=LINUX_FACT, source_url="https://b.example/2"),
    ]
    path = facts._write_unconfirmed_sheet(
        tmp_path, "sheet-1", NOW, {"confirmed": False, "origins": []}, central, kept, [central],
        [], [], primary=["Fortnite"], canonical=["Fortnite"],
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [entry["source_url"] for entry in payload["claims_by_source"]] == [
        "https://a.example/1",
        "https://b.example/2",
    ]
    assert payload["corroboration"] == [
        {
            "source_url": "https://b.example/2",
            "score": payload["corroboration"][0]["score"],
            "matched_claim": "Epic Games confirms Fortnite for Linux",
        }
    ]
    assert payload["corroboration"][0]["score"] >= facts.DEFAULT_CORROBORATION_OVERLAP


def test_facts_records_a_corroborated_version_and_diagnostics(tmp_path):
    a_text = "Fortnite is coming to Linux once Epic improves Easy Anti-Cheat."
    b_text = "Fortnite will come to Linux once Epic improves Easy Anti-Cheat."
    sources = [
        source(link="https://a.example/1", text=a_text),
        source(link="https://b.example/2", name="Beta", owner="co-b", text=b_text),
    ]
    payloads = [
        {"claims": [claim(claim="Fortnite is coming to Linux", value=a_text, origin="unstated", source_url="https://a.example/1")]},
        {"claims": [claim(claim="Fortnite will come to Linux once Epic improves Easy Anti-Cheat", value=b_text, origin="unstated", source_url="https://b.example/2")]},
    ]
    generate, _calls = per_source_generate(payloads)
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("corr-1", sources), id="corr-1",
            title="Fortnite is coming to Linux", entities=["Fortnite"],
            output_dir=tmp_path, now=NOW, generate=generate,
        )
    sheet = json.loads((tmp_path / "unconfirmed" / "corr-1.json").read_text(encoding="utf-8"))
    central = sheet["central_claim"]
    assert central["source_urls"] == ["https://a.example/1"]
    corroborated = [v for v in central["versions"] if v.get("corroborated")]
    assert [v["source_url"] for v in corroborated] == ["https://b.example/2"]
    # The corroborated version keeps the other source's own value and kind.
    b_claim = next(c for c in sheet["claims"] if c["source_urls"] == ["https://b.example/2"])
    assert corroborated[0]["value"] == b_claim["value"]
    assert corroborated[0]["kind"] == b_claim["kind"]
    # The corroborating claim is still its own merged entry, not folded into the
    # central claim's wording.
    assert len(sheet["claims"]) == 2
    by_url = {entry["source_url"]: entry for entry in sheet["corroboration"]}
    assert by_url["https://b.example/2"]["matched_claim"] == (
        "Fortnite will come to Linux once Epic improves Easy Anti-Cheat"
    )
    assert by_url["https://b.example/2"]["score"] >= facts.DEFAULT_CORROBORATION_OVERLAP


# --- evidence: quote, attribution and speaker are checked verbatim (stage 15) --


def test_claim_schema_requires_the_evidence_fields():
    item = facts.CLAIM_SCHEMA["properties"]["claims"]["items"]
    for field in ("quote_ids", "attribution_ids", "speaker", "speaker_type"):
        assert field in item["properties"]
        assert field in item["required"]
    assert item["properties"]["quote_ids"]["type"] == "array"
    assert item["properties"]["quote_ids"]["items"] == {"type": "integer"}
    assert item["properties"]["attribution_ids"]["items"] == {"type": "integer"}
    assert item["properties"]["speaker_type"]["enum"] == list(facts.SPEAKER_TYPES)
    assert item["additionalProperties"] is False
    # The model cites evidence by sentence id; it never returns quote text.
    assert "quote" not in item["properties"]
    assert "attribution_quote" not in item["properties"]


def test_validate_claims_fills_and_normalizes_evidence_fields():
    raw = {
        "claim": "X", "value": "y", "source_url": "https://a.example/1",
        "confidence": 0.5, "origin": "sony", "kind": "reported", "affiliation": "",
        "quote": "  quoted line  ", "attribution_quote": "said Sony",
        "speaker": "  Sony  ", "speaker_type": "Organization",
    }
    validated = facts.validate_claims({"claims": [raw]})[0]
    assert validated["quote"] == "quoted line"
    assert validated["attribution_quote"] == "said Sony"
    assert validated["speaker"] == "Sony"
    assert validated["speaker_type"] == "organization"


def test_validate_claims_defaults_missing_evidence_fields():
    raw = {
        "claim": "X", "value": "y", "source_url": "https://a.example/1",
        "confidence": 0.5, "origin": "sony", "kind": "reported", "affiliation": "",
    }
    validated = facts.validate_claims({"claims": [raw]})
    assert validated[0]["quote"] == ""
    assert validated[0]["attribution_quote"] == ""
    assert validated[0]["speaker"] == ""
    assert validated[0]["speaker_type"] == "none"


def test_validate_claims_clips_evidence_quotes_and_unknown_speaker_type():
    raw = {
        "claim": "X",
        "value": "y",
        "source_url": "https://a.example/1",
        "confidence": 0.5,
        "origin": "sony",
        "kind": "reported",
        "affiliation": "",
        "quote": "q" * 500,
        "attribution_quote": "a" * 400,
        "speaker": "Sony",
        "speaker_type": "wizard",
    }
    validated = facts.validate_claims({"claims": [raw]})
    assert len(validated[0]["quote"]) == facts.MAX_QUOTE_CHARS
    assert len(validated[0]["attribution_quote"]) == facts.MAX_ATTRIBUTION_QUOTE_CHARS
    assert validated[0]["speaker_type"] == "none"


def test_claim_schema_exposes_every_evidence_key_as_required():
    item = facts.CLAIM_SCHEMA["properties"]["claims"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert facts.CLAIM_SCHEMA["additionalProperties"] is False


def test_verify_evidence_accepts_a_verbatim_quote_and_attribution():
    text = 'Epic Games said "the update lands in June" to fans.'
    result = facts.verify_evidence(
        claim(
            quote='"the update lands in June"',
            attribution_quote="Epic Games said",
            speaker="Epic Games",
            speaker_type="organization",
        ),
        text,
    )
    assert result["quote_found"] is True
    assert result["attribution_found"] is True
    assert result["speaker_grounded"] is True
    assert result["ok"] is True
    assert result["reason"] == ""


def test_verify_evidence_normalizes_curly_quotes_dashes_and_whitespace():
    text = "Epic Games said \u201cthe update lands in June\u201d \u2013 soon."
    result = facts.verify_evidence(
        claim(
            quote='"the update lands in June" - soon',
            attribution_quote="Epic Games   said",
            speaker="Epic",
            speaker_type="organization",
        ),
        text,
    )
    assert result["quote_found"] is True
    assert result["attribution_found"] is True
    assert result["ok"] is True


def test_verify_evidence_quote_must_be_verbatim():
    result = facts.verify_evidence(
        claim(
            quote="Sony will ship in June",
            attribution_quote="Sony said",
            speaker="Sony",
            speaker_type="organization",
        ),
        "Sony said it would ship later this year.",
    )
    assert result["quote_found"] is False
    assert result["ok"] is False
    assert result["reason"] == "quote not found"


def test_verify_evidence_attribution_must_be_in_source():
    result = facts.verify_evidence(
        claim(
            quote="it would ship later",
            attribution_quote="Microsoft said",
            speaker="Sony",
            speaker_type="organization",
        ),
        "Sony said it would ship later this year.",
    )
    assert result["quote_found"] is True
    assert result["attribution_found"] is False
    assert result["ok"] is False
    assert result["reason"] == "attribution quote not found"


def test_verify_evidence_empty_quotes_fail():
    result = facts.verify_evidence(
        claim(speaker="Sony", speaker_type="organization"),
        "Sony said something.",
    )
    assert result["quote_found"] is False
    assert result["attribution_found"] is False
    assert result["ok"] is False
    assert result["reason"] == "quote missing"


def test_verify_evidence_grounds_a_speaker_named_in_the_attribution():
    result = facts.verify_evidence(
        claim(
            quote="the update lands in June",
            attribution_quote="Epic Games said",
            speaker="Epic Games",
            speaker_type="organization",
        ),
        "Epic Games said the update lands in June.",
    )
    assert result["speaker_grounded"] is True


def test_verify_evidence_needs_a_company_coference_named_nearby():
    claim_text = claim(
        quote="it vibe coded the launcher",
        attribution_quote="The company says",
        speaker="Epic Games",
        speaker_type="organization",
    )
    source_text = "The company says it vibe coded the launcher."
    without = facts.verify_evidence(claim_text, source_text)
    assert without["speaker_grounded"] is False
    assert without["reason"] == "speaker not grounded"

    with_headline = facts.verify_evidence(
        claim_text, source_text, "Epic Games ships a new launcher"
    )
    assert with_headline["speaker_grounded"] is True


def test_verify_evidence_grounds_a_speaker_in_a_preceding_sentence():
    text = "Epic Games stayed quiet. The company says the update lands in June."
    result = facts.verify_evidence(
        claim(
            quote="the update lands in June",
            attribution_quote="The company says",
            speaker="Epic Games",
            speaker_type="organization",
        ),
        text,
    )
    assert result["speaker_grounded"] is True


def test_verify_evidence_rejects_an_anonymous_speaker():
    result = facts.verify_evidence(
        claim(
            quote="the plan is in motion",
            attribution_quote="four industry sources said",
            speaker="four industry sources",
            speaker_type="anonymous",
        ),
        "The plan is in motion, four industry sources said.",
    )
    assert result["anonymous"] is True
    assert result["ok"] is False
    assert result["reason"] == "speaker is anonymous"


def test_verify_evidence_rejects_an_outlet_citing_itself():
    result = facts.verify_evidence(
        claim(
            quote="the release is planned",
            attribution_quote="Eurogamer understands",
            speaker="Eurogamer",
            speaker_type="outlet",
        ),
        "Eurogamer understands the release is planned.",
        source_outlet="Eurogamer",
    )
    assert result["self_reference"] is True
    assert result["ok"] is False
    assert result["reason"] == "source cites itself"


def test_verify_evidence_rejects_a_product_as_speaker():
    result = facts.verify_evidence(
        claim(
            quote="the update is coming",
            attribution_quote="the launcher client said",
            speaker="Steam Deck",
            speaker_type="none",
        ),
        "The update is coming, the launcher client said.",
        entities=["Steam Deck"],
    )
    assert result["product"] is True
    assert result["ok"] is False
    assert result["reason"] == "speaker is a product"


def test_verify_evidence_accepts_a_first_party_own_site_post():
    text = "The Acme X1 is available now at every retailer."
    result = facts.verify_evidence(
        claim(
            quote="The Acme X1 is available now at every retailer",
            speaker="Acme Games",
            speaker_type="organization",
        ),
        text,
        "Acme X1 availability",
        source_outlet="Acme Games",
        source_tier=1,
        source_owner="Acme Games",
    )
    assert result["attribution_mode"] == "first_party_site"
    assert result["self_reference"] is False
    assert result["speaker_grounded"] is True
    assert result["ok"] is True
    assert result["reason"] == ""


def test_verify_evidence_rejects_an_unrelated_speaker_on_a_first_party_site():
    text = "The Acme X1 is available now at every retailer."
    result = facts.verify_evidence(
        claim(
            quote="The Acme X1 is available now at every retailer",
            speaker="Beta Corp",
            speaker_type="organization",
        ),
        text,
        "Acme X1 availability",
        source_outlet="Acme Games",
        source_tier=1,
        source_owner="Acme Games",
    )
    assert result["attribution_mode"] == "none"
    assert result["ok"] is False
    assert result["reason"] == "no attribution cue"


def test_verify_evidence_accepts_a_headline_announcement_on_a_tier_one_site():
    text = "The Acme X1 is available now at every retailer."
    result = facts.verify_evidence(
        claim(
            quote="The Acme X1 is available now at every retailer",
            speaker="Acme Games",
            speaker_type="organization",
        ),
        text,
        "Acme Games announces availability of the Acme X1",
        source_outlet="Wire Desk",
        source_tier=1,
        source_owner="wire-desk",
    )
    assert result["attribution_mode"] == "headline_announcement"
    assert result["speaker_grounded"] is True
    assert result["ok"] is True
    assert result["reason"] == ""


def test_verify_evidence_accepts_a_reposted_press_release_with_a_marker():
    text = "The Acme X1 is available now at every retailer."
    result = facts.verify_evidence(
        claim(
            quote="The Acme X1 is available now at every retailer",
            speaker="Acme Games",
            speaker_type="organization",
        ),
        text,
        "(PR) Acme Games announces availability of the Acme X1",
        source_outlet="Wire Desk",
        source_tier=2,
        source_owner="wire-desk",
    )
    assert result["attribution_mode"] == "headline_announcement"
    assert result["ok"] is True
    assert result["reason"] == ""


def test_verify_evidence_rejects_an_unattributed_claim_without_a_press_marker():
    text = "The Acme X1 is available now at every retailer."
    result = facts.verify_evidence(
        claim(quote="The Acme X1 is available now at every retailer"),
        text,
        "Acme Games announces availability of the Acme X1",
        source_outlet="Wire Desk",
        source_tier=2,
        source_owner="wire-desk",
    )
    assert result["attribution_mode"] == "none"
    assert result["ok"] is False
    assert result["reason"] == "no named speaker"


def test_verify_evidence_finds_an_attribution_quote_in_the_title():
    result = facts.verify_evidence(
        claim(
            quote="The X1 ships in June",
            attribution_quote="Acme Games said",
            speaker="Acme Games",
            speaker_type="organization",
        ),
        "The X1 ships in June.",
        "Acme Games said the X1 ships in June",
    )
    assert result["attribution_found"] is True
    assert result["attribution_mode"] == "explicit"
    assert result["ok"] is True


def test_press_release_markers_reads_the_config_with_a_default():
    assert "(PR)" in facts.press_release_markers()


def test_facts_stores_evidence_per_claim_and_a_per_source_summary(tmp_path):
    text = "Epic Games said Fortnite is coming to Linux. Epic Games will ship it soon."
    sources = [source(link="https://a.example/1", tier=1, name="Alpha", text=text)]
    payload = {
        "claims": [
            claim(
                claim="Fortnite is coming to Linux",
                value="coming to Linux",
                origin="epic games",
                source_url="https://a.example/1",
                quote="Fortnite is coming to Linux",
                attribution_quote="Epic Games said",
                speaker="Epic Games",
                speaker_type="organization",
            )
        ]
    }
    generate = make_generate(payload)
    sheet = facts.facts(
        gathered("ev-1", sources),
        id="ev-1",
        title="Fortnite is coming to Linux",
        entities=["Fortnite"],
        output_dir=tmp_path,
        now=NOW,
        generate=generate,
    )
    merged = sheet["claims"][0]
    assert merged["evidence"][0]["source_url"] == "https://a.example/1"
    assert merged["evidence"][0]["ok"] is True
    assert sheet["evidence"] == [
        {
            "source_url": "https://a.example/1",
            "claims": 1,
            "ok": 1,
            "failed": 0,
            "reasons": {},
            "modes": {"explicit": 1},
        }
    ]


def test_unconfirmed_sheet_carries_evidence_diagnostics(tmp_path):
    text = "A rumor circulates about the launch."
    sources = [source(link="https://a.example/1", tier=2, name="Alpha", text=text)]
    payload = {
        "claims": [
            claim(
                claim="Launch rumor",
                value="a rumor",
                kind="rumor",
                origin="unstated",
                source_url="https://a.example/1",
                quote="A rumor circulates about the launch",
                attribution_quote="A rumor circulates",
                speaker="",
                speaker_type="none",
            )
        ]
    }
    generate = make_generate(payload)
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("ev-2", sources),
            id="ev-2",
            title="Launch rumor",
            entities=["Launch"],
            output_dir=tmp_path,
            now=NOW,
            generate=generate,
        )
    sheet = json.loads((tmp_path / "unconfirmed" / "ev-2.json").read_text(encoding="utf-8"))
    assert sheet["evidence"]
    assert sheet["claims"][0]["evidence"]


# --- evidence cited by sentence id (stage 15b) --------------------------------


POINTER_TEXT = (
    "Rebel Wolves has announced that the next update adds petting. "
    "The update arrives on November 2. "
    "Analysts said the petting mode may expand later. "
    "An unrelated sentence about weather patterns follows."
)
POINTER_TITLE = "Rebel Wolves announced a petting update"


def pointer_sentences():
    return facts._numbered_sentences(POINTER_TEXT, POINTER_TITLE)


def test_numbered_sentences_numbers_the_headline_and_the_body():
    sentences = pointer_sentences()
    assert sentences[0] == POINTER_TITLE
    assert sentences[1] == "Rebel Wolves has announced that the next update adds petting."
    assert len(sentences) == 5


def test_cited_sentence_becomes_the_quote_verbatim():
    cited = facts._cite_claim_evidence(
        claim(
            claim="Rebel Wolves announced a petting update",
            value="the next update adds petting",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[3],
            attribution_ids=[],
        ),
        pointer_sentences(),
        aliases={},
    )
    assert cited["quote"] == "Analysts said the petting mode may expand later."


def test_two_cited_sentences_join_in_order():
    cited = facts._cite_claim_evidence(
        claim(
            claim="Rebel Wolves announced a petting update",
            value="the next update adds petting",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[2, 1],
            attribution_ids=[],
        ),
        pointer_sentences(),
        aliases={},
    )
    assert cited["quote"] == (
        "The update arrives on November 2. "
        "Rebel Wolves has announced that the next update adds petting."
    )


def test_sentence_ids_that_name_no_sentence_are_dropped():
    assert facts._sentence_ids([3, 3, "x", 1, 2, 4]) == [3, 1, 2]
    cited = facts._cite_claim_evidence(
        claim(
            claim="Rebel Wolves announced a petting update",
            value="the next update adds petting",
            origin="unstated",
            kind="reported",
            quote_ids=[1, "S3", 3.5, 1, 99, -2],
            attribution_ids=[99],
        ),
        pointer_sentences(),
        aliases={},
    )
    assert cited["quote"] == pointer_sentences()[1]
    assert cited["attribution_quote"] == ""


def test_cited_headline_credits_the_speaker_in_the_title():
    cited = facts._cite_claim_evidence(
        claim(
            claim="Rebel Wolves announced a petting update",
            value="the next update adds petting",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[1],
            attribution_ids=[0],
        ),
        pointer_sentences(),
        aliases={},
        # The headline sits outside the claim's quote, so the attribution
        # guard now needs the story's entities to accept it as the speaker's.
        entities=["Rebel Wolves"],
    )
    assert cited["attribution_quote"] == "Rebel Wolves announced"
    result = facts.verify_evidence(
        cited,
        POINTER_TEXT,
        POINTER_TITLE,
        "Rebel Wire",
        aliases={},
        source_tier=1,
        source_owner="rebel",
    )
    assert result["attribution_found"] is True
    assert result["ok"] is True


@pytest.mark.parametrize(
    "sentence, speaker, expected",
    [
        (
            "Rebel Wolves has announced that the next update adds petting.",
            "Rebel Wolves",
            "Rebel Wolves has announced",
        ),
        ("According to MP1st, the clips show an early build.", "MP1st", "According to MP1st"),
        (
            "The Cyberpunk movie won't follow the game, CD Projekt chief says.",
            "CD Projekt chief",
            "CD Projekt chief says",
        ),
        ("The update adds petting mode.", "Rebel Wolves", ""),
    ],
)
def test_attribution_span_is_the_shortest_cue_span_naming_the_speaker(
    sentence, speaker, expected
):
    names = facts._evidence_speaker_names(speaker, {})
    assert facts._attribution_span(sentence, names) == expected


def test_cited_sentence_that_does_not_support_the_claim_fails():
    cited = facts._cite_claim_evidence(
        claim(
            claim="PlayStation Plus adds three new monthly games",
            value="three new monthly games",
            origin="Sony",
            kind="confirmed",
            speaker="Sony",
            speaker_type="organization",
            quote_ids=[4],
            attribution_ids=[],
        ),
        pointer_sentences(),
        aliases={},
    )
    entry = facts._evidence_for_claim(
        cited,
        source(link="https://a.example/1", text=POINTER_TEXT, title=POINTER_TITLE),
        aliases={},
        entities=["Rebel Wolves"],
    )
    assert entry["ok"] is False
    assert entry["reason"] == "quote does not support claim"


def test_claim_citing_no_sentences_keeps_the_quote_missing_failure():
    cited = facts._cite_claim_evidence(
        claim(
            claim="Rebel Wolves announced a petting update",
            value="the next update adds petting",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
        ),
        pointer_sentences(),
        aliases={},
    )
    assert cited["quote"] == ""
    entry = facts._evidence_for_claim(
        cited,
        source(link="https://a.example/1", text=POINTER_TEXT, title=POINTER_TITLE),
        aliases={},
        entities=[],
    )
    assert entry["reason"] == "quote missing"


# --- per-sentence verification, title dedup and the S0 remap -----------------


def test_non_adjacent_cited_sentences_verify_separately():
    text = (
        "Rebel Wolves announced the petting update today. "
        "The studio set a release window for it. "
        "Analysts said the petting mode may expand later. "
        "An unrelated sentence about weather patterns follows. "
        "Pre-orders open on November 2, 2026."
    )
    sentences = facts._numbered_sentences(text, "")
    cited = facts._cite_claim_evidence(
        claim(
            claim="The petting mode may expand later",
            value="expand later",
            origin="Analysts",
            kind="estimate",
            quote_ids=[3, 5],
        ),
        sentences,
        aliases={},
    )
    assert cited["quote"] == sentences[3] + " " + sentences[5]
    result = facts.verify_evidence(cited, text, "", "Alpha", aliases={})
    # The joined quote is not contiguous in the source (S4 sits between S3
    # and S5), yet each cited sentence is checked on its own.
    assert result["quote_found"] is True


def test_a_cited_sentence_absent_from_the_source_fails():
    # The stored quote itself occurs in the source, so only the id rule can
    # fail this: id 99 names no sentence of the source.
    cited = claim(
        claim="Rebel Wolves announced a petting update",
        value="the next update adds petting",
        origin="Rebel Wolves",
        kind="confirmed",
        speaker="Rebel Wolves",
        speaker_type="organization",
        quote="The update arrives on November 2.",
        quote_ids=[99],
    )
    result = facts.verify_evidence(cited, POINTER_TEXT, POINTER_TITLE, "Alpha", aliases={})
    assert result["quote_found"] is False
    assert result["reason"] == "quote not found"


def test_title_repeated_as_the_first_body_sentence_verifies_after_dedup():
    title = "Rebel Wolves announced a petting update"
    text = (
        "Rebel Wolves announced a petting update. "
        "The update arrives on November 2. "
        "Analysts said the petting mode may expand later."
    )
    sentences = facts._numbered_sentences(text, title)
    # The body's copy of the headline is dropped, so S1 is the next sentence.
    assert sentences == [
        title,
        "The update arrives on November 2.",
        "Analysts said the petting mode may expand later.",
    ]
    cited = facts._cite_claim_evidence(
        claim(
            claim="Rebel Wolves announced a petting update",
            value="the next update adds petting",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[0, 1],
        ),
        sentences,
        aliases={},
    )
    assert cited["quote"] == title + " The update arrives on November 2."
    result = facts.verify_evidence(cited, text, title, "Alpha", aliases={})
    assert result["quote_found"] is True


def test_s0_only_citation_remaps_to_the_best_body_sentence():
    sentences = facts._numbered_sentences(POINTER_TEXT, POINTER_TITLE)
    cited = facts._cite_claim_evidence(
        claim(
            claim="Rebel Wolves announced a petting update",
            value="the next update adds petting",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[0],
        ),
        sentences,
        aliases={},
        entities=["Rebel Wolves"],
    )
    assert cited["remapped_from_title"] is True
    assert cited["quote_ids"] == [1]
    assert cited["quote"] == sentences[1]
    entry = facts._evidence_for_claim(
        cited,
        source(link="https://a.example/1", text=POINTER_TEXT, title=POINTER_TITLE),
        aliases={},
        entities=["Rebel Wolves"],
    )
    assert entry["remapped_from_title"] is True
    assert entry["ok"] is True


def test_s0_only_citation_keeps_the_title_and_fails_when_nothing_supports_it():
    title = "Summer sale announced for July"
    text = (
        "Summer sale announced for July. "
        "Acme Games will discount older releases during it. "
        "The sale runs from July 1 to July 15."
    )
    sentences = facts._numbered_sentences(text, title)
    cited = facts._cite_claim_evidence(
        claim(
            claim="Microsoft raises the Game Pass price",
            value="a Game Pass price increase",
            origin="Microsoft",
            kind="confirmed",
            speaker="Microsoft",
            speaker_type="organization",
            quote_ids=[0],
        ),
        sentences,
        aliases={},
        entities=["Acme Games"],
    )
    # No body sentence names the entity while sharing enough content words,
    # so the claim keeps the title citation and carries no remap flag.
    assert cited["quote_ids"] == [0]
    assert "remapped_from_title" not in cited
    assert cited["quote"] == title
    entry = facts._evidence_for_claim(
        cited,
        source(link="https://a.example/1", text=text, title=title),
        aliases={},
        entities=["Acme Games"],
    )
    assert entry["ok"] is False
    assert entry["reason"] == "quote does not support claim"
    assert "remapped_from_title" not in entry


# --- diacritic speakers in attribution spans ---------------------------------


def test_attribution_span_credits_a_diacritic_speaker_next_to_a_cue():
    names = facts._evidence_speaker_names("Michał Nowakowski", {})
    assert (
        facts._attribution_span("co-CEO Michał Nowakowski told Polish PAP Biznes.", names)
        == "Michał Nowakowski told"
    )


@pytest.mark.parametrize(
    "speaker, sentence, expected",
    [
        ("Michał Nowakowski", "Michal Nowakowski told PAP Biznes the studio delayed it.", "Michal Nowakowski told"),
        ("Michal Nowakowski", "Michał Nowakowski told PAP Biznes the studio delayed it.", "Michał Nowakowski told"),
    ],
)
def test_attribution_span_finds_a_diacritic_speaker_across_spellings(
    speaker, sentence, expected
):
    names = facts._evidence_speaker_names(speaker, {})
    assert facts._attribution_span(sentence, names) == expected


# --- attribution sentences and the primary-entity guard ----------------------


def test_detail_claim_cites_the_earlier_attributing_sentence():
    cited = facts._cite_claim_evidence(
        claim(
            claim="The update arrives on November 2",
            value="November 2",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[2],
            attribution_ids=[1],
        ),
        pointer_sentences(),
        aliases={},
        entities=["Rebel Wolves"],
    )
    assert cited["quote"] == "The update arrives on November 2."
    assert cited["attribution_quote"] == "Rebel Wolves has announced"


def test_an_attribution_sentence_sharing_no_entity_grants_no_attribution():
    text = (
        "Rebel Wolves delayed the petting update. "
        "Michał Nowakowski told Polish PAP Biznes that the studio needs more time."
    )
    sentences = facts._numbered_sentences(text)
    # The span alone would credit the speaker -- the sentence names him next
    # to a cue -- but it names no primary entity of the story, so the guard
    # keeps the attribution empty.
    names = facts._evidence_speaker_names("Michał Nowakowski", {})
    assert facts._attribution_span(sentences[2], names) == "Michał Nowakowski told"
    cited = facts._cite_claim_evidence(
        claim(
            claim="Rebel Wolves delayed the petting update",
            value="the petting update",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Michał Nowakowski",
            speaker_type="person",
            quote_ids=[1],
            attribution_ids=[2],
        ),
        sentences,
        aliases={},
        entities=["Rebel Wolves"],
    )
    assert cited["attribution_quote"] == ""


# --- v15: product speakers and the attribution proximity rule ----------------


def test_a_speaker_naming_the_story_lead_entity_is_rejected_even_as_an_organization():
    result = facts.verify_evidence(
        claim(
            quote="dawnwalker launches on playstation 5 today",
            attribution_quote="",
            speaker="Dawnwalker",
            speaker_type="organization",
        ),
        "Dawnwalker launches on PlayStation 5 today.",
        entities=["Dawnwalker"],
    )
    assert result["product"] is True
    assert result["ok"] is False
    assert result["reason"] == "speaker is a product"


def test_a_speaker_naming_a_known_console_is_rejected_even_as_an_organization():
    result = facts.verify_evidence(
        claim(
            quote="the handheld ships next month",
            attribution_quote="the launcher client said",
            speaker="Steam Deck",
            speaker_type="organization",
        ),
        "The handheld ships next month, the launcher client said.",
        entities=[],
    )
    assert result["product"] is True
    assert result["reason"] == "speaker is a product"


def test_a_lead_entity_speaker_that_owns_an_attribution_cue_is_accepted():
    cited = facts._cite_claim_evidence(
        claim(
            claim="The petting update arrives on November 2",
            value="November 2",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[1],
            attribution_ids=[1],
        ),
        pointer_sentences(),
        aliases={},
        entities=["Rebel Wolves"],
    )
    result = facts.verify_evidence(
        cited, POINTER_TEXT, POINTER_TITLE, "Alpha", aliases={}, entities=["Rebel Wolves"]
    )
    assert result["product"] is False
    assert result["ok"] is True


def test_a_known_organization_keeps_its_leading_role_speaker():
    text = "According to Valve, the handheld ships next month."
    cited = facts._cite_claim_evidence(
        claim(
            claim="The handheld ships next month",
            value="next month",
            origin="Valve",
            kind="confirmed",
            speaker="Valve",
            speaker_type="organization",
            quote_ids=[1],
            attribution_ids=[1],
        ),
        facts._numbered_sentences(text),
        aliases={},
        entities=["Valve"],
    )
    result = facts.verify_evidence(
        cited, text, "", "Alpha", aliases={}, entities=["Valve"]
    )
    assert result["product"] is False
    assert result["ok"] is True


def test_an_attribution_sentence_in_the_same_paragraph_grants_attribution():
    text = "The patch ships in December. Rebel Wolves has announced the delay."
    sentences = facts._numbered_sentences(text)
    paragraphs = facts._sentence_paragraphs(text, sentences)
    assert paragraphs == [-1, 0, 0]
    base = claim(
        claim="The patch ships in December",
        value="in December",
        origin="Rebel Wolves",
        kind="confirmed",
        speaker="Rebel Wolves",
        speaker_type="organization",
        quote_ids=[1],
        attribution_ids=[2],
    )
    # The attribution sentence follows the quote, shares no entity with the
    # claim and no two content words: only the shared paragraph credits it.
    granted = facts._cite_claim_evidence(
        base, sentences, aliases={}, entities=["Rebel Wolves"], paragraphs=paragraphs
    )
    assert granted["attribution_quote"] == "Rebel Wolves has announced"
    denied = facts._cite_claim_evidence(
        base,
        sentences,
        aliases={},
        entities=["Rebel Wolves"],
        paragraphs=[-1, 0, 1],
    )
    assert denied["attribution_quote"] == ""


def test_an_attribution_sentence_three_before_the_quote_grants_attribution():
    text = (
        "Rebel Wolves has announced the delay.\n\n"
        "A retailer listing slipped out early.\n\n"
        "The console exclusive missed its date.\n\n"
        "The patch ships in December."
    )
    sentences = facts._numbered_sentences(text)
    paragraphs = facts._sentence_paragraphs(text, sentences)
    assert paragraphs == [-1, 0, 1, 2, 3]
    cited = facts._cite_claim_evidence(
        claim(
            claim="The patch ships in December",
            value="in December",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[4],
            attribution_ids=[1],
        ),
        sentences,
        aliases={},
        entities=["Rebel Wolves"],
        paragraphs=paragraphs,
    )
    assert cited["attribution_quote"] == "Rebel Wolves has announced"


def test_an_attribution_sentence_twelve_before_the_quote_grants_none():
    fillers = "\n\n".join(
        f"Sentence number {number} about unrelated matters."
        for number in range(1, 12)
    )
    text = (
        "Rebel Wolves has announced the delay.\n\n"
        f"{fillers}\n\n"
        "The patch ships in December."
    )
    sentences = facts._numbered_sentences(text)
    paragraphs = facts._sentence_paragraphs(text, sentences)
    cited = facts._cite_claim_evidence(
        claim(
            claim="The patch ships in December",
            value="in December",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[13],
            attribution_ids=[1],
        ),
        sentences,
        aliases={},
        entities=["Rebel Wolves"],
        paragraphs=paragraphs,
    )
    assert cited["attribution_quote"] == ""


def test_an_attribution_sentence_naming_a_different_speaker_grants_none():
    text = (
        "Rebel Wolves announced the delay today. "
        "Sony said the console ships in June."
    )
    sentences = facts._numbered_sentences(text)
    paragraphs = facts._sentence_paragraphs(text, sentences)
    cited = facts._cite_claim_evidence(
        claim(
            claim="Rebel Wolves announced the delay",
            value="the delay",
            origin="Rebel Wolves",
            kind="confirmed",
            speaker="Rebel Wolves",
            speaker_type="organization",
            quote_ids=[1],
            attribution_ids=[2],
        ),
        sentences,
        aliases={},
        entities=["Rebel Wolves"],
        paragraphs=paragraphs,
    )
    assert cited["attribution_quote"] == ""


# --- surname speakers, denial cues and the citation fallbacks ----------------


def fisher_names() -> set[str]:
    return facts._evidence_speaker_names("Jeff Fisher", {}, speaker_type="person")


def test_a_trailing_source_parenthetical_stays_out_of_the_attribution_span():
    # 'says Fisher (via 4Gamers)' ends the span at the name: the source
    # parenthetical trails the speaker's words and never joins them.
    assert (
        facts._attribution_span(
            'The RTX 5090 remains the fastest card on the market," says Fisher '
            "(via 4Gamers).",
            fisher_names(),
        )
        == "says Fisher"
    )


def test_a_surname_mention_next_to_a_cue_builds_the_attribution_span():
    # The sentence names only the surname, which is a match key of the
    # full-name speaker "Jeff Fisher".
    assert (
        facts._attribution_span(
            "Here's what Fisher said about the wider delay: production continues.",
            fisher_names(),
        )
        == "Fisher said"
    )


def test_a_long_apposition_keeps_the_cue_inside_the_span_window():
    # The role phrase between the comma pair is an aside: its nineteen words
    # would push "has denied" past the cue gap, but they never count.
    span = facts._attribution_span(
        "However, Jeff Fisher, the senior vice president of Nvidia's GeForce "
        "gaming business who has held the role since 2015, has denied its "
        "demise, and the line stays on sale.",
        fisher_names(),
    )
    assert span.startswith("Jeff Fisher")
    assert span.endswith("has denied")
    assert "Fisher" in span and "denied" in span


def test_the_denial_and_response_cues_are_attribution_cues():
    cues = facts.attribution_cues()
    for cue in (
        "denied",
        "denies",
        "rejected",
        "responded",
        "replied",
        "stressed",
        "emphasized",
        "insisted",
        "acknowledged",
        "admitted",
        "warned",
        "cautioned",
        "argued",
        "described",
        "noted",
        "pointed out",
        "speaking at",
        "in a statement to",
    ):
        assert cue in cues, f"{cue!r} missing from facts.attribution_cues"
        assert facts._attribution_has_cue(f"The company {cue} the report")
    assert (
        facts._attribution_span(
            "Jeff Fisher has denied its demise", fisher_names()
        )
        == "Jeff Fisher has denied"
    )


def test_a_surname_only_mention_is_credited_to_the_full_name_speaker():
    text = (
        "Jeff Fisher said the line stays alive. "
        "Here's what Fisher said about the wider delay: production continues. "
    )
    sentences = facts._numbered_sentences(text)
    cited = facts._cite_claim_evidence(
        claim(
            claim="production continues",
            value="production continues",
            origin="Jeff Fisher",
            kind="reported",
            speaker="Jeff Fisher",
            speaker_type="person",
            quote_ids=[2],
            attribution_ids=[2],
        ),
        sentences,
        aliases={},
        paragraphs=facts._sentence_paragraphs(text, sentences),
    )
    assert cited["attribution_quote"] == "Fisher said"
    # The sheet holds one Fisher, so the surname-only span credits "Jeff
    # Fisher" and verification accepts it.
    result = facts.verify_evidence(
        cited, text, "", "Alpha", aliases={}, sheet_speakers={"Jeff Fisher"}
    )
    assert result["attribution_found"] is True
    assert result["ok"] is True
    assert result["reason"] == ""


def test_a_surname_shared_by_a_second_speaker_in_the_story_credits_no_one():
    # "Fisher said" is ambiguous with two Fishers in the story: only the full
    # name may credit its speaker, so the short attribution credits nobody.
    text = (
        "Jeff Fisher said the line stays alive. "
        "Here's what Fisher said about the wider delay: production continues. "
    )
    cited = claim(
        claim="production continues",
        value="production continues",
        quote="Here's what Fisher said about the wider delay: production continues.",
        attribution_quote="Fisher said",
        speaker="Jeff Fisher",
        speaker_type="person",
    )
    result = facts.verify_evidence(
        cited,
        text,
        "",
        "Alpha",
        aliases={},
        sheet_speakers={"Jeff Fisher", "Dana Fisher"},
    )
    assert result["attribution_found"] is False
    assert result["ok"] is False
    assert result["reason"] == "attribution quote not found"


def test_a_claim_citing_no_attribution_sentence_takes_the_attribution_from_its_quote():
    text = "The mod ships in January. Jeff Fisher said the mod ships in January."
    sentences = facts._numbered_sentences(text)
    cited = facts._cite_claim_evidence(
        claim(
            claim="the mod ships in January",
            value="in January",
            origin="Jeff Fisher",
            kind="reported",
            speaker="Jeff Fisher",
            speaker_type="person",
            quote_ids=[2],
            attribution_ids=[],
        ),
        sentences,
        aliases={},
        paragraphs=facts._sentence_paragraphs(text, sentences),
    )
    assert cited["quote"] == sentences[2]
    assert cited["attribution_quote"] == "Jeff Fisher said"


def test_a_claim_with_no_attribution_sentence_falls_back_to_the_sentence_before_its_quote():
    # The quoted sentence itself names no one and carries no cue; the sentence
    # introducing it, in the same paragraph, carries the speaker.
    text = (
        "Jeff Fisher said the line stays alive. "
        "The lineup carries the new card forward through the cycle."
    )
    sentences = facts._numbered_sentences(text)
    cited = facts._cite_claim_evidence(
        claim(
            claim="the lineup carries the new card",
            value="the new card",
            origin="Jeff Fisher",
            kind="reported",
            speaker="Jeff Fisher",
            speaker_type="person",
            quote_ids=[2],
            attribution_ids=[],
        ),
        sentences,
        aliases={},
        paragraphs=facts._sentence_paragraphs(text, sentences),
    )
    assert cited["quote"] == sentences[2]
    assert cited["attribution_quote"] == "Jeff Fisher said"


def test_an_attributed_claim_without_a_quote_borrows_the_following_sentence():
    text = (
        "Jeff Fisher said the line stays alive. "
        "The RTX 5090 line stays alive through the cycle, the executive said. "
        "Sunny weather expected tomorrow across the region."
    )
    sentences = facts._numbered_sentences(text)
    cited = facts._cite_claim_evidence(
        claim(
            claim="the RTX 5090 line stays alive",
            value="through the cycle",
            origin="Jeff Fisher",
            kind="reported",
            speaker="Jeff Fisher",
            speaker_type="person",
            quote_ids=[],
            attribution_ids=[1],
        ),
        sentences,
        aliases={},
        entities=["Nvidia"],
        paragraphs=facts._sentence_paragraphs(text, sentences),
    )
    assert cited["quote_ids"] == [2]
    assert cited["quote"] == sentences[2]
    assert cited["attribution_quote"] == "Jeff Fisher said"
    entry = facts._evidence_for_claim(
        cited,
        source(link="https://a.example/1", text=text, title=""),
        aliases={},
        entities=["Nvidia"],
    )
    assert entry["ok"] is True
    assert entry["reason"] == ""


def test_an_attributed_claim_with_no_following_sentence_keeps_quote_missing():
    # Nothing after the attribution is about the claim, so no sentence is
    # borrowed: the quote stays empty and the claim fails as quote missing.
    text = (
        "Jeff Fisher said the line stays alive. "
        "Sunny weather expected tomorrow across the region."
    )
    sentences = facts._numbered_sentences(text)
    cited = facts._cite_claim_evidence(
        claim(
            claim="the line stays alive",
            value="stays alive",
            origin="Jeff Fisher",
            kind="reported",
            speaker="Jeff Fisher",
            speaker_type="person",
            quote_ids=[],
            attribution_ids=[1],
        ),
        sentences,
        aliases={},
        entities=["Nvidia"],
        paragraphs=facts._sentence_paragraphs(text, sentences),
    )
    assert cited["quote"] == ""
    assert cited["attribution_quote"] == "Jeff Fisher said"
    entry = facts._evidence_for_claim(
        cited,
        source(link="https://a.example/1", text=text, title=""),
        aliases={},
        entities=["Nvidia"],
    )
    assert entry["reason"] == "quote missing"
    assert entry["ok"] is False


def test_a_headline_restating_claim_citing_s0_verifies_after_the_remapped_quote():
    title = "Acme Games announced a space RPG today"
    text = (
        "Acme Games announced a space RPG today. "
        "Acme Games has confirmed that the space RPG launches next spring."
    )
    sentences = facts._numbered_sentences(text, title)
    cited = facts._cite_claim_evidence(
        claim(
            claim="Acme Games announced a space RPG",
            value="the space RPG launches next spring",
            origin="Acme Games",
            kind="confirmed",
            speaker="Acme Games",
            speaker_type="organization",
            quote_ids=[0],
        ),
        sentences,
        aliases={},
        entities=["Acme Games"],
    )
    assert cited["remapped_from_title"] is True
    assert cited["quote_ids"] == [1]
    entry = facts._evidence_for_claim(
        cited,
        source(link="https://a.example/1", text=text, title=title),
        aliases={},
        entities=["Acme Games"],
    )
    assert entry["remapped_from_title"] is True
    assert entry["ok"] is True


def test_validate_claims_keeps_literal_quotes_of_an_older_caller():
    raw = claim(
        quote="Sony said the console ships in June",
        attribution_quote="Sony said",
        speaker="Sony",
        speaker_type="organization",
    )
    validated = facts.validate_claims({"claims": [raw]})[0]
    assert validated["quote"] == "Sony said the console ships in June"
    assert validated["attribution_quote"] == "Sony said"
    assert validated["quote_ids"] == []
    assert validated["attribution_ids"] == []


def test_facts_fills_evidence_from_the_cited_sentence_ids(tmp_path):
    sources = [
        source(
            link="https://a.example/1", tier=1, name="Alpha",
            text=POINTER_TEXT, title=POINTER_TITLE,
        )
    ]
    payload = {
        "claims": [
            claim(
                claim="Rebel Wolves announced a petting update",
                value="the next update adds petting",
                origin="Rebel Wolves",
                kind="confirmed",
                source_url="https://a.example/1",
                speaker="Rebel Wolves",
                speaker_type="organization",
                quote_ids=[1],
                attribution_ids=[1],
            )
        ]
    }
    sheet = facts.facts(
        gathered("pc-1", sources),
        id="pc-1",
        title=POINTER_TITLE,
        entities=["Rebel Wolves"],
        output_dir=tmp_path,
        now=NOW,
        generate=make_generate(payload),
    )
    evidence = sheet["claims"][0]["evidence"][0]
    assert evidence["quote"] == "Rebel Wolves has announced that the next update adds petting."
    assert evidence["attribution_quote"] == "Rebel Wolves has announced"
    assert evidence["ok"] is True


# --- code-set source URLs, grouping guards and evidence confirmation --------- -


def use_mode(monkeypatch, mode):
    """Force ``facts`` to read ``facts.confirmation_mode`` as ``mode``."""
    real = facts.load_config

    def load(path=None):
        config = real(path)
        config.setdefault("facts", {})["confirmation_mode"] = mode
        return config

    monkeypatch.setattr(facts, "load_config", load)


def grouping_generate(payloads, groups):
    """A generate stub that replays extraction payloads and answers grouping.

    ``groups`` is a list of id lists; each is wrapped as the ``{"ids": [...]}``
    object the grouping schema requires.
    """
    calls: list[str] = []
    extraction: list[int] = []
    wrapped = [{"ids": list(ids)} for ids in groups]

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        if is_grouping_call(json_schema):
            value = {"groups": wrapped}
            calls.append("group")
        else:
            value = payloads[len(extraction)]
            extraction.append(1)
            calls.append(role)
        if run_state is not None:
            run_state.record(Generation(role=role, provider="mock", model="m", family="f", value=value))
        return Generation(role=role, provider="mock", model="m", family="f", value=value, fallbacks=[])

    return generate, calls


ACME_TITLE = "Acme Arena is Acme Games' fastest-selling game"
ACME_ENTITIES = ["Acme Arena", "Acme Games"]
ACME_TEXT = (
    "Acme Games announced Acme Arena is the fastest-selling game in its history. "
    "Acme Games said the milestone came in its first week."
)


def acme_claim(url, **overrides):
    payload = {
        "claim": "Acme Arena is Acme Games' fastest-selling game",
        "value": "fastest-selling game",
        "source_url": url,
        "confidence": 0.9,
        "origin": "Acme Games",
        "kind": "confirmed",
        "affiliation": "",
        "quote": "Acme Arena is the fastest-selling game",
        "attribution_quote": "Acme Games announced",
        "speaker": "Acme Games",
        "speaker_type": "organization",
    }
    payload.update(overrides)
    return payload


def acme_scenario(count=3, owners=None, text=ACME_TEXT):
    owners = owners or [f"owner{index}" for index in range(count)]
    sources, payloads = [], []
    for index in range(count):
        url = f"https://outlet{index}.example/acme"
        sources.append(
            source(link=url, name=f"Outlet {index}", owner=owners[index], tier=2, title=ACME_TITLE, text=text)
        )
        payloads.append({"claims": [acme_claim(url)]})
    return sources, payloads


def ev_claim(
    url,
    *,
    speaker="Acme Games",
    speaker_type="organization",
    kind="confirmed",
    ok=True,
    affiliation="",
):
    return {
        "source_url": url,
        "speaker": speaker,
        "speaker_type": speaker_type,
        "kind": kind,
        "origin": speaker,
        "affiliation": affiliation,
        "evidence": {"ok": ok},
    }


def by_url_map(*pairs):
    return {facts._source_url_key(url): {"owner": owner} for url, owner in pairs}


def test_validate_claims_overrides_a_misspelled_source_url():
    claims = facts.validate_claims(
        {"claims": [acme_claim("https://outlet0.example/acme/typo")]},
        source_url="https://outlet0.example/acme",
    )
    assert claims[0]["source_url"] == "https://outlet0.example/acme"


def test_facts_sets_every_claim_source_url_from_the_extraction_call(tmp_path):
    src = source(link="https://a.example/real", tier=1, text="Acme Games said the game is out.")
    payload = {
        "claims": [
            claim(
                claim="The game is out", value="out",
                source_url="https://a.example/reel",  # the model misspells the URL
                origin="acme games", kind="reported",
            )
        ]
    }
    result = facts.facts(
        gathered("u-1", [src]), id="u-1", title="The game is out",
        output_dir=tmp_path, now=NOW, generate=make_generate(payload),
    )
    assert result["claims"][0]["source_urls"] == ["https://a.example/real"]


def test_build_grouping_prompt_lists_ids_and_clips_labels():
    items = [
        {"id": "c0", "source_id": "s0", "claim": {"claim": "x" * 500, "value": "y"}},
        {"id": "c1", "source_id": "s1", "claim": {"claim": "short", "value": "z"}},
    ]
    prompt = facts.build_grouping_prompt(items)
    assert "- c0 | s0 | " in prompt
    assert "- c1 | s1 | short z" in prompt
    assert "x" * (facts.GROUPING_LABEL_CHARS + 1) not in prompt


def test_parse_groups_rejects_a_malformed_response():
    with pytest.raises(facts.FactsError):
        facts.parse_groups({"nope": []})
    with pytest.raises(facts.FactsError):
        facts.parse_groups({"groups": [{"ids": "not-a-list"}]})


def test_group_reason_drops_a_single_source_group():
    a = acme_claim("https://a.example/1")
    b = acme_claim("https://a.example/1")
    assert "one source" in facts.group_reason([a, b], ACME_ENTITIES)


def test_group_reason_keeps_a_group_when_a_source_title_names_the_entity():
    a = acme_claim("https://a.example/1")
    b = acme_claim(
        "https://b.example/1",
        claim="The studio opens its doors this year",
        value="opens its doors",
    )
    items = facts.grouping_items([a, b], [source(link="https://a.example/1"), source(link="https://b.example/1")])
    accepted, dropped = facts.apply_group_guards(
        [["c0", "c1"]],
        items,
        ACME_ENTITIES,
        {"https://a.example/1": "", "https://b.example/1": "Acme Arena opens its doors"},
    )
    assert [group["ids"] for group in accepted] == [["c0", "c1"]]
    assert dropped == []


def test_group_reason_drops_when_no_title_supports_the_omitting_member():
    a = acme_claim("https://a.example/1")
    b = acme_claim(
        "https://b.example/1",
        claim="The studio opens its doors this year",
        value="opens its doors",
    )
    items = facts.grouping_items([a, b], [source(link="https://a.example/1"), source(link="https://b.example/1")])
    accepted, dropped = facts.apply_group_guards(
        [["c0", "c1"]],
        items,
        ACME_ENTITIES,
        {"https://a.example/1": "", "https://b.example/1": "Doors open this week"},
    )
    assert accepted == []
    assert dropped == [{"ids": ["c0", "c1"], "reason": "the claims name no shared primary entity"}]


def test_group_reason_keeps_a_member_omitting_the_name_when_others_share_words():
    a = acme_claim(
        "https://a.example/1",
        claim="Acme Arena opens a new fantasy arena this year",
        value="a new fantasy arena",
    )
    b = acme_claim(
        "https://b.example/1",
        claim="New fantasy arena opens its doors this year",
        value="opens its doors",
    )
    assert facts.group_reason([a, b], ACME_ENTITIES) == ""


def test_group_reason_drops_two_different_facts_sharing_fewer_than_three_words():
    a = acme_claim(
        "https://a.example/1",
        claim="Acme Arena sold 2 million copies",
        value="2 million copies",
    )
    b = acme_claim(
        "https://b.example/1",
        claim="The publisher lays off 300 staff worldwide",
        value="300 staff worldwide",
    )
    assert "shared primary entity" in facts.group_reason([a, b], ACME_ENTITIES)


def test_group_reason_drops_a_group_where_no_member_is_supported_by_any_path():
    a = acme_claim("https://a.example/1", claim="A quiet week in the industry", value="a quiet week")
    b = acme_claim("https://b.example/1", value="another quiet week")
    b = {**b, "claim": "Another quiet week elsewhere"}
    assert "shared primary entity" in facts.group_reason([a, b], ACME_ENTITIES)


def test_group_reason_drops_conflicting_figures():
    a = acme_claim("https://a.example/1", claim="Acme Arena sold 2 million", value="2 million copies")
    b = acme_claim("https://b.example/1", claim="Acme Arena sold 3 million", value="3 million copies")
    assert "conflicting" in facts.group_reason([a, b], ["Acme Arena"])


def test_group_reason_drops_a_rumor_mixed_with_a_confirmed_claim():
    a = acme_claim("https://a.example/1")
    b = acme_claim("https://b.example/1", kind="rumor")
    assert "rumor or opinion" in facts.group_reason([a, b], ACME_ENTITIES)


def test_apply_group_guards_ignores_unknown_ids_and_dedupes():
    items = facts.grouping_items(
        [acme_claim("https://a.example/1"), acme_claim("https://b.example/1")],
        [source(link="https://a.example/1"), source(link="https://b.example/1", owner="co-b")],
    )
    accepted, dropped = facts.apply_group_guards(
        [["c0", "c1", "zz"], ["c0", "c1"]], items, ACME_ENTITIES
    )
    assert [group["ids"] for group in accepted] == [["c0", "c1"]]
    assert dropped == [{"ids": ["c0", "c1"], "reason": "fewer than two usable claims"}]


def test_fact_groups_sheet_exposes_ids_and_reasons():
    record = {
        "groups": [{"ids": ["c0", "c1"], "claims": []}],
        "dropped": [{"ids": ["c2", "c3"], "reason": "nope"}],
        "claims": [{"id": "c0", "source_id": "s0", "label": "x"}],
        "failed": None,
    }
    sheet = facts.fact_groups_sheet(record)
    assert sheet["accepted"] == [{"ids": ["c0", "c1"]}]
    assert sheet["dropped"] == [{"ids": ["c2", "c3"], "reason": "nope"}]
    assert sheet["claims"] == [{"id": "c0", "source_id": "s0", "label": "x"}]
    assert sheet["failed"] is None


def test_evidence_confirmation_first_party_counts_distinct_owners_once():
    by_url = by_url_map(
        ("https://a/1", "co-a"), ("https://b/1", "co-b"), ("https://c/1", "co-b")
    )
    claims = [ev_claim("https://a/1"), ev_claim("https://b/1"), ev_claim("https://c/1")]
    result = facts.evidence_confirmation(claims, by_url, aliases={})
    assert result["confirmed"] is True
    assert result["rule"] == "first-party"
    assert result["first_party_organizations"] == ["acme games"]
    assert result["first_party_sources"] == 2


def test_evidence_confirmation_needs_enough_owners():
    by_url = by_url_map(("https://a/1", "co-a"))
    result = facts.evidence_confirmation([ev_claim("https://a/1")], by_url, aliases={})
    assert result["confirmed"] is False
    assert result["rule"] == "unconfirmed"


def test_evidence_confirmation_ignores_failed_evidence():
    by_url = by_url_map(("https://a/1", "co-a"), ("https://b/1", "co-b"))
    claims = [ev_claim("https://a/1", ok=False), ev_claim("https://b/1", ok=True)]
    result = facts.evidence_confirmation(claims, by_url, aliases={})
    assert result["confirmed"] is False


def test_evidence_confirmation_ignores_anonymous_speakers():
    by_url = by_url_map(("https://a/1", "co-a"), ("https://b/1", "co-b"))
    claims = [
        ev_claim("https://a/1", speaker="people familiar with the matter", speaker_type="none"),
        ev_claim("https://b/1", speaker="people familiar with the matter", speaker_type="none"),
    ]
    result = facts.evidence_confirmation(claims, by_url, aliases={})
    assert result["confirmed"] is False


def test_evidence_confirmation_ignores_an_outlet_citing_itself():
    by_url = {
        facts._source_url_key("https://a/1"): {"owner": "co-a", "source_name": "Outlet One"},
        facts._source_url_key("https://b/1"): {"owner": "co-b", "source_name": "Outlet Two"},
    }
    claims = [
        ev_claim("https://a/1", speaker="Outlet One", speaker_type="outlet"),
        ev_claim("https://b/1", speaker="Outlet Two", speaker_type="outlet"),
    ]
    result = facts.evidence_confirmation(claims, by_url, aliases={})
    assert result["confirmed"] is False


def test_evidence_confirmation_two_independent_origins():
    by_url = by_url_map(("https://a/1", "co-a"), ("https://b/1", "co-b"))
    claims = [
        ev_claim("https://a/1", speaker="Jane Coder", speaker_type="person", kind="reported"),
        ev_claim("https://b/1", speaker="John Artist", speaker_type="person", kind="reported"),
    ]
    result = facts.evidence_confirmation(claims, by_url, aliases={})
    assert result["confirmed"] is True
    assert result["rule"] == "two-independent-origins"


def test_evidence_confirmation_collapses_one_org_spelled_two_ways_by_alias():
    # "Remedy" is an alias of "Remedy Entertainment" (data/orgs.json), so the two
    # spellings are one speaker and confirm through first-party, not
    # two-independent-origins.
    by_url = by_url_map(("https://a/1", "co-a"), ("https://b/1", "co-b"))
    claims = [
        ev_claim("https://a/1", speaker="Remedy", speaker_type="organization"),
        ev_claim("https://b/1", speaker="Remedy Entertainment", speaker_type="organization"),
    ]
    result = facts.evidence_confirmation(claims, by_url)
    assert result["confirmed"] is True
    assert result["rule"] == "first-party"
    assert result["first_party_organizations"] == ["remedy entertainment"]
    assert result["first_party_sources"] == 2
    assert result["origins"] == ["remedy entertainment"]


def test_evidence_confirmation_collapses_contained_names_not_in_the_files():
    # Neither name is in data/orgs.json or data/origins.json; the whole-word
    # containment rule still makes them one identity.
    by_url = by_url_map(("https://a/1", "co-a"), ("https://b/1", "co-b"))
    claims = [
        ev_claim("https://a/1", speaker="Acme", speaker_type="organization"),
        ev_claim("https://b/1", speaker="Acme Games", speaker_type="organization"),
    ]
    result = facts.evidence_confirmation(claims, by_url, aliases={})
    assert result["confirmed"] is True
    assert result["rule"] == "first-party"
    assert result["first_party_organizations"] == ["acme"]


def test_evidence_confirmation_keeps_distinct_organizations_distinct():
    by_url = by_url_map(("https://a/1", "co-a"), ("https://b/1", "co-b"))
    claims = [
        ev_claim("https://a/1", speaker="Acme Games", speaker_type="organization"),
        ev_claim("https://b/1", speaker="Beta Corp", speaker_type="organization"),
    ]
    result = facts.evidence_confirmation(claims, by_url, aliases={})
    assert result["confirmed"] is True
    assert result["rule"] == "two-independent-origins"
    assert result["origins"] == ["acme games", "beta corp"]


def test_evidence_confirmation_folds_a_person_into_their_employer_only_with_affiliation():
    by_url = by_url_map(("https://a/1", "co-a"), ("https://b/1", "co-b"))
    with_affiliation = [
        ev_claim(
            "https://a/1", speaker="Jane Coder", speaker_type="person",
            kind="reported", affiliation="Acme Games",
        ),
        ev_claim("https://b/1", speaker="Acme Games", speaker_type="organization"),
    ]
    folded = facts.evidence_confirmation(with_affiliation, by_url, aliases={})
    assert folded["origins"] == ["acme games"]
    assert folded["confirmed"] is False

    without_affiliation = [
        ev_claim("https://a/1", speaker="Jane Coder", speaker_type="person", kind="reported"),
        ev_claim("https://b/1", speaker="Acme Games", speaker_type="organization"),
    ]
    distinct = facts.evidence_confirmation(without_affiliation, by_url, aliases={})
    assert distinct["origins"] == ["acme games", "jane coder"]
    assert distinct["confirmed"] is True
    assert distinct["rule"] == "two-independent-origins"


def test_evidence_confirmation_two_spellings_from_one_owner_count_once():
    by_url = by_url_map(("https://a/1", "co-a"), ("https://b/1", "co-a"))
    claims = [
        ev_claim("https://a/1", speaker="Remedy", speaker_type="organization"),
        ev_claim("https://b/1", speaker="Remedy Entertainment", speaker_type="organization"),
    ]
    result = facts.evidence_confirmation(claims, by_url)
    assert result["confirmed"] is False
    assert result["origins"] == ["remedy entertainment"]


def test_evidence_confirmation_tier1_short_circuits():
    result = facts.evidence_confirmation([], {}, tier1_confirms=True, aliases={})
    assert result["confirmed"] is True
    assert result["rule"] == "tier1"


def test_evidence_mode_lets_the_evidence_verdict_decide(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    text_a = "Jane Coder said Acme Arena is the fastest-selling game in its history."
    text_b = "John Artist said Acme Arena is the fastest-selling game in its history."
    sources = [
        source(link="https://a.example/1", name="Alpha", owner="co-a", tier=2, title=ACME_TITLE, text=text_a),
        source(link="https://b.example/2", name="Beta", owner="co-b", tier=2, title=ACME_TITLE, text=text_b),
    ]
    payloads = [
        {"claims": [acme_claim("https://a.example/1", origin="unstated", kind="reported",
                               speaker="Jane Coder", speaker_type="person",
                               attribution_quote="Jane Coder said")]},
        {"claims": [acme_claim("https://b.example/2", origin="unstated", kind="reported",
                               speaker="John Artist", speaker_type="person",
                               attribution_quote="John Artist said")]},
    ]
    generate, _calls = grouping_generate(payloads, [["c0", "c1"]])
    result = facts.facts(
        gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
        entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
    )
    assert result["confirmation"]["rule"] == "two-independent-origins"
    assert result["confirmation"]["rule_source"] == "evidence"
    assert result["confirmation"]["confirmed"] is True


def test_evidence_mode_confirms_a_first_party_restatement_across_sources(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    sources, payloads = acme_scenario(3)
    generate, calls = grouping_generate(payloads, [["c0", "c1", "c2"]])
    result = facts.facts(
        gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
        entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
    )
    assert result["confirmation"]["rule"] == "first-party"
    assert result["confirmation"]["confirmed"] is True
    assert result["confirmation"]["rule_source"] == "evidence"
    assert result["confirmation"]["first_party_organizations"] == ["acme games"]
    assert result["fact_groups"]["accepted"] == [{"ids": ["c0", "c1", "c2"]}]
    assert calls == ["fast", "fast", "fast", "group"]


def test_evidence_mode_first_party_confirms_two_owners_of_the_same_organization(monkeypatch, tmp_path):
    # Positive path: two sources from two different owners each carry a claim in
    # the central group whose verbatim quote and attribution quote are present
    # in their synthetic texts and whose speaker is the same organization. The
    # rule is "first-party", rule_source "evidence", verified_claims 2.
    use_mode(monkeypatch, "evidence")
    sources, payloads = acme_scenario(2, owners=["co-a", "co-b"])
    generate, calls = grouping_generate(payloads, [["c0", "c1"]])
    result = facts.facts(
        gathered("acme-first-party", sources), id="acme-first-party", title=ACME_TITLE,
        entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
    )
    assert result["confirmation"]["confirmed"] is True
    assert result["confirmation"]["rule"] == "first-party"
    assert result["confirmation"]["rule_source"] == "evidence"
    assert result["confirmation"]["verified_claims"] == 2
    assert result["confirmation"]["first_party_organizations"] == ["acme games"]
    assert result["confirmation"]["reason"] == (
        "2 independent sources attribute the central fact to acme games with verified evidence"
    )
    assert calls == ["fast", "fast", "group"]


def test_evidence_mode_two_different_organizations_confirm_as_two_origins(monkeypatch, tmp_path):
    # Variant: the two verified speakers are different organizations, so the rule
    # is "two-independent-origins", rule_source "evidence", verified_claims 2.
    use_mode(monkeypatch, "evidence")
    sources = [
        source(link="https://a.example/1", name="Outlet A", owner="co-a", tier=2,
               title=ACME_TITLE, text=ACME_TEXT),
        source(link="https://b.example/2", name="Outlet B", owner="co-b", tier=2,
               title=ACME_TITLE,
               text="Beta Studios announced Acme Arena is the fastest-selling game in its history."),
    ]
    payloads = [
        {"claims": [acme_claim("https://a.example/1", speaker="Acme Games")]},
        {"claims": [acme_claim("https://b.example/2", speaker="Beta Studios",
                               attribution_quote="Beta Studios announced")]},
    ]
    generate, calls = grouping_generate(payloads, [["c0", "c1"]])
    result = facts.facts(
        gathered("acme-two-orgs", sources), id="acme-two-orgs", title=ACME_TITLE,
        entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
    )
    assert result["confirmation"]["confirmed"] is True
    assert result["confirmation"]["rule"] == "two-independent-origins"
    assert result["confirmation"]["rule_source"] == "evidence"
    assert result["confirmation"]["verified_claims"] == 2
    assert result["confirmation"]["origins"] == ["acme games", "beta studios"]
    assert result["confirmation"]["reason"] == (
        "two independent named origins state the central fact"
    )
    assert calls == ["fast", "fast", "group"]


def test_evidence_mode_same_owner_is_not_two_origins(monkeypatch, tmp_path):
    # Variant: the two sources share an owner, so the two verified claims count
    # as one origin and the story does not confirm. The rule is "unconfirmed"
    # (rule_source "evidence", verified_claims 2).
    use_mode(monkeypatch, "evidence")
    sources, payloads = acme_scenario(2, owners=["co-a", "co-a"])
    generate, calls = grouping_generate(payloads, [["c0", "c1"]])
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-shared-owner", sources), id="acme-shared-owner", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
        )
    assert calls == ["fast", "fast", "group"]
    sheet = json.loads((tmp_path / "unconfirmed" / "acme-shared-owner.json").read_text(encoding="utf-8"))
    assert sheet["confirmation"]["confirmed"] is False
    assert sheet["confirmation"]["rule"] == "unconfirmed"
    assert sheet["confirmation"]["rule_source"] == "evidence"
    assert sheet["confirmation"]["verified_claims"] == 2
    assert sheet["confirmation"]["first_party_organizations"] == []
    assert sheet["confirmation"]["reason"] == (
        "the central fact is not backed by enough verified cross-source evidence"
    )
    assert not (tmp_path / "acme-shared-owner.json").exists()


def test_evidence_mode_needs_enough_verified_sources(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    sources, payloads = acme_scenario(3)
    payloads[1] = {"claims": [acme_claim(sources[1]["link"], quote="a quote not in the text")]}
    payloads[2] = {"claims": [acme_claim(sources[2]["link"], quote="another missing quote")]}
    generate, _calls = grouping_generate(payloads, [["c0", "c1", "c2"]])
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
        )
    sheet = json.loads((tmp_path / "unconfirmed" / "acme-1.json").read_text(encoding="utf-8"))
    assert sheet["confirmation"]["rule"] == "unconfirmed"
    assert sheet["confirmation"]["first_party_organizations"] == []


def test_evidence_mode_counts_a_shared_owner_once(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    sources, payloads = acme_scenario(2, owners=["co-a", "co-a"])
    generate, _calls = grouping_generate(payloads, [["c0", "c1"]])
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
        )


def test_evidence_mode_ignores_a_different_fact_by_the_same_org(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    sources, payloads = acme_scenario(2)
    payloads[1] = {
        "claims": [
            acme_claim(
                sources[1]["link"],
                claim="Acme Arena has been delayed",
                value="delayed",
                quote="the milestone came in its first week",
                attribution_quote="Acme Games said",
            )
        ]
    }
    generate, _calls = grouping_generate(payloads, [])
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
        )


def test_evidence_mode_rejects_a_leak_based_single_source(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    text = "A person familiar with the matter said Acme Arena is coming to Xbox."
    src = source(link="https://a.example/1", name="Alpha", tier=2, title=ACME_TITLE, text=text)
    payload = {
        "claims": [
            acme_claim(
                "https://a.example/1", claim="Acme Arena is coming to Xbox",
                value="coming to Xbox", kind="rumor", origin="unstated",
                quote="Acme Arena is coming to Xbox",
                attribution_quote="A person familiar with the matter said",
                speaker="a person familiar with the matter", speaker_type="none",
            )
        ]
    }
    generate, _calls = grouping_generate([payload], [])
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-1", [src]), id="acme-1", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
        )


def test_grouping_failure_records_the_reason_and_continues(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    sources, payloads = acme_scenario(2, owners=["co-a", "co-a"])

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        if is_grouping_call(json_schema):
            raise RuntimeError("grouping transport down")
        value = payloads.pop(0)
        if run_state is not None:
            run_state.record(Generation(role=role, provider="mock", model="m", family="f", value=value))
        return Generation(role=role, provider="mock", model="m", family="f", value=value, fallbacks=[])

    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
        )
    sheet = json.loads((tmp_path / "unconfirmed" / "acme-1.json").read_text(encoding="utf-8"))
    assert "grouping call failed" in sheet["fact_groups"]["failed"]
    assert sheet["confirmation"]["confirmed"] is False


def test_grouping_drops_a_group_with_conflicting_figures(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    text_a = "Acme Games said Acme Arena sold 2 million copies."
    text_b = "Acme Games said Acme Arena sold 3 million copies."
    sources = [
        source(link="https://a.example/1", name="Alpha", owner="co-a", tier=2, title=ACME_TITLE, text=text_a),
        source(link="https://b.example/2", name="Beta", owner="co-b", tier=2, title=ACME_TITLE, text=text_b),
    ]
    payloads = [
        {"claims": [acme_claim("https://a.example/1", claim="Acme Arena sold 2 million copies",
                               value="2 million copies", quote="Acme Arena sold 2 million copies")]},
        {"claims": [acme_claim("https://b.example/2", claim="Acme Arena sold 3 million copies",
                               value="3 million copies", quote="Acme Arena sold 3 million copies")]},
    ]
    generate, _calls = grouping_generate(payloads, [["c0", "c1"]])
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
        )
    sheet = json.loads((tmp_path / "unconfirmed" / "acme-1.json").read_text(encoding="utf-8"))
    assert sheet["fact_groups"]["accepted"] == []
    assert any("conflicting" in entry["reason"] for entry in sheet["fact_groups"]["dropped"])


def test_shadow_mode_stores_both_verdicts_and_logs_disagreement(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.INFO)
    use_mode(monkeypatch, "shadow")
    sources, payloads = acme_scenario(2)
    # No verbatim quote: legacy confirms from the origin, the evidence rule does
    # not, so the two verdicts disagree.
    for payload in payloads:
        payload["claims"][0]["quote"] = ""
        payload["claims"][0]["attribution_quote"] = ""
    generate, _calls = grouping_generate(payloads, [])
    result = facts.facts(
        gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
        entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
    )
    assert result["confirmation_legacy"]["confirmed"] is True
    assert result["confirmation_evidence"]["confirmed"] is False
    assert result["confirmation"]["confirmed"] is True, "shadow mode lets legacy decide"
    assert any("DISAGREE" in record.message for record in caplog.records)


def test_legacy_mode_does_not_run_grouping(monkeypatch, tmp_path):
    use_mode(monkeypatch, "legacy")
    sources, payloads = acme_scenario(2)
    generate, calls = grouping_generate(payloads, [["c0", "c1"]])
    result = facts.facts(
        gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
        entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
    )
    assert calls == ["fast", "fast"]
    assert "fact_groups" not in result


def test_grouping_is_skipped_when_the_budget_is_spent(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    sources, payloads = acme_scenario(2)
    for payload in payloads:
        payload["claims"][0]["quote"] = ""
        payload["claims"][0]["attribution_quote"] = ""
    generate, calls = grouping_generate(payloads, [["c0", "c1"]])
    run_state = RunState()
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
            entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
            run_state=run_state, model_call_budget=2,
        )
    assert calls == ["fast", "fast"]
    assert run_state.calls == 2
    sheet = json.loads((tmp_path / "unconfirmed" / "acme-1.json").read_text(encoding="utf-8"))
    assert "budget" in sheet["fact_groups"]["failed"]


def test_grouping_call_counts_toward_the_model_calls(monkeypatch, tmp_path):
    use_mode(monkeypatch, "evidence")
    sources, payloads = acme_scenario(3)
    generate, calls = grouping_generate(payloads, [["c0", "c1", "c2"]])
    run_state = RunState()
    result = facts.facts(
        gathered("acme-1", sources), id="acme-1", title=ACME_TITLE,
        entities=ACME_ENTITIES, output_dir=tmp_path, now=NOW, generate=generate,
        run_state=run_state, model_call_budget=4,
    )
    assert calls == ["fast", "fast", "fast", "group"]
    assert run_state.calls == 4
    assert result["confirmation"]["confirmed"] is True


@pytest.mark.parametrize("mode", facts.CONFIRMATION_MODES)
def test_arc_raiders_fixture_stays_unconfirmed_in_every_mode(monkeypatch, tmp_path, mode):
    use_mode(monkeypatch, mode)
    fixture, data, generate = load_arc_fixture()
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            data, id=fixture["id"], title=ARC_TITLE, output_dir=tmp_path, now=NOW,
            generate=generate,
        )


@pytest.mark.parametrize("mode", facts.CONFIRMATION_MODES)
def test_dmz_feature_bullets_survive_every_mode(monkeypatch, tmp_path, mode):
    use_mode(monkeypatch, mode)
    src = source(
        link="https://blog.playstation.com/dmz", name="PlayStation Blog", owner="sony",
        tier=1, text=DMZ_FEATURE_BULLETS,
    )
    payload = {
        "claims": [
            claim(claim="DMZ has base stations that capture territory", value="base stations", source_url="https://blog.playstation.com/dmz"),
            claim(claim="DMZ offers three deployment options", value="on foot, by vehicle, or from the air", source_url="https://blog.playstation.com/dmz"),
            claim(claim="DMZ Cash buys deployables", value="in-game DMZ Cash", source_url="https://blog.playstation.com/dmz"),
        ]
    }
    result = facts.facts(
        gathered("dmz-1", [src]), id="dmz-1", output_dir=tmp_path, now=NOW,
        generate=make_generate(payload),
    )
    assert [c["claim"] for c in result["claims"]] == [
        "DMZ has base stations that capture territory",
        "DMZ offers three deployment options",
        "DMZ Cash buys deployables",
    ]
    assert result["confirmation"]["confirmed"] is True


@pytest.mark.parametrize("mode", facts.CONFIRMATION_MODES)
def test_tokon_tier1_story_confirms_in_every_mode(monkeypatch, tmp_path, mode):
    use_mode(monkeypatch, mode)
    ps_blog = source(
        link=TOKON_PS_URL, name="PlayStation Blog", owner="sony", tier=1, title=TOKON_TITLE,
        text=(
            "Marvel Tōkon: Fighting Souls unveils its Year 1 DLC roster. "
            "Phoenix Cyclops joins Marvel Tōkon: Fighting Souls on November 2. "
            "The PlayStation Blog confirmed the roster."
        ),
    )
    payload = {
        "claims": [
            claim(
                claim="Phoenix Cyclops joins Marvel Tōkon: Fighting Souls on November 2",
                value="Phoenix Cyclops joins on November 2",
                source_url=TOKON_PS_URL, origin="PlayStation Blog", kind="confirmed",
                quote="Phoenix Cyclops joins Marvel Tōkon: Fighting Souls on November 2",
                attribution_quote="The PlayStation Blog confirmed",
                speaker="PlayStation Blog", speaker_type="organization",
            )
        ]
    }
    result = facts.facts(
        gathered("tokon-1", [ps_blog]), id="tokon-1", title=TOKON_TITLE,
        entities=["Marvel Tōkon: Fighting Souls", "Phoenix Cyclops"],
        output_dir=tmp_path, now=NOW, generate=make_generate(payload),
    )
    assert result["confirmation"]["confirmed"] is True


# --- apostrophe normalization in coherence, persisted evidence and first-party -


CANCELLED_DESTINY_CLAIM = (
    "Screenshots reveal cancelled Assassin{apostrophe}s Creed Destiny game set in China"
)
ASSASSIN_ENTITIES = ["assassin creed", "qin dynasty china"]


@pytest.mark.parametrize("apostrophe", ["'", "\u2019"])
def test_coherence_keeps_the_cancelled_assassins_creed_claim(apostrophe):
    claims = [
        claim(
            claim=CANCELLED_DESTINY_CLAIM.format(apostrophe=apostrophe),
            value="cancelled game set in China",
            source_url="https://a.example/1",
        ),
        claim(
            claim="Mario Kart is getting a new cup",
            value="Mario Kart",
            source_url="https://a.example/1",
        ),
    ]
    sources = [source(link="https://a.example/1", name="Alpha", title="", text="body text")]
    kept, dropped, primary = facts.coherence_check(claims, sources, entities=ASSASSIN_ENTITIES)
    assert [entry["claim"] for entry in kept] == [claims[0]["claim"]]
    assert [entry["claim"] for entry in dropped] == ["Mario Kart is getting a new cup"]
    assert "assassin creed" in primary


def test_saved_evidence_entries_carry_speaker_and_quote_fields(tmp_path):
    text = "Epic Games said Fortnite is coming to Linux. Epic Games will ship it soon."
    sources = [source(link="https://a.example/1", tier=1, name="Alpha", text=text)]
    payload = {
        "claims": [
            claim(
                claim="Fortnite is coming to Linux",
                value="coming to Linux",
                origin="epic games",
                source_url="https://a.example/1",
                quote="Fortnite is coming to Linux",
                attribution_quote="Epic Games said",
                speaker="Epic Games",
                speaker_type="organization",
            )
        ]
    }
    sheet = facts.facts(
        gathered("ev-fields-1", sources),
        id="ev-fields-1",
        title="Fortnite is coming to Linux",
        entities=["Fortnite"],
        output_dir=tmp_path,
        now=NOW,
        generate=make_generate(payload),
    )
    entry = sheet["claims"][0]["evidence"][0]
    assert entry["speaker"] == "Epic Games"
    assert entry["speaker_type"] == "organization"
    assert entry["quote"] == "Fortnite is coming to Linux"
    assert entry["attribution_quote"] == "Epic Games said"


def test_unconfirmed_evidence_carries_fields_and_truncates_a_long_quote(tmp_path):
    quote = "a" * 300
    sources = [source(link="https://a.example/1", tier=2, name="Alpha", text=quote + ".")]
    payload = {
        "claims": [
            claim(
                claim="Launch rumor",
                value="a rumor",
                kind="rumor",
                origin="unstated",
                source_url="https://a.example/1",
                quote=quote,
                attribution_quote="",
                speaker="",
                speaker_type="none",
            )
        ]
    }
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(
            gathered("ev-fields-2", sources),
            id="ev-fields-2",
            title="Launch rumor",
            entities=["Launch"],
            output_dir=tmp_path,
            now=NOW,
            generate=make_generate(payload),
        )
    sheet = json.loads((tmp_path / "unconfirmed" / "ev-fields-2.json").read_text(encoding="utf-8"))
    entry = sheet["claims"][0]["evidence"][0]
    assert entry["speaker"] == ""
    assert entry["speaker_type"] == "none"
    assert entry["attribution_quote"] == ""
    assert entry["quote"] == "a" * facts.EVIDENCE_FIELD_CHARS
    assert len(entry["quote"]) == 200


def first_party_claim(
    url,
    speaker,
    *,
    owner,
    outlet,
    quote,
    speaker_type="organization",
    kind="confirmed",
    tier=1,
):
    evidence = facts.verify_evidence(
        {
            "quote": quote,
            "attribution_quote": "",
            "speaker": speaker,
            "speaker_type": speaker_type,
        },
        quote + " More body text.",
        source_outlet=outlet,
        source_tier=tier,
        source_owner=owner,
    )
    return {
        "source_url": url,
        "speaker": speaker,
        "speaker_type": speaker_type,
        "kind": kind,
        "origin": speaker,
        "affiliation": "",
        "evidence": evidence,
    }


def test_evidence_confirmation_first_party_own_site_speakers_resolve_to_the_owner():
    by_url = by_url_map(
        ("https://news.xbox.com/1", "microsoft"),
        ("https://news.xbox.com/2", "xbox"),
    )
    claims = [
        first_party_claim(
            "https://news.xbox.com/1", "Xbox Wire",
            owner="microsoft", outlet="Xbox Wire",
            quote="Cloud gaming arrives on more devices",
        ),
        first_party_claim(
            "https://news.xbox.com/2", "Xbox",
            owner="xbox", outlet="Xbox",
            quote="Cloud gaming arrives on more devices",
        ),
    ]
    for entry in claims:
        assert entry["evidence"]["attribution_mode"] == "first_party_site"
        assert entry["evidence"]["ok"] is True
    result = facts.evidence_confirmation(claims, by_url)
    assert result["confirmed"] is True
    assert result["rule"] == "first-party"
    assert result["first_party_organizations"] == ["microsoft"]
    assert result["first_party_sources"] == 2
    assert result["origins"] == ["microsoft"]


def test_evidence_confirmation_playstation_blog_speaker_resolves_to_sony():
    by_url = by_url_map(
        ("https://blog.playstation.com/1", "sony"),
        ("https://blog.playstation.com/2", "playstation"),
    )
    claims = [
        first_party_claim(
            "https://blog.playstation.com/1", "PlayStation Blog",
            owner="sony", outlet="PlayStation Blog",
            quote="The update arrives today",
        ),
        first_party_claim(
            "https://blog.playstation.com/2", "PlayStation",
            owner="playstation", outlet="PlayStation",
            quote="The update arrives today",
        ),
    ]
    for entry in claims:
        assert entry["evidence"]["attribution_mode"] == "first_party_site"
        assert entry["evidence"]["ok"] is True
    result = facts.evidence_confirmation(claims, by_url)
    assert result["confirmed"] is True
    assert result["rule"] == "first-party"
    assert result["first_party_organizations"] == ["sony"]
    assert result["first_party_sources"] == 2
    assert result["origins"] == ["sony"]


def test_evidence_confirmation_own_site_claims_from_one_owner_count_once():
    by_url = by_url_map(
        ("https://news.xbox.com/1", "microsoft"),
        ("https://news.xbox.com/2", "microsoft"),
    )
    claims = [
        first_party_claim(
            "https://news.xbox.com/1", "Xbox Wire",
            owner="microsoft", outlet="Xbox Wire",
            quote="Cloud gaming arrives on more devices",
        ),
        first_party_claim(
            "https://news.xbox.com/2", "Xbox",
            owner="microsoft", outlet="Xbox Wire",
            quote="Cloud gaming arrives on more devices",
        ),
    ]
    result = facts.evidence_confirmation(claims, by_url)
    assert result["confirmed"] is False
    assert result["first_party_sources"] == 0
    assert result["origins"] == ["microsoft"]


def test_evidence_confirmation_tier2_own_site_speaker_still_fails_as_self_reference():
    by_url = by_url_map(
        ("https://eurogamer.example/1", "eurogamer"),
        ("https://b.example/2", "co-b"),
    )
    eurogamer = first_party_claim(
        "https://eurogamer.example/1", "Eurogamer",
        owner="eurogamer", outlet="Eurogamer",
        quote="The game ships today", speaker_type="outlet", tier=2,
    )
    assert eurogamer["evidence"]["self_reference"] is True
    assert eurogamer["evidence"]["ok"] is False
    other = ev_claim("https://b.example/2", speaker="Acme Games", speaker_type="organization")
    result = facts.evidence_confirmation([eurogamer, other], by_url, aliases={})
    assert result["confirmed"] is False
    assert result["origins"] == ["acme games"]


def test_evidence_confirmation_tier1_unrelated_speaker_is_not_first_party():
    by_url = by_url_map(("https://news.xbox.com/1", "microsoft"))
    unrelated = first_party_claim(
        "https://news.xbox.com/1", "Contoso Games",
        owner="microsoft", outlet="Xbox Wire",
        quote="A new game ships today",
    )
    assert unrelated["evidence"]["attribution_mode"] != "first_party_site"
    assert unrelated["evidence"]["ok"] is False
    result = facts.evidence_confirmation([unrelated], by_url, aliases={})
    assert result["confirmed"] is False
    assert result["first_party_organizations"] == []


def test_extraction_prompt_states_the_sentence_citation_rule():
    # v15: quote_ids cite body sentences; only a headline-restating claim may
    # cite S0 alone, and a speaker is never the game, product or platform.
    assert facts.PROMPT_VERSION == "facts-v15"
    instruction = facts.SYSTEM_INSTRUCTION
    assert "Evidence is cited by numbered sentence, never copied" in instruction
    assert "Rebel Wolves has confirmed that" in instruction
    assert "cite that sentence in both quote_ids and attribution_ids" in instruction
    assert "Never cite a number that is not given in the numbered text" in instruction
    assert "quote_ids cites body sentences, S1 onward" in instruction
    assert "it cites the headline alone as [0]" in instruction
    assert "Never cite 0 together with body sentences" in instruction
    assert "never cite the headline for a detail claim" in instruction
    assert "never cite an attribution sentence that does not name its speaker" in instruction
    assert "cite that earlier sentence in attribution_ids" in instruction
    assert "cite the headline (S0) in attribution_ids only" in instruction
    assert "A speaker is never the game, product or platform the story is about" in instruction


# --- headline-only sources: one title claim, never a model call ---------------


def _headline_only_source(link, name, tier, owner, title):
    return {**source(link=link, name=name, tier=tier, owner=owner, text=""), "title": title, "body_status": "headline_only"}


def test_headline_only_claim_confirms_only_when_the_title_names_the_owner():
    announced = _headline_only_source(
        "https://news.acme.example/1", "Acme News", 1, "acme",
        "Acme announces the new console today",
    )
    built = facts._headline_only_claim(announced, facts.load_origin_aliases())
    assert built["claim"] == "Acme announces the new console today"
    assert built["kind"] == "confirmed"
    assert built["evidence"]["ok"] is True
    assert built["evidence"]["attribution_mode"] == "headline_announcement"
    assert built["origin"] != facts.UNSTATED_ORIGIN

    plain = _headline_only_source(
        "https://news.acme.example/1", "Acme News", 1, "acme",
        "A new console is coming this year",
    )
    built_plain = facts._headline_only_claim(plain, facts.load_origin_aliases())
    assert built_plain["kind"] == "reported"
    assert built_plain["evidence"]["ok"] is False
    assert built_plain["evidence"]["attribution_mode"] == "none"


# --- absence rule and attribution hardening ----------------------------------


def test_absence_claim_becomes_reported_with_no_speaker():
    entry = facts.validate_claims(
        {
            "claims": [
                claim(
                    claim="Activision hasn't revealed an exact time",
                    value="no exact time",
                    kind="statement",
                    origin="activision",
                    quote="Activision hasn't revealed an exact time",
                    attribution_quote="Activision hasn't revealed",
                    speaker="Activision",
                    speaker_type="organization",
                )
            ]
        }
    )[0]
    assert entry["kind"] == "reported"
    assert entry["origin"] == facts.UNSTATED_ORIGIN
    assert entry["speaker"] == ""
    assert entry["speaker_type"] == "none"
    assert entry["absence"] is True


def test_absence_claim_never_keeps_its_origin():
    entry = claim(
        claim="Activision hasn't revealed an exact time",
        value="no exact time",
        kind="statement",
        origin="activision",
        source_url="https://a.example/1",
        speaker="Activision",
        speaker_type="organization",
    )
    sources = [
        source(
            link="https://a.example/1",
            tier=2,
            name="PC Gamer",
            text="Activision hasn't revealed an exact time for the campaign unlock.",
        )
    ]
    resolved = facts.resolve_claim_origins([entry], sources)
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN
    assert resolved[0]["speaker"] == ""


def test_absence_claim_never_counts_toward_first_party_or_confirmation():
    entry = claim(
        claim="Activision hasn't revealed an exact time",
        value="no exact time",
        kind="statement",
        origin="activision",
        source_url="https://a.example/1",
        speaker="Activision",
        speaker_type="organization",
    )
    sources = [
        source(
            link="https://a.example/1",
            tier=2,
            name="PC Gamer",
            text="Activision hasn't revealed an exact time for the campaign unlock.",
        )
    ]
    by_url = facts._sources_by_url(sources)
    assert facts._first_party_origins([entry], by_url, {}, None)[2] == set()
    assert facts._confirmation_origins([entry], by_url) == []


def test_announcement_claim_is_not_treated_as_an_absence():
    entry = facts.validate_claims(
        {
            "claims": [
                claim(
                    claim="Activision announced the unlock time",
                    value="the unlock time",
                    kind="statement",
                    origin="activision",
                    quote="Activision announced the unlock time",
                    attribution_quote="Activision announced",
                    speaker="Activision",
                    speaker_type="organization",
                )
            ]
        }
    )[0]
    assert entry["kind"] == "statement"
    assert entry["origin"] == "activision"
    assert entry["speaker"] == "Activision"
    assert not entry.get("absence")


def test_verify_evidence_rejects_an_attribution_equal_to_the_claim_quote():
    phrase = "the update lands in June"
    result = facts.verify_evidence(
        claim(
            quote=phrase,
            attribution_quote=phrase,
            speaker="Epic Games",
            speaker_type="organization",
        ),
        "Epic Games said the update lands in June.",
    )
    assert result["quote_found"] is True
    assert result["attribution_found"] is False
    assert result["ok"] is False


def test_verify_evidence_credits_an_attribution_with_a_cue():
    result = facts.verify_evidence(
        claim(
            quote="the update lands in June",
            attribution_quote="According to Epic Games",
            speaker="Epic Games",
            speaker_type="organization",
        ),
        "According to Epic Games, the update lands in June.",
    )
    assert result["attribution_found"] is True
    assert result["ok"] is True


def test_verify_evidence_first_party_site_still_passes_without_a_cue():
    text = "The Acme X1 is available now at every retailer."
    result = facts.verify_evidence(
        claim(
            quote="The Acme X1 is available now at every retailer",
            attribution_quote="Acme Games availability",
            speaker="Acme Games",
            speaker_type="organization",
        ),
        text,
        "Acme X1 availability",
        source_outlet="Acme Games",
        source_tier=1,
        source_owner="Acme Games",
    )
    assert result["attribution_mode"] == "first_party_site"
    assert result["ok"] is True


def test_verify_evidence_headline_announcement_still_passes_without_a_cue():
    text = "The Acme X1 is available now at every retailer."
    result = facts.verify_evidence(
        claim(
            quote="The Acme X1 is available now at every retailer",
            attribution_quote="Acme Games announces availability",
            speaker="Acme Games",
            speaker_type="organization",
        ),
        text,
        "Acme Games announces availability of the Acme X1",
        source_outlet="Wire Desk",
        source_tier=1,
        source_owner="wire-desk",
    )
    assert result["attribution_mode"] == "headline_announcement"
    assert result["ok"] is True


# --- in-sentence attributions: cue, speaker name, affirmative ----------------


def test_verify_evidence_accepts_an_in_sentence_attribution_naming_the_speaker():
    text = "CD Projekt chief says the remake ships in 2027."
    result = facts.verify_evidence(
        claim(
            quote="the remake ships in 2027",
            attribution_quote="CD Projekt chief says",
            speaker="CD Projekt",
            speaker_type="organization",
        ),
        text,
    )
    assert result["attribution_found"] is True
    assert result["repaired_attribution"] is False
    assert result["ok"] is True


def test_verify_evidence_accepts_an_according_to_attribution_inside_the_claim():
    text = "According to MP1st, the remaster ships in June."
    result = facts.verify_evidence(
        claim(
            quote="the remaster ships in June",
            attribution_quote="According to MP1st",
            speaker="MP1st",
            speaker_type="outlet",
        ),
        text,
    )
    assert result["attribution_found"] is True
    assert result["ok"] is True


def test_verify_evidence_accepts_a_person_attribution_inside_the_claim():
    text = "Nowakowski said the studio targets 2027."
    result = facts.verify_evidence(
        claim(
            quote="the studio targets 2027",
            attribution_quote="Nowakowski said",
            speaker="Nowakowski",
            speaker_type="person",
        ),
        text,
    )
    assert result["attribution_found"] is True
    assert result["ok"] is True


def test_verify_evidence_rejects_a_negated_attribution_and_the_absence_rule_still_applies():
    text = "Activision hasn't revealed an exact time for the campaign unlock."
    result = facts.verify_evidence(
        claim(
            claim="an exact time",
            value="an exact time",
            quote="an exact time for the campaign unlock",
            attribution_quote="Activision hasn't revealed",
            speaker="Activision",
            speaker_type="organization",
        ),
        text,
    )
    assert result["attribution_found"] is False
    assert result["repaired_attribution"] is False
    assert result["ok"] is False
    assert result["reason"] == "attribution quote not found"
    entry = facts.validate_claims(
        {
            "claims": [
                claim(
                    claim="Activision hasn't revealed an exact time",
                    value="no exact time",
                    kind="statement",
                    origin="activision",
                    quote="Activision hasn't revealed an exact time",
                    attribution_quote="Activision hasn't revealed",
                    speaker="Activision",
                    speaker_type="organization",
                )
            ]
        }
    )[0]
    assert entry["kind"] == "reported"
    assert entry["speaker"] == ""
    assert entry["absence"] is True


def test_verify_evidence_rejects_an_attribution_without_a_cue():
    text = "Rebel Wolves 2027 tour. The roguelike ships in June."
    result = facts.verify_evidence(
        claim(
            quote="the roguelike ships in June",
            attribution_quote="Rebel Wolves 2027",
            speaker="Rebel Wolves",
            speaker_type="organization",
        ),
        text,
    )
    assert result["attribution_found"] is False
    assert result["repaired_attribution"] is False
    assert result["ok"] is False
    assert result["reason"] == "attribution quote not found"


def test_verify_evidence_rejects_an_attribution_omitting_the_speaker():
    text = "Rebel Wolves has confirmed that the roguelike ships in June."
    result = facts.verify_evidence(
        claim(
            quote="the roguelike ships in June",
            attribution_quote="has confirmed that",
            speaker="Rebel Wolves",
            speaker_type="organization",
        ),
        text,
    )
    assert result["attribution_found"] is False
    assert result["repaired_attribution"] is False
    assert result["ok"] is False
    assert result["reason"] == "attribution quote not found"


def test_verify_evidence_repairs_a_paraphrased_attribution_from_the_quote():
    text = "Rebel Wolves has announced that the roguelike ships in 2027."
    result = facts.verify_evidence(
        claim(
            quote="Rebel Wolves has announced that the roguelike ships in 2027",
            attribution_quote="Rebel Wolves has confirmed that",
            speaker="Rebel Wolves",
            speaker_type="organization",
        ),
        text,
    )
    assert result["quote_found"] is True
    assert result["attribution_found"] is True
    assert result["repaired_attribution"] is True
    assert result["attribution_mode"] == "explicit"
    assert result["ok"] is True


def test_verify_evidence_never_repairs_a_negated_attribution_from_the_quote():
    text = "Activision hasn't revealed an exact time for the campaign unlock."
    result = facts.verify_evidence(
        claim(
            claim="the campaign unlock schedule",
            value="an exact time",
            quote="Activision hasn't revealed an exact time for the campaign unlock",
            attribution_quote="Activision has confirmed that",
            speaker="Activision",
            speaker_type="organization",
        ),
        text,
    )
    assert result["quote_found"] is True
    assert result["attribution_found"] is False
    assert result["repaired_attribution"] is False
    assert result["ok"] is False


def test_verify_evidence_never_repairs_a_quote_without_a_cue():
    text = "Rebel Wolves ships in June."
    result = facts.verify_evidence(
        claim(
            quote="Rebel Wolves ships in June",
            attribution_quote="Rebel Wolves announced",
            speaker="Rebel Wolves",
            speaker_type="organization",
        ),
        text,
    )
    assert result["quote_found"] is True
    assert result["attribution_found"] is False
    assert result["repaired_attribution"] is False
    assert result["ok"] is False


def test_verify_evidence_repair_leaves_first_party_site_unaffected():
    text = "The Acme X1 is available now at every retailer."
    result = facts.verify_evidence(
        claim(
            quote="The Acme X1 is available now at every retailer",
            attribution_quote="Acme Games availability",
            speaker="Acme Games",
            speaker_type="organization",
        ),
        text,
        "Acme X1 availability",
        source_outlet="Acme Games",
        source_tier=1,
        source_owner="Acme Games",
    )
    assert result["attribution_mode"] == "first_party_site"
    assert result["repaired_attribution"] is False
    assert result["ok"] is True


def test_verify_evidence_repair_leaves_headline_announcement_unaffected():
    text = "The Acme X1 is available now at every retailer."
    result = facts.verify_evidence(
        claim(
            quote="The Acme X1 is available now at every retailer",
            attribution_quote="Acme Games announces availability",
            speaker="Acme Games",
            speaker_type="organization",
        ),
        text,
        "Acme Games announces availability of the Acme X1",
        source_outlet="Wire Desk",
        source_tier=1,
        source_owner="wire-desk",
    )
    assert result["attribution_mode"] == "headline_announcement"
    assert result["repaired_attribution"] is False
    assert result["ok"] is True


def test_ps5_only_claim_is_off_topic_for_an_unrelated_game_story():
    only_ps5 = claim(
        claim="PS5 consoles moved 21 million units",
        value="21 million consoles",
        source_url="https://a.example/1",
    )
    unrelated_source = source(
        link="https://a.example/1",
        name="Alpha",
        title="PS5 consoles moved 21 million units",
        text="PS5 consoles moved 21 million units last quarter.",
    )
    kept, dropped, primary = facts.coherence_check(
        [only_ps5],
        [unrelated_source],
        title="Galaxy Racers ships on 2 November",
        entities=["Galaxy Racers"],
    )
    assert primary == ["galaxy racers"]
    assert kept == []
    assert dropped == [only_ps5]


def test_headline_only_source_makes_no_model_call_and_yields_one_title_claim(monkeypatch, tmp_path):
    use_mode(monkeypatch, "legacy")
    title = "Acme announces the new console today"
    official = _headline_only_source("https://news.acme.example/1", "Acme News", 1, "acme", title)
    outlet = _headline_only_source("https://outlet.example/2", "Outlet", 2, "outlet", title)

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        raise AssertionError("a headline-only source must never reach the model")

    result = facts.facts(
        gathered("headline-only", [official, outlet]),
        id="headline-only",
        title=title,
        output_dir=tmp_path,
        now=NOW,
        generate=generate,
    )
    assert len(result["claims"]) == 1
    assert result["claims"][0]["claim"] == title
    assert result["confirmation"]["rule"] == "tier1"
    assert [source["link"] for source in result["sources"]] == [
        "https://news.acme.example/1", "https://outlet.example/2",
    ]


def test_is_coreference_phrase_matches_role_noun_suffixes():
    # A phrase corefers when its trailing words are a role noun, even when
    # non-role words precede them ("an update the developer").
    assert facts._is_coreference_phrase("the studio") is True
    assert facts._is_coreference_phrase("the developer") is True
    assert facts._is_coreference_phrase("an update the developer") is True
    assert facts._is_coreference_phrase("even though the studio") is True
    assert facts._is_coreference_phrase("an update") is False
    assert facts._is_coreference_phrase("the game") is False


def test_coreference_phrase_resolves_across_paragraphs():
    # A role phrase in a later paragraph still resolves to the organization
    # the previous paragraph named, when it sits within the sentence window.
    src = source(text="Bethesda announced a delay.\n\nThe studio confirmed the patch lands in May.")
    resolved = facts.resolve_claim_origins(
        [claim(claim="the patch lands in May", origin="bethesda", source_url="https://a.example/1")], [src]
    )
    assert resolved[0]["origin"] == "bethesda"


def test_role_phrase_with_two_candidates_stays_unresolved():
    # Two organizations named before the phrase: it cannot say which one it
    # stands for, so it stays unresolved instead of guessing.
    text = "Bethesda announced the delay. Capcom commented on the release. The studio confirmed the patch lands in May."
    position = text.find("The studio")
    assert facts._nearest_preceding_organization(text, position, entities=[]) == ""


def test_role_phrase_title_breaks_tie_when_it_names_one_candidate():
    # The headline names one of the two candidates, which then wins the tie.
    text = "Bethesda announced the delay. Capcom commented on the release. The studio confirmed the patch lands in May."
    position = text.find("The studio")
    resolved = facts._nearest_preceding_organization(
        text, position, entities=[], title="Bethesda delays the patch"
    )
    assert resolved == "bethesda"


def test_role_phrase_stays_unresolved_when_title_names_both_candidates():
    # The headline names both candidates, so it cannot break the tie either.
    text = "Bethesda announced the delay. Capcom commented on the release. The studio confirmed the patch lands in May."
    position = text.find("The studio")
    resolved = facts._nearest_preceding_organization(
        text, position, entities=[], title="Bethesda and Capcom discuss the patch"
    )
    assert resolved == ""


def test_role_phrase_beyond_sentence_window_stays_unresolved():
    # An organization in an earlier paragraph, more than the sentence window
    # away, no longer grounds the role phrase: the claim is unstated.
    fillers = " ".join("The filler sentence number %d appears here." % i for i in range(1, 10))
    text = "Bethesda announced the delay today.\n\n" + fillers + " The studio confirmed the patch lands in May."
    src = source(text=text)
    resolved = facts.resolve_claim_origins(
        [claim(claim="the patch lands in May", origin="bethesda", source_url="https://a.example/1")], [src]
    )
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN


def test_role_phrase_does_not_resolve_to_a_product():
    # A product that shares the organization pattern never vouches for the
    # role phrase; the phrase stays unresolved.
    text = "Steam Deck shipped worldwide today. The studio confirmed the patch lands in May."
    position = text.find("The studio")
    assert facts._nearest_preceding_organization(text, position, entities=["Steam Deck"]) == ""


def test_role_phrase_does_not_resolve_to_the_publishing_outlet():
    # The publishing outlet never vouches for its own article's role phrase.
    text = "Xbox Wire published a delay guide today. The studio confirmed the patch lands in May."
    position = text.find("The studio")
    assert facts._nearest_preceding_organization(text, position, entities=[], outlet="Xbox Wire") == ""


def test_object_role_phrase_without_a_cue_does_not_resolve():
    # "the studio" is the object of "Players asked", not the speaker of an
    # attribution cue, so it never grounds the claim's origin.
    src = source(
        text="Bethesda shipped the patch. Players asked the studio for a refund. The report says the patch lands in May."
    )
    resolved = facts.resolve_claim_origins(
        [claim(claim="the patch lands in May", origin="bethesda", source_url="https://a.example/1")], [src]
    )
    assert resolved[0]["origin"] == facts.UNSTATED_ORIGIN


def test_role_phrase_attribution_span_is_kept_for_evidence():
    # A role-phrase subject with no named speaker still yields an attribution
    # span for the evidence record, and grounding accepts it when the phrase
    # sits next to the quoted claim.
    text = "Techland delayed the patch. Even though the studio has said the delay is short, fans remain annoyed."
    sentences = facts._numbered_sentences(text)
    paragraphs = facts._sentence_paragraphs(text, sentences)
    cited = facts._cite_claim_evidence(
        claim(
            claim="The patch lands in May", value="in May", origin="Techland",
            speaker="Techland", speaker_type="organization",
            quote_ids=[1], attribution_ids=[2],
        ),
        sentences, aliases={}, entities=["Techland"], paragraphs=paragraphs,
    )
    assert cited["attribution_quote"] == "Even though the studio has said"
    result = facts.verify_evidence(cited, text, "", "Alpha", aliases={}, entities=["Techland"])
    assert result["ok"] is True


def test_speaker_is_product_rejects_a_bare_product_title():
    # A speaker that is the story's lead product or named in its title is the
    # subject, not a speaker.
    assert facts._speaker_is_product("Dawnwalker", ["The Blood of Dawnwalker"], "") is True
    assert facts._speaker_is_product("Dawnwalker", [], "", story_title="Blood of Dawnwalker update") is True
    # A multi-word organization sharing one word with a product is kept.
    assert facts._speaker_is_product("Rebel Wolves", ["Wolves of Winter"], "") is False
    # A known organization alias is kept.
    assert facts._speaker_is_product("Xbox", [], "", story_title="Xbox delays the patch") is False
    # A cue subject that is also the lead entity is kept.
    assert facts._speaker_is_product("Rebel Wolves", ["Rebel Wolves"], "Rebel Wolves has announced") is False

