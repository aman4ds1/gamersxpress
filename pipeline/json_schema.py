"""Tiny JSON-Schema-subset validator shared by the facts and verify stages.

Supports only the keywords our model-output schemas use: ``type`` (object,
array, string, boolean, integer, number), ``properties``, ``required``,
``additionalProperties: false``, ``items``, ``enum``, ``minimum`` and
``maximum``. It returns every problem it finds instead of raising, so callers
decide whether a bad payload is a dropped story or a failed report.
"""

from __future__ import annotations

from typing import Any

SCHEMA_TYPES = ("object", "array", "string", "boolean", "integer", "number")


def schema_errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable schema violations (empty means valid)."""
    errors: list[str] = []
    kind = schema.get("type")
    if isinstance(kind, list):
        if not any(_matches_type(value, candidate) for candidate in kind):
            return [f"{path}: expected one of {kind}, got {type(value).__name__}"]
        return errors

    if kind == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object, got {type(value).__name__}"]
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                errors.append(f"{path}: unexpected keys {sorted(extra)}")
        for key, subschema in properties.items():
            if key in value:
                errors.extend(schema_errors(value[key], subschema, f"{path}.{key}"))
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
    elif kind == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array, got {type(value).__name__}"]
        items = schema.get("items", {})
        for index, item in enumerate(value):
            errors.extend(schema_errors(item, items, f"{path}[{index}]"))
    elif kind == "string":
        if not isinstance(value, str):
            errors.append(f"{path}: expected string, got {type(value).__name__}")
        elif "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: value {value!r} not in {schema['enum']}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{path}: expected boolean, got {type(value).__name__}")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path}: expected integer, got {type(value).__name__}")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{path}: expected number, got {type(value).__name__}")
        else:
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{path}: {value} is below minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"{path}: {value} is above maximum {schema['maximum']}")
    return errors


def _matches_type(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


__all__ = ["SCHEMA_TYPES", "schema_errors"]