import logging

import pytest

import providers


class FakeProvider(providers.Provider):
    def __init__(self, name, results=None, *, env_key=None):
        self.name = name
        self.env_key = env_key or f"{name.upper()}_KEY"
        self.results = list(results or [])
        self.calls = []

    def complete(self, model, prompt, *, api_key, json_schema=None, transport=None):
        self.calls.append(
            {"model": model, "prompt": prompt, "api_key": api_key, "json_schema": json_schema}
        )
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return "ok"


def make_config(role_chain, max_attempts=3, role="writer"):
    return {
        "roles": {role: role_chain},
        "retry": {
            "max_attempts": max_attempts,
            "base_delay_seconds": 1,
            "max_delay_seconds": 30,
        },
    }


def chain(*triples):
    return [{"provider": p, "model": m, "family": f} for p, m, f in triples]


def keyed(*names):
    return {f"{name.upper()}_KEY": "k" for name in names}


def test_dry_run_returns_generation_with_family():
    result = providers.generate("writer", "hello", dry_run=True)
    assert isinstance(result, providers.Generation)
    assert result.provider == "mock"
    assert result.model == "gemini-3.5-flash"
    assert result.family == "google"
    assert result.value == "[mock:gemini-3.5-flash] hello"


def test_dry_run_with_schema_returns_dict():
    schema = {
        "type": "object",
        "properties": {"claim": {"type": "string"}, "score": {"type": "integer"}},
        "required": ["claim", "score"],
    }
    result = providers.generate("writer", "hello", schema, dry_run=True)
    assert result.value == {"claim": "mock", "score": 0}


def test_json_schema_result_is_parsed():
    provider = FakeProvider("a", ['{"claim": "x"}'])
    result = providers.generate(
        "writer", "p", {"type": "object"}, config=make_config(chain(("a", "m", "fam"))),
        providers={"a": provider}, env=keyed("a"),
    )
    assert result.value == {"claim": "x"}


def test_code_fences_are_stripped():
    provider = FakeProvider("a", ['```json\n{"ok": true}\n```'])
    result = providers.generate(
        "writer", "p", {"type": "object"}, config=make_config(chain(("a", "m", "fam"))),
        providers={"a": provider}, env=keyed("a"),
    )
    assert result.value == {"ok": True}


def test_generation_reports_provider_model_family():
    provider = FakeProvider("a", ["text"])
    result = providers.generate(
        "writer", "p", config=make_config(chain(("a", "model-from-config", "family-x"))),
        providers={"a": provider}, env=keyed("a"),
    )
    assert (result.provider, result.model, result.family) == ("a", "model-from-config", "family-x")
    assert provider.calls[0]["model"] == "model-from-config"


def test_fallback_on_non_retryable_error():
    first = FakeProvider("a", [providers.ProviderError("bad request", retryable=False)])
    second = FakeProvider("b", ["from-b"])
    sleeps = []
    result = providers.generate(
        "writer", "p", config=make_config(chain(("a", "m1", "fa"), ("b", "m2", "fb"))),
        providers={"a": first, "b": second}, env=keyed("a", "b"),
        sleep=sleeps.append,
    )
    assert result.value == "from-b"
    assert result.provider == "b"
    assert result.family == "fb"
    assert len(result.fallbacks) == 1
    assert "bad request" in result.fallbacks[0]
    assert sleeps == []


def test_retry_then_success_with_backoff():
    provider = FakeProvider(
        "a",
        [
            providers.ProviderError("rate limited", retryable=True),
            providers.ProviderError("server error", retryable=True),
            "done",
        ],
    )
    sleeps = []
    result = providers.generate(
        "writer", "p", config=make_config(chain(("a", "m", "fam")), max_attempts=3),
        providers={"a": provider}, env=keyed("a"), sleep=sleeps.append,
    )
    assert result.value == "done"
    assert sleeps == [1, 2]
    assert len(provider.calls) == 3
    assert result.fallbacks == []


def test_retry_exhausted_then_falls_back():
    first = FakeProvider("a", [providers.ProviderError("429", retryable=True)] * 2)
    second = FakeProvider("b", ["from-b"])
    result = providers.generate(
        "writer", "p", config=make_config(chain(("a", "m1", "fa"), ("b", "m2", "fb")), max_attempts=2),
        providers={"a": first, "b": second}, env=keyed("a", "b"),
        sleep=lambda _delay: None,
    )
    assert result.value == "from-b"
    assert len(first.calls) == 2


def test_invalid_json_falls_back_to_next_provider():
    first = FakeProvider("a", ["this is not json"])
    second = FakeProvider("b", ['{"ok": true}'])
    result = providers.generate(
        "writer", "p", {"type": "object"},
        config=make_config(chain(("a", "m1", "fa"), ("b", "m2", "fb"))),
        providers={"a": first, "b": second}, env=keyed("a", "b"),
        sleep=lambda _delay: None,
    )
    assert result.value == {"ok": True}


def test_missing_api_key_skips_provider():
    first = FakeProvider("a", ["never"], env_key="A_MISSING")
    second = FakeProvider("b", ["from-b"])
    result = providers.generate(
        "writer", "p", config=make_config(chain(("a", "m1", "fa"), ("b", "m2", "fb"))),
        providers={"a": first, "b": second}, env=keyed("b"),
    )
    assert result.value == "from-b"
    assert first.calls == []


def test_unknown_provider_is_skipped():
    second = FakeProvider("b", ["from-b"])
    result = providers.generate(
        "writer", "p", config=make_config(chain(("ghost", "m1", "x"), ("b", "m2", "fb"))),
        providers={"b": second}, env=keyed("b"),
    )
    assert result.value == "from-b"


def test_all_providers_fail_raises_skiprun():
    first = FakeProvider("a", [providers.ProviderError("down", retryable=True)])
    second = FakeProvider("b", [providers.ProviderError("down", retryable=False)])
    with pytest.raises(providers.SkipRun) as excinfo:
        providers.generate(
            "writer", "p", config=make_config(chain(("a", "m1", "fa"), ("b", "m2", "fb")), max_attempts=1),
            providers={"a": first, "b": second}, env=keyed("a", "b"),
            sleep=lambda _delay: None,
        )
    assert "role 'writer'" in str(excinfo.value)


def test_unknown_role_raises_value_error():
    with pytest.raises(ValueError, match="unknown role"):
        providers.generate("narrator", "p", config=make_config([]))


# --- writer/verifier family guard (PLAN.md principle 4) ----------------------


def test_verifier_uses_non_writer_family_after_writer_fallback():
    writer_chain = chain(("gemini", "g-model", "google"), ("mistral", "m-model", "mistral"))
    verifier_chain = chain(("mistral", "m-model", "mistral"), ("gemini", "g-model", "google"))
    config = {
        "roles": {"writer": writer_chain, "verifier": verifier_chain},
        "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 30},
    }
    gemini = FakeProvider("gemini", [providers.ProviderError("down", retryable=False)])
    mistral = FakeProvider("mistral", ["written"])
    run = providers.RunState()

    writer = providers.generate(
        "writer", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
        env=keyed("gemini", "mistral"), run_state=run, sleep=lambda _d: None,
    )
    assert writer.family == "mistral"
    assert run.writer_family == "mistral"

    verifier = providers.generate(
        "verifier", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
        env=keyed("gemini", "mistral"), run_state=run, sleep=lambda _d: None,
    )
    assert verifier.provider == "gemini"
    assert verifier.family == "google"
    assert verifier.family != writer.family
    assert len(mistral.calls) == 1  # mistral served the writer only, skipped for verifier


def test_verifier_skiprun_when_every_verifier_shares_writer_family():
    config = {
        "roles": {
            "writer": chain(("gemini", "g", "google"), ("mistral", "m", "mistral")),
            "verifier": chain(("mistral", "m", "mistral"), ("mistral", "m2", "mistral")),
        },
        "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 30},
    }
    gemini = FakeProvider("gemini", [providers.ProviderError("down", retryable=False)])
    mistral = FakeProvider("mistral", ["written", "verified"])
    run = providers.RunState()

    providers.generate(
        "writer", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
        env=keyed("gemini", "mistral"), run_state=run, sleep=lambda _d: None,
    )
    assert run.writer_family == "mistral"

    with pytest.raises(providers.SkipRun) as excinfo:
        providers.generate(
            "verifier", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
            env=keyed("gemini", "mistral"), run_state=run, sleep=lambda _d: None,
        )
    message = str(excinfo.value)
    assert "different model families" in message
    assert "mistral" in message


def test_verifier_normal_case_uses_different_family():
    config = {
        "roles": {
            "writer": chain(("gemini", "g", "google")),
            "verifier": chain(("mistral", "m", "mistral"), ("gemini", "g", "google")),
        },
        "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 30},
    }
    gemini = FakeProvider("gemini", ["written", "verified"])
    mistral = FakeProvider("mistral", ["verified"])
    run = providers.RunState()

    writer = providers.generate(
        "writer", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
        env=keyed("gemini", "mistral"), run_state=run,
    )
    verifier = providers.generate(
        "verifier", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
        env=keyed("gemini", "mistral"), run_state=run,
    )
    assert writer.family == "google"
    assert verifier.family == "mistral"
    assert writer.family != verifier.family


# --- shipped-config family pairings (PLAN.md principle 4) --------------------
# These mirror the configure chains: gemini(google) + groq(openai) + mistral.
# Every writer family must leave the verifier a different-family model.


def test_pairing_gemini_writer_with_groq_verifier():
    config = {
        "roles": {
            "writer": chain(("gemini", "g", "google"), ("groq", "x", "openai"), ("mistral", "m", "mistral")),
            "verifier": chain(("groq", "x", "openai"), ("gemini", "g", "google"), ("mistral", "m", "mistral")),
        },
        "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 30},
    }
    gemini = FakeProvider("gemini", ["written", "never"])
    groq = FakeProvider("groq", ["verified"])
    mistral = FakeProvider("mistral", ["never"])
    run = providers.RunState()

    writer = providers.generate(
        "writer", "p", config=config,
        providers={"gemini": gemini, "groq": groq, "mistral": mistral},
        env=keyed("gemini", "groq", "mistral"), run_state=run,
    )
    verifier = providers.generate(
        "verifier", "p", config=config,
        providers={"gemini": gemini, "groq": groq, "mistral": mistral},
        env=keyed("gemini", "groq", "mistral"), run_state=run,
    )
    assert writer.family == "google"
    assert verifier.provider == "groq"
    assert verifier.family == "openai"
    assert verifier.family != writer.family
    assert len(gemini.calls) == 1  # gemini writer only; excluded from the verifier


def test_pairing_groq_writer_with_gemini_verifier():
    config = {
        "roles": {
            "writer": chain(("gemini", "g", "google"), ("groq", "x", "openai"), ("mistral", "m", "mistral")),
            "verifier": chain(("groq", "x", "openai"), ("gemini", "g", "google"), ("mistral", "m", "mistral")),
        },
        "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 30},
    }
    gemini = FakeProvider("gemini", [providers.ProviderError("down", retryable=False), "verified"])
    groq = FakeProvider("groq", ["written", "never"])
    mistral = FakeProvider("mistral", ["never"])
    run = providers.RunState()

    writer = providers.generate(
        "writer", "p", config=config,
        providers={"gemini": gemini, "groq": groq, "mistral": mistral},
        env=keyed("gemini", "groq", "mistral"), run_state=run,
    )
    verifier = providers.generate(
        "verifier", "p", config=config,
        providers={"gemini": gemini, "groq": groq, "mistral": mistral},
        env=keyed("gemini", "groq", "mistral"), run_state=run,
    )
    assert writer.provider == "groq"
    assert writer.family == "openai"
    assert verifier.provider == "gemini"
    assert verifier.family == "google"
    assert verifier.family != writer.family
    assert len(groq.calls) == 1  # groq writer only; excluded from the verifier


def test_pairing_mistral_writer_with_groq_verifier():
    config = {
        "roles": {
            "writer": chain(("gemini", "g", "google"), ("groq", "x", "openai"), ("mistral", "m", "mistral")),
            "verifier": chain(("groq", "x", "openai"), ("gemini", "g", "google"), ("mistral", "m", "mistral")),
        },
        "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 30},
    }
    gemini = FakeProvider("gemini", [providers.ProviderError("down", retryable=False), "never"])
    groq = FakeProvider("groq", [providers.ProviderError("down", retryable=False), "verified"])
    mistral = FakeProvider("mistral", ["written", "never"])
    run = providers.RunState()

    writer = providers.generate(
        "writer", "p", config=config,
        providers={"gemini": gemini, "groq": groq, "mistral": mistral},
        env=keyed("gemini", "groq", "mistral"), run_state=run,
    )
    verifier = providers.generate(
        "verifier", "p", config=config,
        providers={"gemini": gemini, "groq": groq, "mistral": mistral},
        env=keyed("gemini", "groq", "mistral"), run_state=run,
    )
    assert writer.provider == "mistral"
    assert writer.family == "mistral"
    assert verifier.provider == "groq"
    assert verifier.family == "openai"
    assert verifier.family != writer.family


def test_shipped_config_guard_holds_for_every_writer_family():
    def enabled(entries):
        return [entry for entry in entries if entry.get("enabled", True)]

    config = providers.load_config()
    writer_families = {entry["family"] for entry in enabled(config["roles"]["writer"])}
    verifier = enabled(config["roles"]["verifier"])
    for family in writer_families:
        remaining = [entry for entry in verifier if entry["family"] != family]
        assert remaining, f"verifier has no eligible model when writer family is {family}"
        assert any(entry.get("enabled", True) for entry in remaining)


def test_disabled_entries_are_never_called():
    config = {
        "roles": {"writer": chain(("gemini", "g", "google"), ("mistral", "m", "mistral"))},
        "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 30},
    }
    config["roles"]["writer"][1]["enabled"] = False
    gemini = FakeProvider("gemini", ["ok"])
    mistral = FakeProvider("mistral", ["never"])

    result = providers.generate(
        "writer", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
        env=keyed("gemini", "mistral"), dry_run=False,
    )
    assert result.provider == "gemini"
    assert result.model == "g"
    assert mistral.calls == []


def test_all_disabled_role_raises_skiprun():
    config = make_config(chain(("gemini", "g", "google")))
    config["roles"]["writer"][0]["enabled"] = False
    with pytest.raises(providers.SkipRun, match="disabled"):
        providers.generate(
            "writer", "p", config=config, providers={"gemini": FakeProvider("gemini", ["x"])},
            env=keyed("gemini"), dry_run=False,
        )


def test_verifier_without_writer_family_raises_and_skips_provider():
    config = make_config(chain(("mistral", "m", "mistral")), role="verifier")
    mistral = FakeProvider("mistral", ["never"])
    with pytest.raises(providers.MissingWriterFamilyError):
        providers.generate(
            "verifier", "p", config=config, providers={"mistral": mistral}, env=keyed("mistral"),
        )
    assert mistral.calls == []


def test_verifier_with_empty_run_state_raises():
    config = make_config(chain(("mistral", "m", "mistral")), role="verifier")
    run = providers.RunState()
    mistral = FakeProvider("mistral", ["never"])
    with pytest.raises(providers.MissingWriterFamilyError):
        providers.generate(
            "verifier", "p", config=config, providers={"mistral": mistral},
            env=keyed("mistral"), run_state=run,
        )
    assert mistral.calls == []


def test_verifier_dry_run_without_state_is_allowed():
    config = make_config(chain(("mistral", "m", "mistral")), role="verifier")
    result = providers.generate("verifier", "p", config=config, dry_run=True)
    assert result.provider == "mock"
    assert result.family == "mistral"


def test_verifier_all_mock_chain_without_state_is_allowed():
    config = make_config(chain(("mock", "m", "mistral")), role="verifier")
    result = providers.generate(
        "verifier", "p", config=config, providers={"mock": providers.MockProvider()},
        env={}, dry_run=False,
    )
    assert result.provider == "mock"
    assert result.family == "mistral"


def test_explicit_exclude_families_filters_any_role():
    config = make_config(chain(("gemini", "g", "google"), ("mistral", "m", "mistral")))
    gemini = FakeProvider("gemini", ["never"])
    mistral = FakeProvider("mistral", ["ok"])
    result = providers.generate(
        "writer", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
        env=keyed("gemini", "mistral"), exclude_families={"google"},
    )
    assert result.provider == "mistral"
    assert gemini.calls == []


def test_explicit_exclude_families_skiprun_when_empty():
    config = make_config(chain(("gemini", "g", "google")))
    with pytest.raises(providers.SkipRun, match="different"):
        providers.generate(
            "writer", "p", config=config, providers={"gemini": FakeProvider("gemini")},
            env=keyed("gemini"), exclude_families={"google"},
        )


def test_run_report_lists_families_and_fallbacks():
    config = {
        "roles": {
            "writer": chain(("gemini", "g", "google"), ("mistral", "m", "mistral")),
            "verifier": chain(("mistral", "m", "mistral"), ("gemini", "g", "google")),
        },
        "retry": {"max_attempts": 1, "base_delay_seconds": 1, "max_delay_seconds": 30},
    }
    gemini = FakeProvider("gemini", [providers.ProviderError("down", retryable=False), "verified"])
    mistral = FakeProvider("mistral", ["written"])
    run = providers.RunState()

    providers.generate(
        "writer", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
        env=keyed("gemini", "mistral"), run_state=run, sleep=lambda _d: None,
    )
    providers.generate(
        "verifier", "p", config=config, providers={"gemini": gemini, "mistral": mistral},
        env=keyed("gemini", "mistral"), run_state=run, sleep=lambda _d: None,
    )

    report = run.report()
    assert report["writer_family"] == "mistral"
    assert report["verifier_family"] == "google"
    assert report["writer"]["provider"] == "mistral"
    assert report["verifier"]["provider"] == "gemini"
    assert any("google" in entry for entry in report["fallbacks"])


def test_generate_logs_chosen_family(caplog):
    provider = FakeProvider("a", ["text"])
    with caplog.at_level(logging.INFO, logger="gamersxpress.pipeline"):
        providers.generate(
            "writer", "p", config=make_config(chain(("a", "m", "fam"))),
            providers={"a": provider}, env=keyed("a"),
        )
    assert any("family=fam" in record.getMessage() for record in caplog.records)


# --- provider wire formats ---------------------------------------------------


def test_gemini_payload_and_extract():
    provider = providers.GeminiProvider()
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    url, payload, headers = provider.prepare("gemini-x", "hi", "KEY", schema)
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent"
    assert headers["x-goog-api-key"] == "KEY"
    assert payload["contents"][0]["parts"][0]["text"] == "hi"
    generation = payload["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseSchema"]["type"] == "OBJECT"
    assert generation["responseSchema"]["properties"]["a"]["type"] == "STRING"
    assert "additionalProperties" not in generation["responseSchema"]

    data = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
    assert provider.extract(data) == "hello"


def test_gemini_extract_handles_blocked_response():
    provider = providers.GeminiProvider()
    with pytest.raises(providers.ProviderError) as excinfo:
        provider.extract({"promptFeedback": {"blockReason": "SAFETY"}})
    assert excinfo.value.retryable is False


def test_mistral_payload_and_extract():
    provider = providers.MistralProvider()
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    url, payload, headers = provider.prepare("mistral-x", "hi", "KEY", schema)
    assert url == "https://api.mistral.ai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer KEY"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"] == schema
    assert payload["response_format"]["json_schema"]["strict"] is True

    data = {"choices": [{"message": {"content": "hello"}}]}
    assert provider.extract(data) == "hello"


def test_groq_is_registered_with_groq_key():
    provider = providers.default_providers()["groq"]
    assert isinstance(provider, providers.GroqProvider)
    assert provider.env_key == "GROQ_API_KEY"
    assert provider.base_url == "https://api.groq.com/openai/v1"


def test_groq_payload_and_extract():
    provider = providers.GroqProvider()
    url, payload, headers = provider.prepare("openai/gpt-oss-20b", "hi", "KEY", None)
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer KEY"
    assert payload["model"] == "openai/gpt-oss-20b"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]

    data = {"choices": [{"message": {"content": "hello"}}]}
    assert provider.extract(data) == "hello"


def test_http_post_json_sends_identifying_user_agent(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured["ua"] = request.get_header("User-agent")
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    result = providers.http_post_json(
        "https://api.groq.com/openai/v1/chat/completions", {"a": 1}, {"Content-Type": "application/json"}
    )
    assert result == {"ok": True}
    assert captured["url"].startswith("https://api.groq.com/openai/v1/chat/completions")
    assert captured["ua"] == "GamersXpress/0.1 (+https://gamersxpress.com)"


def test_complete_uses_injected_transport():
    provider = providers.GeminiProvider()
    captured = {}

    def transport(url, payload, headers):
        captured["url"] = url
        return {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}

    assert provider.complete("m", "p", api_key="k", transport=transport) == "x"
    assert captured["url"].endswith(":generateContent")


@pytest.mark.parametrize(
    "status,expected",
    [(429, True), (500, True), (503, True), (408, False), (400, False), (404, False)],
)
def test_retryable_status_classification(status, expected):
    assert providers._is_retryable_status(status) is expected


# --- config and env ----------------------------------------------------------


def test_load_config_reads_roles(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "roles:\n  fast:\n    - provider: gemini\n      model: m\n      family: google\n"
        "retry:\n  max_attempts: 5\n",
        encoding="utf-8",
    )
    config = providers.load_config(path)
    assert config["roles"]["fast"] == [{"provider": "gemini", "model": "m", "family": "google"}]
    assert config["retry"]["max_attempts"] == 5


def test_load_config_rejects_missing_roles(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("retry:\n  max_attempts: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="roles"):
        providers.load_config(path)


def test_load_config_rejects_missing_family(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "roles:\n  fast:\n    - provider: gemini\n      model: m\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="family"):
        providers.load_config(path)


def test_load_env_reads_file_without_overriding_existing(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\nGEMINI_API_KEY=file-key\nMISTRAL_API_KEY=\"quoted\"\nBAD LINE\n",
        encoding="utf-8",
    )
    env = providers.load_env(path, base={"GEMINI_API_KEY": "real-key"})
    assert env["GEMINI_API_KEY"] == "real-key"
    assert env["MISTRAL_API_KEY"] == "quoted"
    assert "BAD LINE" not in env


def test_shipped_config_has_all_roles_and_families():
    config = providers.load_config()
    for role in providers.VALID_ROLES:
        assert config["roles"].get(role), f"missing role {role}"
    for entries in config["roles"].values():
        for entry in entries:
            assert entry["provider"] and entry["model"] and entry["family"]
