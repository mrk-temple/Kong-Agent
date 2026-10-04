import asyncio
import os

import pytest

from kong.contracts import Act, Action, Criterion, FinalResponse, History, Observation
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def runtime_for(tmp_path):
    (tmp_path / "artifact.txt").write_text("good", encoding="utf-8")
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("verify", ScriptedModel([]), workspace,
        default_registry(workspace, allow_process=True, allow_terminal=os.name == "nt"),
        store=RunStore(tmp_path / ".kong/runs"), completion_scope="mechanical")
    runtime.state.success_criteria = [Criterion(id="check", description="check artifact", kind="tool_success",
        tool_name="process_exec", after_last_write=True, artifact_paths=["artifact.txt"])]
    return runtime


def process(code):
    return Action(name="process_exec", arguments={"argv": ["python", "-c", code]})


CHECK = "from pathlib import Path; assert Path('artifact.txt').read_text() == 'good'"
WRITE_BAD = "from pathlib import Path; Path('artifact.txt').write_text('bad')"
WRITE_GOOD = "from pathlib import Path; Path('artifact.txt').write_text('good')"


def problems(runtime):
    return runtime.controller.completion_gate.check(runtime.state,
        FinalResponse(content="done", evidence_ids=[o.action_id for o in runtime.history.observations if o.success]), runtime.history)


@pytest.mark.parametrize("writer", [
    Action(name="write_file", arguments={"path": "artifact.txt", "content": "bad", "overwrite": True}),
    Action(name="patch_file", arguments={"path": "artifact.txt", "old_text": "good", "new_text": "bad"}),
    process(WRITE_BAD),
])
def test_real_write_paths_invalidate_old_check_and_require_recheck(tmp_path, writer):
    async def scenario():
        runtime = runtime_for(tmp_path)
        await runtime._actions(Act(actions=[process(CHECK)]))
        assert not problems(runtime)
        await runtime._actions(Act(actions=[writer]))
        assert (tmp_path / "artifact.txt").read_text() == "bad"
        assert problems(runtime)
        await runtime._actions(Act(actions=[process(WRITE_GOOD)]))
        assert problems(runtime)  # Restoring bytes through a mutating command is not a check.
        await runtime._actions(Act(actions=[process(CHECK)]))
        assert not problems(runtime)
        snapshot = runtime.store.load(runtime.state.run_id)
        assert snapshot.schema_version == 4 and snapshot.history.observations[-1].artifact_versions
        restored = Runtime.restore(snapshot, ScriptedModel([]), runtime.workspace, runtime.registry, store=runtime.store)
        assert not problems(restored)
        (tmp_path / "artifact.txt").write_text("changed while offline")
        assert problems(restored)
        await runtime.registry.aclose()
    asyncio.run(scenario())


def test_same_process_check_then_modify_cannot_certify_new_artifact(tmp_path):
    async def scenario():
        runtime = runtime_for(tmp_path)
        await runtime._actions(Act(actions=[process(CHECK + "; " + WRITE_BAD)]))
        assert runtime.history.observations[-1].success and problems(runtime)
        await runtime.registry.aclose()
    asyncio.run(scenario())


def test_unknown_effect_and_unfinished_intent_require_reverification(tmp_path):
    async def scenario():
        runtime = runtime_for(tmp_path)
        await runtime._actions(Act(actions=[process(CHECK)]))
        assert not problems(runtime)
        # Failed unknown tools may have side effects; success is not required for invalidation.
        await runtime._actions(Act(actions=[Action(name="unknown_remote_write")]))
        assert problems(runtime)
        await runtime._actions(Act(actions=[process(CHECK)]))
        assert not problems(runtime)
        runtime.history.append("action_intent", 1, action_id="interrupted", action={"name": "terminal"})
        assert problems(runtime)
        assert not runtime.controller.completion_gate.criteria_results(runtime.state, runtime.history, {})[0]["verified"]
        await runtime.registry.aclose()
    asyncio.run(scenario())


def test_external_change_during_real_read_cannot_bind_old_output_to_new_version(tmp_path):
    async def scenario():
        runtime = runtime_for(tmp_path)
        runtime.state.success_criteria = [Criterion(id="read", description="Read current artifact", kind="tool_success",
            tool_name="read_file", artifact_paths=["artifact.txt"])]
        tool = runtime.registry.get("read_file")
        original = tool.run
        read_finished, changed = asyncio.Event(), asyncio.Event()
        async def delayed_read(**arguments):
            result = await original(**arguments)
            read_finished.set()
            await changed.wait()
            return result
        async def external_writer():
            await read_finished.wait()
            (tmp_path / "artifact.txt").write_text("bad")
            changed.set()
        tool.run = delayed_read
        writer = asyncio.create_task(external_writer())
        try:
            await runtime._actions(Act(actions=[Action(name="read_file", arguments={"path":"artifact.txt"})]))
            await writer
            assert runtime.history.observations[-1].success
            assert "good" in str(runtime.history.observations[-1].output)
            assert problems(runtime)
            tool.run = original
            await runtime._actions(Act(actions=[Action(name="read_file", arguments={"path":"artifact.txt"})]))
            assert not problems(runtime)
        finally:
            await runtime.registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["tool_success", "tool_result", "evidence"])
def test_default_current_criteria_cannot_bypass_version_binding(tmp_path, kind):
    async def scenario():
        runtime = runtime_for(tmp_path)
        extra = {"tool_name": "process_exec"} if kind != "evidence" else {}
        if kind == "tool_result":
            extra["output_match"] = {"exit_code": 0}
        runtime.state.success_criteria = [Criterion(id="check", description="current artifact", kind=kind, **extra)]
        await runtime._actions(Act(actions=[process(CHECK)]))
        decision = FinalResponse(content="done", evidence_ids=["a1"], criteria_evidence={"check": ["a1"]})
        gate = runtime.controller.completion_gate
        assert not gate.check(runtime.state, decision, runtime.history)
        await runtime._actions(Act(actions=[Action(name="write_file", arguments={"path": "artifact.txt", "content": "bad", "overwrite": True})]))
        assert gate.check(runtime.state, decision, runtime.history)
        assert not gate.criteria_results(runtime.state, runtime.history, {"check": ["a1"]})[0]["verified"]
        await runtime.registry.aclose()
    asyncio.run(scenario())


def test_external_current_readback_is_invalidated_by_later_write(tmp_path):
    from kong.contracts import RunState
    from kong.runtime.gates import CompletionGate
    snapshot = Observation(action_id="a1", turn=1, action=Action(name="browser", arguments={"operation": "snapshot"}),
                           success=True, output={"text": "Saved: Kong"}, fingerprint="a", output_hash="a", effect="read")
    click = snapshot.model_copy(update={"action_id": "a2", "effect": "unknown",
        "action": Action(name="browser", arguments={"operation": "click"})})
    criterion = Criterion(id="dom", description="current saved value", kind="tool_result", tool_name="browser",
                          operation="snapshot", output_contains={"/text": "Saved: Kong"})
    history = History(observations=[snapshot, click])
    gate = CompletionGate(Workspace(tmp_path))
    assert gate.check(RunState(goal="current remote state", success_criteria=[criterion]),
                      FinalResponse(content="done", evidence_ids=["a1"]), history)
    with pytest.raises(ValueError, match="Historical event"):
        Criterion(id="invalid", description="cannot mix", kind="tool_success", tool_name="process_exec",
                  evidence_scope="event", artifact_paths=["artifact.txt"])


@pytest.mark.parametrize("tool,operation,output", [("browser", "click", {"text": "Saved: Kong"}),
                                                   ("mcp", "call", {"result": "ok"}),
                                                   ("terminal", "start", {"running": True})])
@pytest.mark.parametrize("kind", ["tool_success", "evidence"])
@pytest.mark.parametrize("explicit_paths", [False, True])
def test_external_write_receipt_is_not_current_state_even_with_local_hash(tmp_path, tool, operation, output, kind, explicit_paths):
    from kong.contracts import RunState
    from kong.runtime.evidence import fingerprint
    from kong.runtime.gates import CompletionGate
    (tmp_path / "artifact.txt").write_text("unchanged local bytes")
    versions = {"artifact.txt": fingerprint(Workspace(tmp_path), "artifact.txt")}
    observation = Observation(action_id="a1", turn=1, action=Action(name=tool, arguments={"operation": operation}),
                              success=True, output=output, fingerprint="a", output_hash="a", evidence_version=2,
                              artifact_versions=versions, artifact_versions_before=versions)
    extra = {"tool_name": tool} if kind == "tool_success" else {}
    if explicit_paths:
        extra["artifact_paths"] = ["artifact.txt"]
    criterion = Criterion(id="remote", description="current remote state", kind=kind, **extra)
    history = History(observations=[observation])
    decision = FinalResponse(content="done", evidence_ids=["a1"], criteria_evidence={"remote": ["a1"]})
    gate = CompletionGate(Workspace(tmp_path))
    assert gate.check(RunState(goal="remote", success_criteria=[criterion]), decision, history)
    criterion.artifact_paths = []
    criterion.evidence_scope = "event"
    assert not gate.check(RunState(goal="historical receipt", success_criteria=[criterion], completion_scope="mechanical"), decision, history)


@pytest.mark.skipif(os.name != "nt", reason="Native terminal requires Windows")
def test_native_terminal_write_invalidates_process_verification(tmp_path):
    pytest.importorskip("winpty")
    async def scenario():
        runtime = runtime_for(tmp_path)
        try:
            await runtime._actions(Act(actions=[process(CHECK)]))
            assert not problems(runtime)
            await runtime._actions(Act(actions=[Action(name="terminal", arguments={
                "operation": "start", "argv": ["python", "-c", WRITE_BAD], "wait_seconds": 1})]))
            assert runtime.history.observations[-1].success
            session = runtime.history.observations[-1].output["session"]
            await runtime._actions(Act(actions=[Action(name="terminal", arguments={"operation": "read", "session": session, "wait_seconds": 1})]))
            assert (tmp_path / "artifact.txt").read_text() == "bad" and problems(runtime)
            await runtime._actions(Act(actions=[Action(name="terminal", arguments={"operation": "close", "session": session})]))
            await runtime._actions(Act(actions=[process(WRITE_GOOD)]))
            await runtime._actions(Act(actions=[process(CHECK)]))
            assert not problems(runtime)
        finally:
            await runtime.registry.aclose()
    asyncio.run(scenario())


def test_legacy_evidence_loads_without_becoming_current_proof(tmp_path):
    runtime = runtime_for(tmp_path)
    old = Observation(action_id="a1", turn=1, action=process(CHECK), success=True, fingerprint="f", output_hash="h")
    raw = runtime.snapshot().model_dump()
    raw["schema_version"] = 1
    raw["history"] = History(observations=[old]).model_dump()
    import json
    runtime.store.path(runtime.state.run_id).write_text(json.dumps(raw), encoding="utf-8")
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id), ScriptedModel([]), runtime.workspace, runtime.registry, store=runtime.store)
    assert problems(restored)
