import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict

from kong.tools.base import Tool, ToolCall, ToolResult
from kong.tools.builtin.read_file import ReadFileTool
from kong.tools.executor import ToolExecutor
from kong.tools.registry import ToolRegistry


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExplodingTool(Tool):
    name = "explode"
    description = "Raise an error for executor testing."
    args_model = NoArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError("expected failure")


def test_successful_tool_call_returns_tool_result(tmp_path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("hello", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ReadFileTool())

    result = asyncio.run(
        ToolExecutor(registry).execute(
            ToolCall(name="read_file", arguments={"path": str(path)})
        )
    )

    assert isinstance(result, ToolResult)
    assert result.model_dump() == {"success": True, "output": "hello", "error": None}


def test_unknown_tool_returns_failure() -> None:
    result = asyncio.run(
        ToolExecutor(ToolRegistry()).execute(ToolCall(name="missing"))
    )

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.output is None
    assert "Unknown tool: missing" in result.error


def test_validation_error_returns_failure_without_running_tool() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool())

    result = asyncio.run(
        ToolExecutor(registry).execute(ToolCall(name="read_file", arguments={}))
    )

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.output is None
    assert result.error.startswith("Validation error:")
    assert "path" in result.error


def test_execution_error_returns_failure_without_propagating() -> None:
    registry = ToolRegistry()
    registry.register(ExplodingTool())

    result = asyncio.run(
        ToolExecutor(registry).execute(ToolCall(name="explode"))
    )

    assert isinstance(result, ToolResult)
    assert result.model_dump() == {
        "success": False,
        "output": None,
        "error": "Execution error: expected failure",
    }
