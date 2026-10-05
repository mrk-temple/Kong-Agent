"""MCP connections must be explicitly closed before declared completion."""
import asyncio
from types import SimpleNamespace

import pytest

from kong.contracts import Act, Action, FinalResponse
from kong.integrations.mcp import MCPArgs
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate
from kong.runtime.loop import Runtime
from kong.runtime.resource_closure import closure_result
from kong.tools.base import Tool, ToolResult
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


class Connections(Tool):
    name = "mcp"
    args_model = MCPArgs

    def __init__(self):
        self.connections = set()

    async def run(self, operation, server="", **kwargs):
        if operation == "servers":
            return ToolResult(success=True, output={"servers": ["one", "two"]})
        if operation == "close":
            self.connections.discard(server)
            return ToolResult(success=True, output={"closed": server})
        self.connections.add(server)
        return ToolResult(success=True, output={"tools": []})


def test_mcp_connection_requirement_rejects_model_omission(tmp_path):
    (tmp_path / "result.txt").write_text("42", encoding="utf-8")
    ws = Workspace(tmp_path)
    registry = default_registry(ws)
    tool = Connections()
    registry.register(tool)
    rt = Runtime.new("get result and close connection", ScriptedModel([]), ws, registry,
        completion_contract={"version": 2, "family": "data",
            "artifacts": [{"id": "result", "path": "result.txt", "format": "text", "expected_text": "42"}],
            "resource_closures": [{"id": "connection", "tool": "mcp"}]})
    async def action(name, **args):
        await rt._actions(Act(actions=[Action(name=name, arguments=args)]))
    asyncio.run(action("mcp", operation="list", server="one"))
    asyncio.run(action("read_file", path="result.txt"))
    gate = CompletionGate(ws)
    response = FinalResponse(content="done", evidence_ids=["a2"])
    failures = gate.check(rt.state, response, rt.history)
    assert failures and tool.connections == {"one"}
    feedback = gate.rejection_feedback(rt.state, response, rt.history, failures)
    close = next(step for step in feedback["remaining_steps"] if step["kind"] == "close_resource")
    assert close["action"] == {"name": "mcp", "arguments": {"operation": "close", "server": "one"}}
    asyncio.run(action("mcp", operation="close", server="one"))
    asyncio.run(action("read_file", path="result.txt"))
    assert not gate.check(rt.state, FinalResponse(content="done", evidence_ids=["a3", "a4"]), rt.history)
    assert not tool.connections


def observation(op, server="one", *, success=True, unknown=False, closed=None, dispatched=True):
    return SimpleNamespace(action=Action(name="mcp", arguments={"operation": op, "server": server}),
        success=success, outcome_unknown=unknown, output={"closed": closed or server} if op == "close" else {"tools": []},
        action_id=op + server, dispatched=dispatched)


@pytest.mark.parametrize("case", ["servers_only", "missing", "wrong_close", "partial", "reopen", "unknown", "failed_list"])
def test_mcp_closure_rejects_incomplete_lifecycles(case):
    rows = [observation("list")]
    if case == "servers_only":
        rows = [observation("servers")]
    if case == "wrong_close":
        rows.append(observation("close", "other"))
    if case == "partial":
        rows += [observation("list", "two"), observation("close")]
    if case == "reopen":
        rows += [observation("close"), observation("call")]
    if case == "unknown":
        rows += [observation("call", unknown=True), observation("close")]
    if case == "failed_list":
        rows = [observation("list", success=False)]
    result = closure_result(SimpleNamespace(tool="mcp"), SimpleNamespace(observations=rows))
    assert not result[0]


def test_mcp_closure_rejects_forged_close_receipt():
    rows = [observation("list"), observation("close", closed="other")]
    assert not closure_result(SimpleNamespace(tool="mcp"), SimpleNamespace(observations=rows))[0]


def test_all_mcp_connections_closed_and_unscheduled_calls_do_not_reopen():
    rows = [observation("list"), observation("list", "two"), observation("close"),
            observation("close", "two"), observation("call", dispatched=False)]
    verified, evidence, issues, actions = closure_result(SimpleNamespace(tool="mcp"), SimpleNamespace(observations=rows))
    assert verified and set(evidence) == {"closeone", "closetwo"}
    assert not issues and not actions
