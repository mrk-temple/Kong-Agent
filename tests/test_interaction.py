import asyncio
import json

import pytest

from kong.contracts import Act, Action, FinalResponse, NeedUserInput, Status, Mode, Plan, Criterion, PlanStep
from kong.models.base import ModelError
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.base import ToolResult
from kong.tools.builtin.read_file import ReadFileTool
from kong.tools.builtin.write_file import WriteFileTool
from kong.tools.defaults import default_registry
from kong.tools.permissions import PermissionPolicy
from kong.tools.registry import ToolRegistry
from kong.workspace import Workspace


def action(name, **arguments):
    return Action(name=name, arguments=arguments)


def runtime_for(path, model, **kwargs):
    workspace = Workspace(path)
    return Runtime.new("original goal", model, workspace, default_registry(workspace),
                       store=RunStore(path / ".kong" / "runs"), **kwargs)


def events(runtime):
    return [e.model_dump(mode="json") for e in runtime.run_events]


def test_explicit_builtin_legacy_builder_receives_constraint_without_index_error(tmp_path):
    from kong.context import ContextBuilder
    model = ScriptedModel([NeedUserInput(question="review")])
    runtime = runtime_for(tmp_path, model, context_builder=ContextBuilder())
    runtime.submit_input("constraint", "preserve all original files")
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    assert "preserve all original files" in json.dumps(model.contexts[-1].messages)
    assert not any(e.type == "model_error" for e in runtime.history.events)


def test_input_invalidates_model_decision_and_constraints_survive_restore(tmp_path):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class Model:
            calls = 0
            async def generate(self, context):
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    await release.wait()
                    return Act(actions=[action("write_file", path="old.txt", content="obsolete")])
                assert "must keep original files" in json.dumps(context.messages)
                return NeedUserInput(question="review")
        runtime = runtime_for(tmp_path, Model())
        task = asyncio.create_task(runtime.run())
        await started.wait()
        input_id = runtime.submit_input("constraint", "must keep original files")
        release.set()
        assert (await task).status == Status.WAITING_USER
        assert not (tmp_path / "old.txt").exists() and not runtime.history.observations
        assert any(e["kind"] == "decision_discarded" for e in events(runtime))
        applied = [e for e in events(runtime) if e["kind"] == "input_applied"]
        assert applied[0]["data"]["input_id"] == input_id
        model = ScriptedModel([FinalResponse(content="done")])
        restored = Runtime.restore(runtime.store.load(runtime.state.run_id), model, runtime.workspace,
                                   runtime.registry, store=runtime.store)
        restored.human("answer", text="continue")
        await restored.run()
        assert "must keep original files" in json.dumps(model.contexts[0].messages)
        seq = [e["sequence"] for e in events(restored)]
        assert seq == list(range(1, len(seq) + 1))
        assert events(restored)[-1]["status"] == restored.state.status.value == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("kind,text", [("constraint", "do not write tail"), ("goal", "new goal")])
def test_input_after_first_action_revokes_old_batch(tmp_path, kind, text):
    model = ScriptedModel([Act(actions=[action("write_file", path="first.txt", content="first"),
                                       action("write_file", path="tail.txt", content="tail")]),
                           NeedUserInput(question="new decision")])
    runtime = runtime_for(tmp_path, model)
    def on_event(kind_, data):
        if kind_ == "action_finished" and data["id"] == "a1":
            runtime.submit_input(kind, text)
    runtime.on_event = on_event
    asyncio.run(runtime.run())
    assert (tmp_path / "first.txt").exists() and not (tmp_path / "tail.txt").exists()
    assert len(runtime.history.observations) == 1
    assert text in json.dumps(model.contexts[-1].messages)
    assert runtime.state.turn_count == model.calls == 2


def test_cancel_waiting_model_and_resume_keeps_budget(tmp_path):
    async def scenario():
        started = asyncio.Event()
        class Waiting:
            async def generate(self, context):
                started.set()
                await asyncio.Event().wait()
        runtime = runtime_for(tmp_path, Waiting())
        task = asyncio.create_task(runtime.run())
        await started.wait()
        runtime.submit_input("cancel")
        assert (await asyncio.wait_for(task, 1)).status == Status.STOPPED
        assert runtime.state.turn_count == 1 and not runtime.pending_action_ids
        restored = Runtime.restore(runtime.store.load(runtime.state.run_id), ScriptedModel([FinalResponse(content="ok")]),
                                   runtime.workspace, runtime.registry, store=runtime.store)
        restored.human("resume")
        await restored.run()
        assert restored.state.turn_count == 2
        assert not runtime.history.observations
    asyncio.run(scenario())


def test_cancel_allows_started_write_to_settle_but_prevents_next_write(tmp_path):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class SlowWrite(WriteFileTool):
            async def run(self, **kwargs):
                started.set()
                await release.wait()
                return await super().run(**kwargs)
        workspace = Workspace(tmp_path)
        registry = ToolRegistry()
        registry.register(SlowWrite(workspace))
        model = ScriptedModel([Act(actions=[action("write_file", path="first.txt", content="known"),
                                           action("write_file", path="tail.txt", content="forbidden")])])
        runtime = Runtime.new("write", model, workspace, registry, store=RunStore(tmp_path / ".kong" / "runs"))
        task = asyncio.create_task(runtime.run())
        await started.wait()
        runtime.submit_input("cancel")
        release.set()
        assert (await task).status == Status.STOPPED
        assert (tmp_path / "first.txt").read_text() == "known" and not (tmp_path / "tail.txt").exists()
        assert len(runtime.history.observations) == 1 and not runtime.pending_action_ids
    asyncio.run(scenario())


def test_task_cancellation_preserves_uncertain_write_and_never_replays(tmp_path):
    async def scenario():
        started = asyncio.Event()
        class UnknownWrite(WriteFileTool):
            calls = 0
            async def run(self, **kwargs):
                self.calls += 1
                await super().run(**kwargs)
                started.set()
                await asyncio.Event().wait()
        workspace = Workspace(tmp_path)
        registry = ToolRegistry()
        tool = UnknownWrite(workspace)
        registry.register(tool)
        runtime = Runtime.new("write", ScriptedModel([Act(actions=[action("write_file", path="once.txt", content="once")])]),
                              workspace, registry, store=RunStore(tmp_path / ".kong" / "runs"))
        task = asyncio.create_task(runtime.run())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        snapshot = runtime.store.load(runtime.state.run_id)
        assert snapshot.pending_action_ids == ["a1"] and snapshot.pending_action_id == "a1"
        restored = Runtime.restore(snapshot, ScriptedModel([]), workspace, registry, store=runtime.store)
        assert (await restored.run()).status == Status.WAITING_USER
        assert tool.calls == 1
        restored.resolve_interrupted_action("once.txt already exists; read before any further write")
        assert not restored.pending_action_ids and restored.pending_action_id is None
    asyncio.run(scenario())


def test_read_concurrency_associates_every_result_including_failures(tmp_path):
    async def scenario():
        (tmp_path / "slow.txt").write_text("slow")
        (tmp_path / "fast.txt").write_text("fast")
        class DelayedRead(ReadFileTool):
            active = peak = 0
            async def run(self, **kwargs):
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    await asyncio.sleep(0.06 if kwargs["path"] == "slow.txt" else 0.01)
                    return await super().run(**kwargs)
                finally:
                    self.active -= 1
        workspace = Workspace(tmp_path)
        registry = ToolRegistry()
        tool = DelayedRead(workspace)
        registry.register(tool)
        runtime = Runtime.new("read", ScriptedModel([]), workspace, registry, read_concurrency=3)
        await runtime._actions(Act(actions=[action("read_file", path=p) for p in ["slow.txt", "missing.txt", "fast.txt"]]))
        assert tool.peak == 3 and not runtime.pending_action_ids
        observations = runtime.history.observations
        assert [o.action_id for o in observations] == ["a1", "a2", "a3"]
        assert [o.success for o in observations] == [True, False, True]
        assert observations[0].output == "slow" and observations[2].output == "fast"
    asyncio.run(scenario())


def test_parallel_cancel_drains_all_started_reads_without_starting_tail(tmp_path):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class SlowRead(ReadFileTool):
            count = 0
            async def run(self, **kwargs):
                self.count += 1
                if self.count == 2:
                    started.set()
                await release.wait()
                return ToolResult(success=True, output=kwargs["path"])
        workspace = Workspace(tmp_path)
        registry = ToolRegistry()
        registry.register(SlowRead(workspace))
        model = ScriptedModel([Act(actions=[action("read_file", path=p) for p in ["1", "2", "3"]])])
        runtime = Runtime.new("read", model, workspace, registry, read_concurrency=2)
        task = asyncio.create_task(runtime.run())
        await started.wait()
        runtime.submit_input("cancel")
        release.set()
        assert (await task).status == Status.STOPPED
        assert [o.output for o in runtime.history.observations] == ["1", "2"]
        assert not runtime.pending_action_ids
    asyncio.run(scenario())


def test_restore_multiple_pending_ids_requires_individual_resolution(tmp_path):
    runtime = runtime_for(tmp_path, ScriptedModel([]))
    runtime.pending_action_ids = ["a1", "a2", "a3"]
    runtime.pending_action_id = "a1"
    runtime.save()
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id), ScriptedModel([]), runtime.workspace, runtime.registry)
    for remaining in [2, 1]:
        restored.resolve_interrupted_action("checked read")
        assert len(restored.pending_action_ids) == remaining and restored.state.status == Status.WAITING_USER
        with pytest.raises(ValueError):
            restored.human("answer", text="continue")
    restored.resolve_interrupted_action("checked last read")
    assert restored.state.status == Status.RUNNING and not restored.pending_action_ids


def test_queued_inputs_recovered_before_model_request(tmp_path):
    runtime = runtime_for(tmp_path, ScriptedModel([]))
    runtime.submit_input("constraint", "preserve this after crash")
    snapshot = runtime.store.load(runtime.state.run_id)
    model = ScriptedModel([FinalResponse(content="ok")])
    restored = Runtime.restore(snapshot, model, runtime.workspace, runtime.registry, store=runtime.store)
    asyncio.run(restored.run())
    assert "preserve this after crash" in json.dumps(model.contexts[0].messages)
    assert not restored.queued_inputs


def test_input_acknowledgement_already_has_durable_cancel_and_whole_batch(tmp_path):
    runtime = runtime_for(tmp_path, ScriptedModel([]))
    runtime.submit_input("constraint", "do not lose this")
    runtime.submit_input("cancel")
    acknowledged = []
    def inspect_saved(kind, data):
        if kind == "input_applied":
            snapshot = runtime.store.load(runtime.state.run_id)
            assert snapshot.state.status == Status.STOPPED and not snapshot.queued_inputs
            assert snapshot.constraints == ["do not lose this"]
            acknowledged.append(data["kind"])
    runtime.on_event = inspect_saved
    runtime._apply_inputs()
    assert acknowledged == ["constraint", "cancel"]


def test_cancel_active_host_process_keeps_unknown_intent_and_prevents_tail(tmp_path):
    async def scenario():
        workspace = Workspace(tmp_path)
        registry = default_registry(workspace, allow_process=True)
        script = "from pathlib import Path; import time; Path('started.txt').write_text('started'); time.sleep(30)"
        runtime = Runtime.new("process", ScriptedModel([Act(actions=[action("process_exec", argv=["python", "-c", script]),
            action("write_file", path="tail.txt", content="no")])]), workspace, registry,
            store=RunStore(tmp_path / ".kong" / "runs"))
        task = asyncio.create_task(runtime.run())
        for _ in range(300):
            if (tmp_path / "started.txt").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "started.txt").exists()
        runtime.submit_input("cancel")
        assert (await asyncio.wait_for(task, 3)).status == Status.STOPPED
        assert runtime.pending_action_ids == ["a1"] and not (tmp_path / "tail.txt").exists()
        snapshot = runtime.store.load(runtime.state.run_id)
        restored = Runtime.restore(snapshot, ScriptedModel([]), workspace, registry, store=runtime.store)
        assert (await restored.run()).status == Status.WAITING_USER
        assert runtime.metrics.model_calls == 1
    asyncio.run(scenario())


def test_changed_approved_plan_requires_fresh_proposal(tmp_path):
    runtime = runtime_for(tmp_path, ScriptedModel([]), mode=Mode.PLAN)
    plan = Plan(goal="goal", rationale="review", steps=[PlanStep(id="s", title="write")],
                success_criteria=[Criterion(id="c", description="file", kind="file_exists", path="first.txt")])
    runtime.controller.propose(runtime.state, plan)
    runtime.human("approve", revision=1)
    runtime.submit_input("constraint", "write a different filename")
    runtime._apply_inputs()
    assert runtime.state.requires_replan and runtime.state.plan is None
    assert runtime.controller.authorize(runtime.state, FinalResponse(content="done"))
    assert runtime.controller.authorize(runtime.state, Act(actions=[action("write_file", path="first.txt", content="old")]))
    with pytest.raises(ValueError):
        runtime.human("approve", revision=1)


@pytest.mark.parametrize("category,retryable,expected_calls", [("rate_limit", True, 3), ("timeout", True, 3),
    ("connection", True, 3), ("service_http", True, 3), ("configuration", False, 1),
    ("billing", False, 1), ("authentication", False, 1)])
def test_model_retries_have_visible_global_budget(tmp_path, category, retryable, expected_calls):
    class Broken:
        calls = 0
        async def generate(self, context):
            self.calls += 1
            raise ModelError("safe", category=category, retryable=retryable)
    runtime = runtime_for(tmp_path, Broken())
    asyncio.run(runtime.run())
    assert runtime.model.calls == runtime.state.turn_count == expected_calls
    assert len([e for e in events(runtime) if e["kind"] == "model_retry"]) == expected_calls - 1
    runtime.human("answer", text="retry once")
    asyncio.run(runtime.run())
    assert runtime.model.calls == expected_calls + 1  # Restarting drive cannot reset retry allowance.


def test_unknown_tool_timeout_pauses_and_is_never_retried(tmp_path):
    async def scenario():
        class WriteThenTimeout(WriteFileTool):
            calls = 0
            execution_timeout = 0.03
            async def run(self, **kwargs):
                self.calls += 1
                await super().run(**kwargs)
                await asyncio.Event().wait()
        workspace = Workspace(tmp_path)
        registry = ToolRegistry()
        tool = WriteThenTimeout(workspace)
        registry.register(tool)
        runtime = Runtime.new("write", ScriptedModel([Act(actions=[action("write_file", path="once.txt", content="once"),
            action("write_file", path="tail.txt", content="tail")])]), workspace, registry)
        assert (await runtime.run()).status == Status.WAITING_USER
        assert tool.calls == 1 and runtime.pending_action_ids == ["a1"]
        assert runtime.history.observations[0].outcome_unknown
        assert (tmp_path / "once.txt").exists() and not (tmp_path / "tail.txt").exists()
        with pytest.raises(ValueError):
            runtime.human("answer", text="retry")
    asyncio.run(scenario())


def test_restore_rechecks_host_permissions_and_audits_changes(tmp_path):
    (tmp_path / "source.txt").write_text("read")
    runtime = runtime_for(tmp_path, ScriptedModel([NeedUserInput(question="pause")]))
    asyncio.run(runtime.run())
    model = ScriptedModel([Act(actions=[action("write_file", path="blocked.txt", content="blocked")]),
                           NeedUserInput(question="denied")])
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id), model, runtime.workspace, runtime.registry,
                               store=runtime.store, permissions=PermissionPolicy(write_roots=[]))
    restored.human("answer", text="continue")
    asyncio.run(restored.run())
    assert not (tmp_path / "blocked.txt").exists()
    assert restored.history.observations[-1].success is False
    policies = [e["data"]["fingerprint"] for e in events(restored) if e["kind"] == "permissions"]
    assert len(set(policies)) == 2


def test_event_consumer_failure_cannot_replay_or_lose_a_write(tmp_path):
    runtime = runtime_for(tmp_path, ScriptedModel([Act(actions=[action("write_file", path="once.txt", content="ok")]),
        FinalResponse(content="done", evidence_ids=["a1"])]))
    def broken(kind, data):
        raise RuntimeError("renderer failure")
    runtime.on_event = broken
    asyncio.run(runtime.run())
    assert runtime.state.status == Status.COMPLETED and len(runtime.history.observations) == 1
    assert (tmp_path / "once.txt").read_text() == "ok"
