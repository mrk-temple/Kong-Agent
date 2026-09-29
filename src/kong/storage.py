"""Atomic snapshots keep control state and audit history as separate contracts."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal

from pydantic import Field

from kong.contracts import Contract, History, RunState, RunMetrics


class Snapshot(Contract):
    schema_version: int = 1
    workspace: str
    state: RunState
    history: History = Field(default_factory=History)
    pending_action_id: str | None = None
    driver: Literal["compatible", "demo"] = "compatible"
    thread_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    project_id: str | None = None
    metrics: RunMetrics | None = None


class RunStore:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid run ID")
        return self.directory / f"{run_id}.json"

    def save(self, snapshot: Snapshot) -> None:
        target = self.path(snapshot.state.run_id)
        descriptor, temp_name = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(snapshot.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def load(self, run_id: str) -> Snapshot:
        snapshot = Snapshot.model_validate_json(self.path(run_id).read_text(encoding="utf-8"))
        if snapshot.schema_version != 1 or snapshot.state.run_id != run_id:
            raise ValueError("Unsupported snapshot version or mismatched run ID")
        return snapshot

    def save_report(self, run_id: str, report: dict) -> Path:
        target = self.report_path(run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return target

    def report_path(self, run_id: str) -> Path:
        return self.directory.parent / "reports" / self.path(run_id).name

    def list_runs(self) -> list[dict]:
        items = []
        for path in sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))["state"]
                items.append({key: state[key] for key in ("run_id", "goal", "mode", "status", "turn_count")})
            except (OSError, ValueError, KeyError):
                continue
        return items

    @contextmanager
    def lock(self, run_id: str):
        """OS lock releases on process death; lock files are intentionally retained."""
        path = self.path(run_id).with_suffix(".lock")
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("This run is already open in another process") from None
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
