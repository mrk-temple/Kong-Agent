"""I-05/06/07/10: current bytes alone do not establish task correctness."""
import asyncio
import json

import pytest

from kong.contracts import Act, Action, FinalResponse, Mode, NeedUserInput, Status
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def data_contract():
    return {"version": 1, "family": "data", "protected_paths": ["rows.csv", "correction.md"],
        "required_reads": ["rows.csv", "correction.md"],
        "artifacts": [{"id": "report", "path": "report.json", "format": "json", "additional_keys": False,
            "json_fields": [{"name": "included_statuses", "type": "array_string", "allowed": ["active"]},
                            {"name": "total", "type": "integer"}]}],
        "aggregates": [
            {"id": "included", "source": "rows.csv", "artifact_id": "report", "field": "included_statuses",
             "operation": "unique_values", "column": "status", "filters": {"status": ["active"]}},
            {"id": "sum", "source": "rows.csv", "artifact_id": "report", "field": "total",
             "operation": "sum", "column": "value", "filters": {"status": ["active"]}}]}


def make_runtime(path, contract, decisions=(), **kwargs):
    workspace = Workspace(path)
    return Runtime.new("follow correction and produce report", ScriptedModel(list(decisions)), workspace,
                       default_registry(workspace, allow_process=True), completion_contract=contract,
                       store=RunStore(path / ".kong" / "runs"), **kwargs)


def prepare_data(path):
    (path / "rows.csv").write_text("status,value\nactive,4\narchived,50\nactive,3\n", encoding="utf-8")
    (path / "correction.md").write_text("Only active rows", encoding="utf-8")


async def actions(runtime, *items):
    await runtime._actions(Act(actions=[Action(name=name, arguments=args) for name, args in items]))


def complete_problems(runtime):
    ids = [o.action_id for o in runtime.history.observations if o.success]
    return CompletionGate(runtime.workspace).check(runtime.state, FinalResponse(content="done", evidence_ids=ids),
                                                   runtime.history)


@pytest.mark.parametrize("report", [
    {"included_statuses": 2, "total": 7},
    {"included_statuses": ["active"], "total": True},
    {"included_statuses": ["active"], "total": 57},
    {"included_statuses": ["active"]},
    {"included_statuses": ["archived"], "total": 7},
    {"included_statuses": ["active"], "total": 7, "obsolete_count": 3},
])
def test_fresh_but_incorrect_json_cannot_complete(tmp_path, report):
    prepare_data(tmp_path)
    runtime = make_runtime(tmp_path, data_contract())
    async def scenario():
        await actions(runtime, ("read_file", {"path": "rows.csv"}), ("read_file", {"path": "correction.md"}),
            ("write_file", {"path": "report.json", "content": json.dumps(report)}),
            ("read_file", {"path": "report.json"}))
    asyncio.run(scenario())
    assert any("Contract" in p for p in complete_problems(runtime))
    assert all(o.success for o in runtime.history.observations)


def test_final_correct_json_source_reads_and_current_readback_are_accepted(tmp_path):
    prepare_data(tmp_path)
    runtime = make_runtime(tmp_path, data_contract())
    async def scenario():
        await actions(runtime, ("read_file", {"path": "rows.csv"}), ("read_file", {"path": "correction.md"}),
            ("write_file", {"path": "report.json", "content": '{"included_statuses":["active"],"total":7}'}),
            ("read_file", {"path": "report.json"}))
    asyncio.run(scenario())
    assert complete_problems(runtime) == []


def test_readback_then_mutate_needs_final_version_readback(tmp_path):
    prepare_data(tmp_path)
    runtime = make_runtime(tmp_path, data_contract())
    content = '{"included_statuses":["active"],"total":7}'
    async def scenario():
        await actions(runtime, ("read_file", {"path": "rows.csv"}), ("read_file", {"path": "correction.md"}),
            ("write_file", {"path": "report.json", "content": content}), ("read_file", {"path": "report.json"}),
            ("write_file", {"path": "report.json", "content": content + "\n"}))
        assert any("readback" in p for p in complete_problems(runtime))
        await actions(runtime, ("read_file", {"path": "report.json"}))
    asyncio.run(scenario())
    assert complete_problems(runtime) == []


def test_protected_source_hash_survives_restore_and_cannot_be_rebaselined(tmp_path):
    prepare_data(tmp_path)
    runtime = make_runtime(tmp_path, data_contract())
    runtime.save()
    (tmp_path / "rows.csv").write_text("status,value\nactive,999\n", encoding="utf-8")
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id), ScriptedModel([]), runtime.workspace,
                               runtime.registry, store=runtime.store)
    async def scenario():
        await actions(restored, ("read_file", {"path": "rows.csv"}), ("read_file", {"path": "correction.md"}),
            ("write_file", {"path": "report.json", "content": '{"included_statuses":["active"],"total":999}'}),
            ("read_file", {"path": "report.json"}))
    asyncio.run(scenario())
    assert any("protected" in p for p in complete_problems(restored))


@pytest.mark.parametrize("text", ['{"total":7,"total":7}', '{"total":NaN}', '[]'])
def test_json_parseability_is_strict(tmp_path, text):
    runtime = make_runtime(tmp_path, {"family": "data", "artifacts": [{"id": "r", "path": "report.json",
        "format": "json", "json_fields": [{"name": "total", "type": "integer"}]}]})
    asyncio.run(actions(runtime, ("write_file", {"path": "report.json", "content": text}),
                        ("read_file", {"path": "report.json"})))
    assert any("Contract" in p for p in complete_problems(runtime))


def code_contract():
    return {"family": "code", "protected_paths": ["check.py"],
        "artifacts": [{"id": "code", "path": "calc.py", "format": "file", "require_readback": False}],
        "commands": [{"id": "tests", "argv": ["python", "check.py"], "stdout_contains": ["checks passed"]}]}


def prepare_code(path):
    (path / "calc.py").write_text("def total(values):\n    return sum(values)\n", encoding="utf-8")
    (path / "check.py").write_text("from calc import total\nassert total([2,3])==5\nprint('checks passed')\n", encoding="utf-8")


def test_code_verify_then_mutate_cannot_reuse_old_test_observation(tmp_path):
    prepare_code(tmp_path)
    runtime = make_runtime(tmp_path, code_contract())
    async def scenario():
        await actions(runtime, ("process_exec", {"argv": ["python", "check.py"]}))
        assert complete_problems(runtime) == []
        await actions(runtime, ("write_file", {"path": "calc.py", "content": "def total(values):\n    return sum(values)+1\n"}))
        assert any("command" in p for p in complete_problems(runtime))
        await actions(runtime, ("process_exec", {"argv": ["python", "check.py"]}))
        assert any("command" in p for p in complete_problems(runtime))
        await actions(runtime, ("write_file", {"path": "calc.py", "content": "def total(values):\n    return sum(values)\n"}),
            ("process_exec", {"argv": ["python", "check.py"]}))
    asyncio.run(scenario())
    assert complete_problems(runtime) == []


def test_correct_exit_from_wrong_command_does_not_satisfy_contract(tmp_path):
    prepare_code(tmp_path)
    runtime = make_runtime(tmp_path, code_contract())
    asyncio.run(actions(runtime, ("process_exec", {"argv": ["python", "-c", "print('checks passed')"]})))
    assert any("command" in p for p in complete_problems(runtime))


def test_missing_final_readback_and_external_mutation_fail_closed(tmp_path):
    runtime = make_runtime(tmp_path, {"family": "data", "artifacts": [{"id": "r", "path": "result.txt",
        "format": "text", "expected_text": "OK"}]})
    asyncio.run(actions(runtime, ("write_file", {"path": "result.txt", "content": "OK"})))
    assert any("readback" in p for p in complete_problems(runtime))
    asyncio.run(actions(runtime, ("read_file", {"path": "result.txt"})))
    assert complete_problems(runtime) == []
    (tmp_path / "result.txt").write_text("WRONG", encoding="utf-8")
    assert complete_problems(runtime)


def test_read_before_ask_is_a_visible_requirement_without_implicit_tools(tmp_path):
    (tmp_path / "claim.txt").write_text("unsupported assertion", encoding="utf-8")
    model_decisions = [NeedUserInput(question="Please provide evidence."),
        Act(actions=[Action(name="read_file", arguments={"path": "claim.txt"})]),
        NeedUserInput(question="Please provide evidence.")]
    runtime = make_runtime(tmp_path, {"family": "data", "allow_completion": False,
        "read_before_ask": ["claim.txt"], "required_question": "Please provide evidence."}, model_decisions)
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    assert runtime.model.calls == 3 and len(runtime.history.observations) == 1
    assert runtime.state.pending_question == "Please provide evidence."
    assert "claim.txt" in json.dumps(runtime.model.contexts[0].messages)
    assert any(e.payload.get("ask_rejected") for e in runtime.history.events)


def test_read_before_ask_does_not_bypass_plan_preapproval(tmp_path):
    (tmp_path / "claim.txt").write_text("unsupported", encoding="utf-8")
    runtime = make_runtime(tmp_path, {"family": "data", "allow_completion": False,
        "read_before_ask": ["claim.txt"]}, [NeedUserInput(question="Clarify the plan.")], mode=Mode.PLAN)
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    assert runtime.model.calls == 1 and not runtime.history.observations


def test_changed_goal_or_constraint_requires_host_contract_update(tmp_path):
    prepare_data(tmp_path)
    runtime = make_runtime(tmp_path, data_contract())
    runtime.submit_input("constraint", "Only archived rows now")
    runtime._apply_inputs()
    assert runtime.state.status == Status.WAITING_USER
    assert any("invalidated" in p for p in complete_problems(runtime))
    revised = data_contract()
    for aggregate in revised["aggregates"]:
        aggregate["filters"] = {"status": ["archived"]}
    revised["artifacts"][0]["json_fields"][0]["allowed"] = ["archived"]
    runtime.set_completion_contract(revised)
    runtime.human("answer", text="Host updated the contract to reflect the new constraint.")
    assert runtime.state.status == Status.RUNNING and runtime.state.turn_count == 0
