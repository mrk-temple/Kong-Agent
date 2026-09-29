import asyncio
import json
import os
from pathlib import Path
import time

import pytest

from kong.contracts import Act, Action, Criterion, FinalResponse, Mode, NeedDiscovery, NeedUserInput, Status
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.base import ToolCall
from kong.tools.builtin.process_exec import ProcessExecTool
from kong.tools.defaults import default_registry
from kong.tools.executor import ToolExecutor
from kong.tools.registry import ToolRegistry
from kong.workspace import Workspace


def executor(tmp_path, enabled=True):
    return ToolExecutor(default_registry(Workspace(tmp_path), allow_process=enabled))


def command(code, **kwargs):
    return ToolCall(name="process_exec", arguments={"argv": ["python", "-c", code], **kwargs})


def test_process_opt_in_is_not_a_model_argument(tmp_path):
    assert not asyncio.run(executor(tmp_path, False).execute(command("print('no')"))).success
    registry = ToolRegistry()
    registry.register(ProcessExecTool(Workspace(tmp_path)))
    result = asyncio.run(ToolExecutor(registry).execute(command("print('no')")))
    assert not result.success and "disabled" in result.error
    call = command("print('no')", enabled=True)
    assert not asyncio.run(executor(tmp_path).execute(call)).success


def test_process_unicode_cwd_stderr_and_no_implicit_shell(tmp_path, monkeypatch):
    (tmp_path / "资料 folder").mkdir()
    monkeypatch.setenv("KONG_TEST_SECRET", "must-not-inherit")
    code = ("import os,sys,json; print(json.dumps([os.getcwd(),os.getenv('KONG_TEST_SECRET'),sys.argv[1]],ensure_ascii=False)); "
            "print('中文错误',file=sys.stderr)")
    call = ToolCall(name="process_exec", arguments={"argv": ["python", "-c", code, "a & b; $(no)"], "cwd": "资料 folder"})
    result = asyncio.run(executor(tmp_path).execute(call))
    assert result.success, result
    values = json.loads(result.output["stdout"])
    assert Path(values[0]) == tmp_path / "资料 folder"
    assert values[1:] == [None, "a & b; $(no)"]
    assert "中文错误" in result.output["stderr"]
    assert result.output["exit_code"] == 0


@pytest.mark.parametrize("cwd", ["../", ".kong", ".env", "nonexistent"])
def test_process_rejects_invalid_working_directory(tmp_path, cwd):
    result = asyncio.run(executor(tmp_path).execute(command("print('no')", cwd=cwd)))
    assert not result.success


def test_process_failure_and_missing_program(tmp_path):
    result = asyncio.run(executor(tmp_path).execute(command("import sys; print('failed',file=sys.stderr); sys.exit(7)")))
    assert not result.success
    assert result.output["exit_code"] == 7 and "failed" in result.output["stderr"]
    result = asyncio.run(executor(tmp_path).execute(ToolCall(name="process_exec", arguments={"argv": ["kong-missing-76a86"]})))
    assert not result.success


@pytest.mark.skipif(os.name != "nt", reason="Windows batch interpreter")
def test_batch_file_requires_explicit_shell(tmp_path):
    (tmp_path / "test.cmd").write_text("@echo off\r\necho bad > leaked\r\n")
    call = ToolCall(name="process_exec", arguments={"argv": ["./test.cmd"]})
    result = asyncio.run(executor(tmp_path).execute(call))
    assert not result.success and "explicit" in result.error
    assert not (tmp_path / "leaked").exists()


def test_large_both_streams_are_drained_but_bounded(tmp_path):
    code = "import os; os.write(1,b'a'*300000); os.write(2,b'b'*300000)"
    result = asyncio.run(executor(tmp_path).execute(command(code, max_output_bytes=512)))
    assert result.success, result
    for name, char in [("stdout", "a"), ("stderr", "b")]:
        assert result.output[name] == char * 512
        assert result.output[name + "_bytes"] == 300000
        assert result.output[name + "_truncated"]
    assert len(result.model_dump_json()) < 2000


async def wait_file(path):
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(0.02)


def tree_code(parent_wait=True):
    # Child survives the parent unless containment actually works.
    child = "import pathlib,time; pathlib.Path('child-ready').write_text('yes'); time.sleep(1.5); pathlib.Path('leaked').write_text('bad')"
    return ("import subprocess,sys,pathlib,time; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
            "\nwhile not pathlib.Path('child-ready').exists(): time.sleep(.01)\n"
            "pathlib.Path('parent-ready').write_text('yes')\n" +
            ("time.sleep(60)" if parent_wait else ""))


def test_timeout_kills_tree_and_reports_partial_output(tmp_path):
    code = "print('started',flush=True); " + tree_code()
    result = asyncio.run(executor(tmp_path).execute(command(code, timeout_seconds=0.8)))
    assert not result.success and result.output["timed_out"]
    assert "started" in result.output["stdout"]
    assert result.output["exit_code"] is None
    assert (tmp_path / "parent-ready").exists()
    time.sleep(1.6)
    assert not (tmp_path / "leaked").exists()


def test_normal_exit_cleans_descendants_without_waiting_for_their_pipes(tmp_path):
    result = asyncio.run(executor(tmp_path).execute(command(tree_code(False), timeout_seconds=5)))
    assert result.success, result
    time.sleep(1.6)
    assert not (tmp_path / "leaked").exists()


def test_cancel_kills_tree_and_preserves_uncertain_action_for_recovery(tmp_path):
    workspace = Workspace(tmp_path)
    store = RunStore(tmp_path / ".kong" / "runs")
    call = command(tree_code())
    runtime = Runtime.new("execute", ScriptedModel([Act(actions=[Action(**call.model_dump())])]),
        workspace, default_registry(workspace, allow_process=True), store=store)

    async def scenario():
        task = asyncio.create_task(runtime.run())
        await wait_file(tmp_path / "parent-ready")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    time.sleep(1.6)
    assert not (tmp_path / "leaked").exists()
    saved = store.load(runtime.state.run_id)
    assert saved.pending_action_id == "a1"
    assert saved.state.stop_reason == "user_interrupted"
    restored = Runtime.restore(saved, ScriptedModel([]), workspace, default_registry(workspace), store=store)
    assert restored.state.status == Status.WAITING_USER
    assert "process_exec" not in restored.registry.names()


@pytest.mark.skipif(os.name != "nt", reason="Windows job assignment")
def test_job_assignment_failure_never_launches_user_command(tmp_path, monkeypatch):
    from kong.environments.windows_job import WindowsJob
    def fail(*args):
        raise OSError("job assignment denied")
    monkeypatch.setattr(WindowsJob, "assign", fail)
    result = asyncio.run(executor(tmp_path).execute(command("from pathlib import Path; Path('bad').write_text('bad')")))
    assert not result.success and "job assignment denied" in result.error
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("decision_type,mode", [(NeedDiscovery, Mode.AUTO), (Act, Mode.PLAN)])
def test_process_cannot_bypass_discovery_or_plan_gate(tmp_path, decision_type, mode):
    workspace = Workspace(tmp_path)
    call = command("from pathlib import Path; Path('bad').write_text('bad')")
    extra = {"reason": "inspect"} if decision_type is NeedDiscovery else {}
    runtime = Runtime.new("blocked", ScriptedModel([
        decision_type(actions=[Action(**call.model_dump())], **extra), NeedUserInput(question="next?")]),
        workspace, default_registry(workspace, allow_process=True), mode)
    asyncio.run(runtime.run())
    assert not runtime.history.observations
    assert not (tmp_path / "bad").exists()


def test_local_read_execute_artifact_verify_runtime(tmp_path):
    (tmp_path / "input.txt").write_text("3\n5\n8\n", encoding="utf-8")
    code = "from pathlib import Path; total=sum(map(int,Path('input.txt').read_text().split())); Path('report.txt').write_text('Total: '+str(total),encoding='utf-8'); print('report.txt')"
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("read and total", ScriptedModel([
        Act(actions=[Action(name="read_file", arguments={"path": "input.txt"})]),
        Act(actions=[Action(**command(code).model_dump())]),
        Act(actions=[Action(name="read_file", arguments={"path": "report.txt"})]),
        FinalResponse(content="Total: 16", evidence_ids=["a3"])]),
        workspace, default_registry(workspace, allow_process=True),
        store=RunStore(tmp_path / ".kong" / "runs"))
    runtime.state.success_criteria = [Criterion(id="total", description="correct sum", kind="file_contains", path="report.txt", contains="Total: 16")]
    assert asyncio.run(runtime.run()).status == Status.COMPLETED
    assert (tmp_path / "report.txt").read_text() == "Total: 16"
    assert runtime.context_engine.last_packet.budget_enforced
    assert runtime.state.plan is None


def test_repeated_process_result_does_not_manufacture_progress(tmp_path):
    workspace = Workspace(tmp_path)
    action = Action(**command("print('same')").model_dump())
    runtime = Runtime.new("test", ScriptedModel([
        Act(actions=[action, action]), FinalResponse(content="done", evidence_ids=["a2"])]),
        workspace, default_registry(workspace, allow_process=True))
    asyncio.run(runtime.run())
    assert runtime.state.progress_state.progress_count == 1


def test_patch_exact_match_preserves_line_endings_and_rejects_ambiguity(tmp_path):
    target = tmp_path / "note.txt"
    target.write_bytes("开始\r\nold\r\n结束\r\n".encode())
    call = ToolCall(name="patch_file", arguments={"path": "note.txt", "old_text": "old", "new_text": "new"})
    result = asyncio.run(executor(tmp_path).execute(call))
    assert result.success
    assert target.read_bytes() == "开始\r\nnew\r\n结束\r\n".encode()
    assert result.output["previous_sha256"] != result.output["sha256"]
    assert not asyncio.run(executor(tmp_path).execute(call)).success
    target.write_text("old old", encoding="utf-8")
    assert not asyncio.run(executor(tmp_path).execute(call)).success
    assert target.read_text() == "old old"


@pytest.mark.parametrize("path", ["../outside", ".env", ".kong/run"])
def test_patch_uses_workspace_boundary(tmp_path, path):
    call = ToolCall(name="patch_file", arguments={"path": path, "old_text": "old", "new_text": "new"})
    assert not asyncio.run(executor(tmp_path).execute(call)).success
