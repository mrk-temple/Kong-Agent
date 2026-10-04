"""Bounded local artifact fingerprints and conservative effect invalidation.

These hashes describe local bytes at observation time, not remote systems or
semantic correctness. Unavailable fingerprints fail closed.
"""
import hashlib
import json
import os
from contextvars import ContextVar
from functools import wraps
from inspect import iscoroutinefunction, signature
from pathlib import Path
from kong.runtime.metrics import attributed, count, last_count


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


_verification = ContextVar("kong_sync_verification", default=None)


def record_digest(workspace, path, data):
    records = [(path.relative_to(workspace.root).as_posix(), hashlib.sha256(data).hexdigest())]
    return hashlib.sha256(json.dumps(records, ensure_ascii=False).encode()).hexdigest()


def identity(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@attributed("evidence_hash_seconds")
def _scan(workspace, path):
    count("hash_calls")
    try:
        if not path.exists():
            return {"hash":"missing", "kind":"missing", "leaves":{}, "directories":{}}
        directories_seen = {}
        if path.is_file():
            paths = [path]
            kind = "file"
        elif path.is_dir():
            kind = "directory"
            paths = []
            def unreadable(error):
                raise error
            for root, directories, files in os.walk(path, followlinks=False, onerror=unreadable):
                directories_seen[Path(root)] = identity(Path(root).stat())
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
        records, total, leaves = [], 0, {}
        for child in sorted(paths):
            if child.is_symlink():
                return None
            before = child.stat()
            total += before.st_size
            if total > 100_000_000:
                return None
            with child.open("rb") as stream:
                data = stream.read(before.st_size + 1)
            count("hashed_file_count")
            count("hashed_bytes", len(data))
            digest = hashlib.sha256(data).hexdigest()
            after = child.stat()
            if len(data) != before.st_size or identity(before) != identity(after):
                return None
            records.append((child.relative_to(workspace.root).as_posix(), digest))
            leaves[child] = (record_digest(workspace, child, data), identity(after))
        return {"hash":hashlib.sha256(json.dumps(records, ensure_ascii=False).encode()).hexdigest(),
                "kind":kind, "leaves":leaves, "directories":directories_seen}
    except (OSError, ValueError):
        return None


class VerificationPass:
    """Ephemeral synchronous memo; the final sample always reads actual bytes."""
    def __init__(self, workspace):
        self.workspace, self.entries, self.requested = workspace, {}, set()
        self.invalid = False

    def sample(self, path):
        if len(self.requested) >= 20000 or len(self.entries) >= 20000:
            self.invalid = True
            return None
        self.requested.add(path)
        if path in self.entries:
            count("hash_cache_hits")
            return self.entries[path]["hash"]
        sample = _scan(self.workspace, path)
        if sample is None:
            self.invalid = True
            return None
        self.entries[path] = sample
        # A directory scan has already sampled each contained file's actual bytes.
        for child, value in sample["leaves"].items():
            if len(self.entries) >= 20000:
                self.invalid = True
                break
            if child not in self.entries:
                self.entries[child] = {"hash":value[0], "kind":"file", "leaves":{child:value}, "directories":{}}
        return sample["hash"]

    def semantic_sample(self, path, data):
        if len(self.requested) >= 20000 or len(self.entries) >= 20000:
            self.invalid = True
            return
        self.requested.add(path)
        value = record_digest(self.workspace, path, data)
        if path in self.entries:
            if self.entries[path]["hash"] != value:
                self.invalid = True
        else:
            self.entries[path] = {"hash":value, "kind":"file",
                                  "leaves":{path:(value, identity(path.stat()))}, "directories":{}}

    def stable(self):
        # Revalidate minimal requested roots; explicit child checks share their root.
        roots = []
        for path in sorted(self.requested, key=lambda p: (len(p.parts), str(p))):
            if not any(self.entries.get(root, {}).get("kind") == "directory" and path.is_relative_to(root) for root in roots):
                roots.append(path)
        for path in roots:
            count("hash_validation_reads")
            sample = _scan(self.workspace, path)
            if sample is None or sample != self.entries.get(path):
                self.invalid = True
        return not self.invalid


def verified_pass(result_kind, workspace_arg=0):
    """Only synchronous validator entry points may own a memo lifetime."""
    def decorate(function):
        if iscoroutinefunction(function):
            raise TypeError("Verification memo cannot span an asynchronous function")
        workspace_name = list(signature(function).parameters)[workspace_arg]
        @wraps(function)
        def call(*args, **kwargs):
            candidate = args[workspace_arg] if len(args) > workspace_arg else kwargs[workspace_name]
            workspace = getattr(candidate, "workspace", candidate)
            active = _verification.get()
            if active is not None and active.workspace is workspace:
                return function(*args, **kwargs)
            current = VerificationPass(workspace)
            token = _verification.set(current)
            try:
                result = function(*args, **kwargs)
                stable = current.stable()
            finally:
                _verification.reset(token)
            if stable:
                return result
            problem = "Verification files changed or were unreadable during validation; obtain current read-only evidence."
            if result_kind == "problems":
                return result + [problem]
            if result_kind == "rows":
                for row in result:
                    if row["kind"] not in {"binding", "completion"}:
                        row["verified"] = False
                        row.setdefault("issues", []).append({"code":"verification.changed_during_read"})
                return result + [{"scope":"runtime", "id":"verification:stability", "kind":"version", "verified":False,
                                  "description":problem, "evidence_ids":[], "issues":[{"code":"verification.changed_during_read"}]}]
            result.update(satisfied=[], current_valid_observations=[], criterion_observations={},
                          artifact_mutation_required=None, additional_tool_required=True,
                          remaining_steps=[{"kind":"ask", "reason":problem}], next_action=problem)
            result.setdefault("unsatisfied", []).append("verification:stability")
            result.setdefault("reasons", []).append(problem)
            return result
        return call
    return decorate


def register_semantic_read(workspace, path, data):
    count("semantic_file_reads")
    count("semantic_bytes", len(data))
    active = _verification.get()
    if active is not None and active.workspace is workspace:
        active.semantic_sample(path, data)


def fingerprint(workspace, value):
    count("hash_requests")
    try:
        path = workspace.resolve(value)
        active = _verification.get()
        if active is not None and active.workspace is workspace:
            return active.sample(path)
        sample = _scan(workspace, path)
        return sample["hash"] if sample is not None else None
    except (OSError, ValueError):
        return None


def capture_versions(workspace, state, action=None):
    paths = {path for criterion in criteria(state) for path in targets(criterion, state)
             if needs_versions(criterion) and (action is None or action.name not in {"browser", "mcp", "terminal"}
                                              or criterion.artifact_paths)}
    if state.completion_contract:
        paths.update(state.completion_contract.version_paths())
    last_count("artifact_count", len(paths))
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
