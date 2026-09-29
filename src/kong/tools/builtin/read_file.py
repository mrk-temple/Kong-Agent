from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from kong.tools.base import Tool, ToolResult
from kong.workspace import Workspace


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: StrictStr = Field(
        description="Path to the text file to read."
    )
    offset: int = Field(default=0, ge=0, description="Character offset; use next_offset from a truncated context excerpt.")
    max_chars: int | None = Field(default=None, ge=1, le=16000, description="Return a bounded excerpt; use 1600 for code review without context truncation.")


class ReadFileTool(Tool):
    read_only = True
    name = "read_file"
    description = "Read UTF-8 text. For long files use offset/max_chars to read successive excerpts; context truncation provides next_offset."
    args_model = ReadFileArgs

    def __init__(self, workspace: Workspace | None = None):
        self.workspace = workspace

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs["path"]

        try:
            target = self.workspace.resolve(path) if self.workspace else Path(path)
            if target.stat().st_size > 256_000:
                return ToolResult(success=False, error="File exceeds the V0.1 256 KB read limit")
            content = target.read_text(encoding="utf-8")
            offset, max_chars = kwargs.get("offset", 0), kwargs.get("max_chars")
            if offset or max_chars is not None:
                limit = max_chars or 8000
                excerpt = content[offset:offset + limit]
                return ToolResult(success=True, output={"path": path, "offset": offset,
                    "content": excerpt, "size": len(content), "next_offset": offset + len(excerpt),
                    "truncated": offset + len(excerpt) < len(content)})

            return ToolResult(
                success=True,
                output=content,
            )

        except Exception as exc:
            return ToolResult(
                success=False,
                error=str(exc),
            )
