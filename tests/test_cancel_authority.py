"""I-02/03/04: cancellation is durable control, independent of input capacity."""
import asyncio
import json
import sys

import httpx
import pytest

from kong.config import ModelConfig
from kong.contracts import Act, Action, FinalResponse, Status
from kong.models.base import ModelError
from kong.models.compatible import CompatibleModel
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.base import ToolResult
from kong.tools.builtin.read_file import ReadFileTool
from kong.tools.defaults import default_registry
from kong.tools.registry import ToolRegistry
from kong.workspace import Workspace


def make_runtime(path, model, registry=None, **kwargs):
    workspace = Workspace(path)
    return Runtime.new("original goal", model, workspace, registry or default_registry(workspace),
                       store=RunStore(path / ".kong" / "runs"), **kwargs)


def fill_queue(runtime):
    ids = [runtime.submit_input("constraint", f"preserve constraint {i}") for i in range(32)]
    with pytest.raises(ValueError):
        runtime.submit_input("constraint", "overflow")
    return ids


def test_full_queue_cancel_is_durable_before_ack_and_idempotent(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedModel([]))
    ids = fill_queue(runtime)
    cancel_id = runtime.submit_input("cancel")
    assert runtime.submit_input("cancel") == cancel_id
    snapshot = runtime.store.load(runtime.state.run_id)
    assert [item.id for item in snapshot.queued_inputs] == ids
    acknowledged = []
    def check_ack(kind, data):
        if kind == "input_applied":
            saved = runtime.store.load(runtime.state.run_id)
            assert saved.state.status == Status.STOPPED
            assert not saved.queued_inputs and len(saved.constraints) == 32
            acknowledged.append(data["input_id"])
    runtime.on_event = check_ack
    runtime._apply_inputs()
    assert acknowledged == ids + [cancel_id]
    assert runtime.submit_input("cancel") == cancel_id
    assert runtime.state.turn_count == 0


def test_full_queue_cancel_interrupts_model_wait_without_executing_decision(tmp_path):
    async def scenario():
        started = asyncio.Event()
        class WaitingModel:
            async def generate(self, context):
                started.set()
                await asyncio.Event().wait()
        runtime = make_runtime(tmp_path, WaitingModel())
        task = asyncio.create_task(runtime.run())
        await started.wait()
        fill_queue(runtime)
        runtime.submit_input("cancel")
        assert (await asyncio.wait_for(task, 2)).status == Status.STOPPED
        assert runtime.state.turn_count == runtime.metrics.model_calls == 1
        assert not runtime.history.observations and not runtime.pending_action_ids
    asyncio.run(scenario())


def test_full_queue_cancel_closes_partial_provider_stream(tmp_path):
    async def scenario():
        partial, closed = asyncio.Event(), asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                payload = {"choices": [{"index": 0, "delta": {"content":
                    '{"kind":"act","actions":[{"name":"write_file","arguments":{"path":"forbidden.txt","content":"x"}}]}'},
                    "finish_reason": None}]}
                yield ("data: " + json.dumps(payload) + "\n\n").encode()
                partial.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream()))) as client:
            model = CompatibleModel(ModelConfig(base_url="http://localhost/v1", model="fixture", stream=True), client)
            runtime = make_runtime(tmp_path, model)
            task = asyncio.create_task(runtime.run())
            await partial.wait()
            fill_queue(runtime)
            runtime.submit_input("cancel")
            assert (await asyncio.wait_for(task, 2)).status == Status.STOPPED
            assert closed.is_set() and not (tmp_path / "forbidden.txt").exists()
            assert not runtime.history.observations
    asyncio.run(scenario())


def test_full_queue_cancel_wakes_retry_wait_without_new_request(tmp_path):
    async def scenario():
        sleeping = asyncio.Event()
        class RetryModel:
            calls = 0
            async def generate(self, context):
                self.calls += 1
                raise ModelError("safe", category="rate_limit", retryable=True, retry_after=5)
        runtime = make_runtime(tmp_path, RetryModel())
        def event(kind, data):
            if kind == "model_retry":
                sleeping.set()
        runtime.on_event = event
        task = asyncio.create_task(runtime.run())
        await sleeping.wait()
        fill_queue(runtime)
        runtime.submit_input("cancel")
        assert (await asyncio.wait_for(task, 2)).status == Status.STOPPED
        assert runtime.model.calls == runtime.state.turn_count == 1
        assert len([e for e in runtime.run_events if e.kind == "model_retry"]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("concurrency,count", [(1, 1), (2, 2)])
def test_full_queue_cancel_drains_started_reads_and_revokes_tail(tmp_path, concurrency, count):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class Read(ReadFileTool):
            calls = 0
            async def run(self, **kwargs):
                self.calls += 1
                if self.calls == count:
                    started.set()
                await release.wait()
                return ToolResult(success=True, output=kwargs["path"])
        workspace = Workspace(tmp_path)
        registry = ToolRegistry()
        tool = Read(workspace)
        registry.register(tool)
        model = ScriptedModel([Act(actions=[Action(name="read_file", arguments={"path": str(i)}) for i in range(4)])])
        runtime = make_runtime(tmp_path, model, registry, read_concurrency=concurrency)
        task = asyncio.create_task(runtime.run())
        await started.wait()
        fill_queue(runtime)
        runtime.submit_input("cancel")
        release.set()
        assert (await task).status == Status.STOPPED
        assert tool.calls == count
        assert [o.action_id for o in runtime.history.observations] == [f"a{i+1}" for i in range(count)]
        assert all(o.success for o in runtime.history.observations) and not runtime.pending_action_ids
    asyncio.run(scenario())


def test_full_queue_cancel_process_keeps_unknown_effect_and_explicit_resume(tmp_path):
    async def scenario():
        workspace = Workspace(tmp_path)
        registry = default_registry(workspace, allow_process=True)
        script = "from pathlib import Path; import time; Path('started.txt').write_text('once'); time.sleep(30)"
        model = ScriptedModel([Act(actions=[Action(name="process_exec", arguments={"argv": [sys.executable, "-c", script]}),
            Action(name="write_file", arguments={"path": "tail.txt", "content": "no"})])])
        runtime = make_runtime(tmp_path, model, registry)
        task = asyncio.create_task(runtime.run())
        for _ in range(300):
            if (tmp_path / "started.txt").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "started.txt").exists()
        fill_queue(runtime)
        runtime.submit_input("cancel")
        assert (await asyncio.wait_for(task, 3)).status == Status.STOPPED
        assert runtime.pending_action_ids == ["a1"] and not (tmp_path / "tail.txt").exists()
        restored = Runtime.restore(runtime.store.load(runtime.state.run_id), ScriptedModel([]), workspace, registry,
                                   store=runtime.store)
        assert (await restored.run()).status == Status.WAITING_USER
        assert restored.state.turn_count == 1 and restored.metrics.model_calls == 1
        restored.resolve_interrupted_action("inspected started.txt; effect is known; do not replay")
        assert restored.state.status == Status.STOPPED
        assert not restored.pending_action_ids
        assert (await restored.run()).status == Status.STOPPED
        assert restored.metrics.model_calls == 1
        restored.human("resume")
        assert restored.state.status == Status.RUNNING and restored.state.turn_count == 1
    asyncio.run(scenario())


def test_cancel_durable_before_driver_start_survives_restore_then_explicit_resume(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedModel([]))
    fill_queue(runtime)
    runtime.submit_input("cancel")
    model = ScriptedModel([FinalResponse(content="resumed")])
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id), model, runtime.workspace, runtime.registry,
                               store=runtime.store)
    assert asyncio.run(restored.run()).status == Status.STOPPED
    assert model.calls == restored.state.turn_count == 0
    resumed = Runtime.restore(restored.store.load(restored.state.run_id), model, runtime.workspace, runtime.registry,
                              store=runtime.store)
    assert asyncio.run(resumed.run()).status == Status.STOPPED
    resumed.human("resume")
    assert asyncio.run(resumed.run()).status == Status.COMPLETED
    assert resumed.state.turn_count == model.calls == 1
    assert len(resumed.constraints) == 32


def test_cancel_from_action_callback_does_not_start_allocated_but_unstarted_process(tmp_path):
    workspace = Workspace(tmp_path)
    registry = default_registry(workspace, allow_process=True)
    script = "from pathlib import Path; Path('forbidden.txt').write_text('no')"
    runtime = make_runtime(tmp_path, ScriptedModel([Act(actions=[Action(name="process_exec",
        arguments={"argv": [sys.executable, "-c", script]})])]), registry)
    def event(kind, data):
        if kind == "action_started":
            fill_queue(runtime)
            runtime.submit_input("cancel")
    runtime.on_event = event
    assert asyncio.run(runtime.run()).status == Status.STOPPED
    assert not (tmp_path / "forbidden.txt").exists()
    assert runtime.metrics.tool_calls == 0 and not runtime.pending_action_ids


def test_legacy_queued_cancel_snapshot_still_stops_without_model_call(tmp_path):
    from kong.runtime.interaction import RunInput
    runtime = make_runtime(tmp_path, ScriptedModel([]))
    snapshot = runtime.snapshot()
    snapshot.schema_version = 3
    snapshot.queued_inputs = [RunInput(kind="constraint", text="legacy"), RunInput(kind="cancel")]
    runtime.store.save(snapshot)
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id), ScriptedModel([]), runtime.workspace,
                               runtime.registry, store=runtime.store)
    assert asyncio.run(restored.run()).status == Status.STOPPED
    assert restored.metrics.model_calls == 0 and restored.constraints == ["legacy"]
