import asyncio

import pytest

from kong.context import ContextBuilder
from kong.contracts import Act, Action, FinalResponse, History, RunState, Status
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore, Snapshot
from kong.tools.base import ToolCall
from kong.tools.defaults import default_registry
from kong.tools.executor import ToolExecutor
from kong.workspace import Workspace


@pytest.mark.parametrize("path", ["../outside.txt", ".env", ".env.local", ".git/config", ".kong/runs/a", "kong.local.toml", "api.txt", "API.TXT", ".ENV", ".KONG/private", "KONG.LOCAL.TOML"])
def test_workspace_rejects_reserved_and_outside_paths(tmp_path, path):
    workspace = Workspace(tmp_path)
    executor = ToolExecutor(default_registry(workspace))
    result = asyncio.run(executor.execute(ToolCall(name="write_file", arguments={"path": path, "content": "no"})))
    assert not result.success


def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host lacks symlink privilege")
    with pytest.raises(ValueError):
        Workspace(root).resolve("link/private.txt")


def test_write_requires_explicit_overwrite_and_has_real_artifact(tmp_path):
    workspace = Workspace(tmp_path)
    executor = ToolExecutor(default_registry(workspace))
    action = ToolCall(name="write_file", arguments={"path": "doc.txt", "content": "first"})
    assert asyncio.run(executor.execute(action)).success
    action.arguments["content"] = "second"
    assert not asyncio.run(executor.execute(action)).success
    assert (tmp_path / "doc.txt").read_text() == "first"
    action.arguments["overwrite"] = True
    assert asyncio.run(executor.execute(action)).success
    assert (tmp_path / "doc.txt").read_text() == "second"


def test_search_ignores_private_config_and_reports_matches(tmp_path):
    (tmp_path / "info.txt").write_text("needle\nsecond", encoding="utf-8")
    (tmp_path / ".env").write_text("needle", encoding="utf-8")
    workspace = Workspace(tmp_path)
    executor = ToolExecutor(default_registry(workspace))
    result = asyncio.run(executor.execute(ToolCall(name="search_files", arguments={"text": "needle"})))
    assert result.success
    assert result.output["matches"] == [{"path": "info.txt", "line": 1, "text": "needle"}]
    assert result.output["truncated"] is False


def test_store_rejects_path_traversal_and_roundtrips(tmp_path):
    store = RunStore(tmp_path)
    with pytest.raises(ValueError):
        store.load("../anything")
    snapshot = Snapshot(workspace=str(tmp_path), state=RunState(goal="goal"))
    store.save(snapshot)
    assert store.load(snapshot.state.run_id) == snapshot
    assert store.list_runs()[0]["goal"] == "goal"


def test_os_lock_prevents_concurrent_run_ownership(tmp_path):
    store = RunStore(tmp_path)
    run_id = RunState(goal="goal").run_id
    with store.lock(run_id):
        with pytest.raises(ValueError, match="already open"):
            with store.lock(run_id):
                pass
    with store.lock(run_id):
        pass


def test_context_is_bounded_view_without_destroying_history():
    state = RunState(goal="original goal")
    history = History()
    for index in range(100):
        history.append("feedback", index, text="x" * 2000)
    before = history.model_dump_json()
    context = ContextBuilder(max_history_chars=5000).build(state, history, [])
    assert len(context.messages[-1]["content"]) < 6000
    assert "original goal" in context.messages[2]["content"]
    assert history.model_dump_json() == before
    assert "history" not in state.model_dump()


def test_model_failure_pauses_without_fake_success(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("goal", ScriptedModel([]), workspace, default_registry(workspace))
    state = asyncio.run(runtime.run())
    assert state.status == Status.WAITING_USER
    assert state.turn_count == 1
    assert state.final_output is None


def test_cancel_persists_and_resume_keeps_budget(tmp_path):
    class SlowModel:
        async def generate(self, context):
            await asyncio.sleep(10)
    workspace = Workspace(tmp_path)
    store = RunStore(tmp_path / ".kong" / "runs")
    runtime = Runtime.new("goal", SlowModel(), workspace, default_registry(workspace), store=store)
    async def scenario():
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    loaded = store.load(runtime.state.run_id)
    assert loaded.state.stop_reason == "user_interrupted"
    runtime.human("resume")
    assert runtime.state.turn_count == 1
    assert runtime.state.hard_turn_limit == 40


def test_restore_refuses_different_workspace(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    workspace = Workspace(other)
    with pytest.raises(ValueError, match="original workspace"):
        Runtime.restore(Snapshot(workspace=str(tmp_path), state=RunState(goal="goal")),
                        ScriptedModel([]), workspace, default_registry(workspace))


def test_crash_after_side_effect_requires_inspection_not_replay(tmp_path):
    from pydantic import BaseModel
    from kong.tools.base import Tool

    started = asyncio.Event()
    class NoArgs(BaseModel):
        pass
    class SlowWrite(Tool):
        name = "slow_write"
        description = "Write then wait before returning the result"
        args_model = NoArgs
        async def run(self, **kwargs):
            (tmp_path / "effect.txt").write_text("written once")
            started.set()
            await asyncio.sleep(10)

    workspace = Workspace(tmp_path)
    registry = default_registry(workspace)
    registry.register(SlowWrite())
    store = RunStore(tmp_path / ".kong" / "runs")
    runtime = Runtime.new("goal", ScriptedModel([Act(actions=[Action(name="slow_write")])]), workspace, registry, store=store)
    async def scenario():
        task = asyncio.create_task(runtime.run())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    loaded = store.load(runtime.state.run_id)
    assert loaded.pending_action_id == "a1"
    assert not loaded.history.observations
    assert (tmp_path / "effect.txt").read_text() == "written once"
    restored = Runtime.restore(loaded, ScriptedModel([
        Act(actions=[Action(name="read_file", arguments={"path": "effect.txt"})]),
        FinalResponse(content="已核验", evidence_ids=["a2"]),
    ]), workspace, registry, store=store)
    restored.resolve_interrupted_action("文件已写入")
    assert asyncio.run(restored.run()).status == Status.COMPLETED
    assert restored.history.observations[0].action_id == "a2"
    assert restored.state.turn_count == 3
