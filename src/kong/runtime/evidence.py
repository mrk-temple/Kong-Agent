"""Bounded local artifact fingerprints and conservative effect invalidation.

These hashes describe local bytes at observation time, not remote systems or
semantic correctness. Unavailable fingerprints fail closed.
"""
import hashlib
import json
import os
from pathlib import Path


def criteria(state):
    return list(state.success_criteria) + (state.plan.success_criteria if state.plan else [])


def targets(criterion, state):
    # Explicit scope is best; preserve old criteria by fingerprinting the workspace.
    return criterion.artifact_paths or ["."]


def needs_versions(criterion):
    if criterion.evidence_scope == "event":
        return False
    if criterion.artifact_paths or criterion.after_last_write:
        return True
    return (criterion.kind in {"evidence", "tool_success", "tool_result"}
            and criterion.tool_name not in {"browser", "mcp", "terminal"})


def fingerprint(workspace, value):
    try:
        path = workspace.resolve(value)
        if not path.exists():
            return "missing"
        if path.is_file():
            paths = [path]
        elif path.is_dir():
            paths = []
            def unreadable(error):
                raise error
            for root, directories, files in os.walk(path, followlinks=False, onerror=unreadable):
                allowed = []
                for name in directories:
                    child = Path(root) / name
                    try:
                        workspace.resolve(str(child))
                    except ValueError:
                        continue
                    if child.is_symlink() or child.is_junction():
                        return None
                    allowed.append(name)
                directories[:] = allowed
                for name in files:
                    child = Path(root) / name
                    try:
                        workspace.resolve(str(child))
                    except ValueError:
                        continue
                    paths.append(child)
                if len(paths) > 10000:
                    return None
        else:
            return None
        records, total = [], 0
        for child in sorted(paths):
            if child.is_symlink():
                return None
            before = child.stat()
            total += before.st_size
            if total > 100_000_000:
                return None
            digest = hashlib.sha256(child.read_bytes()).hexdigest()
            after = child.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return None
            records.append((child.relative_to(workspace.root).as_posix(), digest))
        return hashlib.sha256(json.dumps(records, ensure_ascii=False).encode()).hexdigest()
    except (OSError, ValueError):
        return None


def capture_versions(workspace, state, action=None):
    paths = {path for criterion in criteria(state) for path in targets(criterion, state)
             if needs_versions(criterion) and (action is None or action.name not in {"browser", "mcp", "terminal"}
                                              or criterion.artifact_paths)}
    return {path: fingerprint(workspace, path) for path in sorted(paths)}


def action_effect(action, registry):
    if action.name in {"process_exec", "terminal", "mcp", "browser"}:
        # Even a terminal read can observe a still-running writer. Browser readback
        # is the fresh external observation; it cannot establish future state.
        if action.name == "browser" and action.arguments.get("operation") in {"snapshot", "screenshot"}:
            return "read"
        if action.name == "mcp" and action.arguments.get("operation") in {"list_servers", "list_tools"}:
            return "read"
        return "unknown"
    if action.name not in registry.names():
        return "unknown"
    return "read" if registry.get(action.name).read_only else "mutation"


def fresh(criterion, history, index, workspace, state):
    observation = history.observations[index]
    completed = {o.action_id for o in history.observations}
    evidence_event = next((i for i, e in enumerate(history.events) if e.type == "observation"
                           and e.payload.get("observation", {}).get("action_id") == observation.action_id), -1)
    if any(e.type == "action_intent" and e.payload.get("action_id") not in completed
           for e in history.events[evidence_event + 1:]):
        return False
    if any(o.effect != "read" for o in history.observations[index + 1:]):
        return False
    if observation.action.name in {"browser", "mcp", "terminal"}:
        operation = observation.action.arguments.get("operation")
        if observation.action.name == "browser":
            readback = operation == "snapshot" and observation.effect == "read"
        elif observation.action.name == "terminal":
            readback = (operation == "read" and isinstance(observation.output, dict)
                        and observation.output.get("running") is False
                        and type(observation.output.get("exit_code")) is int)
        else:
            # Generic MCP calls lack a trusted resource-specific readback adapter.
            readback = False
        if not readback:
            return False  # Local hashes cannot turn a remote write receipt into readback.
        if not criterion.artifact_paths:
            return True
        # Explicit local artifacts require version checks in addition to readback.
    if not needs_versions(criterion):
        return False
    if observation.evidence_version != 2:
        return False  # Legacy records have no version binding; recheck explicitly.
    for path in targets(criterion, state):
        saved = observation.artifact_versions.get(path)
        if saved is None or saved != fingerprint(workspace, path):
            return False
        if observation.artifact_versions_before.get(path) != saved:
            return False  # A concurrent change during even a read prevents stable version binding.
    return True
