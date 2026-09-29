import asyncio

import pytest

from kong.contracts import (
    Act, Action, CompletePlanStep, Criterion, FinalResponse, Mode, NeedDiscovery,
    NeedUserInput, Plan, PlanProposal, PlanStep, ProgressAssessment, Status,
)
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def plan():
    return Plan(goal="original", rationale="结构性选择", steps=[PlanStep(id="s1", title="整理资料")],
                success_criteria=[Criterion(id="c1", description="实际读取资料")])


def make_runtime(tmp_path, decisions, mode=Mode.AUTO, store=False):
    workspace = Workspace(tmp_path)
    model = ScriptedModel(decisions)
    runtime = Runtime.new("完成目标", model, workspace, default_registry(workspace), mode,
                          store=RunStore(tmp_path / ".kong" / "runs") if store else None)
    return runtime, model


def run(runtime):
    return asyncio.run(runtime.run())


def read(name):
    return Act(actions=[Action(name="read_file", arguments={"path": name})])


def test_chat_one_call_no_router(tmp_path):
    runtime, model = make_runtime(tmp_path, [FinalResponse(content="你好")])
    assert run(runtime).status == Status.COMPLETED
    assert model.calls == 1
    assert runtime.state.mode == Mode.AUTO
    assert runtime.state.plan is None


def test_many_actions_renew_lease_without_plan(tmp_path):
    decisions = []
    for index in range(12):
        (tmp_path / f"{index}.txt").write_text(f"distinct {index}", encoding="utf-8")
        decisions.append(read(f"{index}.txt"))
        if (index + 1) % 4 == 0:
            decisions.append(ProgressAssessment(diagnosis="新信息且路径明确", next_strategy="continue"))
    decisions.append(FinalResponse(content="读取完毕", evidence_ids=["a12"]))
    runtime, model = make_runtime(tmp_path, decisions)
    assert run(runtime).status == Status.COMPLETED
    assert runtime.state.plan is None
    assert runtime.state.mode == Mode.AUTO
    assert len(runtime.history.observations) == 12
    assert model.calls == 16


def test_inspect_100_identical_files_without_a_plan(tmp_path):
    actions = []
    for index in range(100):
        name = f"file-{index}.txt"
        (tmp_path / name).write_text("same contents", encoding="utf-8")
        actions.append(Action(name="read_file", arguments={"path": name}))
    decisions = []
    for batch_index, offset in enumerate(range(0, 100, 8), 1):
        decisions.append(Act(actions=actions[offset:offset + 8]))
        if batch_index % 4 == 0:
            decisions.append(ProgressAssessment(diagnosis="逐个检查新文件，路径明确", next_strategy="continue"))
    decisions.append(FinalResponse(content="检查完100份文件", evidence_ids=[f"a{i}" for i in range(1, 101)]))
    runtime, model = make_runtime(tmp_path, decisions)
    assert run(runtime).status == Status.COMPLETED
    assert len(runtime.history.observations) == 100
    assert model.calls == 17
    assert runtime.state.plan is None


def test_fast_hard_limit_cannot_renew(tmp_path):
    decisions = []
    for index in range(6):
        (tmp_path / str(index)).write_text(str(index))
        decisions.append(read(str(index)))
    runtime, model = make_runtime(tmp_path, decisions, Mode.FAST)
    state = run(runtime)
    assert state.status == Status.STOPPED
    assert state.stop_reason == "fast_limit_suggest_auto"
    assert model.calls == 4
    with pytest.raises(ValueError):
        runtime.human("resume")


def test_fast_rejects_plan_without_changing_mode(tmp_path):
    runtime, _ = make_runtime(tmp_path, [PlanProposal(plan=plan()), NeedUserInput(question="是否另开 AUTO？")], Mode.FAST)
    assert run(runtime).status == Status.WAITING_USER
    assert runtime.state.plan is None
    assert runtime.state.mode == Mode.FAST


def test_plan_mode_blocks_actions_and_early_finish(tmp_path):
    runtime, _ = make_runtime(tmp_path, [read("missing"), FinalResponse(content="完成"), PlanProposal(plan=plan())], Mode.PLAN)
    assert run(runtime).status == Status.WAITING_USER
    assert not runtime.history.observations
    assert runtime.state.pending_approval.revision == 1


def test_plan_is_paused_without_additional_model_calls(tmp_path):
    runtime, model = make_runtime(tmp_path, [PlanProposal(plan=plan()), FinalResponse(content="不应执行")])
    run(runtime)
    run(runtime)
    assert model.calls == 1
    assert runtime.state.mode == Mode.AUTO
    with pytest.raises(ValueError):
        runtime.human("approve", revision=2)


def test_discussion_cannot_execute_tools_then_revision_requires_approval(tmp_path):
    runtime, _ = make_runtime(tmp_path, [PlanProposal(plan=plan()), read("missing"), PlanProposal(plan=plan())])
    run(runtime)
    runtime.human("discuss", text="换一个方向")
    run(runtime)
    assert not runtime.history.observations
    assert runtime.state.plan_revision == 2
    assert runtime.state.plan.approved_revision is None
    with pytest.raises(ValueError):
        runtime.human("approve", revision=1)
    runtime.human("approve", revision=2)
    assert runtime.state.plan.approved_revision == 2


def test_edit_clears_model_forged_approval_and_completion(tmp_path):
    forged = plan()
    forged.steps[0].done = True
    forged.approved_revision = 99
    runtime, _ = make_runtime(tmp_path, [PlanProposal(plan=forged)])
    run(runtime)
    assert not runtime.state.plan.steps[0].done
    runtime.human("edit", plan=forged)
    assert runtime.state.plan_revision == 2
    assert runtime.state.plan.approved_revision is None
    assert not runtime.state.plan.steps[0].done


def test_approved_plan_completes_only_with_steps_and_evidence(tmp_path):
    (tmp_path / "info.txt").write_text("evidence")
    runtime, _ = make_runtime(tmp_path, [
        PlanProposal(plan=plan()), read("info.txt"),
        FinalResponse(content="过早完成", evidence_ids=["a1"], criteria_evidence={"c1": ["a1"]}),
        CompletePlanStep(step_id="s1", evidence_ids=["invented"]),
        CompletePlanStep(step_id="s1", evidence_ids=["a1"]),
        FinalResponse(content="已完成", evidence_ids=["a1"], criteria_evidence={"c1": ["a1"]}),
    ], Mode.PLAN)
    run(runtime)
    runtime.human("approve", revision=1)
    assert run(runtime).status == Status.COMPLETED
    assert runtime.state.final_output == "已完成"
    assert runtime.state.plan.steps[0].evidence_ids == ["a1"]


def test_major_replan_pauses_approved_execution(tmp_path):
    runtime, model = make_runtime(tmp_path, [PlanProposal(plan=plan()), PlanProposal(plan=plan()), read("missing")])
    run(runtime)
    runtime.human("approve", revision=1)
    run(runtime)
    assert runtime.state.pending_approval.revision == 2
    assert runtime.state.status == Status.WAITING_USER
    assert model.calls == 2


def test_failed_tool_is_not_completion(tmp_path):
    runtime, _ = make_runtime(tmp_path, [read("missing"), FinalResponse(content="完成", evidence_ids=["a1"]), NeedUserInput(question="请提供文件")])
    assert run(runtime).status == Status.WAITING_USER
    assert runtime.state.final_output is None


def test_real_recovery_can_complete(tmp_path):
    (tmp_path / "good").write_text("data")
    runtime, _ = make_runtime(tmp_path, [read("bad"), read("good"), FinalResponse(content="已找到资料", evidence_ids=["a2"])])
    assert run(runtime).status == Status.COMPLETED


def test_repetition_cannot_earn_unlimited_autonomy(tmp_path):
    (tmp_path / "same").write_text("same")
    runtime, model = make_runtime(tmp_path, [read("same"), read("same"), read("same"),
        ProgressAssessment(diagnosis="重复读取", next_strategy="discover"),
        read("same"), read("same"),
        ProgressAssessment(diagnosis="仍无新信息", next_strategy="discover"),
        FinalResponse(content="不应到达")])
    assert run(runtime).status == Status.WAITING_USER
    assert model.calls == 7
    assert runtime.state.progress_state.recovery_attempts == 1


def test_consecutive_failures_trigger_assessment(tmp_path):
    runtime, _ = make_runtime(tmp_path, [read("a"), read("b"),
        ProgressAssessment(diagnosis="连续失败且无进展", next_strategy="continue")])
    assert run(runtime).status == Status.WAITING_USER
    assert runtime.state.progress_state.progress_count == 0


def test_hard_limit_never_resets_on_approval_or_answer(tmp_path):
    runtime, _ = make_runtime(tmp_path, [PlanProposal(plan=plan())])
    runtime.state.hard_turn_limit = 1
    run(runtime)
    runtime.human("approve", revision=1)
    assert run(runtime).status == Status.STOPPED
    assert runtime.state.turn_count == 1


def test_discovery_cannot_write_even_in_batch(tmp_path):
    runtime, _ = make_runtime(tmp_path, [NeedDiscovery(reason="探索", actions=[
        Action(name="list_dir"), Action(name="write_file", arguments={"path": "bad", "content": "no"})]),
        FinalResponse(content="不应算完成"), NeedUserInput(question="需要确认写入目标")])
    assert run(runtime).status == Status.WAITING_USER
    assert not (tmp_path / "bad").exists()
    assert not runtime.history.observations


def test_multiple_actions_are_one_turn(tmp_path):
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    runtime, _ = make_runtime(tmp_path, [Act(actions=[Action(name="read_file", arguments={"path": n}) for n in ["a", "b"]]),
                                       FinalResponse(content="done", evidence_ids=["a1", "a2"])])
    assert run(runtime).turn_count == 2
    assert len(runtime.history.observations) == 2


def test_batch_stops_after_first_failed_action(tmp_path):
    runtime, _ = make_runtime(tmp_path, [Act(actions=[Action(name="read_file", arguments={"path": "missing"}),
        Action(name="write_file", arguments={"path": "should-not-exist", "content": "no"})]), NeedUserInput(question="路径？")])
    run(runtime)
    assert len(runtime.history.observations) == 1
    assert not (tmp_path / "should-not-exist").exists()


def test_deterministic_criterion_beats_model_claim(tmp_path):
    runtime, _ = make_runtime(tmp_path, [FinalResponse(content="我保证完成"), NeedUserInput(question="文件在哪")])
    runtime.state.success_criteria = [Criterion(id="file", description="产物存在", kind="file_exists", path="missing")]
    assert run(runtime).status == Status.WAITING_USER
    assert runtime.state.final_output is None


def test_persist_approval_and_resume_exact_state(tmp_path):
    runtime, _ = make_runtime(tmp_path, [PlanProposal(plan=plan())], store=True)
    run(runtime)
    loaded = runtime.store.load(runtime.state.run_id)
    restored = Runtime.restore(loaded, ScriptedModel([NeedUserInput(question="补充资料")]),
                               runtime.workspace, runtime.registry, store=runtime.store)
    assert restored.state.turn_count == 1
    assert restored.state.pending_approval.revision == 1
    restored.human("approve", revision=1)
    run(restored)
    assert restored.state.turn_count == 2
    assert restored.state.plan.approved_revision == 1


def test_unknown_inflight_action_never_replayed(tmp_path):
    runtime, _ = make_runtime(tmp_path, [], store=True)
    runtime.pending_action_id = "a1"
    runtime.state.requires_evidence = True
    runtime.save()
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id), ScriptedModel([]), runtime.workspace, runtime.registry)
    assert restored.state.status == Status.WAITING_USER
    with pytest.raises(ValueError):
        restored.human("answer", text="继续")
    restored.resolve_interrupted_action("我检查了，文件没有写入")
    assert restored.pending_action_id is None
    assert restored.state.status == Status.RUNNING


def test_reject_is_terminal(tmp_path):
    runtime, model = make_runtime(tmp_path, [PlanProposal(plan=plan()), read("missing")])
    run(runtime)
    runtime.human("reject")
    run(runtime)
    assert runtime.state.stop_reason == "user_rejected"
    assert model.calls == 1
