"""I-05/06/07/10: repair the submission without redoing a correct artifact."""
import asyncio

from kong.contracts import Act, Action, Criterion, FinalResponse, Status
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def act(name, **arguments):
    return Act(actions=[Action(name=name, arguments=arguments)])


def latest_feedback(runtime):
    return next(e.payload["completion_feedback"] for e in reversed(runtime.history.events)
                if "completion_feedback" in e.payload)


def test_historical_recovery_delivers_correct_product_without_repeated_read(tmp_path):
    (tmp_path / "instructions.txt").write_text("RECOVERED", encoding="utf-8")
    model = ScriptedModel([
        act("read_file", path="missing.txt"),
        Act(actions=[Action(name="read_file", arguments={"path": "instructions.txt"}),
            Action(name="write_file", arguments={"path": "result.txt", "content": "RECOVERED"}),
            Action(name="read_file", arguments={"path": "result.txt"})]),
        FinalResponse(content="done"), FinalResponse(content="done", evidence_ids=["a4"]),
    ])
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("recover", model, workspace, default_registry(workspace))
    runtime.state.success_criteria = [Criterion(id="result", description="Exact recovery", kind="file_equals",
        path="result.txt", expected_text="RECOVERED")]
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    feedback = latest_feedback(runtime)
    assert "a4" in feedback["current_valid_observations"]
    assert feedback["artifact_mutation_required"] is False
    assert feedback["additional_tool_required"] is False
    assert "corrected" in feedback["next_action"]
    assert len(runtime.history.observations) == runtime.metrics.tool_calls == 4
    assert runtime.state.turn_count == model.calls == 4


def test_current_host_contract_id_can_repair_wrong_binding_without_new_action(tmp_path):
    workspace = Workspace(tmp_path)
    model = ScriptedModel([Act(actions=[Action(name="write_file", arguments={"path": "out.txt", "content": "OK"}),
        Action(name="read_file", arguments={"path": "out.txt"})]),
        FinalResponse(content="done", evidence_ids=["a1"]), FinalResponse(content="done", evidence_ids=["a2"])])
    runtime = Runtime.new("write report", model, workspace, default_registry(workspace),
        completion_contract={"family": "data", "artifacts": [{"id": "out", "path": "out.txt", "format": "text", "expected_text": "OK"}]})
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    feedback = latest_feedback(runtime)
    assert feedback["artifact_mutation_required"] is False and feedback["additional_tool_required"] is False
    assert "a2" in feedback["current_valid_observations"]
    assert len(runtime.history.observations) == 2 and runtime.state.turn_count == 3
    assert "a1" not in feedback["current_valid_observations"]


def test_evidence_criterion_can_repair_mapping_without_more_tools(tmp_path):
    workspace = Workspace(tmp_path)
    model = ScriptedModel([Act(actions=[Action(name="write_file", arguments={"path": "out.txt", "content": "OK"}),
        Action(name="read_file", arguments={"path": "out.txt"})]), FinalResponse(content="done", evidence_ids=["a2"]),
        FinalResponse(content="done", evidence_ids=["a2"], criteria_evidence={"current": ["a2"]})])
    runtime = Runtime.new("verify current output", model, workspace, default_registry(workspace))
    runtime.state.success_criteria = [Criterion(id="current", description="Current proof", artifact_paths=["out.txt"])]
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    feedback = latest_feedback(runtime)
    assert feedback["unsatisfied"] == ["current"]
    assert feedback["artifact_mutation_required"] is False and feedback["additional_tool_required"] is False
    assert feedback["criterion_observations"]["current"] == ["a2"]
    assert len(runtime.history.observations) == 2


def test_rejection_feedback_never_invents_or_binds_evidence(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("write", ScriptedModel([]), workspace, default_registry(workspace))
    runtime.state.success_criteria = [Criterion(id="exists", description="Output", kind="file_exists", path="out.txt")]
    asyncio.run(runtime._actions(act("write_file", path="out.txt", content="OK")))
    decision = FinalResponse(content="done", evidence_ids=["invented"])
    gate = CompletionGate(workspace)
    problems = gate.check(runtime.state, decision, runtime.history)
    feedback = gate.rejection_feedback(runtime.state, decision, runtime.history, problems)
    assert runtime.state.final_output is None and decision.evidence_ids == ["invented"]
    assert "invented" not in feedback["available_successful_observations"]
    assert len(runtime.history.observations) == 1
