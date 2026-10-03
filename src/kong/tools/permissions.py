"""Host-owned operation/resource policy; decisions cannot expand it."""
import json
from pathlib import Path

from pydantic import Field
from kong.contracts import Contract


class PermissionPolicy(Contract):
    allowed_tools: list[str] | None = None
    operations: dict[str, list[str]] = Field(default_factory=dict)
    read_roots: list[str] = Field(default_factory=lambda: ["."])
    write_roots: list[str] = Field(default_factory=lambda: ["."])
    mcp_servers: list[str] | None = None
    mcp_tools: dict[str, list[str]] = Field(default_factory=dict)

    def covers(self, workspace, path: str, *, write: bool = False) -> bool:
        target = workspace.resolve(path)
        roots = self.write_roots if write else self.read_roots
        return any(target.is_relative_to(workspace.resolve(root)) for root in roots)

    def check(self, call, tool) -> str | None:
        if self.allowed_tools is not None and call.name not in self.allowed_tools:
            return "Tool is outside the host permission policy"
        operation = call.arguments.get("operation", "run")
        if call.name in self.operations and operation not in self.operations[call.name]:
            return "Operation is outside the host permission policy"
        if call.name == "mcp" and operation != "servers" and self.mcp_servers is not None:
            if call.arguments.get("server") not in self.mcp_servers:
                return "MCP server is outside the host permission policy"
        if call.name == "mcp" and operation == "call" and call.arguments.get("server") in self.mcp_tools:
            if call.arguments.get("tool") not in self.mcp_tools[call.arguments["server"]]:
                return "MCP tool operation is outside the host permission policy"
        workspace = getattr(tool, "workspace", None)
        file_operation = call.name in {"read_file", "list_dir", "search_files", "write_file", "patch_file"} or (
            call.name == "browser" and operation == "screenshot")
        if file_operation and workspace:
            try:
                write = call.name in {"write_file", "patch_file", "browser"}
                if not self.covers(workspace, call.arguments.get("path", "."), write=write):
                    return "Path is outside the permitted resource roots"
                if call.name == "patch_file" and not self.covers(workspace, call.arguments["path"]):
                    return "Patch requires both read and write authority for the resource"
            except (ValueError, OSError):
                return "Path is outside the permitted resource roots"
        elif file_operation and (self.read_roots != ["."] or self.write_roots != ["."]):
            return "Resource-scoped file execution requires a bound workspace"
        if call.name in {"process_exec", "terminal"} and getattr(tool, "backend", None) is None:
            if self.read_roots != ["."] or self.write_roots != ["."]:
                return "Resource-scoped process execution requires the Docker backend"
        backend = getattr(tool, "backend", None)
        if backend:
            try:
                if not self.covers(workspace, backend.relative_root):
                    return "Sandbox mount is outside the permitted read roots"
                backend.writable = self.covers(workspace, backend.relative_root, write=True)
            except (ValueError, OSError):
                return "Sandbox mount is outside the permitted resource roots"
        return None


class ResourceWorkspace:
    """Per-call view: recursive reads recheck every resolved descendant."""
    def __init__(self, workspace, policy):
        self.original = workspace
        self.root = workspace.root
        self.policy = policy

    def resolve(self, value):
        target = self.original.resolve(value)
        if not self.policy.covers(self.original, str(target)):
            raise ValueError("Path is outside the permitted read roots")
        return target


def load_permissions(path: Path | None) -> PermissionPolicy:
    return PermissionPolicy.model_validate_json(path.read_text(encoding="utf-8-sig")) if path else PermissionPolicy()


def policy_fingerprint(policy) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(policy.model_dump(), sort_keys=True).encode()).hexdigest()
