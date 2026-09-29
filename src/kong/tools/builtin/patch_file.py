import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from kong.tools.base import Tool, ToolResult
from kong.tools.builtin.write_file import WriteFileTool
from kong.workspace import Workspace


class PatchFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: StrictStr
    old_text: StrictStr = Field(min_length=1, max_length=256000)
    new_text: StrictStr = Field(max_length=256000)


class PatchFileTool(Tool):
    name = "patch_file"
    description = ("Replace exactly one literal UTF-8 text occurrence in an existing workspace file. "
                   "Read first; missing or ambiguous old_text is rejected. Preserves other text and line endings. "
                   "No regex, insertion by line number, or automatic retry.")
    args_model = PatchFileArgs

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    async def run(self, **kwargs: Any) -> ToolResult:
        target = self.workspace.resolve(kwargs["path"])
        with target.open("rb") as stream:
            original = stream.read(256001)
        if len(original) > 256000:
            return ToolResult(success=False, error="File exceeds the 256 KB patch limit")
        text = original.decode("utf-8")
        if text.count(kwargs["old_text"]) != 1:
            return ToolResult(success=False, error="old_text must match exactly once; read the file and provide unique text")
        updated = text.replace(kwargs["old_text"], kwargs["new_text"], 1)
        if len(updated.encode("utf-8")) > 256000:
            return ToolResult(success=False, error="Patched file exceeds the 256 KB limit")
        if updated == text:
            return ToolResult(success=False, error="Patch makes no change")
        result = await WriteFileTool(self.workspace).run(path=kwargs["path"], content=updated, overwrite=True)
        result.output["previous_sha256"] = hashlib.sha256(original).hexdigest()
        return result
