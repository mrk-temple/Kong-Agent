import asyncio
from copy import copy

from kong.tools.base import ToolCall, ToolResult
from kong.tools.registry import ToolRegistry
from kong.tools.permissions import PermissionPolicy, ResourceWorkspace
from kong.tools.validator import (
    ToolValidationError,
    validate_arguments,
)


class ToolExecutor:

    def __init__(self, registry: ToolRegistry, timeout: float = 30, *, permissions: PermissionPolicy | None = None) -> None:
        self.registry = registry
        self.timeout = timeout
        self.permissions = permissions or PermissionPolicy()

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
            denial = self.permissions.check(ToolCall(name=tool_call.name, arguments=arguments), tool)
            if denial:
                return ToolResult(success=False, error=denial)
            if (tool_call.name in {"read_file", "list_dir", "search_files"}
                    and self.permissions.read_roots != ["."] and getattr(tool, "workspace", None)):
                tool = copy(tool)
                tool.workspace = ResourceWorkspace(tool.workspace, self.permissions)
            timeout = tool.execution_timeout if tool.execution_timeout is not None else self.timeout
            return await asyncio.wait_for(tool.run(**arguments), timeout=timeout)

        except TimeoutError:
            return ToolResult(success=False, error="Tool timed out; inspect the environment before retrying writes",
                              outcome_unknown=not tool.read_only)

        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Execution error: {exc}",
                outcome_unknown=not tool.read_only,
            )
