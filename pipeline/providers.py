"""Role-based LLM generation with provider fallback.

``generate(role, prompt, json_schema=None)`` looks up the ordered provider/model
chain for a role in ``pipeline/config.yaml`` and tries each provider until one
succeeds, returning a :class:`Generation` with the provider, model and model
family that actually served the call. Rate limits and transient server errors
are retried with exponential backoff before moving on to the next provider. If
every provider for the role fails, :class:`SkipRun` is raised so callers skip
the run instead of publishing something unverified (see docs/PLAN.md sections
3.5 and 8).

The writer and verifier must use different model families (PLAN.md principle
4). Callers pass a shared :class:`RunState` across stages; the writer's actual
family is recorded on it, and when the verifier runs its chain is filtered to
remove that family. This is fail-closed: a verifier call with no recorded
writer family raises :class:`MissingWriterFamilyError` without calling any
provider, and if filtering leaves nothing :class:`SkipRun` is raised. Only
``dry_run`` and all-mock chains bypass the guard.

Model names, provider order and families are never hardcoded here; they live in
``pipeline/config.yaml``.

Adding a provider
-----------------
Services that speak the OpenAI chat-completions dialect (Groq, Cerebras,
OpenRouter) only need to subclass :class:`OpenAICompatibleProvider` and set
``name``, ``env_key`` and ``base_url``. Then register an instance in
:func:`default_providers`. Models and order still stay in YAML.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config.yaml")
VALID_ROLES = ("fast", "writer", "verifier")

logger = logging.getLogger("gamersxpress.pipeline")

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_GEMINI_DROP_KEYS = {
    "additionalProperties",
    "$schema",
    "$defs",
    "definitions",
    "title",
    "default",
}

# Honest, identifying User-Agent for API calls to LLM providers. Sent on every
# HTTP request so the providers and the models-listing tool identify us.
_USER_AGENT = "GamersXpress/0.1 (+https://gamersxpress.com)"


class SkipRun(Exception):
    """Raised when every provider for a role is unavailable.

    Callers must treat this as "publish nothing" rather than a crash.
    """


class MissingWriterFamilyError(Exception):
    """Raised when the verifier runs without a recorded writer family.

    This is fail-closed: without knowing the writer's model family we cannot
    guarantee the verifier uses a different one (PLAN.md principle 4), so no
    provider is called. It is distinct from :class:`SkipRun` because it is a
    wiring bug, not a provider outage: run.py counts it as a failed run.
    """


class ProviderError(Exception):
    """A provider call failed. ``retryable`` marks transient failures."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class Generation:
    """The result of one successful ``generate`` call.

    ``value`` is the text, or parsed JSON when a ``json_schema`` was given.
    ``family`` is the underlying model family (from config.yaml), which is what
    guarantees the writer and verifier stay on different model families.
    ``fallbacks`` lists the providers that were skipped or failed before this
    one succeeded.
    """

    role: str
    provider: str
    model: str
    family: str
    value: Any
    fallbacks: list[str] = field(default_factory=list)

    @property
    def text(self) -> Any:
        return self.value


@dataclass
class RunState:
    """In-memory run state shared across pipeline stages.

    Records which provider/model/family actually served each role so the
    verifier can be forced onto a different family, and so the run report can
    show the writer family, verifier family, and any fallbacks.
    """

    generations: dict[str, Generation] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def record(self, generation: Generation) -> None:
        self.generations[generation.role] = generation

    def add_failures(self, messages: list[str]) -> None:
        self.failures.extend(messages)

    @property
    def writer_family(self) -> str | None:
        writer = self.generations.get("writer")
        return writer.family if writer else None

    @property
    def verifier_family(self) -> str | None:
        verifier = self.generations.get("verifier")
        return verifier.family if verifier else None

    def fallbacks(self) -> list[str]:
        collected = list(self.failures)
        for generation in self.generations.values():
            collected.extend(generation.fallbacks)
        return collected

    def report(self) -> dict:
        def describe(generation: Generation | None) -> dict | None:
            if generation is None:
                return None
            return {
                "provider": generation.provider,
                "model": generation.model,
                "family": generation.family,
            }

        return {
            "writer": describe(self.generations.get("writer")),
            "verifier": describe(self.generations.get("verifier")),
            "writer_family": self.writer_family,
            "verifier_family": self.verifier_family,
            "fallbacks": self.fallbacks(),
        }


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def http_post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float = 60,
) -> dict:
    """POST JSON and return the parsed response, raising ProviderError on failure."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={**dict(headers), "User-Agent": _USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = _safe_read(exc)
        raise ProviderError(
            f"HTTP {exc.code} from {url}: {detail}",
            retryable=_is_retryable_status(exc.code),
            status=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"network error calling {url}: {exc.reason}", retryable=True) from exc
    except TimeoutError as exc:
        raise ProviderError(f"timeout calling {url}", retryable=True) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid JSON from {url}: {exc}", retryable=True) from exc


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:500]
    except Exception:
        return "<no body>"


def _strip_code_fences(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


def _parse_json(text: str) -> Any:
    try:
        return json.loads(_strip_code_fences(text))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"provider returned invalid JSON: {exc}", retryable=True) from exc


class Provider:
    """Base class for a single LLM service."""

    name = ""
    env_key = ""

    def prepare(
        self,
        model: str,
        prompt: str,
        api_key: str,
        json_schema: dict | None,
    ) -> tuple[str, dict, dict]:
        raise NotImplementedError

    def extract(self, data: dict) -> str:
        raise NotImplementedError

    def complete(
        self,
        model: str,
        prompt: str,
        *,
        api_key: str,
        json_schema: dict | None = None,
        transport: Callable[..., dict] | None = None,
    ) -> str:
        transport = transport or http_post_json
        url, payload, headers = self.prepare(model, prompt, api_key, json_schema)
        return self.extract(transport(url, payload, headers))


class OpenAICompatibleProvider(Provider):
    """Base for OpenAI chat-completions compatible services."""

    base_url = ""

    def prepare(
        self,
        model: str,
        prompt: str,
        api_key: str,
        json_schema: dict | None,
    ) -> tuple[str, dict, dict]:
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema, "strict": True},
            }
        return url, payload, headers

    def extract(self, data: dict) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {exc}", retryable=True) from exc
        if content is None:
            raise ProviderError("empty content in response", retryable=True)
        return content


class MistralProvider(OpenAICompatibleProvider):
    name = "mistral"
    env_key = "MISTRAL_API_KEY"
    base_url = "https://api.mistral.ai/v1"


class GroqProvider(OpenAICompatibleProvider):
    """GroqCloud (api.groq.com). OpenAI chat-completions dialect.

    Base URL and authentication follow console.groq.com/docs/api-reference:
    ``https://api.groq.com/openai/v1`` with a ``Bearer`` token. Model ids are
    namespaced by their upstream (for example ``meta-llama/llama-4-...`` for
    Meta's Llama, ``openai/gpt-oss-120b`` for OpenAI); the config ``family``
    must still describe the underlying model, not Groq.
    """

    name = "groq"
    env_key = "GROQ_API_KEY"
    base_url = "https://api.groq.com/openai/v1"


class GeminiProvider(Provider):
    """Google AI Studio (Gemini API) generateContent."""

    name = "gemini"
    env_key = "GEMINI_API_KEY"
    base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    def prepare(
        self,
        model: str,
        prompt: str,
        api_key: str,
        json_schema: dict | None,
    ) -> tuple[str, dict, dict]:
        url = f"{self.base_url}/{model}:generateContent"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        payload: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
        if json_schema is not None:
            payload["generationConfig"] = {
                "responseMimeType": "application/json",
                "responseSchema": _to_gemini_schema(json_schema),
            }
        return url, payload, headers

    def extract(self, data: dict) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            raise ProviderError(f"no candidates (blocked?): {feedback}", retryable=False)
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        if not text:
            raise ProviderError(
                f"empty response (finishReason={candidate.get('finishReason')})",
                retryable=True,
            )
        return text


class MockProvider(Provider):
    """Deterministic offline provider used for --dry-run and tests."""

    name = "mock"
    env_key = "MOCK_API_KEY"

    def complete(
        self,
        model: str,
        prompt: str,
        *,
        api_key: str = "",
        json_schema: dict | None = None,
        transport: Callable[..., dict] | None = None,
    ) -> str:
        if json_schema is not None:
            return json.dumps(_sample_from_schema(json_schema))
        return f"[mock:{model}] {prompt}"


def _to_gemini_schema(schema: Any) -> Any:
    """Translate standard JSON Schema to the Gemini responseSchema subset."""
    if isinstance(schema, list):
        return [_to_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _GEMINI_DROP_KEYS:
            continue
        if key == "type" and isinstance(value, str):
            out[key] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            out[key] = {prop: _to_gemini_schema(sub) for prop, sub in value.items()}
        elif key in ("items", "not"):
            out[key] = _to_gemini_schema(value)
        elif key in ("anyOf", "oneOf", "allOf"):
            out[key] = [_to_gemini_schema(item) for item in value]
        else:
            out[key] = value
    return out


def _sample_from_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return None
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = schema_type[0]
    schema_type = schema_type.lower() if isinstance(schema_type, str) else "object"
    if schema_type == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required") or list(properties)
        return {key: _sample_from_schema(properties.get(key, {})) for key in required}
    if schema_type == "array":
        return [_sample_from_schema(schema.get("items", {}))]
    if schema_type == "string":
        return (schema.get("enum") or ["mock"])[0]
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0.0
    if schema_type == "boolean":
        return False
    return None


def default_providers() -> dict[str, Provider]:
    """Registered providers. Add Cerebras/OpenRouter here when needed."""
    return {
        "gemini": GeminiProvider(),
        "mistral": MistralProvider(),
        "groq": GroqProvider(),
        "mock": MockProvider(),
    }


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    roles = data.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise ValueError(f"{path} must define a non-empty 'roles' mapping")
    for role, entries in roles.items():
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{path}: role {role!r} must be a non-empty list")
        for entry in entries:
            for key in ("provider", "model", "family"):
                if not isinstance(entry, dict) or not entry.get(key):
                    raise ValueError(f"{path}: role {role!r} entry missing {key!r}: {entry}")
    return data


def load_env(path: str | Path | None = None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return environment variables, merged with a local gitignored .env.

    Real environment variables win over .env values.
    """
    env = dict(os.environ) if base is None else dict(base)
    path = Path(path) if path else PROJECT_ROOT / ".env"
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env


def _env_flag(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _complete_with_retry(
    provider: Provider,
    model: str,
    prompt: str,
    json_schema: dict | None,
    api_key: str,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    sleep: Callable[[float], None],
    role: str,
) -> Any:
    attempt = 0
    while True:
        try:
            result = provider.complete(model, prompt, api_key=api_key, json_schema=json_schema)
            return _parse_json(result) if json_schema is not None else result
        except ProviderError as exc:
            if not exc.retryable or attempt >= max_attempts - 1:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            logger.warning(
                "role=%s provider=%s model=%s retry %s/%s after %.1fs: %s",
                role, provider.name, model, attempt + 1, max_attempts - 1, delay, exc,
            )
            sleep(delay)
            attempt += 1


def _chain_is_mock(chain: list[dict], providers: Mapping[str, Provider]) -> bool:
    """True when every model in the chain is served by a MockProvider."""
    if not chain:
        return False
    return all(isinstance(providers.get(entry.get("provider")), MockProvider) for entry in chain)


def _select_chain(
    role: str,
    chain: list[dict],
    exclude_families: set[str] | None,
    run_state: RunState | None = None,
) -> list[dict]:
    """Drop models whose family must not be used for this role."""
    if not exclude_families:
        return chain

    kept = [entry for entry in chain if entry.get("family") not in exclude_families]
    for entry in chain:
        if entry.get("family") in exclude_families:
            logger.info(
                "role=%s excluding provider=%s model=%s family=%s (excluded families=%s)",
                role, entry.get("provider"), entry.get("model"), entry.get("family"),
                sorted(exclude_families),
            )
    if not kept:
        message = (
            f"cannot run role {role!r}: every available model shares the excluded "
            f"family {sorted(exclude_families)}; writer and verifier must use different "
            f"model families, so the run is skipped and nothing is published"
        )
        if run_state is not None:
            run_state.add_failures([message])
        logger.error(message)
        raise SkipRun(message)
    return kept


def generate(
    role: str,
    prompt: str,
    json_schema: dict | None = None,
    *,
    config: dict | None = None,
    providers: Mapping[str, Provider] | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool | None = None,
    sleep: Callable[[float], None] = time.sleep,
    run_state: RunState | None = None,
    exclude_families: set[str] | None = None,
) -> Generation:
    """Generate for a role and return the provider/model/family that served it.

    ``Generation.value`` holds the text, or parsed JSON when ``json_schema`` is
    given. Tries each provider/model in the role's chain from config.yaml,
    retrying transient failures with backoff.

    Fail-closed verifier guard: when ``role == "verifier"`` the chain is filtered
    to remove the writer's actual model family (recorded on ``run_state``). If no
    ``run_state`` carrying a writer family is supplied, :class:`MissingWriterFamilyError`
    is raised, and if no model remains :class:`SkipRun` is raised. Either way no
    provider is called. ``dry_run`` and all-mock chains bypass the guard.
    """
    if config is None:
        config = load_config()
    roles = config.get("roles", {})
    if role not in roles:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(roles)}")

    role_chain = [entry for entry in roles[role] if entry.get("enabled", True)]
    if not role_chain:
        message = (
            f"cannot run role {role!r}: every configured model is disabled "
            f"(enabled: false) in pipeline/config.yaml"
        )
        if run_state is not None:
            run_state.add_failures([message])
        logger.error(message)
        raise SkipRun(message)

    if dry_run is None:
        dry_run = _env_flag(os.environ.get("DRY_RUN"))
    providers = providers if providers is not None else default_providers()

    if (
        exclude_families is None
        and role == "verifier"
        and not dry_run
        and not _chain_is_mock(role_chain, providers)
    ):
        writer_family = run_state.writer_family if run_state is not None else None
        if writer_family is None:
            message = (
                "refusing to run the verifier: no writer family is recorded on RunState. "
                "Create one RunState per run and pass it from the writer stage to the "
                "verifier stage. --dry-run and mock providers bypass this guard."
            )
            logger.error(message)
            raise MissingWriterFamilyError(message)
        exclude_families = {writer_family}

    chain = _select_chain(role, role_chain, exclude_families, run_state)

    fallbacks: list[str] = []

    if dry_run:
        entry = chain[0]
        result = MockProvider().complete(entry["model"], prompt, json_schema=json_schema)
        value = _parse_json(result) if json_schema is not None else result
        generation = Generation(
            role=role, provider="mock", model=entry["model"], family=entry["family"],
            value=value, fallbacks=fallbacks,
        )
        logger.info(
            "role=%s dry-run provider=mock model=%s family=%s",
            role, generation.model, generation.family,
        )
        if run_state is not None:
            run_state.record(generation)
        return generation

    if env is None:
        env = load_env()

    retry = config.get("retry", {})
    max_attempts = int(retry.get("max_attempts", 3))
    base_delay = float(retry.get("base_delay_seconds", 1))
    max_delay = float(retry.get("max_delay_seconds", 30))

    for entry in chain:
        name = entry.get("provider")
        model = entry.get("model")
        family = entry.get("family")
        provider = providers.get(name)
        if provider is None:
            fallbacks.append(f"role={role} {name} (family={family}): unknown provider")
            logger.warning("fallback role=%s: %s is not a registered provider", role, name)
            continue
        api_key = env.get(provider.env_key, "")
        if not api_key and not isinstance(provider, MockProvider):
            fallbacks.append(f"role={role} {name} (family={family}): missing {provider.env_key}")
            logger.warning("fallback role=%s: missing env %s for provider %s", role, provider.env_key, name)
            continue
        try:
            value = _complete_with_retry(
                provider, model, prompt, json_schema, api_key,
                max_attempts, base_delay, max_delay, sleep, role,
            )
        except ProviderError as exc:
            fallbacks.append(f"role={role} {name} ({model}, family={family}): {exc}")
            logger.warning("fallback role=%s provider=%s model=%s family=%s: %s", role, name, model, family, exc)
            continue

        generation = Generation(
            role=role, provider=name, model=model, family=family,
            value=value, fallbacks=list(fallbacks),
        )
        logger.info(
            "role=%s provider=%s model=%s family=%s fallbacks=%d",
            role, name, model, family, len(fallbacks),
        )
        if run_state is not None:
            run_state.record(generation)
        return generation

    if run_state is not None:
        run_state.add_failures(fallbacks)
    raise SkipRun(f"all providers failed for role {role!r}: " + "; ".join(fallbacks))


__all__ = [
    "SkipRun",
    "MissingWriterFamilyError",
    "ProviderError",
    "Generation",
    "RunState",
    "Provider",
    "OpenAICompatibleProvider",
    "GeminiProvider",
    "MistralProvider",
    "GroqProvider",
    "MockProvider",
    "VALID_ROLES",
    "generate",
    "load_config",
    "load_env",
    "default_providers",
    "http_post_json",
]
