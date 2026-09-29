import os
import tempfile
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from kong.tools.base import Tool, ToolResult
from kong.workspace import Workspace


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: StrictStr
    content: StrictStr = Field(max_length=256_000)
    overwrite: StrictBool = False


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create UTF-8 text in the workspace. Existing files require overwrite=true. Atomic replacement."
    args_model = WriteFileArgs

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    async def run(self, **kwargs: Any) -> ToolResult:
        path = self.workspace.resolve(kwargs["path"])
        content = kwargs["content"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if not kwargs.get("overwrite", False):
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
        else:
            descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".kong-write-")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        import hashlib
        return ToolResult(success=True, output={
            "path": str(path.relative_to(self.workspace.root)),
            "bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        })
