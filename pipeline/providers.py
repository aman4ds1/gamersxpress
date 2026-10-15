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

import copy
import http.client
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config.yaml")
VALID_ROLES = ("fast", "writer", "verifier")

#: Prompt-token estimate: one token per ~3.5 characters. This is the same
#: approximation the pipeline uses elsewhere to size a prompt before a provider's
#: rate limit is consulted. ``len(prompt) / _PROMPT_CHARS_PER_TOKEN``.
_PROMPT_CHARS_PER_TOKEN = 3.5

#: Default tokens-per-minute limits for OpenAI-compatible providers, keyed by
#: provider name then model id. Groq's Free Plan coding-model limits, from
#: https://console.groq.com/docs/rate-limits (checked 2026-10-15): gpt-oss-120b
#: and gpt-oss-20b are 8K TPM; qwen3.8-27b is listed at 8K TPM on the live page,
#: so 6K is kept here as the conservative default. Overridable via
#: ``provider.limits`` in config.yaml. A model that is absent is not capped.
DEFAULT_TPM_LIMITS: dict[str, dict[str, int]] = {
    "groq": {
        "openai/gpt-oss-120b": 8000,
        "openai/gpt-oss-20b": 8000,
        "qwen/qwen3.8-27b": 6000,
    },
}

#: Smallest output budget worth a call, per role. A rate-limit-sized completion
#: cap below this means the prompt nearly fills the provider's per-minute limit,
#: so the provider is skipped with reason "prompt too large for provider rate
#: limit" instead of sending a request that would be rejected.
DEFAULT_MIN_OUTPUT_TOKENS: dict[str, int] = {
    "writer": 1500,
    "verifier": 1200,
    "fast": 600,
}

#: Hard wall-clock caps (seconds) for one provider call and for a whole role
#: across its providers. Overridable via ``provider.max_call_seconds`` and
#: ``provider.max_role_seconds`` in config.yaml. A non-positive value disables it.
DEFAULT_MAX_CALL_SECONDS: dict[str, float] = {
    "writer": 150.0,
    "verifier": 120.0,
    "fast": 60.0,
}
DEFAULT_MAX_ROLE_SECONDS: dict[str, float] = {
    "writer": 420.0,
    "verifier": 300.0,
    "fast": 180.0,
}

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


class ProviderTimeoutError(ProviderError):
    """One provider attempt was abandoned for exceeding its wall-clock timeout.

    Retryable like a transient failure, but a hung attempt is retried at most
    once on the same provider before falling back (a second hang is unlikely to
    be a blip, and each retry costs the full call timeout).
    """

    def __init__(self, message: str):
        super().__init__(message, retryable=True)


class _RoleDeadlineExceeded(Exception):
    """Internal: a role ran past its ``max_role_seconds`` deadline.

    Never escapes :func:`generate`; it is converted there into a
    :class:`SkipRun` naming the role, the elapsed time and the last error.
    """

    def __init__(self, role: str, max_role: float, elapsed: float, last_error: str | None = None):
        self.role = role
        self.max_role = max_role
        self.elapsed = elapsed
        self.last_error = last_error
        detail = (
            f"role {role!r} exceeded max_role_seconds {int(max_role)}s "
            f"({elapsed:.0f}s elapsed)"
        )
        if last_error is not None:
            detail += f"; last error: {last_error}"
        super().__init__(detail)


class PromptTooLargeError(ProviderError):
    """The prompt leaves too little output budget under a provider's TPM limit.

    Raised before any request is sent, so the provider is skipped rather than
    rejected with HTTP 413. The message is the skip reason callers record.
    """

    def __init__(self, limit: int, prompt_estimate: float):
        super().__init__(
            "prompt too large for provider rate limit "
            f"(prompt ~{prompt_estimate:.0f} tokens, limit {limit} TPM)",
            retryable=False,
            status=413,
        )
        self.limit = limit
        self.prompt_estimate = prompt_estimate


@dataclass
class Completion:
    """One provider response plus metadata used for retry and reporting.

    ``text`` is the raw output text. ``finish_reason`` says why generation
    stopped; ``"length"`` (Gemini's ``MAX_TOKENS``) means the response is
    truncated and must not be parsed. ``usage`` and ``raw`` carry the provider's
    token usage and the full response object so verify reports can show them.
    ``value`` holds parsed JSON when a ``json_schema`` was requested (set by the
    retry helper, not by the provider).
    """

    text: str
    finish_reason: str | None = None
    usage: dict | None = None
    raw: dict | None = None
    value: Any = None


@dataclass
class Generation:
    """The result of one successful ``generate`` call.

    ``value`` is the text, or parsed JSON when a ``json_schema`` was given.
    ``family`` is the underlying model family (from config.yaml), which is what
    guarantees the writer and verifier stay on different model families.
    ``fallbacks`` lists the providers that were skipped or failed before this
    one succeeded. ``finish_reason``, ``usage`` and ``raw`` come from the serving
    provider's response and are recorded so the verify report can save them.
    """

    role: str
    provider: str
    model: str
    family: str
    value: Any
    fallbacks: list[str] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict | None = None
    raw: Any = None

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
    writers: list[Generation] = field(default_factory=list)
    # Total recorded generations this run; gates the candidate search's model
    # budget (``score.model_call_budget``) in run.py.
    calls: int = 0

    def record(self, generation: Generation) -> None:
        self.calls += 1
        # Keep every writer call: the original draft and, when the verifier
        # rejects it, the repair. ``generations["writer"]`` stays the most
        # recent one, so ``writer_family`` is the family of the text currently
        # being verified (the repaired draft), and the report can still list
        # every writer family.
        if generation.role == "writer":
            self.writers.append(generation)
        self.generations[generation.role] = generation

    def add_failures(self, messages: list[str]) -> None:
        self.failures.extend(messages)

    @property
    def writer_family(self) -> str | None:
        writer = self.generations.get("writer")
        return writer.family if writer else None

    @property
    def writer_families(self) -> list[str]:
        """Model families of every writer call this run, oldest first.

        The last entry is ``writer_family``: the family that produced the text
        the verifier is checking. After a repair that fell back to another
        family the list has two entries (the original and the repair).
        """
        return [writer.family for writer in self.writers]

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
            "writer_families": self.writer_families,
            "verifier_family": self.verifier_family,
            "fallbacks": self.fallbacks(),
        }


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


#: Statuses that mean "the service is busy, wait and try again": 429 (rate
#: limited) and 503 (overloaded/unavailable). They clear on a timescale of
#: seconds, so they get the longer, jittered ``busy_delays_seconds`` schedule
#: instead of the sub-second exponential backoff a generic transient error gets.
_BUSY_STATUSES = frozenset({429, 503})


class _RoleTimeBudget:
    """Total backoff time one role may spend, shared across its providers.

    Bounds worst-case pipeline latency: ``generate`` creates one budget per role
    and passes it into every provider attempt, so a slow-failing first provider
    cannot spend the whole run's patience before the fallback is tried. ``spend``
    refuses a delay that would exceed the remaining budget so the total slept
    never exceeds the budget.
    """

    def __init__(self, seconds: float):
        self.remaining = max(0.0, float(seconds))

    def spend(self, delay: float) -> bool:
        if delay <= 0:
            return True
        if delay > self.remaining:
            return False
        self.remaining -= delay
        return True


def estimate_prompt_tokens(prompt: str) -> float:
    """Return a tokens estimate for ``prompt`` using chars/3.5."""
    return len(prompt) / _PROMPT_CHARS_PER_TOKEN


def _is_request_too_large(exc: Exception) -> bool:
    """True when ``exc`` indicates a payload rejected for being too large."""
    message = str(exc).lower()
    return exc.status == 413 or "request too large" in message


def _limit_from_error_message(message: str) -> int | None:
    """Parse the provider's limit token count from an error message."""
    match = re.search(r"limit\s+(\d+)", message, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _call_with_timeout(fn: Callable[[], Any], timeout: float | None) -> Any:
    """Run ``fn`` in a thread and return its result, abandoning after ``timeout``.

    On timeout, the worker may still complete later, but its return value is
    discarded. The caller never reads it. Any exception the worker raises is
    propagated by the caller only if it finishes within ``timeout``.
    """
    if timeout is None or timeout <= 0:
        return fn()
    box: dict[str, Any] = {}
    done = threading.Event()

    def worker() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=worker, name="llm-call", daemon=True)
    thread.start()
    if not done.wait(timeout):
        raise ProviderTimeoutError(
            f"provider call exceeded {timeout:.0f}s (abandoned)"
        )
    if "error" in box:
        raise box["error"]
    return box["value"]


def _attempt_timeout(
    deadline: float | None,
    max_call: float,
    role: str,
    max_role: float,
    last_error: str | None,
) -> float | None:
    """Seconds one attempt may run: ``min(max_call_seconds, role time left)``.

    Both the worker thread and the HTTP socket use this same value so a call can
    never outlive its budget. With no role deadline, it is ``max_call_seconds``
    (or ``None`` when that is disabled). Raises :class:`_RoleDeadlineExceeded`
    when no time is left.
    """
    if deadline is None:
        return max_call if max_call > 0 else None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _RoleDeadlineExceeded(role, max_role, max_role - remaining, last_error)
    return min(max_call, remaining) if max_call > 0 else remaining


def _timeout_transport(timeout: float) -> Callable[..., dict]:
    """A transport bound to ``timeout`` so the socket timeout matches the worker."""

    def transport(url: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> dict:
        return http_post_json(url, payload, headers, timeout=timeout)

    return transport


def _tpm_limit(provider_name: str, model: str, limits: Mapping[str, Mapping[str, int]]) -> int | None:
    """Return the per-minute token limit for this provider/model, or None."""
    models = limits.get(provider_name)
    if not models:
        return None
    limit = models.get(model)
    return int(limit) if limit is not None else None


def _sized_options_for_openai(
    provider_name: str,
    model: str,
    prompt: str,
    options: Mapping[str, Any] | None,
    role: str,
    limits: Mapping[str, Mapping[str, int]],
    min_outputs: Mapping[str, int],
) -> tuple[dict, int | None]:
    """Return sized options and the effective cap, or raise PromptTooLargeError.

    Returns ``(sized_options, cap)``. ``cap`` is the effective ``max_completion_tokens``
    after capping to the configured value and to ``0.9*limit - est``. If ``cap``
    falls below the role's minimum output, :class:`PromptTooLargeError` is raised.
    """
    options_map = dict(options or {})
    limit = _tpm_limit(provider_name, model, limits)
    if limit is None:
        return options_map, None
    est = estimate_prompt_tokens(prompt)
    configured = options_map.get("max_completion_tokens")
    budget = 0.9 * limit - est
    if budget <= 0:
        raise PromptTooLargeError(int(limit), est)
    if configured is None:
        cap = int(budget)
    else:
        cap = min(int(configured), int(budget))
    min_out = int(min_outputs.get(role, 0))
    if cap < min_out:
        raise PromptTooLargeError(int(limit), est)
    sized = dict(options_map)
    sized["max_completion_tokens"] = cap
    return sized, cap


def _jitter(delay: float, ratio: float, rand: Callable[[], float]) -> float:
    """Scale ``delay`` by a random +/- ``ratio`` fraction to avoid retry lockstep."""
    if ratio <= 0:
        return round(delay, 3)
    factor = 1.0 + ratio * (2.0 * rand() - 1.0)
    return round(max(0.0, delay * factor), 3)



def http_post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float = 60,
) -> dict:
    """POST JSON and return the parsed response, raising ProviderError on failure.

    The whole exchange (connect, send, read, decode) is normalised: any
    ``OSError`` subclass (``ConnectionResetError``, ``ConnectionAbortedError``,
    ``BrokenPipeError``, ``ssl.SSLError``, ``socket.timeout``/``TimeoutError``)
    and any ``http.client.HTTPException`` (``RemoteDisconnected``,
    ``IncompleteRead``) becomes a retryable :class:`ProviderError`, so a dropped
    connection fails the attempt instead of escaping the retry logic. A bad
    status or malformed body is reported the same way.
    """
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
    except (http.client.HTTPException, OSError) as exc:
        # ConnectionResetError/ConnectionAbortedError/BrokenPipeError/ssl.SSLError
        # are OSError subclasses; TimeoutError/socket.timeout are too. The reset
        # that motivated this handler arrives while reading the body, after
        # urlopen already returned, so it can only be caught here.
        raise ProviderError(
            f"connection error calling {url}: {type(exc).__name__}: {exc}",
            retryable=True,
        ) from exc
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
        options: Mapping[str, Any] | None = None,
    ) -> tuple[str, dict, dict]:
        raise NotImplementedError

    def extract(self, data: dict) -> Completion:
        raise NotImplementedError

    def complete(
        self,
        model: str,
        prompt: str,
        *,
        api_key: str,
        json_schema: dict | None = None,
        options: Mapping[str, Any] | None = None,
        transport: Callable[..., dict] | None = None,
    ) -> Completion:
        transport = transport or http_post_json
        url, payload, headers = self.prepare(model, prompt, api_key, json_schema, options)
        return self.extract(transport(url, payload, headers))


class OpenAICompatibleProvider(Provider):
    """Base for OpenAI chat-completions compatible services."""

    base_url = ""
    #: Documented request options this provider understands. Only these keys are
    #: ever copied into the payload; unknown option keys are dropped with a
    #: warning, so a mistyped key can never silently reach the API.
    OPTION_KEYS = frozenset({"max_completion_tokens", "reasoning_effort", "response_format"})

    def prepare(
        self,
        model: str,
        prompt: str,
        api_key: str,
        json_schema: dict | None,
        options: Mapping[str, Any] | None = None,
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
        options = dict(options or {})
        unknown = set(options) - self.OPTION_KEYS
        if unknown:
            logger.warning(
                "provider=%s dropping undocumented option(s) %s",
                self.__class__.__name__, sorted(unknown),
            )
        if json_schema is not None:
            # Structured output wins over the response_format option: a strict
            # json_schema already forces JSON through response_format.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema, "strict": True},
            }
        elif options.get("response_format") == "json_object":
            # Documented as {"type": "json_object"}: guarantees a JSON object
            # (only applied when the role needs JSON output, never the writer).
            payload["response_format"] = {"type": "json_object"}
        if "max_completion_tokens" in options:
            # Groq reasoning-model docs: max_completion_tokens caps output,
            # including reasoning tokens; the 1024 default truncates complex
            # reasoning with finish_reason="length".
            payload["max_completion_tokens"] = int(options["max_completion_tokens"])
        if options.get("reasoning_effort") in ("low", "medium", "high"):
            # Groq reasoning-model docs: reasoning_effort low/medium/high.
            payload["reasoning_effort"] = options["reasoning_effort"]
        return url, payload, headers

    def extract(self, data: dict) -> Completion:
        try:
            choice = data["choices"][0]
            content = choice.get("message", {}).get("content")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(f"unexpected response shape: {exc}", retryable=True) from exc
        if content is None:
            raise ProviderError("empty content in response", retryable=True)
        return Completion(
            text=content,
            finish_reason=str(choice.get("finish_reason")) or None,
            usage=data.get("usage"),
            raw=data,
        )


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

    #: Documented request options for generateContent. Only these are sent.
    OPTION_KEYS = frozenset({"response_mime_type", "max_output_tokens"})

    def prepare(
        self,
        model: str,
        prompt: str,
        api_key: str,
        json_schema: dict | None,
        options: Mapping[str, Any] | None = None,
    ) -> tuple[str, dict, dict]:
        url = f"{self.base_url}/{model}:generateContent"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        payload: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
        options = dict(options or {})
        unknown = set(options) - self.OPTION_KEYS
        if unknown:
            logger.warning(
                "provider=%s dropping undocumented option(s) %s",
                self.__class__.__name__, sorted(unknown),
            )
        generation_config: dict[str, Any] = {}
        if json_schema is not None:
            generation_config = {
                "responseMimeType": "application/json",
                "responseSchema": _to_gemini_schema(json_schema),
            }
        elif options.get("response_mime_type"):
            # Documented generationConfig.responseMimeType, e.g. "application/json".
            generation_config["responseMimeType"] = options["response_mime_type"]
        if "max_output_tokens" in options:
            # Documented generationConfig.maxOutputTokens output-token cap.
            generation_config["maxOutputTokens"] = int(options["max_output_tokens"])
        if generation_config:
            payload["generationConfig"] = generation_config
        return url, payload, headers

    def extract(self, data: dict) -> Completion:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            raise ProviderError(f"no candidates (blocked?): {feedback}", retryable=False)
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        finish_reason = _gemini_finish_reason(candidate.get("finishReason"))
        # A MAX_TOKENS stop is a truncation, handled (and reported) as
        # finish_reason="length"; anything else with no text is a failure.
        if not text and finish_reason != "length":
            raise ProviderError(
                f"empty response (finishReason={candidate.get('finishReason')})",
                retryable=True,
            )
        return Completion(
            text=text,
            finish_reason=finish_reason,
            usage=data.get("usageMetadata"),
            raw=data,
        )


def _gemini_finish_reason(reason: Any) -> str | None:
    if reason is None:
        return None
    name = str(reason)
    if name.upper() in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS"):
        return "length"
    return name.lower() or None


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
        options: Mapping[str, Any] | None = None,
        transport: Callable[..., dict] | None = None,
    ) -> Completion:
        if json_schema is not None:
            return Completion(text=json.dumps(_sample_from_schema(json_schema)), finish_reason="stop")
        return Completion(text=f"[mock:{model}] {prompt}", finish_reason="stop")


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


#: Parsed-config cache keyed by resolved path -> (mtime_ns, size, parsed data).
#: Re-parsing a large config.yaml dominates the score stage (score_cluster reads
#: news settings once per cluster), so the parsed result is reused until the file
#: changes on disk.
_CONFIG_CACHE: dict[str, tuple[int, int, dict]] = {}


def clear_config_cache() -> None:
    """Drop every cached parsed config.

    Call this from tests (and after rewriting a config) so a stale entry can
    never leak between callers.
    """
    _CONFIG_CACHE.clear()


def load_config(path: str | Path | None = None) -> dict:
    """Load and validate ``config.yaml``.

    The parsed mapping is cached per resolved path and invalidated whenever the
    file's ``mtime_ns`` or size changes, so an edited config is picked up and two
    different paths never share an entry. Every call returns a fresh deep copy so
    a caller that mutates the result cannot poison the cache. Use
    :func:`clear_config_cache` to drop all entries.
    """
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    resolved = str(path.resolve())
    signature: tuple[int, int] | None
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = None
    if signature is not None:
        cached = _CONFIG_CACHE.get(resolved)
        if cached is not None and cached[0] == signature[0] and cached[1] == signature[1]:
            return copy.deepcopy(cached[2])
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
    if signature is not None:
        _CONFIG_CACHE[resolved] = (signature[0], signature[1], data)
    return copy.deepcopy(data)


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


def _bump_token_budget(options: dict, multiplier: int, cap: int) -> dict | None:
    """Return options with the token budget scaled for a truncated-output retry.

    Grows whichever budget key the provider uses (max_completion_tokens for
    OpenAI-compatible services, max_output_tokens for Gemini), clamped to ``cap``.
    Returns None when no budget key is set, the multiplier is invalid, or the
    cap would not grow the budget (so a retry would not help).
    """
    key = "max_completion_tokens" if "max_completion_tokens" in options else "max_output_tokens"
    if key not in options or multiplier <= 0:
        return None
    current = int(options[key])
    grown = min(int(current * multiplier), cap)
    if grown <= current:
        return None
    return {**options, key: grown}


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
    options: Mapping[str, Any] | None = None,
    length_retries: int = 1,
    length_budget_multiplier: int = 2,
    length_budget_cap: int = 0,
    busy_delays: Sequence[float] = (),
    jitter_ratio: float = 0.0,
    rand: Callable[[], float] = random.random,
    time_budget: _RoleTimeBudget | None = None,
    deadline: float | None = None,
    max_call: float = 0.0,
    max_role: float = 0.0,
) -> Completion:
    """Call ``provider.complete`` with backoff, returning a :class:`Completion`.

    Transient errors retry ``max_attempts`` times with exponential backoff. A
    429 or 503 uses the longer, jittered ``busy_delays`` schedule instead (about
    10s then 30s): those mean the service is busy, not that the request was bad.
    Every backoff delay is drawn through ``rand`` and capped by the shared
    per-role ``time_budget``; once the budget cannot cover the next wait,
    retrying stops and the error propagates so the caller falls back to the next
    provider.

    One clock per role: ``deadline`` is the monotonic end of ``max_role_seconds``
    and every attempt's timeout is ``min(max_call, time left)``, passed to both
    the worker thread and the HTTP socket. The deadline is re-checked before each
    attempt and before each backoff sleep, and a sleep never overshoots it, so
    the whole role call (retries included) cannot run past the deadline. A hung
    attempt (:class:`ProviderTimeoutError`) is retried at most once on the same
    provider before falling back. The transport safety net turns a stray
    ``OSError``/``http.client.HTTPException`` into a retryable
    :class:`ProviderError`; programming errors still propagate.

    A ``finish_reason="length"`` response is truncated output, which must never
    be parsed: it retries up to ``length_retries`` times with a larger token
    budget. If it stays truncated, a ``json_schema`` request raises
    :class:`ProviderError` (so the next provider is tried), while a schema-less
    call (the verifier) returns the truncated :class:`Completion` unchanged so
    the caller fails the run without parsing it.
    """
    if time_budget is None:
        time_budget = _RoleTimeBudget(float("inf"))
    attempt = 0
    timeout_retries = 0
    budget = dict(options or {})
    length_remaining = max(0, int(length_retries))
    last_error: str | None = None
    while True:
        timeout = _attempt_timeout(deadline, max_call, role, max_role, last_error)
        transport = _timeout_transport(timeout) if timeout is not None else None
        try:
            completion = _call_with_timeout(
                lambda: provider.complete(
                    model, prompt, api_key=api_key, json_schema=json_schema,
                    options=budget, transport=transport,
                ),
                timeout,
            )
        except ProviderError as exc:
            error: ProviderError | None = exc
        except (http.client.HTTPException, OSError) as exc:
            # Safety net for a transport failure that escaped http_post_json: an
            # injected transport, or a provider that reads the socket itself.
            # These are transient; programming errors (TypeError, KeyError, ...)
            # are neither OSError nor HTTPException and keep propagating.
            error = ProviderError(
                f"connection error from {provider.name}/{model}: "
                f"{type(exc).__name__}: {exc}",
                retryable=True,
            )
            error.__cause__ = exc
        else:
            error = None

        if error is not None:
            last_error = str(error)
            if isinstance(error, ProviderTimeoutError):
                # A hung attempt gets one retry on the same provider, no matter
                # how many generic retries the config allows.
                if timeout_retries >= 1 or attempt >= max_attempts - 1:
                    raise error
                timeout_retries += 1
            elif not error.retryable or attempt >= max_attempts - 1:
                raise error
            if error.status in _BUSY_STATUSES and busy_delays:
                raw_delay = float(busy_delays[min(attempt, len(busy_delays) - 1)])
            else:
                raw_delay = min(base_delay * (2 ** attempt), max_delay)
            delay = _jitter(raw_delay, jitter_ratio, rand)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _RoleDeadlineExceeded(
                        role, max_role, max_role - remaining, last_error
                    )
                delay = min(delay, remaining)
            if not time_budget.spend(delay):
                logger.warning(
                    "role=%s provider=%s model=%s: retry time budget exhausted "
                    "(%.1fs left, next wait %.1fs); falling back: %s",
                    role, provider.name, model, time_budget.remaining, delay, error,
                )
                raise error
            logger.warning(
                "role=%s provider=%s model=%s retry %s/%s after %.1fs: %s",
                role, provider.name, model, attempt + 1, max_attempts - 1, delay, error,
            )
            sleep(delay)
            attempt += 1
            continue

        if completion.finish_reason == "length" and length_remaining > 0:
            bumped = _bump_token_budget(budget, length_budget_multiplier, length_budget_cap)
            if bumped is not None:
                budget = bumped
                length_remaining -= 1
                logger.warning(
                    "role=%s provider=%s model=%s response truncated (finish_reason=length); "
                    "retrying with a larger token budget",
                    role, provider.name, model,
                )
                continue

        if completion.finish_reason == "length":
            # Truncated JSON must never be parsed. With a json_schema, fall back
            # to the next provider; without one (the verifier), propagate the
            # truncation so the caller fails the run instead of skipping it.
            if json_schema is not None:
                raise ProviderError(
                    f"truncated response from {provider.name}/{model} (finish_reason=length)",
                    retryable=True,
                )
            return Completion(
                text=completion.text,
                finish_reason="length",
                usage=completion.usage,
                raw=completion.raw,
                value=completion.text,
            )

        value = _parse_json(completion.text) if json_schema is not None else completion.text
        return Completion(
            text=completion.text,
            finish_reason=completion.finish_reason,
            usage=completion.usage,
            raw=completion.raw,
            value=value,
        )


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
    rand: Callable[[], float] = random.random,
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

    # Load provider/time/TPM limits from config.
    provider_cfg = config.get("provider", {}) or {}
    max_call = float((provider_cfg.get("max_call_seconds") or {}).get(role, DEFAULT_MAX_CALL_SECONDS.get(role, 0)) or 0)
    max_role = float((provider_cfg.get("max_role_seconds") or {}).get(role, DEFAULT_MAX_ROLE_SECONDS.get(role, 0)) or 0)

    # Build TPM limits mapping (config overrides defaults).
    limits_cfg = {}
    limits = {p: dict(m) for p, m in DEFAULT_TPM_LIMITS.items()}
    for p, m in (provider_cfg.get("limits") or {}).items():
        models = limits.setdefault(p, {})
        if isinstance(m, dict):
            for model_id, val in m.items():
                try:
                    models[model_id] = int(val)
                except (ValueError, TypeError):
                    pass
    limits_cfg = limits

    min_outputs = dict(DEFAULT_MIN_OUTPUT_TOKENS)
    for r, v in (provider_cfg.get("min_output_tokens") or {}).items():
        try:
            min_outputs[str(r)] = int(v)
        except (ValueError, TypeError):
            pass

    if (
        exclude_families is None
        and role == "verifier"
        and not dry_run
        and not _chain_is_mock(role_chain, providers)
    ):
        # ``writer_family`` is the family of the most recent writer output, i.e.
        # the text the verifier is checking. After a repair it is the repair's
        # family (not the original's), so the guard excludes the family that
        # actually produced the draft. A recorded family with no eligible
        # verifier leaves ``_select_chain`` to raise SkipRun (publish nothing);
        # MissingWriterFamilyError is only for a not-recorded writer family.
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
        result = MockProvider().complete(
            entry["model"], prompt, json_schema=json_schema, options=entry.get("options")
        )
        value = _parse_json(result.text) if json_schema is not None else result.text
        generation = Generation(
            role=role, provider="mock", model=entry["model"], family=entry["family"],
            value=value, fallbacks=fallbacks, finish_reason=result.finish_reason,
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
    busy_delays = [float(value) for value in (retry.get("busy_delays_seconds") or ())]
    jitter_ratio = float(retry.get("jitter_ratio", 0) or 0)
    total_time_budget = float(retry.get("total_time_budget_seconds", 0) or 0)
    # A non-positive budget means "unlimited"; the shipped config sets a real one.
    time_budget = _RoleTimeBudget(total_time_budget if total_time_budget > 0 else float("inf"))
    length_retries = int(retry.get("length_retries", 1))
    length_budget_multiplier = int(retry.get("length_budget_multiplier", 2))
    length_budget_cap = int(retry.get("length_budget_cap", 0))

    # One clock per role: started once and never restarted by a retry or a
    # fallback. role_deadline is the monotonic instant this role must finish by.
    role_deadline = time.monotonic() + max_role if max_role > 0 else None
    last_error: str | None = None

    def _deadline_skip(exc: _RoleDeadlineExceeded) -> SkipRun:
        fallbacks.append(f"role={role}: {exc}")
        if run_state is not None:
            run_state.add_failures(list(fallbacks))
        logger.warning("fallback role=%s: %s", role, exc)
        return SkipRun(str(exc))

    for entry in chain:
        # Role wall-clock deadline, checked before each provider so a provider
        # that would run out the clock is never called.
        if role_deadline is not None and time.monotonic() >= role_deadline:
            elapsed = max_role - (role_deadline - time.monotonic())
            raise _deadline_skip(
                _RoleDeadlineExceeded(role, max_role, elapsed, last_error)
            )
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
        entry_options = entry.get("options")
        cap_for_length = length_budget_cap
        sized = entry_options
        shrunk_once = False
        # Pre-size for OpenAI-compatible.
        if isinstance(provider, OpenAICompatibleProvider):
            try:
                sized, cap = _sized_options_for_openai(name, model, prompt, entry_options, role, limits_cfg, min_outputs)
                if cap is not None:
                    cap_for_length = min(length_budget_cap, cap) if length_budget_cap else cap
                entry_options = sized
            except PromptTooLargeError as exc:
                fallbacks.append(f"role={role} {name} ({model}, family={family}): prompt too large for provider rate limit")
                logger.warning("fallback role=%s provider=%s model=%s family=%s: prompt too large for provider rate limit", role, name, model, family)
                continue
        try:
            completion = _complete_with_retry(
                provider, model, prompt, json_schema, api_key,
                max_attempts, base_delay, max_delay, sleep, role,
                options=entry_options,
                length_retries=length_retries,
                length_budget_multiplier=length_budget_multiplier,
                length_budget_cap=cap_for_length,
                busy_delays=busy_delays,
                jitter_ratio=jitter_ratio,
                rand=rand,
                time_budget=time_budget,
                deadline=role_deadline,
                max_call=max_call,
                max_role=max_role,
            )
        except _RoleDeadlineExceeded as exc:
            raise _deadline_skip(exc) from exc
        except ProviderError as exc:
            last_error = str(exc)
            # Handle "Request too large" / 413 once by shrinking.
            if isinstance(provider, OpenAICompatibleProvider) and _is_request_too_large(exc) and not shrunk_once:
                shrunk_once = True
                limit_from_err = _limit_from_error_message(str(exc)) or None
                limit_use = limit_from_err or _tpm_limit(name, model, limits_cfg)
                if limit_use is not None:
                    est = estimate_prompt_tokens(prompt)
                    fit = int(limit_use - est)
                    min_out = int(min_outputs.get(role, 0))
                    if fit >= min_out:
                        sized_shrink = dict(entry_options)
                        sized_shrink["max_completion_tokens"] = fit
                        cap_shrink = fit
                        cap_for_length_s = min(length_budget_cap, cap_shrink) if length_budget_cap else cap_shrink
                        try:
                            completion = _complete_with_retry(
                                provider, model, prompt, json_schema, api_key,
                                max_attempts, base_delay, max_delay, sleep, role,
                                options=sized_shrink,
                                length_retries=length_retries,
                                length_budget_multiplier=length_budget_multiplier,
                                length_budget_cap=cap_for_length_s,
                                busy_delays=busy_delays,
                                jitter_ratio=jitter_ratio,
                                rand=rand,
                                time_budget=time_budget,
                                deadline=role_deadline,
                                max_call=max_call,
                                max_role=max_role,
                            )
                            # Success after shrink; treat as success.
                            generation = Generation(
                                role=role, provider=name, model=model, family=family,
                                value=completion.value, fallbacks=list(fallbacks),
                                finish_reason=completion.finish_reason,
                                usage=completion.usage,
                                raw=completion.raw,
                            )
                            logger.info("role=%s provider=%s model=%s family=%s fallbacks=%d", role, name, model, family, len(fallbacks))
                            if run_state is not None:
                                run_state.record(generation)
                            return generation
                        except _RoleDeadlineExceeded as exc:
                            raise _deadline_skip(exc) from exc
                        except ProviderError:
                            pass  # fall through to append this exc as fallback
            fallbacks.append(f"role={role} {name} ({model}, family={family}): {exc}")
            logger.warning("fallback role=%s provider=%s model=%s family=%s: %s", role, name, model, family, exc)
            continue

        generation = Generation(
            role=role, provider=name, model=model, family=family,
            value=completion.value, fallbacks=list(fallbacks),
            finish_reason=completion.finish_reason,
            usage=completion.usage,
            raw=completion.raw,
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
    "ProviderTimeoutError",
    "Completion",
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
    "clear_config_cache",
    "load_env",
    "default_providers",
    "http_post_json",
]
