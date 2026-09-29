import sys
import os
from pathlib import Path
import shutil
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from kong.environments.process import execute_process, process_environment
from kong.tools.base import Tool, ToolResult
from kong.workspace import Workspace


class ProcessArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[StrictStr] = Field(min_length=1, max_length=128,
        description="Executable and separate arguments; no implicit shell. Use python for Kong's Python interpreter.")
    cwd: StrictStr = Field(default=".", description="Existing directory within the workspace.")
    timeout_seconds: float = Field(default=30, ge=0.1, le=300, allow_inf_nan=False)
    max_output_bytes: int = Field(default=16000, ge=256, le=64000, strict=True,
        description="Maximum retained bytes per stdout/stderr stream. Excess is discarded and marked.")
    encoding: Literal["utf-8", "gb18030"] = "utf-8"

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value):
        if not value[0].strip() or any("\0" in item for item in value) or sum(map(len, value)) > 24000:
            raise ValueError("argv requires an executable, no NUL bytes, and at most 24000 characters")
        return value


class ProcessExecTool(Tool):
    name = "process_exec"
    description = ("Run a noninteractive local command and wait for exit (max 300s). "
        "argv is an argument array; use ['python', 'script.py'] for Kong's Python. "
        "No persistent shell or background jobs: descendants are cleaned up on exit/timeout/cancel. "
        "Runs with the user's OS privileges, NOT a workspace sandbox. "
        "Respect user scope and reserved paths; do not use to bypass file tool restrictions. "
        "Exit zero is execution evidence, not proof of correct artifacts; verify resulting files. "
        "Output is bounded; write large required results to workspace files and inspect them.")
    args_model = ProcessArgs
    family = "process"
    effects = ("process_exec", "local_write", "network_possible")
    environment = "local_process"
    execution_timeout = 315

    def __init__(self, workspace: Workspace, *, enabled: bool = False):
        self.workspace = workspace
        self.enabled = enabled

    async def run(self, **kwargs: Any) -> ToolResult:
        if not self.enabled:
            return ToolResult(success=False, error="Local process execution is disabled; enable --allow-process at launch.")
        cwd = self.workspace.resolve(kwargs["cwd"])
        if not cwd.is_dir():
            return ToolResult(success=False, error="cwd must be an existing workspace directory")
        argv = list(kwargs["argv"])
        if argv[0] == "python":
            argv[0] = sys.executable
        if os.name == "nt":
            candidate = Path(argv[0])
            explicit_path = candidate.is_absolute() or "/" in argv[0] or "\\" in argv[0]
            resolved = shutil.which(str(cwd / candidate))
            if not resolved and not explicit_path:
                resolved = shutil.which(argv[0], path=process_environment().get("PATH", ""))
            if not resolved:
                return ToolResult(success=False, error="Executable not found on PATH or at the requested path")
            argv[0] = str(Path(resolved).resolve())
            if Path(argv[0]).suffix.casefold() in {".bat", ".cmd"}:
                return ToolResult(success=False, error="Batch files require an explicit command interpreter in argv; no implicit shell.")
        result = await execute_process(argv, cwd, timeout_seconds=kwargs["timeout_seconds"],
            max_output_bytes=kwargs["max_output_bytes"], encoding=kwargs["encoding"])
        result.output["cwd"] = str(cwd.relative_to(self.workspace.root))
        return result
