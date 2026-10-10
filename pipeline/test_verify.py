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
        "kind": "confirmed",
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


def model(clauses=None, unsupported=None, rumors=None, regional=None, costs=None, labels=None, categories=None):
    return {
        "clauses": clauses
        if clauses is not None
        else [{"clause": "The console costs $499", "supported": True, "fact_id": "F1"}],
        "unsupported_claims": unsupported or [],
        "rumors_stated_as_fact": rumors or [],
        "unsupported_regional": regional or [],
        "costs_described_as_received": costs or [],
        "invented_labels": labels or [],
        "category_claims": categories or [],
    }


def make_generate(value, *, finish_reason=None, usage=None, raw=None):
    def generate(role, prompt, *, run_state=None, **kwargs):
        return providers.Generation(
            role=role,
            provider="mistral",
            model="m",
            family="mistral",
            value=value,
            finish_reason=finish_reason,
            usage=usage,
            raw=raw,
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


def test_report_records_both_writer_families_after_a_repair(tmp_path):
    run_state = providers.RunState()
    run_state.record(providers.Generation("writer", "gemini", "g", "google", "DRAFT"))
    run_state.record(providers.Generation("writer", "groq", "o", "openai", "REPAIRED"))
    verify_module.verify(
        ARTICLE,
        sheet(id="abc"),
        run_state=run_state,
        generate=make_generate(model()),
        report_dir=tmp_path,
        verify_config=config(),
    )
    data = json.loads((tmp_path / "verify-abc.json").read_text(encoding="utf-8"))
    # The repaired text's family is the one checked against; both are recorded.
    assert data["writer_family"] == "openai"
    assert data["writer_families"] == ["google", "openai"]


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
        generate=make_generate(model([{"clause": "The console costs $599", "supported": True, "fact_id": "F1"}])),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert "$599" in report.unmatched_numbers


def test_invented_claim_fails_model_check(tmp_path):
    payload = model(
        clauses=[{"clause": "Free games forever", "supported": False}],
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


def anticheat_sheet():
    claims = [
        {
            "claim": f"Claim {i}",
            "value": f"Value {i}",
            "source_url": "https://tomshardware.example/arc-raiders",
            "confidence": 0.95,
            "kind": "confirmed",
        }
        for i in range(1, 10)
    ]
    claims += [
        {
            "claim": "Developer using AI anti-cheat for Arc Raiders",
            "value": (
                "Embark Studios is training in-house AI models using machine-learning "
                "and other data to detect cheating in Arc Raiders"
            ),
            "source_url": "https://tomshardware.example/arc-raiders",
            "confidence": 0.95,
            "kind": "confirmed",
        },
        {
            "claim": "Use of Denuvo anti-cheat",
            "value": "Embark Studios uses kernel-level anti-cheat from Denuvo",
            "source_url": "https://tomshardware.example/arc-raiders",
            "confidence": 0.95,
            "kind": "confirmed",
        },
        {
            "claim": "Detects cheaters using Anybrain's AI service",
            "value": "Embark Studios detects cheaters using Anybrain's AI service",
            "source_url": "https://tomshardware.example/arc-raiders",
            "confidence": 0.95,
            "kind": "confirmed",
        },
    ]
    return {
        "id": "arc-raiders-anticheat",
        "generated_at": "2026-10-09T12:00:00+00:00",
        "sources": [
            {
                "source_name": "Tom's Hardware",
                "link": "https://tomshardware.example/arc-raiders",
                "tier": 2,
                "owner": "future-plc",
                "region": "global",
            }
        ],
        "claims": claims,
    }


ANTICHEAT_SENTENCE = "Legitimate players receive increased security via Denuvo and AI anti-cheat."


def test_unsupported_benefit_sentence_fails_against_facts_F10_to_F12(tmp_path):
    payload = model(
        clauses=[
            {
                "clause": "Embark trains in-house AI models to detect cheating",
                "supported": True,
                "fact_id": "F10",
            },
            {
                "clause": "Uses kernel-level anti-cheat from Denuvo",
                "supported": True,
                "fact_id": "F11",
            },
            {
                "clause": "Detects cheaters using Anybrain's AI service",
                "supported": True,
                "fact_id": "F12",
            },
            {"clause": ANTICHEAT_SENTENCE, "supported": False},
        ],
        unsupported=[ANTICHEAT_SENTENCE],
    )
    report = verify_module.verify(
        ANTICHEAT_SENTENCE + "\n",
        anticheat_sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.failure_reason == "article_rejected"
    assert report.unmatched_numbers == []
    assert ANTICHEAT_SENTENCE in report.unsupported


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


# --- clause granularity and wording flags ------------------------------------


def dmz_sheet():
    return {
        "id": "dmz-1",
        "generated_at": "2026-10-09T12:00:00+00:00",
        "sources": [
            {
                "source_name": "Eurogamer",
                "link": "https://eurogamer.example/dmz",
                "tier": 2,
                "owner": "ign-entertainment",
                "region": "uk",
            }
        ],
        "claims": [
            {
                "claim": "Each deployment costs $100,000 DMZ Cash",
                "value": "$100,000 DMZ Cash",
                "source_url": "https://eurogamer.example/dmz",
                "confidence": 0.95,
                "kind": "confirmed",
            }
        ],
    }


DMZ_ARTICLE = "Deploy with vehicles or cash, and each deployment costs $100,000 DMZ Cash.\n"


def test_clause_level_check_flags_unsupported_part_of_mixed_sentence(tmp_path):
    payload = model(
        clauses=[
            {"clause": "Deploy with vehicles or cash", "supported": False},
            {
                "clause": "each deployment costs $100,000 DMZ Cash",
                "supported": True,
                "fact_id": "F1",
            },
        ],
        unsupported=["Deploy with vehicles or cash"],
    )
    report = verify_module.verify(
        DMZ_ARTICLE,
        dmz_sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.unsupported == ["Deploy with vehicles or cash"]
    assert len(report.model_sentences) == 2
    assert report.model_sentences[1]["fact_id"] == "F1"


def test_cost_described_as_received_fails(tmp_path):
    article = "Players get $100,000 DMZ Cash to deploy.\n"
    payload = model(
        clauses=[
            {
                "clause": "Players get $100,000 DMZ Cash to deploy",
                "supported": True,
                "fact_id": "F1",
            }
        ],
        costs=["Players get $100,000 DMZ Cash to deploy"],
    )
    report = verify_module.verify(
        article,
        dmz_sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert "Players get $100,000 DMZ Cash to deploy" in report.unsupported


def test_invented_label_mid_tier_options_fails(tmp_path):
    article = "Mid-tier options cost $100,000 DMZ Cash.\n"
    payload = model(
        clauses=[
            {
                "clause": "Mid-tier options cost $100,000 DMZ Cash",
                "supported": True,
                "fact_id": "F1",
            }
        ],
        labels=["mid-tier options"],
    )
    report = verify_module.verify(
        article,
        dmz_sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert "mid-tier options" in report.unsupported


def test_category_claim_fails(tmp_path):
    article = "The extraction shooter deployment costs $100,000 DMZ Cash.\n"
    payload = model(
        clauses=[
            {
                "clause": "The extraction shooter deployment costs $100,000 DMZ Cash",
                "supported": True,
                "fact_id": "F1",
            }
        ],
        categories=["the game is an extraction shooter"],
    )
    report = verify_module.verify(
        article,
        dmz_sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert "the game is an extraction shooter" in report.unsupported


def test_unsupported_list_is_deduplicated(tmp_path):
    payload = model(
        clauses=[{"clause": "Each deployment costs $100,000 DMZ Cash", "supported": False}],
        unsupported=["Each deployment costs $100,000 DMZ Cash"],
        costs=["Each deployment costs $100,000 DMZ Cash"],
    )
    report = verify_module.verify(
        DMZ_ARTICLE,
        dmz_sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.unsupported == ["Each deployment costs $100,000 DMZ Cash"]


def test_supported_clause_without_cited_fact_fails(tmp_path):
    payload = model(clauses=[{"clause": "The console costs $499", "supported": True}])
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.error is not None
    assert "without a cited fact_id" in report.error


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
        generate=make_generate({"clauses": "nope"}),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.error is not None
    assert "schema" in report.error


# --- failure classification and report fields --------------------------------


def test_truncated_output_is_a_failure_and_never_parsed(tmp_path):
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(
            '{"clauses":[{"clause":"The console costs $49', finish_reason="length",
            usage={"prompt_tokens": 10, "completion_tokens": 5}, raw={"choices": []},
        ),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.failure_reason == "verifier_output_invalid"
    assert report.finish_reason == "length"
    assert "truncated" in report.error
    data = json.loads((tmp_path / "verify-story-1.json").read_text(encoding="utf-8"))
    assert data["failure_reason"] == "verifier_output_invalid"
    assert data["finish_reason"] == "length"
    assert data["token_usage"] == {"prompt_tokens": 10, "completion_tokens": 5}
    assert data["raw_response"] == {"choices": []}


def test_empty_output_classified_as_verifier_output_invalid(tmp_path):
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(""),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.failure_reason == "verifier_output_invalid"
    assert "no output" in report.error


def test_malformed_verifier_output_is_verifier_output_invalid(tmp_path):
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate("no json here"),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.failure_reason == "verifier_output_invalid"


def test_supported_clause_with_unknown_fact_id_fails(tmp_path):
    payload = model(clauses=[{"clause": "The console costs $499", "supported": True, "fact_id": "F99"}])
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.failure_reason == "verifier_output_invalid"
    assert "fact_id not in the sheet" in report.error


def test_unsupported_article_is_article_rejected(tmp_path):
    payload = model(clauses=[{"clause": "The console costs $499", "supported": True, "fact_id": "F1"}])
    report = verify_module.verify(
        "The console costs $599 and a $899 bundle.\n",
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(payload),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is False
    assert report.failure_reason == "article_rejected"
    data = json.loads((tmp_path / "verify-story-1.json").read_text(encoding="utf-8"))
    assert data["failure_reason"] == "article_rejected"


def test_invented_claim_is_article_rejected(tmp_path):
    payload = model(
        clauses=[{"clause": "Free games forever", "supported": False}],
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
    assert report.failure_reason == "article_rejected"
    assert report.error is None


def test_passing_report_has_no_failure_reason(tmp_path):
    report = verify_module.verify(
        ARTICLE,
        sheet(),
        run_state=providers.RunState(),
        generate=make_generate(model()),
        report_dir=tmp_path,
        verify_config=config(),
    )
    assert report.passed is True
    assert report.failure_reason is None


def test_build_prompt_numbers_facts_and_cites_ids():
    prompt = verify_module.build_prompt("Body.", sheet(claim("The console costs $499")))
    assert "F1: 'Console price'" in prompt
    assert "fact_id" in prompt


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
    assert tokens.count("D:2026-10-09") == 2


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


# --- dates -------------------------------------------------------------------

SINGLE_DATE_FORMS = [
    "October 13",
    "13 October",
    "13th October",
    "October 13th",
]

RANGE_DATE_FORMS = [
    "13th to 20th October",
    "October 13 to October 20",
    "Oct 13-20",
]


def date_sheet(form):
    return sheet(claim(value=form))


@pytest.mark.parametrize("form", SINGLE_DATE_FORMS + RANGE_DATE_FORMS)
def test_each_date_form_matches_facts_in_the_same_form(form):
    assert verify_module.code_check(form, date_sheet(form), config())["ok"]


def test_dates_match_across_formats_ordinals_and_order():
    facts = date_sheet("13th to 20th October")
    for form in RANGE_DATE_FORMS:
        assert verify_module.code_check(form, facts, config())["ok"], form

    facts = date_sheet("October 13")
    for form in SINGLE_DATE_FORMS:
        assert verify_module.code_check(form, facts, config())["ok"], form


def test_weekday_prefixed_date_variants_match():
    assert verify_module.code_check("Thursday, October 13", date_sheet("October 13"), config())["ok"]
    assert verify_module.code_check("Thursday, October 13", date_sheet("13 October"), config())["ok"]
    assert verify_module.code_check("Sat 13th to 20th October", date_sheet("Oct 13-20"), config())["ok"]
    assert verify_module.code_check("Friday, Oct 13-20", date_sheet("13th to 20th October"), config())["ok"]


def test_year_matches_only_when_both_sides_state_it():
    facts = date_sheet("13 October 2026")
    assert verify_module.code_check("October 13, 2026", facts, config())["ok"]
    assert verify_module.code_check("October 13", facts, config())["ok"]
    assert verify_module.code_check("13 October 2026", facts, config())["ok"]
    assert verify_module.code_check("October 13, 2025", facts, config())["ok"] is False

    facts = date_sheet("October 13")
    assert verify_module.code_check("October 13, 2026", facts, config())["ok"]


def test_article_date_absent_from_facts_still_fails():
    result = verify_module.code_check("It ships October 21.", date_sheet("13th to 20th October"), config())
    assert result["ok"] is False
    assert "October 21" in result["unmatched"]

    result = verify_module.code_check("It ships Oct 13-20.", date_sheet("October 21"), config())
    assert result["ok"] is False
    assert "Oct 13-20" in result["unmatched"]


def test_different_month_still_fails():
    result = verify_module.code_check("It ships November 13.", date_sheet("October 13"), config())
    assert result["ok"] is False
    assert "November 13" in result["unmatched"]


def test_ordinal_in_non_date_context_creates_no_date():
    result = verify_module.code_check(
        "On the 3rd attempt it worked.", date_sheet("October 13"), config()
    )
    assert result["ok"] is True
    assert result["checked"] == 0


def test_bare_day_number_inside_date_range_is_covered_by_dates():
    facts = date_sheet("13th to 20th October")
    result = verify_module.code_check("Available from October 13 to October 20.", facts, config())
    assert result["ok"] is True
    assert result["checked"] == 2


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
