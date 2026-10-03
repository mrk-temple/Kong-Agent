"""Schema-only diagnostics: never echo values, dynamic keys, or validator text."""
from typing import get_args
import re

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


def repair_feedback(diagnostics):
    """Short schema-only repair guidance; no model values or dynamic keys."""
    schema = DECISION_ADAPTER.json_schema()
    fields = {key for node in schema["$defs"].values() for key in node.get("properties", {})}
    codes = set(get_args(ErrorType)) | set(INVARIANT_MESSAGES) | {"invalid_json", "validation_error"}
    errors = []
    for diagnostic in diagnostics[:4]:
        if not isinstance(diagnostic, dict):
            continue
        path = diagnostic.get("path", "$")
        if (not isinstance(path, str) or len(path) > 200
                or not re.fullmatch(r"\$(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|\*)(?:\[\])?)*", path)
                or any(part not in fields for part in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", path))):
            path = "$.*"
        code = diagnostic.get("code")
        code = code if isinstance(code, str) and code in codes else "validation_error"
        item = {"path": path, "code": code}
        if code in INVARIANT_MESSAGES:
            item["hint"] = INVARIANT_MESSAGES[code]
        if code in {"union_tag_not_found", "union_tag_invalid"}:
            item.update(required=["kind"], allowed_kinds=["respond", "act", "discover", "plan", "ask", "complete_step", "assess"])
        elif code == "criterion_missing_expected":
            item["required_by_kind"] = {"file_equals": ["expected_text"], "file_json": ["json_match"]}
        elif code == "criterion_missing_result_match":
            item.update(required_for="tool_result", required_any_of=[["output_match"], ["output_contains"]],
                allowed_fields=["id", "description", "kind", "tool_name", "operation", "arguments_match",
                                "output_match", "output_contains", "evidence_scope", "artifact_paths", "after_last_write"])
        elif code == "criterion_missing_tool":
            item.update(required_for=["tool_success", "tool_result"], required=["tool_name"])
        elif code == "criterion_missing_path":
            item["required"] = ["path"]
        elif code == "missing" and path.rsplit(".", 1)[-1] in fields:
            item["required"] = [path.rsplit(".", 1)[-1]]
        elif code == "extra_forbidden":
            item["hint"] = "Remove the extra field; use only fields in the supplied kind-specific schema."
        errors.append(item)
    return {"scope": "decision", "errors": errors or [{"path": "$", "code": "validation_error"}],
            "actions_executed": 0, "next": "Return one corrected Decision at these paths on the next budgeted turn."}
