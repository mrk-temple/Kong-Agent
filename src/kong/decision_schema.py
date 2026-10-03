"""Narrow model-facing schemas, independent of legacy snapshot defaults."""
from copy import deepcopy

from kong.contracts import DECISION_ADAPTER


def model_schema(value):
    """Drop annotations, retaining constraints and all literal enum/const values."""
    if isinstance(value, list):
        return [model_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: item if key in {"const", "enum", "examples"} else model_schema(item)
            for key, item in value.items() if key != "default"
            and not (key in {"description", "title"} and isinstance(item, str))}


def nonnullable(node):
    node = deepcopy(node)
    options = node.get("anyOf", [])
    if options:
        allowed = [option for option in options if option.get("type") != "null"]
        if len(allowed) == 1:
            node.pop("anyOf")
            node.update(allowed[0])
    if node.get("default", False) is None:
        node.pop("default")
    return node


def decision_schema(state=None, history=None):
    schema = DECISION_ADAPTER.json_schema()
    definitions = schema["$defs"]
    properties = definitions["Criterion"]["properties"]
    scopes = ["evidence_scope", "artifact_paths"]
    variants = {
        "evidence": (scopes, []),
        "file_exists": (["path"], ["path"]),
        "file_contains": (["path", "contains"], ["path", "contains"]),
        "file_equals": (["path", "expected_text"], ["path", "expected_text"]),
        "file_json": (["path", "json_match"], ["path", "json_match"]),
        "tool_success": (["tool_name", "after_last_write", *scopes], ["tool_name"]),
        "tool_result": (["tool_name", "after_last_write", "operation", "arguments_match", "output_match", "output_contains", *scopes], ["tool_name"]),
    }
    mapping = {}
    for kind, (fields, required) in variants.items():
        name = "Criterion_" + kind
        fields = ["id", "description", *fields]
        props = {key: nonnullable(properties[key]) for key in fields}
        props["kind"] = {"type": "string", "const": kind}
        for key in {"path", "tool_name", "contains", "operation"}.intersection(props):
            props[key]["minLength"] = 1
        node = {"type": "object", "additionalProperties": False, "properties": props,
                "required": ["id", "description", "kind", *required]}
        if kind == "tool_result":
            node["anyOf"] = [{"required": ["output_match"]}, {"required": ["output_contains"]}]
            for key in ("output_match", "output_contains"):
                props[key]["minProperties"] = 1
        definitions[name] = node
        mapping[kind] = "#/$defs/" + name
    definitions["Criterion"] = {"oneOf": [{"$ref": reference} for reference in mapping.values()]}
    # Approval, step completion and revisions belong to the controller. Legacy
    # parser/snapshot models still accept and sanitize these saved fields.
    for name, fields in {"Plan": ("revision", "approved_revision"), "PlanStep": ("done", "evidence_ids")}.items():
        for key in fields:
            definitions[name]["properties"].pop(key, None)
    for variant in schema["oneOf"]:
        node = definitions[variant["$ref"].rsplit("/", 1)[-1]]
        node["required"] = sorted(set(node.get("required", []) + ["kind"]))
    if state is not None:
        plan_discussion = state.pending_approval or (state.mode.value == "plan" and
            (not state.plan or state.plan.approved_revision != state.plan.revision))
        if not plan_discussion and (state.requires_evidence or history is not None and history.observations):
            response = definitions["FinalResponse"]
            response["required"] = sorted(set(response["required"] + ["evidence_ids"]))
            response["properties"]["evidence_ids"]["minItems"] = 1
    return model_schema(schema)
