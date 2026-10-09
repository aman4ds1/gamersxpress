import json

import pytest

import providers
import verify as verify_module


ARTICLE = "## Price\nThe console costs $499.\n"


def claim(value="The console costs $499", source_url="https://news.xbox.com/a"):
    return {
        "claim": "Console price",
        "value": value,
        "source_url": source_url,
        "confidence": 0.9,
        "is_rumor": False,
    }


def sheet(*claims, id="story-1"):
    return {
        "id": id,
        "generated_at": "2026-10-09T12:00:00+00:00",
        "sources": [
            {
                "source_name": "Xbox Wire",
                "link": "https://news.xbox.com/a",
                "tier": 1,
                "owner": "microsoft",
                "region": "global",
            }
        ],
        "claims": list(claims) or [claim()],
    }


def config(whitelist=None):
    return {"list_markers": verify_module.DEFAULT_LIST_MARKERS, "whitelist": whitelist or []}


def model(sentences=None, unsupported=None, rumors=None, regional=None):
    return {
        "sentences": sentences
        if sentences is not None
        else [{"sentence": "The console costs $499", "supported": True, "fact": "Console price"}],
        "unsupported_claims": unsupported or [],
        "rumors_stated_as_fact": rumors or [],
        "unsupported_regional": regional or [],
    }


def make_generate(value):
    def generate(role, prompt, *, run_state=None, **kwargs):
        return providers.Generation(
            role=role, provider="mistral", model="m", family="mistral", value=value
        )

    return generate


# --- passing article ---------------------------------------------------------


def test_correct_article_passes(tmp_path):
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(model()),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is True
    assert report.unmatched_numbers == []
    assert report.unsupported == []
    assert report.error is None
    assert (tmp_path / "verify-story-1.json").is_file()


def test_report_uses_facts_id_and_families(tmp_path):
    run_state = providers.RunState()
    run_state.record(providers.Generation("writer", "gemini", "g", "google", "DRAFT"))
    verify_module.verify(
        ARTICLE,
        sheet(id="abc"),
        run_state=run_state,
        generate=make_generate(model()),
        report_dir=tmp_path,
        verify_config=config(),
    )
    data = json.loads((tmp_path / "verify-abc.json").read_text(encoding="utf-8"))
    assert data["id"] == "abc"
    assert data["passed"] is True
    assert data["writer_family"] == "google"
    assert data["verifier_family"] == "mistral"
    assert data["verifier_provider"] == "mistral"


def test_explicit_id_overrides_facts_id(tmp_path):
    report = verify_module.verify(
        ARTICLE,
        sheet(id="from-sheet"),
        id="from-arg",
        run_state=providers.RunState(),
        generate=make_generate(model()),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.id == "from-arg"
    assert (tmp_path / "verify-from-arg.json").is_file()


def test_verifier_json_wrapped_in_prose_is_parsed(tmp_path):
    text = "Here is the result:\n" + json.dumps(model()) + "\n"
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(text),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is True


# --- failing articles --------------------------------------------------------


def test_wrong_price_fails_code_check_even_if_model_agrees(tmp_path):
    article = "## Price\nThe console costs $599.\n"
    report = verify_module.verify(
        article,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(model([{"sentence": "The console costs $599", "supported": True}])),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert "$599" in report.unmatched_numbers


def test_invented_claim_fails_model_check(tmp_path):
    payload = model(
        sentences=[{"sentence": "Free games forever", "supported": False}],
        unsupported=["Free games forever"],
    )
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert "Free games forever" in report.unsupported


def test_rumor_stated_as_fact_fails(tmp_path):
    payload = model(rumors=["A sequel is coming"])
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert "A sequel is coming" in report.unsupported


def test_unsupported_regional_fails(tmp_path):
    payload = model(regional=["Rs 39,999 in India"])
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert "Rs 39,999 in India" in report.unsupported


def test_malformed_verifier_output_fails(tmp_path):
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate("no json here"),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.error is not None
    assert "not valid JSON" in report.error


def test_schema_invalid_verifier_output_fails(tmp_path):
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate({"sentences": "nope"}),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.error is not None
    assert "schema" in report.error


# --- family guard ------------------------------------------------------------


class StubProvider(providers.Provider):
    name = "stub"
    env_key = "STUB_API_KEY"

    def complete(self, *args, **kwargs):
        raise AssertionError("provider must not be called")


def guard_config(family="google"):
    return {
        "roles": {"verifier": [{"provider": "stub", "model": "s", "family": family}]},
        "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 30},
    }


def test_verifier_without_writer_family_is_refused(tmp_path):
    with pytest.raises(providers.MissingWriterFamilyError):
        verify_module.verify(
            ARTICLE,
            sheet(),
            run_state=None,
            config=guard_config(),
            providers={"stub": StubProvider()},
            env={"STUB_API_KEY": "x"},
            dry_run=False,
            sleep=lambda _delay: None,
            report_dir=tmp_path,
            verify_config=config(),
        )
    assert not (tmp_path / "verify-story-1.json").exists()


def test_same_family_verifier_is_skipped(tmp_path):
    run_state = providers.RunState()
    run_state.record(providers.Generation("writer", "stub", "s", "google", "DRAFT"))
    with pytest.raises(providers.SkipRun):
        verify_module.verify(
            ARTICLE,
            sheet(),
            run_state=run_state,
            config=guard_config(),
            providers={"stub": StubProvider()},
            env={"STUB_API_KEY": "x"},
            dry_run=False,
            sleep=lambda _delay: None,
            report_dir=tmp_path,
            verify_config=config(),
        )
    assert not (tmp_path / "verify-story-1.json").exists()


# --- value extraction and normalization --------------------------------------


def test_extract_values_normalizes_prices():
    tokens = [value["token"] for value in verify_module.extract_values("$1,299 or $1.3k", config())]
    assert "1299" in tokens
    assert "1300" in tokens


def test_extract_values_percent_spec_and_resolution():
    tokens = {
        value["token"]
        for value in verify_module.extract_values("50% faster, 16 GB at 1920x1080", config())
    }
    assert "50%" in tokens
    assert "16gb" in tokens
    assert "1920x1080" in tokens


def test_extract_values_model_number():
    tokens = {value["token"] for value in verify_module.extract_values("RTX 4090", config())}
    assert "rtx4090" in tokens


def test_extract_values_dates_become_iso():
    tokens = [
        value["token"]
        for value in verify_module.extract_values("On Oct 9, 2026 and 2026-10-09", config())
    ]
    assert tokens.count("2026-10-09") == 2


def test_extract_values_ignores_urls():
    tokens = [value["token"] for value in verify_module.extract_values("see https://x.example/2026", config())]
    assert tokens == []


def test_list_markers_are_not_read_as_numbers():
    tokens = [value["token"] for value in verify_module.extract_values("1. First item", config())]
    assert tokens == []


# --- code check --------------------------------------------------------------


def test_code_check_passes_when_values_match_facts():
    result = verify_module.code_check(ARTICLE, sheet(), config())
    assert result["ok"] is True
    assert result["unmatched"] == []
    assert result["checked"] == 1


def test_code_check_reports_unmatched_numbers():
    result = verify_module.code_check("The console costs $599.", sheet(), config())
    assert result["ok"] is False
    assert "$599" in result["unmatched"]


def test_code_check_ignores_front_matter():
    article = "---\ntitle: A $999 rumor\n---\nThe console costs $499.\n"
    result = verify_module.code_check(article, sheet(), config())
    assert result["ok"] is True


def test_whitelist_ignores_year_in_heading():
    cfg = config([{"pattern": r"^(?:19|20)\d{2}$", "context": r"^#{1,6}\s"}])
    result = verify_module.code_check("## 2026 roadmap\nThe console costs $499.\n", sheet(), cfg)
    assert result["ok"] is True


def test_year_outside_heading_is_not_whitelisted():
    cfg = config([{"pattern": r"^(?:19|20)\d{2}$", "context": r"^#{1,6}\s"}])
    result = verify_module.code_check("In 2026 the console costs $499.\n", sheet(), cfg)
    assert result["ok"] is False
    assert "2026" in result["unmatched"]


# --- duplicate Sources -------------------------------------------------------


def test_find_sources_heading_detects_body_section():
    article = ARTICLE + "## Sources\n- [Xbox Wire](https://news.xbox.com/a)\n"
    assert verify_module.find_sources_heading(article) == "## Sources"


def test_find_sources_heading_is_case_insensitive_and_any_level():
    assert verify_module.find_sources_heading("### sources\n") == "### sources"
    assert verify_module.find_sources_heading("#  SOURCES  \n") == "#  SOURCES"


def test_find_sources_heading_ignores_front_matter_sources():
    article = "---\nsources:\n  - name: Xbox Wire\n    url: https://news.xbox.com/a\n---\nBody text.\n"
    assert verify_module.find_sources_heading(article) is None


def test_find_sources_heading_ignores_unrelated_heading():
    assert verify_module.find_sources_heading("## Sources of revenue\n") is None
    assert verify_module.find_sources_heading("The sources say nothing.\n") is None


def test_body_sources_section_fails_verification(tmp_path):
    article = ARTICLE + "## Sources\n- [Xbox Wire](https://news.xbox.com/a)\n"
    report = verify_module.verify(
        article,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(model()),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.duplicate_sources == "## Sources"
    assert json.loads((tmp_path / "verify-story-1.json").read_text(encoding="utf-8"))[
        "duplicate_sources"
    ] == "## Sources"


def test_body_sources_section_fails_even_when_values_and_model_pass(tmp_path):
    article = ARTICLE + "## Sources\n- [Xbox Wire](https://news.xbox.com/a)\n"
    report = verify_module.verify(
        article,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(model()),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.unmatched_numbers == []
    assert report.unsupported == []
    assert report.error is None
    assert report.passed is False


def test_duplicate_sources_is_none_when_body_has_none(tmp_path):
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(model()),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.duplicate_sources is None
    assert report.passed is True


# --- config ------------------------------------------------------------------


def test_load_verify_config_reads_whitelist(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "verify:\n"
        "  list_markers: '^\\s*[-*]\\s+'\n"
        "  number_whitelist:\n"
        "    - pattern: '^19\\d{2}$'\n"
        "    - pattern: '^20\\d{2}$'\n"
        "      context: '^#'\n",
        encoding="utf-8",
    )
    cfg = verify_module.load_verify_config(path)
    assert cfg["list_markers"] == r"^\s*[-*]\s+"
    assert cfg["whitelist"][0] == {"pattern": r"^19\d{2}$", "context": None}
    assert cfg["whitelist"][1] == {"pattern": r"^20\d{2}$", "context": "^#"}


def test_load_verify_config_defaults_when_absent(tmp_path):
    cfg = verify_module.load_verify_config(tmp_path / "missing.yaml")
    assert cfg["list_markers"] == verify_module.DEFAULT_LIST_MARKERS
    assert cfg["whitelist"] == []


def test_shipped_config_whitelists_year_in_heading():
    cfg = verify_module.load_verify_config()
    result = verify_module.code_check("## 2026 roadmap\nThe console costs $499.\n", sheet(), cfg)
    assert result["ok"] is True
