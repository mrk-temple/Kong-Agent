"""Observed verification checkpoints measure closure, not universal correctness."""
import asyncio

from kong.contracts import Act, Action, Criterion, FinalResponse, Status
from kong.models.fake import ScriptedModel
from kong.reporting import run_report
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def test_weak_file_exists_is_reported_as_mechanical_acceptance(tmp_path):
    workspace = Workspace(tmp_path)
    model = ScriptedModel([Act(actions=[Action(name="write_file", arguments={"path": "out.json", "content": '{"included_statuses":2}'})]),
                           FinalResponse(content="done", evidence_ids=["a1"])])
    runtime = Runtime.new("make correct report", model, workspace, default_registry(workspace))
    runtime.state.success_criteria = [Criterion(id="out", description="output", kind="file_exists", path="out.json")]
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    acceptance = run_report(runtime)["verification"]
    assert acceptance["coverage"] == "mechanical_only"
    assert acceptance["business_acceptance_declared"] is False
    assert acceptance["declared_checks_passed"] is True


def test_submission_repair_measures_one_model_call_and_zero_additional_tools(tmp_path):
    workspace = Workspace(tmp_path)
    model = ScriptedModel([Act(actions=[
        Action(name="write_file", arguments={"path": "out.txt", "content": "OK"}),
        Action(name="read_file", arguments={"path": "out.txt"})]),
        FinalResponse(content="done"), FinalResponse(content="done", evidence_ids=["a2"])])
    runtime = Runtime.new("write exact output", model, workspace, default_registry(workspace),
        completion_contract={"family": "data", "artifacts": [{"id": "out", "path": "out.txt", "format": "text", "expected_text": "OK"}]},
        store=RunStore(tmp_path / ".kong/runs"))
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    closure = run_report(runtime)["closure"]
    assert closure["checkpoint_coverage"] == "current_driver"
    assert closure["model_calls_since_first_artifact_checks_passed"] == 2
    assert closure["tool_calls_since_first_artifact_checks_passed"] == 0
    assert closure["active_seconds_since_first_artifact_checks_passed"] >= 0
    assert closure["rejections"][0]["completed_on_next_decision"] is True
    assert closure["rejections"][0]["tool_observations_until_next_respond"] == 0
    saved = runtime.store.load(runtime.state.run_id)
    assert any(e.payload.get("verification_checkpoint") for e in saved.history.events)


def test_legacy_snapshot_missing_checkpoints_stays_unknown(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("legacy", ScriptedModel([]), workspace, default_registry(workspace))
    snapshot = runtime.snapshot()
    snapshot.metrics = None
    restored = Runtime.restore(snapshot, ScriptedModel([]), workspace, runtime.registry)
    closure = run_report(restored)["closure"]
    assert closure["checkpoint_coverage"] == "unmeasured"
    assert closure["model_calls_since_first_artifact_checks_passed"] is None


def test_intermediate_valid_output_is_not_reported_as_final_valid(tmp_path):
    workspace = Workspace(tmp_path)
    model = ScriptedModel([Act(actions=[Action(name="write_file", arguments={"path": "out.txt", "content": "OK"})]),
        Act(actions=[Action(name="write_file", arguments={"path": "out.txt", "content": "WRONG", "overwrite": True})]),
        FinalResponse(content="done", evidence_ids=["a2"])])
    runtime = Runtime.new("write exact output", model, workspace, default_registry(workspace),
        completion_contract={"family": "data", "artifacts": [{"id": "out", "path": "out.txt", "format": "text", "expected_text": "OK"}]})
    runtime.state.hard_turn_limit = 3
    asyncio.run(runtime.run())
    report = run_report(runtime)
    assert report["closure"]["artifact_checks_invalidated_after_first"] is True
    assert report["verification"]["declared_checks_passed"] is False
    assert runtime.state.status != Status.COMPLETED
