"""Default environment completion needs declared conditions, not existence."""
import asyncio
import json

from kong.contracts import Act, Action, Criterion, FinalResponse, Status
from kong.models.fake import ScriptedModel
from kong.reporting import run_report
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def make(path, *, scope=None, output=None):
    workspace = Workspace(path)
    content = json.dumps(output or {"included_statuses": 2, "total": 7})
    decisions = [Act(actions=[
        Action(name="write_file", arguments={"path": "report.json", "content": content}),
        Action(name="read_file", arguments={"path": "report.json"})]),
        FinalResponse(content="The business report is complete", evidence_ids=["a2"])]
    kwargs = {"completion_scope": scope} if scope else {}
    runtime = Runtime.new("Create the correct business report", ScriptedModel(decisions), workspace,
        default_registry(workspace), store=RunStore(path / ".kong/runs"), **kwargs)
    runtime.state.success_criteria = [Criterion(id="out", description="Report exists", kind="file_exists", path="report.json")]
    return runtime


def test_default_wrong_json_is_paused_for_acceptance_not_completed(tmp_path):
    runtime = make(tmp_path)
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    assert runtime.state.final_output is None
    assert runtime.state.stop_reason == "acceptance_criteria_required"
    assert runtime.metrics.model_calls == 2  # No model retry can invent authority.
    assert len(runtime.history.observations) == 2
    assert run_report(runtime)["verification"]["business_acceptance_declared"] is False


def test_irrelevant_exact_condition_does_not_cover_a_weak_output(tmp_path):
    runtime = make(tmp_path)
    (tmp_path / "other.txt").write_text("OK", encoding="utf-8")
    runtime.state.success_criteria.append(Criterion(id="other", description="Other file", kind="file_equals",
                                                  path="other.txt", expected_text="OK"))
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    assert runtime.state.final_output is None


def test_explicit_mechanical_scope_is_persisted_and_never_business_acceptance(tmp_path):
    runtime = make(tmp_path, scope="mechanical")
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    report = run_report(runtime)
    assert report["verification"]["completion_scope"] == "mechanical"
    assert report["verification"]["business_accepted"] is False
    assert "机械" in runtime.state.final_output or "mechanical" in runtime.state.final_output.lower()
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id), ScriptedModel([]),
                               runtime.workspace, runtime.registry)
    assert restored.state.completion_scope == "mechanical"


def test_declared_json_rejects_wrong_and_accepts_correct_values(tmp_path):
    expected = {"included_statuses": ["active"], "total": 7}
    for name, value in [("wrong", None), ("right", expected)]:
        folder = tmp_path / name
        folder.mkdir()
        runtime = make(folder, output=value)
        runtime.state.success_criteria = [Criterion(id="out", description="Exact report", kind="file_json",
                                                    path="report.json", json_match=expected)]
        asyncio.run(runtime.run())
        assert (runtime.state.status == Status.COMPLETED) == (name == "right")


def test_chat_still_completes_without_artifact_contract(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("hello", ScriptedModel([FinalResponse(content="hello")]),
                          workspace, default_registry(workspace))
    assert asyncio.run(runtime.run()).status == Status.COMPLETED


def test_restore_weak_running_snapshot_does_not_get_implicit_mechanical_scope(tmp_path):
    runtime = make(tmp_path)
    snapshot = runtime.snapshot()
    payload = snapshot.model_dump(mode="json")
    payload["state"].pop("completion_scope", None)
    from kong.storage import Snapshot
    restored = Runtime.restore(Snapshot.model_validate(payload), runtime.model, runtime.workspace, runtime.registry)
    assert asyncio.run(restored.run()).status == Status.WAITING_USER
