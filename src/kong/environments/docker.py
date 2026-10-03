"""Linux Docker process isolation, with an explicit dedicated resource mount.

The trusted Docker daemon/image remain part of the boundary. No host socket,
credentials, host PID namespace or host network is passed to the workload.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from kong.environments.process import execute_process
from kong.tools.base import ToolResult


class DockerBackend:
    def __init__(self, workspace, root: str, image: str):
        self.workspace = workspace
        self.root = workspace.resolve(root)
        if self.root == workspace.root or not self.root.is_dir():
            raise ValueError("Docker requires an existing dedicated subdirectory as --sandbox-root")
        if "," in str(self.root):
            raise ValueError("Sandbox mount path cannot contain commas")
        self.relative_root = self.root.relative_to(workspace.root).as_posix()
        if not image.strip() or image.startswith("-"):
            raise ValueError("Sandbox image must be an explicit local Docker image")
        self.image = image
        self.image_id = None
        self.writable = True

    async def _docker(self, args, timeout=15, limit=16000):
        executable = shutil.which("docker")
        if not executable:
            return ToolResult(success=False, error="Docker CLI is unavailable; sandbox fails closed")
        return await execute_process([executable, *args], self.workspace.root,
                                     timeout_seconds=timeout, max_output_bytes=limit, encoding="utf-8")

    def validate_mount(self):
        # Avoid accidentally exposing reserved credentials/Runtime files through the mount.
        # Symlinks/junctions are rejected before launch rather than following host targets.
        count = 0
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                count += 1
                self.workspace.resolve(str(path))
                if path.is_symlink() or path.is_junction() or count > 10000:
                    raise ValueError("Sandbox mount contains links or exceeds 10000 entries")

    async def prepare(self):
        self.validate_mount()
        result = await self._docker(["image", "inspect", "--format", "{{json .Id}}", self.image])
        try:
            image_id = json.loads(result.output["stdout"].strip()) if result.success else None
            if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
                raise ValueError()
            self.image_id = image_id
        except (ValueError, TypeError, KeyError):
            raise ValueError("Sandbox image is unavailable locally; no automatic pull or host fallback") from None

    async def execute(self, argv, cwd, *, timeout_seconds, max_output_bytes, encoding):
        cwd = self.workspace.resolve(str(cwd))
        if not cwd.is_relative_to(self.root):
            return ToolResult(success=False, error="Process cwd is outside the sandbox resource root")
        self.validate_mount()
        if self.image_id is None:
            await self.prepare()
        name = "kong-" + uuid4().hex
        mount = f"type=bind,source={self.root},target=/workspace" + ("" if self.writable else ",readonly")
        args = ["create", "--name", name, "--pull", "never", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
                "--pids-limit", "64", "--memory", "512m", "--cpus", "1", "--restart", "no",
                "--no-healthcheck", "--init", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--mount", mount, "--workdir", "/workspace/" + cwd.relative_to(self.root).as_posix(),
                "--entrypoint", argv[0], self.image_id, *argv[1:]]
        # Keep ownership if cancellation races Docker daemon container creation.
        creating = asyncio.create_task(self._docker(args))
        result = None
        try:
            try:
                created = await asyncio.shield(creating)
            except asyncio.CancelledError:
                await creating
                raise
            if not created.success:
                return ToolResult(success=False, error="Docker container creation failed; no host fallback")
            result = await self._docker(["start", "--attach", name], timeout_seconds, max_output_bytes)
            if result.output:
                result.output["backend"] = "docker"
                result.output["container"] = name
                result.output["image_id"] = self.image_id
                # execute_process decodes output as UTF-8; Docker workloads use UTF-8.
                result.output["encoding"] = "utf-8"
            return result
        finally:
            cleaning = asyncio.create_task(self._docker(["rm", "--force", "--volumes", name]))
            try:
                cleaned = await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                cleaned = await cleaning
                raise
            if result is not None and not cleaned.success:
                result.success = False
                result.outcome_unknown = True
                result.error = "Container cleanup is unconfirmed; inspect the named container before continuing"
