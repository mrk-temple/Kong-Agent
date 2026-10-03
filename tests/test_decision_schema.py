"""I-03/08/09: explicit wire requirements with local, private repair feedback."""
import json

import pytest

from kong.contracts import DECISION_ADAPTER, Mode, RunState
from kong.decision_schema import decision_schema
from kong.models.diagnostics import protocol_diagnostics, repair_feedback


@pytest.mark.parametrize("kind,required", [
    ("evidence", []), ("file_exists", ["path"]), ("file_contains", ["path", "contains"]),
    ("file_equals", ["path", "expected_text"]), ("file_json", ["path", "json_match"]),
    ("tool_success", ["tool_name"]), ("tool_result", ["tool_name"]),
])
def test_criterion_wire_schema_is_narrow_and_requires_fields_by_kind(kind, required):
    schema = decision_schema()
    node = schema["$defs"]["Criterion_" + kind]
    assert set(node["required"]) == {"id", "description", "kind", *required}
    assert node["additionalProperties"] is False
    assert node["properties"]["kind"]["const"] == kind
    if kind != "tool_result":
        assert "operation" not in node["properties"] and "output_match" not in node["properties"]
    if kind == "tool_result":
        assert node["anyOf"] == [{"required": ["output_match"]}, {"required": ["output_contains"]}]
    for field in required:
        assert not any(option.get("type") == "null" for option in node["properties"][field].get("anyOf", []))


def test_tool_final_schema_requires_ids_but_plan_discussion_does_not():
    state = RunState(goal="task", requires_evidence=True)
    final = decision_schema(state)["$defs"]["FinalResponse"]
    assert "evidence_ids" in final["required"] and final["properties"]["evidence_ids"]["minItems"] == 1
    state.mode = Mode.PLAN
    assert "evidence_ids" not in decision_schema(state)["$defs"]["FinalResponse"]["required"]
    assert "approved_revision" not in decision_schema()["$defs"]["Plan"]["properties"]


def test_local_criterion_repair_names_the_actual_required_fields():
    raw = {"kind": "plan", "plan": {"goal": "task", "rationale": "review", "steps": [{"id": "s", "title": "outcome"}],
        "success_criteria": [{"id": "c", "description": "result", "kind": "tool_result", "tool_name": "process_exec"}]}}
    with pytest.raises(ValueError) as error:
        DECISION_ADAPTER.validate_python(raw)
    feedback = repair_feedback(protocol_diagnostics(error.value))
    assert feedback["actions_executed"] == 0
    item = feedback["errors"][0]
    assert item["path"] == "$.plan.success_criteria[]"
    assert item["required_any_of"] == [["output_match"], ["output_contains"]]
    assert "tool_name" in item["allowed_fields"]


def test_repair_feedback_is_bounded_and_never_echoes_untrusted_diagnostic_content():
    canary = "PRIVATE_CREDENTIAL_CANARY"
    feedback = repair_feedback([{"path": "$." + canary, "code": canary, "hint": canary}] * 50)
    assert canary not in json.dumps(feedback)
    assert len(feedback["errors"]) == 4 and len(json.dumps(feedback)) < 2500


def test_exported_schema_rejects_missing_required_fields_without_changing_saved_contract_models():
    from jsonschema import Draft202012Validator
    schema = decision_schema()
    valid = {"kind": "plan", "plan": {"goal": "task", "rationale": "review", "steps": [{"id": "s", "title": "outcome"}],
        "success_criteria": [{"id": "c", "description": "result", "kind": "file_json", "path": "result.json", "json_match": {}}]}}
    assert not list(Draft202012Validator(schema).iter_errors(valid))
    valid["plan"]["success_criteria"][0].pop("json_match")
    assert list(Draft202012Validator(schema).iter_errors(valid))
