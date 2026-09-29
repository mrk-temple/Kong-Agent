import asyncio

from kong.tools.base import ToolCall, ToolResult
from kong.tools.registry import ToolRegistry
from kong.tools.validator import (
    ToolValidationError,
    validate_arguments,
)


class ToolExecutor:

    def __init__(self, registry: ToolRegistry, timeout: float = 30) -> None:
        self.registry = registry
        self.timeout = timeout

    async def execute(
        self,
        tool_call: ToolCall,
    ) -> ToolResult:

        try:
            tool = self.registry.get(tool_call.name)

        except KeyError as exc:
            return ToolResult(
                success=False,
                error=str(exc),
            )

        try:
            arguments = validate_arguments(
                tool,
                tool_call.arguments,
            )

        except ToolValidationError as exc:
            return ToolResult(
                success=False,
                error=f"Validation error: {exc}",
            )

        try:
            timeout = tool.execution_timeout if tool.execution_timeout is not None else self.timeout
            return await asyncio.wait_for(tool.run(**arguments), timeout=timeout)

        except TimeoutError:
            return ToolResult(success=False, error="Tool timed out; inspect the environment before retrying writes")

        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Execution error: {exc}",
            )
