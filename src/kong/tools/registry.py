from kong.tools.base import Tool, ToolDefinition


class ToolRegistry:

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(
                f"Tool already registered: {tool.name}"
            )

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]

        except KeyError:
            raise KeyError(
                f"Unknown tool: {name}"
            )

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def definitions(self) -> list[ToolDefinition]:
        return [
            tool.definition()
            for tool in self._tools.values()
        ]

    async def aclose(self) -> None:
        """Release process-local handles; call once on the owning registry at app exit."""
        errors = []
        for tool in reversed(list(self._tools.values())):
            close = getattr(tool, "aclose", None)
            if close:
                try:
                    await close()
                except Exception as exc:
                    errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError("Tool cleanup failed: " + ", ".join(errors))
