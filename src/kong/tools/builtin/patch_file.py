import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from kong.tools.base import Tool, ToolResult
from kong.tools.builtin.write_file import WriteFileTool
from kong.workspace import Workspace


def read_view_span(text, old):
    """Map one literal read_file-view occurrence back to original characters."""
    view = text.replace("\r\n", "\n").replace("\r", "\n")
    needle = old.replace("\r\n", "\n").replace("\r", "\n")
    count = view.count(needle)
    if count != 1:
        return None, count
    start = view.index(needle)
    boundaries, index = [0], 0
    while index < len(text):
        index += 2 if text[index:index + 2] == "\r\n" else 1
        boundaries.append(index)
    return (boundaries[start], boundaries[start + len(needle)]), count


def replacement_newlines(text, replacement):
    # Preserve the matched region's convention. Mixed regions keep the caller's
    # explicit representation; bytes outside the region are never normalized.
    remainder = text.replace("\r\n", "")
    styles = (["\r\n"] if "\r\n" in text else []) + (["\r"] if "\r" in remainder else []) + (["\n"] if "\n" in remainder else [])
    if len(styles) == 1:
        return replacement.replace("\r\n", "\n").replace("\r", "\n").replace("\n", styles[0])
    return replacement


class PatchFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: StrictStr
    old_text: StrictStr = Field(min_length=1, max_length=256000)
    new_text: StrictStr = Field(max_length=256000)


class PatchFileTool(Tool):
    name = "patch_file"
    description = ("Replace exactly one literal UTF-8 text occurrence in an existing workspace file. "
                   "Read first; accepts read_file's normalized newline view as well as exact text. "
                   "Missing or ambiguous old_text is rejected. Preserves bytes outside the match and its newline convention. "
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
        count = text.count(kwargs["old_text"])
        span = None
        if count == 1:
            start = text.index(kwargs["old_text"])
            span = (start, start + len(kwargs["old_text"]))
        elif count == 0:
            span, count = read_view_span(text, kwargs["old_text"])
        diagnostic = {"path": kwargs["path"], "source_sha256": hashlib.sha256(original).hexdigest(), "matches": count}
        if span is None:
            return ToolResult(success=False, error="old_text must match exactly once; read the file and provide unique text",
                output={**diagnostic, "code": "patch.match_count",
                        "next_action": "Use a unique literal from the current read; do not repeat unchanged failed parameters on this source version."})
        start, end = span
        replacement = replacement_newlines(text[start:end], kwargs["new_text"])
        updated = text[:start] + replacement + text[end:]
        if len(updated.encode("utf-8")) > 256000:
            return ToolResult(success=False, error="Patched file exceeds the 256 KB limit")
        if updated == text:
            return ToolResult(success=False, error="Patch makes no change", output={**diagnostic, "code": "patch.no_change"})
        result = await WriteFileTool(self.workspace).run(path=kwargs["path"], content=updated, overwrite=True)
        result.output["previous_sha256"] = hashlib.sha256(original).hexdigest()
        return result
