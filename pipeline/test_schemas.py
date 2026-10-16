"""Every model-output schema must survive strict JSON-schema providers.

Groq (and OpenAI) reject a ``json_schema`` response format unless every object
lists all of its ``properties`` in ``required`` and sets
``additionalProperties: false``. The pipeline's schemas are also used by the
local validator, so the same invariant is asserted here for all of them, both
as written and as embedded in a real Groq request body.
"""

from __future__ import annotations

import pytest

import facts
import seo
import verify
from providers import GroqProvider

SCHEMAS = {
    "CLAIM_SCHEMA": facts.CLAIM_SCHEMA,
    "GROUPING_SCHEMA": facts.GROUPING_SCHEMA,
    "SEO_SCHEMA": seo.SEO_SCHEMA,
    "VERIFIER_SCHEMA": verify.VERIFIER_SCHEMA,
}


def _object_nodes(schema, path: str = "$"):
    """Yield ``(path, object-schema)`` for every object anywhere in a schema."""
    if not isinstance(schema, dict):
        return
    kind = schema.get("type")
    if kind == "object":
        yield path, schema
        for name, subschema in (schema.get("properties") or {}).items():
            yield from _object_nodes(subschema, f"{path}.{name}")
    elif kind == "array":
        yield from _object_nodes(schema.get("items") or {}, f"{path}[]")


def _strictness_violations(schema) -> list[str]:
    """Return every strict-mode violation in a schema (empty means valid)."""
    violations: list[str] = []
    for path, node in _object_nodes(schema):
        properties = set((node.get("properties") or {}).keys())
        required = set(node.get("required") or [])
        missing = sorted(properties - required)
        if missing:
            violations.append(f"{path}: properties missing from required: {missing}")
        if node.get("additionalProperties") is not False:
            violations.append(f"{path}: additionalProperties must be false")
    return violations


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_object_is_strict_compatible(name):
    schema = SCHEMAS[name]
    objects = list(_object_nodes(schema))
    assert objects, f"{name} contains no object nodes"
    assert _strictness_violations(schema) == []


def test_strictness_check_flags_a_bad_schema():
    bad = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": True,
    }
    violations = _strictness_violations(bad)
    assert any("missing from required" in v for v in violations)
    assert any("additionalProperties must be false" in v for v in violations)


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_groq_request_body_embeds_a_strict_schema(name):
    schema = SCHEMAS[name]
    url, payload, _headers = GroqProvider().prepare(
        "openai/gpt-oss-20b", "hi", "KEY", schema
    )
    assert url.endswith("/chat/completions")
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    embedded = response_format["json_schema"]
    assert embedded["strict"] is True
    assert embedded["schema"] is schema
    assert _strictness_violations(embedded["schema"]) == []


def _claim(**overrides):
    claim = {
        "claim": "Phoenix Cyclops joins on November 2",
        "value": "arrives November 2",
        "source_url": "https://blog.playstation.com/x",
        "confidence": 0.9,
        "origin": "PlayStation Blog",
        "affiliation": "",
        "kind": "confirmed",
    }
    claim.update(overrides)
    return claim


def test_claim_with_empty_affiliation_is_accepted():
    claims = facts.validate_claims({"claims": [_claim()]})
    assert claims[0]["affiliation"] == ""


def test_claim_with_missing_affiliation_is_treated_like_empty():
    claim = _claim()
    del claim["affiliation"]
    claims = facts.validate_claims({"claims": [claim]})
    assert claims[0]["affiliation"] == ""


def test_a_genuinely_missing_required_key_is_still_rejected():
    claim = _claim()
    del claim["origin"]
    with pytest.raises(facts.FactsError):
        facts.validate_claims({"claims": [claim]})
