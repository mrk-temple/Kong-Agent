from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from kong.tools.base import Tool, ToolResult
from kong.workspace import Workspace


class ListDirArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: StrictStr = Field(
        default=".",
        description="Directory path to list."
    )


class ListDirTool(Tool):
    read_only = True
    name = "list_dir"
    description = "List files and directories in a directory."
    args_model = ListDirArgs

    def __init__(self, workspace: Workspace | None = None):
        self.workspace = workspace

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs["path"]

        try:
            target = self.workspace.resolve(path) if self.workspace else Path(path)
            entries = []
            for item in target.iterdir():
                if self.workspace:
                    try:
                        self.workspace.resolve(str(item))
                    except ValueError:
                        continue
                entries.append(item)
                if len(entries) > 1000:
                    return ToolResult(success=False, error="Directory exceeds 1000 entries; use a narrower path")

            items = [
                {
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                }
                for item in sorted(entries, key=lambda p: p.name)
            ]

            return ToolResult(
                success=True,
                output=items,
            )

        except Exception as exc:
            return ToolResult(
                success=False,
                error=str(exc),
            )
