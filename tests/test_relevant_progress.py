"""I-03: leases depend on bounded declared progress, never arbitrary activity."""
import asyncio

from kong.contracts import Act, Action, Criterion, ProgressAssessment, RunState, Status
from kong.models.fake import ScriptedModel
from kong.reporting import run_report
from kong.runtime.gates import ProgressGate
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def read(path, **kwargs):
    return Act(actions=[Action(name="read_file", arguments={"path": path, **kwargs})])


def make(tmp_path, decisions=(), **kwargs):
    ws = Workspace(tmp_path)
    return Runtime.new("produce target.txt", ScriptedModel(list(decisions)), ws, default_registry(ws), **kwargs)


def test_original_irrelevant_reads_cannot_renew(tmp_path):
    decisions = []
    for index in range(9):
        (tmp_path / f"noise{index}").write_text(str(index))
        decisions.append(read(f"noise{index}"))
        if index in {3, 7}:
            decisions.append(ProgressAssessment(diagnosis="new file", next_strategy="continue"))
    rt = make(tmp_path, decisions)
    rt.state.success_criteria = [Criterion(id="out", description="out", kind="file_exists", path="target.txt")]
    asyncio.run(rt.run())
    assert rt.state.status == Status.WAITING_USER
    assert len(rt.history.observations) == 4
    assert rt.state.progress_state.progress_count == 0
    assert rt.state.progress_state.activity_count == 4
    assert not any(e.payload.get("transition", {}).get("reason") == "lease_renewed_with_progress" for e in rt.history.events)


def test_declared_inspection_earns_once_per_resource(tmp_path):
    (tmp_path / "input").mkdir()
    for index in range(4):
        (tmp_path / "input" / str(index)).write_text("same")
    rt = make(tmp_path, progress_paths=["input"])
    asyncio.run(rt._actions(Act(actions=[Action(name="read_file", arguments={"path": f"input/{i}"}) for i in range(4)])))
    assert ProgressGate.can_renew(rt.state)
    assert rt.state.progress_state.progress_count == 4
    asyncio.run(rt._actions(read("input/0", offset=1)))
    assert rt.state.progress_state.progress_count == 4
    summary = run_report(rt)["progress"]
    assert summary["task_credit_count"] == 4
    assert summary["goal_progress_measured"] is False


def test_wrong_mutations_and_empty_chunks_do_not_earn_credit(tmp_path):
    rt = make(tmp_path)
    rt.state.success_criteria = [Criterion(id="value", description="correct", kind="file_equals", path="target.txt", expected_text="yes")]
    for index in range(2):
        asyncio.run(rt._actions(Act(actions=[Action(name="write_file", arguments={"path":"target.txt", "content":str(index)})])))
    asyncio.run(rt._actions(read("target.txt", offset=100)))
    assert rt.state.progress_state.progress_count == 0
    assert not ProgressGate.can_renew(rt.state)


def test_new_verified_condition_is_credit_but_static_authority_is_not(tmp_path):
    (tmp_path / "target.txt").write_text("yes")
    rt = make(tmp_path)
    rt.state.success_criteria = [Criterion(id="value", description="correct", kind="file_equals", path="target.txt", expected_text="yes")]
    asyncio.run(rt._actions(read("target.txt")))
    rt.state.progress_state.checkpoint_progress = rt.state.progress_state.progress_count
    rows = rt.controller.completion_gate.criteria_results(rt.state, rt.history, {})
    ProgressGate.observe_verified(rt.state, rows)
    assert ProgressGate.can_renew(rt.state)
    before = rt.state.progress_state.progress_count
    ProgressGate.observe_verified(rt.state, rows + [{"scope":"contract", "id":"contract:binding", "kind":"binding", "verified":True}])
    assert rt.state.progress_state.progress_count == before


def test_unknown_scope_exploration_is_bounded(tmp_path):
    rt = make(tmp_path)
    for index in range(4):
        (tmp_path / str(index)).write_text(str(index))
        asyncio.run(rt._actions(read(str(index))))
    assert rt.state.progress_state.activity_count == 4
    assert rt.state.progress_state.progress_count == 1


def test_old_activity_snapshot_cannot_renew_or_reset_budget(tmp_path):
    rt = make(tmp_path)
    data = rt.state.model_dump(mode="json")
    data.update(turn_count=10, turn_budget=12, hard_turn_limit=24)
    data["progress_state"] = {"progress_count":9, "checkpoint_progress":0, "observed_hashes":["old"]}
    from kong.contracts import History
    restored = Runtime(rt.model, rt.workspace, rt.registry, RunState.model_validate(data), history=History())
    assert not ProgressGate.can_renew(restored.state)
    assert restored.state.progress_state.activity_count == 9
    assert (restored.state.turn_count, restored.state.turn_budget, restored.state.hard_turn_limit) == (10,12,24)


def test_new_goal_does_not_inherit_credit_or_inspection_scope(tmp_path):
    (tmp_path / "input.txt").write_text("source")
    rt = make(tmp_path, progress_paths=["input.txt"])
    asyncio.run(rt._actions(read("input.txt")))
    assert ProgressGate.can_renew(rt.state)
    rt.submit_input("goal", "a different goal")
    rt._apply_inputs()
    assert rt.state.progress_paths == []
    assert not ProgressGate.can_renew(rt.state)
    assert rt.state.turn_count == 0
