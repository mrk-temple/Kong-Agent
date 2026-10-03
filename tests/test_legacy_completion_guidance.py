"""Rejected legacy criteria need honest steps, not a respond-only instruction."""
import asyncio

from kong.contracts import Act, Action, Criterion, FinalResponse
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def runtime_at(path, contract=None):
    (path / "process.py").write_text("from pathlib import Path\nPath('out.txt').write_text('OK')\n", encoding="utf-8")
    workspace = Workspace(path)
    runtime = Runtime.new("Produce the exact output", ScriptedModel([]), workspace,
        default_registry(workspace, allow_process=True), completion_contract=contract)
    runtime.state.success_criteria = [
        Criterion(id="output", description="Exact output", kind="file_equals", path="out.txt", expected_text="OK"),
        Criterion(id="execution", description="Stable verification", kind="tool_success", tool_name="process_exec",
                  after_last_write=True, artifact_paths=["process.py", "out.txt"])]
    return runtime


def perform(runtime, *actions):
    asyncio.run(runtime._actions(Act(actions=[Action(name=n, arguments=a) for n,a in actions])))


def feedback(runtime, ids=()):
    gate = CompletionGate(runtime.workspace)
    respond = FinalResponse(content="done", evidence_ids=list(ids))
    return gate.rejection_feedback(runtime.state, respond, runtime.history,
                                   gate.check(runtime.state, respond, runtime.history))


def test_generation_receipt_requires_stable_check_then_final_readback(tmp_path):
    contract = {"family":"data", "artifacts":[{"id":"out","path":"out.txt","format":"text","expected_text":"OK"}],
                "commands":[{"id":"generation","argv":["python","process.py"],"verify_final":False}]}
    runtime = runtime_at(tmp_path, contract)
    perform(runtime, ("process_exec", {"argv":["python","process.py"]}), ("read_file", {"path":"out.txt"}))
    result = feedback(runtime, ["a2"])
    assert result["additional_tool_required"] is True
    steps = result["remaining_steps"]
    check = next(i for i,s in enumerate(steps) if s["kind"] == "verify_criterion" and s["requirement"] == "execution")
    read = next(i for i,s in enumerate(steps) if s.get("action",{}).get("arguments",{}).get("path") == "out.txt")
    assert check < read < len(steps) - 1
    assert steps[-1]["kind"] == "respond"
    assert len(runtime.history.observations) == 2  # No implicit replay.


def test_missing_legacy_tool_is_described_without_contract_or_invented_argv(tmp_path):
    runtime = runtime_at(tmp_path)
    (tmp_path / "out.txt").write_text("OK", encoding="utf-8")
    result = feedback(runtime)
    step = next(s for s in result["remaining_steps"] if s["kind"] == "verify_criterion")
    assert step["tool"] == "process_exec"
    assert step["artifact_paths"] == ["process.py", "out.txt"]
    assert "action" not in step
    issues = next(r["issues"] for r in result["requirement_issues"] if r["id"] == "execution")
    assert "criterion.not_observed" in [i["code"] for i in issues]


def test_only_missing_submission_ids_needs_no_additional_tool(tmp_path):
    runtime = runtime_at(tmp_path)
    runtime.state.success_criteria = runtime.state.success_criteria[:1]
    (tmp_path / "out.txt").write_text("OK", encoding="utf-8")
    perform(runtime, ("read_file", {"path":"out.txt"}))
    result = feedback(runtime)
    assert result["additional_tool_required"] is False
    assert [s["kind"] for s in result["remaining_steps"]] == ["respond"]
    assert "a1" in result["available_successful_observations"]


def test_newer_failed_verification_has_a_local_reason_and_no_replay(tmp_path):
    runtime = runtime_at(tmp_path)
    (tmp_path / "out.txt").write_text("OK", encoding="utf-8")
    perform(runtime, ("process_exec", {"argv":["python","process.py"]}),
            ("process_exec", {"argv":["python","missing.py"]}))
    result = feedback(runtime, ["a1"])
    issues = next(r["issues"] for r in result["requirement_issues"] if r["id"] == "execution")
    assert "criterion.latest_failed_or_unknown" in [i["code"] for i in issues]
    assert any(s["kind"] == "verify_criterion" for s in result["remaining_steps"])
    assert len(runtime.history.observations) == 2
