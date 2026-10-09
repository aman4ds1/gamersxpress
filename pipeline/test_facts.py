import datetime as dt
import json
from pathlib import Path

import pytest

import facts
from providers import Generation, RunState

NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)


def source(link="https://a.example/1", name="Alpha", tier=2, owner="co-a", region="us", text="Some body text about the story."):
    return {
        "link": link, "source_name": name, "tier": tier, "owner": owner,
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