"""List the models each configured provider's API key can access.

For every provider referenced in ``pipeline/config.yaml`` roles, this fetches
the provider's public models endpoint and prints the model ids available to the
key in ``.env`` (or the environment). It never prints or logs the key itself.

Endpoints (checked against current docs):

* Gemini — ``GET /v1beta/models`` (ai.google.dev/api/models): auth with the
  ``x-goog-api-key`` header so the key never appears in the URL; paginated
  with ``pageToken``/``pageSize``.
* Mistral — ``GET /v1/models`` (docs.mistral.ai/api/endpoint/models): auth with
  ``Authorization: Bearer``.
* Groq — ``GET /openai/v1/models`` (console.groq.com/docs/api-reference):
  OpenAI-style catalog, auth with ``Authorization: Bearer``.

No new dependencies; uses urllib like pipeline/providers.py.

Usage:
    python -m tools.list_models            # all providers referenced in config
    python -m tools.list_models --provider gemini
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import providers  # noqa: E402  (sys.path bootstrap above)


def _configured_providers(config: dict) -> list[str]:
    """Provider names referenced by config.yaml roles, in first-use order."""
    seen: list[str] = []
    for role, entries in config.get("roles", {}).items():
        for entry in entries or []:
            name = (entry or {}).get("provider")
            if name and name not in seen:
                seen.append(name)
    return seen


def _get_json(url: str, headers: dict) -> dict:
    request = urllib.request.Request(
        url,
        headers={**dict(headers), "User-Agent": providers._USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def list_gemini(provider: providers.GeminiProvider, api_key: str) -> list[dict]:
    """Model ids + text-capability for Google's Gemini API, following pagination."""
    models: list[dict] = []
    page_token = None
    while True:
        params = {"pageSize": "1000"}
        if page_token:
            params["pageToken"] = page_token
        url = provider.base_url + "?" + urlencode(params)
        data = _get_json(
            url,
            {"x-goog-api-key": api_key, "Accept": "application/json"},
        )
        for model in data.get("models", []):
            name = str(model.get("name", "")).removeprefix("models/")
            if not name:
                continue
            methods = model.get("supportedGenerationMethods") or []
            models.append({"id": name, "text": "generateContent" in methods})
        page_token = data.get("nextPageToken")
        if not page_token:
            return models


def list_openai_compatible(provider: providers.Provider, api_key: str) -> list[dict]:
    """Model ids + text-capability for any OpenAI-dialect /models endpoint."""
    url = provider.base_url.rstrip("/") + "/models"
    data = _get_json(
        url,
        {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    entries = data.get("data") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(f"unexpected response shape from {url}")
    models: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id", "")).strip()
        if not model_id:
            continue
        # Some catalogs report chat capability; Groq does not, so fall back to
        # naming: whisper/embedding/guard ids are not general-purpose chat.
        capabilities = entry.get("capabilities") or {}
        if "completion_chat" in capabilities:
            text = bool(capabilities.get("completion_chat"))
        else:
            text = not any(
                marker in model_id.lower()
                for marker in ("whisper", "embed", "guard", "moderation", "orpheus")
            )
        models.append({"id": model_id, "text": text})
    return models


def list_mistral(provider: providers.MistralProvider, api_key: str) -> list[dict]:
    """Mistral's catalog (docs.mistral.ai/api/endpoint/models)."""
    return list_openai_compatible(provider, api_key)


def list_groq(provider: providers.GroqProvider, api_key: str) -> list[dict]:
    """GroqCloud's catalog (console.groq.com/docs/api-reference#list-models)."""
    return list_openai_compatible(provider, api_key)


_LISTERS = {
    "gemini": list_gemini,
    "mistral": list_mistral,
    "groq": list_groq,
}


def list_for(provider: providers.Provider, api_key: str) -> list[dict]:
    lister = _LISTERS.get(provider.name)
    if lister is None:
        raise NotImplementedError(
            f"no models-list implementation for provider {provider.name!r}; "
            "add one to _LISTERS (see the provider's API docs)"
        )
    return lister(provider, api_key)


def _show_provider_config_usage(config: dict, model_id: str) -> bool:
    for role, entries in config.get("roles", {}).items():
        for entry in entries or []:
            if (entry or {}).get("model") == model_id:
                return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--provider", action="append", dest="providers", help="only these providers (repeatable)")
    args = parser.parse_args()

    config = providers.load_config()
    env = providers.load_env()
    registry = providers.default_providers()

    names = [name for name in (args.providers or []) if name] or _configured_providers(config)
    if not names:
        print("no providers found in pipeline/config.yaml roles")
        return 1

    used_models = {
        (entry or {}).get("model")
        for entries in config.get("roles", {}).values()
        for entry in entries or []
    }

    exit_code = 0
    for name in names:
        provider = registry.get(name)
        if provider is None:
            print(f"== {name}: not a registered provider (pipeline/providers.py)")
            exit_code = 1
            continue
        api_key = env.get(provider.env_key, "")
        if not api_key:
            print(f"== {name}: no {provider.env_key} key in .env / environment")
            exit_code = 1
            continue
        try:
            models = sorted(
                list_for(provider, api_key),
                key=lambda entry: entry["id"].lower(),
            )
        except Exception as exc:  # network error, bad key, bad payload
            print(f"== {name}: models lookup failed: {exc}")
            exit_code = 1
            continue

        print(f"== {name} ({provider.env_key}): {len(models)} models")
        for entry in models:
            flags = []
            if entry["text"]:
                flags.append("text")
            if entry["id"] in used_models:
                flags.append("used-in-config")
            print(f"  - {entry['id']}" + (f"  [{', '.join(flags)}]" if flags else ""))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())