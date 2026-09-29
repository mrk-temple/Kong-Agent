import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from kong.tools.base import Tool, ToolResult
from kong.workspace import Workspace


class SearchFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr = Field(min_length=1)
    path: StrictStr = "."


class SearchFilesTool(Tool):
    name = "search_files"
    description = "Literal text search in UTF-8 files, up to 1000 files / 50 matches; reports truncation."
    args_model = SearchFilesArgs
    read_only = True

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    async def run(self, **kwargs: Any) -> ToolResult:
        root = self.workspace.resolve(kwargs.get("path", "."))
        if not root.is_dir():
            return ToolResult(success=False, error="Search path must be an existing directory")
        matches, scanned, skipped = [], 0, 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            allowed = []
            for name in dirs:
                try:
                    self.workspace.resolve(str(os.path.join(directory, name)))
                    allowed.append(name)
                except ValueError:
                    pass
            dirs[:] = sorted(allowed)
            for name in sorted(files):
                if scanned >= 1000 or len(matches) >= 50:
                    return ToolResult(success=True, output={"matches": matches, "truncated": True, "skipped": skipped})
                scanned += 1
                try:
                    path = self.workspace.resolve(str(os.path.join(directory, name)))
                    if path.stat().st_size > 256_000:
                        skipped += 1
                        continue
                    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        if kwargs["text"] in line:
                            matches.append({"path": str(path.relative_to(self.workspace.root)), "line": index, "text": line[:500]})
                            if len(matches) >= 50:
                                return ToolResult(success=True, output={"matches": matches, "truncated": True, "skipped": skipped})
                except (OSError, ValueError, UnicodeError):
                    skipped += 1
        return ToolResult(success=True, output={"matches": matches, "truncated": False, "skipped": skipped})
