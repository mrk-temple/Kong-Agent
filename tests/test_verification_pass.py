"""I-05/I-10: hash reuse is synchronous, revalidated, and fails closed."""
import asyncio
import os
from pathlib import Path

import pytest

from kong.contracts import Act, Action, Criterion, FinalResponse, RunMetrics
from kong.models.fake import ScriptedModel
from kong.runtime.gates import CompletionGate
from kong.runtime.loop import Runtime
from kong.runtime.metrics import metrics_scope
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def setup_run(tmp_path):
    (tmp_path / "target").write_text("good")
    for index in range(25):
        (tmp_path / f"noise{index}").write_text("irrelevant")
    ws = Workspace(tmp_path)
    rt = Runtime.new("read target", ScriptedModel([]), ws, default_registry(ws), completion_scope="mechanical")
    rt.state.success_criteria = [Criterion(id=f"read{i}", description="current read", kind="tool_success", tool_name="read_file") for i in range(4)]
    asyncio.run(rt._actions(Act(actions=[Action(name="read_file", arguments={"path":"target"})])))
    return rt, CompletionGate(ws)


def test_same_workspace_is_hashed_twice_per_pass_not_per_criterion(tmp_path):
    rt, gate = setup_run(tmp_path)
    metrics = RunMetrics()
    with metrics_scope(metrics):
        assert gate.check(rt.state, FinalResponse(content="done", evidence_ids=["a1"]), rt.history) == []
    assert metrics.hashed_file_count == 2 * 26
    assert metrics.hash_cache_hits >= 3
    assert metrics.hash_validation_reads == 1


@pytest.mark.parametrize("change", ["same_metadata", "replace", "directory_add", "directory_remove"])
def test_final_byte_or_topology_change_rejects_cached_evidence(tmp_path, monkeypatch, change):
    rt, gate = setup_run(tmp_path)
    (tmp_path / "empty").mkdir()
    # Recapture after creating the directory, so only the validation change matters.
    asyncio.run(rt._actions(Act(actions=[Action(name="read_file", arguments={"path":"target"})])))
    from kong.runtime import gates
    original = gates.fresh
    count = 0
    def changing(*args):
        nonlocal count
        valid = original(*args)
        count += 1
        if count == 1:
            path = tmp_path / "target"
            stat = path.stat()
            if change == "same_metadata":
                path.write_text("evil")
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            elif change == "replace":
                path.unlink()
                path.write_text("good")
            elif change == "directory_add":
                (tmp_path / "new_empty").mkdir()
            else:
                (tmp_path / "empty").rmdir()
        return valid
    monkeypatch.setattr(gates, "fresh", changing)
    assert gate.check(rt.state, FinalResponse(content="done", evidence_ids=["a2"]), rt.history)


def test_next_pass_and_restore_recompute_actual_bytes(tmp_path):
    rt, gate = setup_run(tmp_path)
    assert not gate.check(rt.state, FinalResponse(content="done", evidence_ids=["a1"]), rt.history)
    path = tmp_path / "target"
    stat = path.stat()
    path.write_text("evil")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert gate.check(rt.state, FinalResponse(content="done", evidence_ids=["a1"]), rt.history)
    from kong.storage import Snapshot
    snapshot = Snapshot(workspace=str(tmp_path), state=rt.state, history=rt.history)
    restored = Runtime.restore(snapshot, rt.model, rt.workspace, rt.registry)
    assert restored.controller.completion_gate.check(restored.state, FinalResponse(content="done", evidence_ids=["a1"]), restored.history)


def test_each_pass_rereads_bytes_and_no_async_memo_is_allowed(tmp_path):
    rt, gate = setup_run(tmp_path)
    metrics = RunMetrics()
    with metrics_scope(metrics):
        for _ in range(2):
            assert not gate.check(rt.state, FinalResponse(content="done", evidence_ids=["a1"]), rt.history)
    assert metrics.hashed_file_count == 4 * 26
    from kong.runtime.evidence import verified_pass
    with pytest.raises(TypeError, match="asynchronous"):
        @verified_pass("rows")
        async def invalid(workspace):
            return []


def test_semantic_read_change_cannot_hide_by_restoring_bytes(tmp_path, monkeypatch):
    rt, gate = setup_run(tmp_path)
    rt.state.success_criteria.append(Criterion(id="value", description="value", kind="file_equals", path="target", expected_text="evil"))
    from kong.runtime import gates
    original = gates.fresh
    calls = 0
    ready = False
    initial = (tmp_path / "target").stat()
    def changing(*args):
        nonlocal calls, ready
        valid = original(*args)
        calls += 1
        if calls == 4:
            (tmp_path / "target").write_text("evil")
            os.utime(tmp_path / "target", ns=(initial.st_atime_ns, initial.st_mtime_ns))
            ready = True
        return valid
    monkeypatch.setattr(gates, "fresh", changing)
    original_open = Path.open
    def open_restore(self, *args, **kwargs):
        nonlocal ready
        if ready and self == tmp_path / "target" and args and args[0] == "rb":
            import io
            with original_open(self, "rb") as stream:
                data = stream.read()
            self.write_text("good")
            os.utime(self, ns=(initial.st_atime_ns, initial.st_mtime_ns))
            ready = False
            return io.BytesIO(data)
        return original_open(self, *args, **kwargs)
    # Open-based legacy semantic sampling receives evil while the final bytes revert.
    monkeypatch.setattr(Path, "open", open_restore)
    assert gate.check(rt.state, FinalResponse(content="done", evidence_ids=["a1"]), rt.history)
