import datetime as dt
import json
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


def claim(claim="Announcement happened", value="it happened", source_url="https://a.example/1", confidence=0.9, is_rumor=False):
    return {
        "claim": claim, "value": value, "source_url": source_url,
        "confidence": confidence, "is_rumor": is_rumor,
    }


def make_generate(payload):
    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        if run_state is not None:
            run_state.record(Generation(role=role, provider="mock", model="m", family="f", value=payload))
        return Generation(role=role, provider="mock", model="m", family="f", value=payload, fallbacks=[])

    return generate


# --- confirmation rule -------------------------------------------------------


@pytest.mark.parametrize(
    "sources, expected",
    [
        ([source(tier=1)], True),
        ([source(tier=1), source(link="https://a2", name="A2", tier=3)], True),
        ([source(), source(link="https://b", name="Beta", owner="co-b")], True),
        ([source()], False),
        ([source(tier=2), source(link="https://a2", name="A2", owner="co-a")], False),
        ([source(tier=3), source(link="https://b", name="Beta", tier=3, owner="co-b")], False),
        ([source(tier=3)], False),
        ([source(), source(link="https://b", name="Beta", tier=3, owner="co-b")], False),
    ],
)
def test_confirmation_rule(sources, expected):
    result = facts.confirmation_from_sources(sources)
    assert result["confirmed"] is expected
    assert result["tier3_sources"] == sum(1 for s in sources if s["tier"] == 3)


def test_confirmation_tier3_never_counts():
    assert facts.confirmation_from_sources([
        source(tier=3), source(link="https://b", name="Beta", tier=3, owner="co-b"),
    ])["confirmed"] is False


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
        {"claims": [claim(is_rumor="yes")]},
        {"claims": [claim(claim=1)]},
        {"claims": [{"claim": "missing rest"}]},
        {"claims": [claim(), {"claim": "extra", "value": "v", "source_url": "https://x", "confidence": 0.5, "is_rumor": False, "bogus": 1}]},
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


# --- facts() end to end ------------------------------------------------------


def test_facts_writes_sheet_and_records_fast_role(tmp_path):
    data = gathered(sources=[source(tier=1)])
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
    assert result["claims"][0]["is_rumor"] is False
    assert result["sources"][0]["source_name"] == "Alpha"

    path = tmp_path / "story-1.json"
    assert path.is_file()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["claims"][0]["source_url"] == "https://a.example/1"
    assert written["extractor"]["family"] == "f"
    assert run_state.generations["fast"].provider == "mock"


def test_facts_rejects_unconfirmed_story_without_model_call(tmp_path):
    calls = []

    def generate(role, prompt, json_schema=None, **kwargs):
        calls.append(role)
        return Generation(role=role, provider="mock", model="m", family="f", value={})

    data = gathered(sources=[source()])  # single tier-2
    with pytest.raises(facts.UnconfirmedStory):
        facts.facts(data, output_dir=tmp_path, now=NOW, generate=generate)
    assert calls == []
    assert not (tmp_path / "story-1.json").exists()


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


# --- prompt ------------------------------------------------------------------


def test_build_prompt_includes_sources_and_text():
    prompt = facts.build_prompt("story-1", [source()])
    assert "story-1" in prompt
    assert "Alpha" in prompt
    assert "https://a.example/1" in prompt
    assert "Some body text" in prompt


def test_build_prompt_truncates_long_text():
    long_text = "word " * 6000
    prompt = facts.build_prompt("s", [source(text=long_text)])
    assert "[truncated]" in prompt
    assert len(prompt) < len(long_text)


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
            tier=2,
        ),
        source(
            link="https://b.example/ace",
            title="Ace Combat 8 Has Sold One Million Copies",
            owner="co-b",
            tier=2,
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
    generation = make_generate({"claims": [on_topic, off_topic]})
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
        source(link="https://a.example/gears", title="Gears of War: E-Day sales", owner="co-a", tier=2),
        source(link="https://b.example/ace", title="Ace Combat 8 sells a million", owner="co-b", tier=2),
    ]
    result = facts.facts(
        gathered("gear", gathered_sources),
        title="Gears of War: E-Day Sold 168,000 Copies on Steam",
        output_dir=tmp_path,
        now=NOW,
        generate=make_generate({"claims": [
            claim(source_url="https://b.example/ace", claim="Ace Combat 8 shipped a million"),
        ]}),
    )
    assert result["claims"] == []
    assert any("off-topic" in record.message for record in caplog.records)


def test_coherence_skips_when_no_primary_entity(tmp_path):
    data = gathered(
        sources=[
            source(title="general gadgets digest"),
            source(link="https://a.example/2", name="Alpha 2", owner="co-b", title="general gadgets digest"),
        ],
    )
    result = facts.facts(
        data,
        output_dir=tmp_path,
        now=NOW,
        generate=make_generate({"claims": [claim()]}),
    )
    assert result["coherence"]["checked"] is False
    assert len(result["claims"]) == 1


def test_coherence_uses_claim_text_when_source_has_no_title(tmp_path):
    data = gathered(
        sources=[
            source(title=""),
            source(link="https://a.example/2", name="Alpha 2", owner="co-b", title=""),
        ],
    )
    result = facts.facts(
        data,
        title="Nvidia confirms next graphics card launch window",
        output_dir=tmp_path,
        now=NOW,
        generate=make_generate({"claims": [claim(claim="Nvidia confirmed the launch window for its next graphics card line")]}),
    )
    assert len(result["claims"]) == 1
    assert result["coherence"]["checked"] is True