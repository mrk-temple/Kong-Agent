"""Schema-only diagnostics: never echo values, dynamic keys, or validator text."""
from typing import get_args

from pydantic import ValidationError
from pydantic_core import ErrorType

from kong.contracts import DECISION_ADAPTER, INVARIANT_MESSAGES


def protocol_diagnostics(error: Exception) -> list[dict[str, str]]:
    if not isinstance(error, ValidationError):
        code = {"JSONDecodeError": "invalid_json", "KeyError": "missing_envelope_field",
                "IndexError": "missing_choice", "TypeError": "invalid_envelope_type"}.get(
                    type(error).__name__, "invalid_envelope")
        return [{"path": "$", "code": code}]
    schema = DECISION_ADAPTER.json_schema()
    codes = set(get_args(ErrorType)) | set(INVARIANT_MESSAGES)

    def resolve(node):
        while "$ref" in node:
            node = schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
        return node

    results = []
    for issue in error.errors(include_url=False, include_context=False, include_input=False)[:16]:
        node, path = schema, "$"
        for part in issue["loc"][:16]:
            node = resolve(node)
            mapping = node.get("discriminator", {}).get("mapping", {})
            if isinstance(part, str) and part in mapping:
                node = resolve({"$ref": mapping[part]})
                continue
            if type(part) is int and "items" in node:
                path += "[]"
                node = node["items"]
            elif isinstance(part, str) and part in node.get("properties", {}):
                path += "." + part
                node = node["properties"][part]
            else:
                # Unknown fields and dictionary keys can contain credentials.
                path += ".*"
                break
        results.append({"path": path, "code": issue["type"] if issue["type"] in codes else "validation_error"})
        if issue["type"] in INVARIANT_MESSAGES:
            results[-1]["hint"] = INVARIANT_MESSAGES[issue["type"]]
    return results
