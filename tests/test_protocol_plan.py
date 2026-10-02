import asyncio
import json

import httpx
import pytest

from kong.config import ModelConfig
from kong.contracts import Mode, Status
from kong.models.compatible import CompatibleModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


PLAN = {"kind": "plan", "plan": {"goal": "work", "rationale": "review", "steps": [{"id": "s1", "title": "do"}],
    "success_criteria": [{"id": "c1", "description": "exists", "kind": "file_exists", "path": "output.txt"}]}}


def scenario(tmp_path, responses, limit=10):
    async def perform():
        calls, contexts, events = [], [], []
        def handler(request):
            contexts.append(json.loads(request.content))
            value = responses[min(len(calls), len(responses) - 1)]
            calls.append(value)
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}],
                                           "usage": {"total_tokens": 7}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            workspace = Workspace(tmp_path)
            runtime = Runtime.new("work", CompatibleModel(ModelConfig(base_url="http://localhost/v1", model="test"), client),
                workspace, default_registry(workspace), mode=Mode.PLAN, store=RunStore(tmp_path / ".kong/runs"),
                on_event=lambda kind, data: events.append((kind, data)))
            runtime.state.hard_turn_limit = limit
            await runtime.run()
            return runtime, calls, contexts, events
    return asyncio.run(perform())


@pytest.mark.parametrize("bad", [
    {"kind": "plan", "plan": {**PLAN["plan"], "steps": []}},
    {"kind": "plan", "plan": {**PLAN["plan"], "steps": [{"id": "s1", "title": "a"}, {"id": "s1", "title": "b"}]}},
    {"kind": "plan", "plan": {**PLAN["plan"], "success_criteria": [{"id": "x", "description": "bad", "kind": "file_exists"}]}},
    {"kind": "act", "actions": [{"name": "write_file", "arguments": {"path": "output.txt", "content": "no"}}], "extra-secret": True},
])
def test_invalid_plan_repairs_to_pending_approval_without_actions(tmp_path, bad):
    repaired = {"kind": "plan", "plan": {**PLAN["plan"], "revision": 999, "approved_revision": 999,
                                         "steps": [{"id": "s1", "title": "do", "done": True, "evidence_ids": ["a1"]}]}}
    runtime, calls, contexts, events = scenario(tmp_path, [bad, repaired])
    assert runtime.state.status == Status.WAITING_USER and runtime.state.pending_approval.revision == 1
    assert runtime.state.plan.approved_revision is None and not runtime.state.plan.steps[0].done
    assert runtime.state.turn_count == runtime.metrics.model_calls == len(calls) == 2
    assert runtime.metrics.tokens == {"total_tokens": 14}
    assert not runtime.history.observations and not (tmp_path / "output.txt").exists()
    assert any(data.get("reason") == "protocol_repair" for _, data in events)
    assert "Correct the JSON format" in json.dumps(contexts[-1])
    assert runtime.store.load(runtime.state.run_id).state.turn_count == 2


def test_field_diagnostics_do_not_leak_keys_values_or_validator_input(tmp_path):
    secret = "credential-and-raw-user-text"
    bad = {"kind": "plan", "plan": {**PLAN["plan"], "steps": [{"id": "x", "title": {secret: secret}, secret: secret}]}, secret: secret}
    runtime, _, _, _ = scenario(tmp_path, [bad, PLAN])
    errors = [event.payload for event in runtime.history.events if event.type == "model_error"]
    serialized = json.dumps(errors)
    assert secret not in serialized
    assert {"path": "$.plan.steps[].title", "code": "string_type"} in errors[0]["diagnostics"]
    assert {"path": "$.plan.steps[].*", "code": "extra_forbidden"} in errors[0]["diagnostics"]
    assert secret not in runtime.store.path(runtime.state.run_id).read_text(encoding="utf-8")


@pytest.mark.parametrize("limit,calls,status", [(1, 1, Status.STOPPED), (10, 2, Status.WAITING_USER)])
def test_plan_repair_does_not_reset_or_escape_budget(tmp_path, limit, calls, status):
    runtime, actual, _, _ = scenario(tmp_path, [{"kind": "plan", "plan": {}}], limit)
    assert len(actual) == runtime.state.turn_count == calls
    assert runtime.state.hard_turn_limit == limit and runtime.state.status == status
    assert not runtime.history.observations


def test_valid_action_after_invalid_plan_still_cannot_bypass_approval(tmp_path):
    action = {"kind": "act", "actions": [{"name": "write_file", "arguments": {"path": "output.txt", "content": "no"}}]}
    runtime, _, _, _ = scenario(tmp_path, [{"kind": "plan", "plan": {}}, action, PLAN])
    assert runtime.state.pending_approval and not runtime.history.observations
    assert not (tmp_path / "output.txt").exists()


def test_cross_field_invariant_has_specific_allowlisted_hint(tmp_path):
    bad = {"kind": "plan", "plan": {**PLAN["plan"], "success_criteria": [
        {"id": "c", "description": "check", "kind": "file_exists", "path": "output.txt", "operation": "secret-operation"}]}}
    runtime, _, _, _ = scenario(tmp_path, [bad, PLAN])
    error = next(event.payload for event in runtime.history.events if event.type == "model_error")
    diagnostic = error["diagnostics"][0]
    assert diagnostic["path"] == "$.plan.success_criteria[]"
    assert diagnostic["code"] == "criterion_result_filters_kind"
    assert "kind=tool_result" in diagnostic["hint"]
    assert "secret-operation" not in json.dumps(error)
    assert runtime.state.pending_approval
