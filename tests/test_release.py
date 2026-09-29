import asyncio
import json

import pytest
from kong.cli import parser
from kong.contracts import Action, Act, Criterion, FinalResponse, History, Observation
from kong.doctor import diagnose
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate, result_evidence
from kong.runtime.loop import Runtime
from kong.storage import RunStore, Snapshot
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def observation(n, name, args, output, success=True):
    return Observation(action_id=f"a{n}", turn=n, action=Action(name=name, arguments=args),
        output=output, success=success, fingerprint=str(n), output_hash=str(n))


def test_terminal_start_cannot_pass_exit_criterion(tmp_path):
    criterion = Criterion(id="exit", description="verified exit", kind="tool_result", tool_name="terminal",
        operation="read", arguments_match={"session":"term_x"}, output_match={"running":False,"exit_code":0})
    history = History(observations=[observation(1,"terminal",{"operation":"start"},{"running":True})])
    assert not result_evidence(criterion, history)
    history.observations.append(observation(2,"terminal",{"operation":"read","session":"other"},{"running":False,"exit_code":0}))
    assert not result_evidence(criterion, history)
    history.observations.append(observation(3,"terminal",{"operation":"read","session":"term_x"},{"running":False,"exit_code":0}))
    assert result_evidence(criterion, history) == ["a3"]
    history.observations.append(observation(4,"terminal",{"operation":"read","session":"term_x"},{"running":False,"exit_code":1},False))
    assert not result_evidence(criterion, history)


def test_browser_and_mcp_typed_result_checks(tmp_path):
    criterion = Criterion(id="mcp", description="result", kind="tool_result", tool_name="mcp", operation="call",
        arguments_match={"server":"demo","tool":"add"}, output_match={"structured_content":{"result":0}})
    history = History(observations=[observation(1,"mcp",{"operation":"call","server":"demo","tool":"add"},
                                              {"structured_content":{"result":False}})])
    assert not result_evidence(criterion, history)  # bool is not an integer result
    history.observations[0].output = {"structured_content":{"result":0}}
    assert result_evidence(criterion, history)
    criterion = Criterion(id="dom", description="DOM text", kind="tool_result", tool_name="browser", operation="snapshot",
        arguments_match={"page":"page_x"}, output_contains={"/text":"Saved: Kong"})
    history.observations.append(observation(2,"browser",{"operation":"snapshot","page":"page_x"},{"text":"Saved: Kong"}))
    assert result_evidence(criterion, history)
    history.observations.append(observation(3,"browser",{"operation":"snapshot","page":"page_x"},{"text":"Error"}))
    assert not result_evidence(criterion, history)
    with pytest.raises(ValueError):
        Criterion(id="bad",description="bad",kind="tool_result",tool_name="terminal")


def test_report_metrics_survive_resume_and_legacy_is_unknown(tmp_path):
    class UsageModel(ScriptedModel):
        last_usage = {"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}
    workspace = Workspace(tmp_path)
    store = RunStore(tmp_path/".kong"/"runs")
    runtime = Runtime.new("hello", UsageModel([FinalResponse(content="hi")]), workspace, default_registry(workspace), store=store)
    asyncio.run(runtime.run())
    report = json.loads(store.report_path(runtime.state.run_id).read_text(encoding="utf-8"))
    assert report["metrics"]["tokens"]["total_tokens"] == 7
    assert report["metrics"]["usage_complete"] and report["unverified"]
    assert len(store.list_runs()) == 1
    restored = Runtime.restore(store.load(runtime.state.run_id), UsageModel([]), workspace, default_registry(workspace), store=store)
    asyncio.run(restored.run())  # completed run cannot call model again
    assert restored.metrics.model_calls == 1 and restored.metrics.tokens["total_tokens"] == 7
    old = runtime.snapshot().model_dump()
    del old["metrics"]
    restored = Runtime.restore(Snapshot.model_validate(old), ScriptedModel([]), workspace, default_registry(workspace))
    assert restored.metrics.legacy_unknown


def test_restore_warns_expired_handles_and_never_replays(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("continue", ScriptedModel([]), workspace, default_registry(workspace))
    runtime.history.observations = [observation(1,"terminal",{"operation":"start"},{"session":"term_old","running":True}),
        observation(2,"browser",{"operation":"open"},{"page":"page_old"}),
        observation(3,"mcp",{"operation":"list","server":"demo"},{"tools":[]})]
    runtime.pending_action_id = "a4"
    snapshot = runtime.snapshot()
    restored = Runtime.restore(snapshot, ScriptedModel([]), workspace,
        default_registry(workspace, allow_terminal=True, allow_browser=True, mcp_servers={}))
    assert restored.pending_action_id == "a4"
    warnings = restored.history.events[-1].payload["unavailable_resources"]
    assert warnings == {"terminal":["term_old"],"browser":["page_old"],"mcp":["demo"]}
    asyncio.run(restored.run())
    assert len(restored.history.observations) == 3


def test_doctor_offline_does_not_call_services_or_expose_keys(tmp_path, monkeypatch):
    config = tmp_path/"config.toml"
    config.write_text('default_profile="test"\n[profiles.test]\nbase_url="https://example.com/v1"\nmodel="test"\napi_key_env="DOCTOR_SECRET"\n')
    monkeypatch.setenv("DOCTOR_SECRET", "secret-that-must-not-appear")
    async def forbidden(*args, **kwargs):
        raise AssertionError("Offline doctor must not call services")
    monkeypatch.setattr("kong.models.compatible.CompatibleModel.generate", forbidden)
    args = parser().parse_args(["--workspace",str(tmp_path),"--config",str(config),"doctor","--json"])
    report = asyncio.run(diagnose(args))
    assert report["ok"] and not report["online"]
    assert "secret-that-must-not-appear" not in json.dumps(report)
    args.config = tmp_path/"missing.toml"
    assert not asyncio.run(diagnose(args))["ok"]


def test_doctor_browser_launch_failure_has_actionable_safe_error(tmp_path, monkeypatch):
    config = tmp_path/"config.toml"
    config.write_text('default_profile="local"\n[profiles.local]\nbase_url="http://127.0.0.1:9/v1"\nmodel="test"\n')
    async def broken(*args):
        raise RuntimeError("secret details must not appear")
    monkeypatch.setattr("kong.integrations.browser.BrowserTool.start", broken)
    args = parser().parse_args(["--workspace",str(tmp_path),"--config",str(config),"--allow-browser","doctor"])
    report = asyncio.run(diagnose(args))
    assert not report["ok"] and "secret details" not in json.dumps(report)
    assert any("playwright install chromium" in c["fix"] for c in report["checks"])


def test_compact_schema_keeps_required_title_fields():
    from kong.context import compact_schema
    from kong.contracts import Plan
    schema = compact_schema(Plan.model_json_schema())
    assert "title" in schema["$defs"]["PlanStep"]["properties"]
    assert "title" in schema["$defs"]["PlanStep"]["required"]
    assert compact_schema({"title":"display", "const":{"title":"literal"}}) == {"const":{"title":"literal"}}


def test_repair_runtime_requires_real_check_after_patch(tmp_path):
    (tmp_path/"calc.py").write_text("def total(values):\n    return sum(values) + 1\n")
    workspace = Workspace(tmp_path)
    registry = default_registry(workspace, allow_process=True)
    runtime = Runtime.new("fix total", ScriptedModel([
        Act(actions=[Action(name="patch_file",arguments={"path":"calc.py","old_text":"sum(values) + 1","new_text":"sum(values)"})]),
        Act(actions=[Action(name="process_exec",arguments={"argv":["python","-c","from calc import total; assert total([2,3])==5; assert total([])==0; print('checks passed')"]})]),
        FinalResponse(content="fixed and tested",evidence_ids=["a2"])]), workspace, registry)
    runtime.state.success_criteria = [Criterion(id="check",description="tests after patch",kind="tool_result",
        tool_name="process_exec",after_last_write=True,output_match={"exit_code":0},output_contains={"/stdout":"checks passed"})]
    asyncio.run(runtime.run())
    assert runtime.state.status == "completed", runtime.state
    report = CompletionGate(workspace).criteria_results(runtime.state,runtime.history,{})
    assert report[0]["verified"] and report[0]["evidence_ids"] == ["a2"]
