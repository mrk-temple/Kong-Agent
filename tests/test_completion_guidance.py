"""I-03/05/06/07/08/10: repair the failing requirement, never weaken it."""
import asyncio
import json

import pytest

from kong.contracts import Act, Action, FinalResponse, Mode, NeedUserInput, Status
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def make_runtime(path, decisions=(), **kwargs):
    (path / "value.py").write_text("value = 7\n", encoding="utf-8")
    (path / "check.py").write_text("from value import value\nassert value == 7\nprint('ok')\n", encoding="utf-8")
    workspace = Workspace(path)
    contract = {"family": "code", "protected_paths": ["check.py"], "required_reads": ["value.py"],
        "artifacts": [{"id": "source", "path": "value.py", "format": "file"}],
        "commands": [{"id": "check", "argv": ["python", "check.py"], "fresh_python": True,
                      "stdout_contains": ["ok"]}]}
    return Runtime.new("repair value and verify", ScriptedModel(list(decisions)), workspace,
                       default_registry(workspace, allow_process=True), completion_contract=contract, **kwargs)


def actions(runtime, *items):
    asyncio.run(runtime._actions(Act(actions=[Action(name=name, arguments=args) for name, args in items])))


def feedback(runtime):
    gate = CompletionGate(runtime.workspace)
    decision = FinalResponse(content="done", evidence_ids=[o.action_id for o in runtime.history.observations if o.success])
    return gate.rejection_feedback(runtime.state, decision, runtime.history, gate.check(runtime.state, decision, runtime.history))


def command_issues(runtime):
    from kong.runtime.completion import contract_results
    return next(r for r in contract_results(runtime.state, runtime.history, runtime.workspace)
                if r["id"] == "command:check")["issues"]


def test_missing_fresh_mode_names_flag_and_orders_final_read_after_check(tmp_path):
    runtime = make_runtime(tmp_path)
    actions(runtime, ("process_exec", {"argv": ["python", "check.py"]}), ("read_file", {"path": "value.py"}))
    result = feedback(runtime)
    assert "command.fresh_mode_missing" in [i["code"] for i in command_issues(runtime)]
    remaining = result["remaining_steps"]
    check = next(step for step in remaining if step.get("action", {}).get("name") == "process_exec")
    assert check["action"]["arguments"]["fresh_python"] is True
    assert remaining.index(check) < next(i for i, step in enumerate(remaining) if step.get("action", {}).get("name") == "read_file")
    assert result["artifact_mutation_required"] is None
    assert len(runtime.history.observations) == 2  # Guidance never runs a tool.


@pytest.mark.parametrize("mutation,code", [
    ("flag", "command.fresh_mode_missing"), ("marker", "command.fresh_marker_missing"),
    ("exit", "command.exit_code_mismatch"), ("stdout", "command.stdout_contains_missing"),
    ("unknown", "command.effect_unknown"), ("timeout", "command.timed_out"),
    ("failure", "command.observation_failed"),
])
def test_command_rejection_identifies_real_requirement(tmp_path, mutation, code):
    runtime = make_runtime(tmp_path)
    actions(runtime, ("process_exec", {"argv": ["python", "check.py"], "fresh_python": True}))
    obs = runtime.history.observations[-1]
    if mutation == "flag":
        obs.action.arguments["fresh_python"] = False
    elif mutation == "marker":
        obs.output["fresh_python_imports"] = False
    elif mutation == "exit":
        obs.output["exit_code"] = 1
    elif mutation == "stdout":
        obs.output["stdout"] = "different"
    elif mutation == "unknown":
        obs.outcome_unknown = True
    elif mutation == "timeout":
        obs.output["timed_out"] = True
    elif mutation == "failure":
        obs.success = False
    assert code in [issue["code"] for issue in command_issues(runtime)]
    assert not next(r for r in CompletionGate(runtime.workspace).criteria_results(runtime.state, runtime.history, {})
                    if r["id"] == "command:check")["verified"]


@pytest.mark.parametrize("arguments,code", [
    ({"argv": ["python", "-c", "print('ok')"]}, "command.argv_mismatch"),
    ({"argv": ["python", "check.py"], "cwd": "sub"}, "command.cwd_mismatch"),
])
def test_wrong_command_selector_is_not_generic_missing_check(tmp_path, arguments, code):
    runtime = make_runtime(tmp_path)
    (tmp_path / "sub").mkdir()
    actions(runtime, ("process_exec", arguments))
    assert code in [issue["code"] for issue in command_issues(runtime)]


def test_verify_then_mutate_feedback_preserves_stale_rejection(tmp_path):
    runtime = make_runtime(tmp_path)
    actions(runtime, ("process_exec", {"argv": ["python", "check.py"], "fresh_python": True}),
            ("write_file", {"path": "value.py", "content": "value = 8\n", "overwrite": True}))
    assert "command.verification_stale" in [issue["code"] for issue in command_issues(runtime)]
    assert feedback(runtime)["additional_tool_required"] is True


def test_live_guidance_exists_before_first_submission_and_pins_current_ids(tmp_path):
    runtime = make_runtime(tmp_path, [Act(actions=[
        Action(name="process_exec", arguments={"argv": ["python", "check.py"], "fresh_python": True}),
        Action(name="read_file", arguments={"path": "value.py"})]),
        FinalResponse(content="done", evidence_ids=["a2"])])
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    packet = runtime.model.contexts[-1]
    guidance = next(item for item in packet.items if item.category == "runtime_feedback")
    assert guidance.priority == 0 and "a2" in guidance.content
    assert packet.messages[-1]["content"].startswith("RUNTIME VERIFICATION STATUS:")
    assert "corrected respond" in guidance.content
    assert runtime.model.calls == 2 and len(runtime.history.observations) == 2


def test_plan_preapproval_never_gets_execution_guidance(tmp_path):
    runtime = make_runtime(tmp_path, [NeedUserInput(question="Clarify scope")], mode=Mode.PLAN)
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    assert not runtime.history.observations
    assert not any(item.category == "runtime_feedback" for item in runtime.model.contexts[0].items)


def test_guidance_survives_large_history_without_raising_budget(tmp_path):
    runtime = make_runtime(tmp_path, [Act(actions=[
        Action(name="process_exec", arguments={"argv": ["python", "check.py"], "fresh_python": True}),
        Action(name="read_file", arguments={"path": "value.py"})]), FinalResponse(content="done", evidence_ids=["a2"])])
    for index in range(30):
        runtime.history.append("feedback", 0, old_diagnostic="noise" * 500, index=index)
    budget = runtime.context_engine.budget.model_dump()
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    packet = runtime.model.contexts[-1]
    assert packet.dropped and packet.estimated_input_tokens <= packet.budget.input_tokens
    assert any(i.category == "runtime_feedback" and i.priority == 0 for i in packet.items)
    assert runtime.context_engine.budget.model_dump() == budget


def test_external_mutation_recomputes_guidance_instead_of_reusing_ready_state(tmp_path):
    from kong.runtime.finalization import completion_guidance
    runtime = make_runtime(tmp_path)
    actions(runtime, ("process_exec", {"argv": ["python", "check.py"], "fresh_python": True}),
            ("read_file", {"path": "value.py"}))
    assert completion_guidance(CompletionGate(runtime.workspace), runtime.state, runtime.history)["additional_tool_required"] is False
    (tmp_path / "value.py").write_text("value = 8\n", encoding="utf-8")
    current = completion_guidance(CompletionGate(runtime.workspace), runtime.state, runtime.history)
    assert current["additional_tool_required"] is True
    assert "a2" not in current["current_valid_observations"]
    assert "command:check" in current["unsatisfied"]


def test_changed_constraint_removes_old_guidance_and_preserves_host_boundary(tmp_path):
    from kong.runtime.finalization import completion_guidance
    runtime = make_runtime(tmp_path)
    actions(runtime, ("read_file", {"path": "value.py"}))
    runtime.submit_input("constraint", "Use another value")
    runtime._apply_inputs()
    assert runtime.state.status == Status.WAITING_USER and runtime.state.contract_invalidated
    assert completion_guidance(CompletionGate(runtime.workspace), runtime.state, runtime.history) is None


def test_exact_text_diagnostic_explains_newline_without_relaxing_contract(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("write exact text", ScriptedModel([]), workspace, default_registry(workspace),
        completion_contract={"family": "data", "artifacts": [
            {"id": "out", "path": "out.txt", "format": "text", "expected_text": '"CURRENT"'}]})
    actions(runtime, ("write_file", {"path": "out.txt", "content": '"CURRENT"\n'}), ("read_file", {"path": "out.txt"}))
    from kong.runtime.completion import contract_results
    result = next(r for r in contract_results(runtime.state, runtime.history, workspace) if r["id"] == "artifact:out")
    assert not result["verified"] and result["issues"][0]["code"] == "artifact.text_mismatch"
    assert result["issues"][0]["newline_only"] is True
    assert feedback(runtime)["additional_tool_required"] is True
    assert '"CURRENT"\n' == (tmp_path / "out.txt").read_text()


def test_json_wrong_type_is_diagnosed_and_never_accepted(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("summarize", ScriptedModel([]), workspace, default_registry(workspace),
        completion_contract={"family": "data", "artifacts": [{"id": "out", "path": "out.json", "format": "json",
            "json_fields": [{"name": "included_statuses", "type": "array_string"}]}]})
    actions(runtime, ("write_file", {"path": "out.json", "content": json.dumps({"included_statuses": 2})}),
            ("read_file", {"path": "out.json"}))
    result = feedback(runtime)
    assert result["additional_tool_required"] and result["artifact_mutation_required"] is True
    issue = next(i for i in result["requirement_issues"] if i["id"] == "artifact:out")
    assert issue["issues"][0]["code"] == "artifact.field_mismatch"
