import pytest

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
            {"claim": "A game was announced", "value": "Game X", "source_url": "https://news.xbox.com/a", "confidence": 0.95, "is_rumor": False},
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