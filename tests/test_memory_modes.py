import asyncio
import json

import pytest

from kong.continuity.engine import ContextEngine
from kong.continuity.memory_contracts import ContinuityPolicy
from kong.contracts import FinalResponse
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore, Snapshot
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def start(path, goal, mode="M1", thread_id=None):
    workspace = Workspace(path)
    runtime = Runtime.new(goal, ScriptedModel([FinalResponse(content="ok")]), workspace,
                          default_registry(workspace), store=RunStore(path / ".kong" / "runs"),
                          thread_id=thread_id, memory_mode=mode)
    asyncio.run(runtime.run())
    return runtime


def test_m0_has_no_derivatives_proposals_or_manifest_but_keeps_raw_sources(tmp_path):
    first = start(tmp_path, "以后叫我c", "M0")
    engine = first.context_engine
    assert not engine.store.state(first.thread.thread_id).items
    assert not engine.store.memories() and not engine.store.episodes(first.thread.thread_id)
    assert not engine.last_packet.thread_memory and not engine.last_packet.episodes
    assert "MemoryDelta" not in engine.last_packet.messages[0]["content"]
    assert not any(i.category == "memory_manifest" for i in engine.last_packet.items)
    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(first.memory_command("add", kind="fact", key="name", text="c"))
    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(engine.rebuild())
    second = start(tmp_path, "你好", "M0", first.thread.thread_id)
    recalled = asyncio.run(second.registry.get("recall_history").run(first.state.run_id, event_index=0))
    assert recalled.success and "以后叫我c" in recalled.output["content"]
    assert second.context_engine.last_packet.recent_conversation


def test_m0_cannot_inject_existing_memory_or_derive_prior_unprocessed_runs(tmp_path):
    first = start(tmp_path, "以后叫我c")
    store = first.context_engine.store
    state_before = store.state(first.thread.thread_id).model_dump_json()
    records_before = [r.model_dump_json() for r in store.memories()]
    episodes_before = [e.model_dump_json() for e in store.episodes(first.thread.thread_id)]
    second = start(tmp_path, "以后叫我d", "M0", first.thread.thread_id)
    packet = second.context_engine.last_packet
    assert not packet.thread_memory and not packet.recalled_memories and not packet.episodes
    assert store.state(first.thread.thread_id).model_dump_json() == state_before
    assert [r.model_dump_json() for r in store.memories()] == records_before
    assert [e.model_dump_json() for e in store.episodes(first.thread.thread_id)] == episodes_before


@pytest.mark.parametrize("mode", ["M0", "M2"])
def test_nondefault_policy_survives_restart_without_changing_raw_snapshot(tmp_path, mode):
    first = start(tmp_path, "你好", mode)
    path = first.store.path(first.state.run_id)
    before = path.read_bytes()
    snapshot = first.store.load(first.state.run_id)
    assert snapshot.schema_version == 5 and snapshot.continuity_policy.memory_mode == mode
    restored = Runtime.restore(snapshot, ScriptedModel([]), first.workspace,
                               default_registry(first.workspace), store=first.store)
    assert restored.context_engine.policy.memory_mode == mode
    assert restored.state == snapshot.state and restored.history == snapshot.history
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="preserve"):
        Runtime.restore(snapshot, ScriptedModel([]), first.workspace, default_registry(first.workspace),
                        store=first.store, memory_mode="M1")


def test_default_schema4_and_old_snapshot_remain_compatible(tmp_path):
    runtime = start(tmp_path, "你好")
    raw = json.loads(runtime.store.path(runtime.state.run_id).read_text(encoding="utf-8"))
    assert raw["schema_version"] == 4 and "continuity_policy" not in raw
    for version in (1, 2, 3, 4):
        older = {**raw, "schema_version": version}
        assert Snapshot.model_validate(older).continuity_policy is None
    with pytest.raises(ValueError):
        Snapshot.model_validate({**raw, "schema_version": 5})
    with pytest.raises(ValueError):
        Runtime.restore(runtime.snapshot(), ScriptedModel([]), runtime.workspace,
                        default_registry(runtime.workspace), store=runtime.store,
                        context_engine=ContextEngine(policy=ContinuityPolicy(memory_mode="M0")))


def test_m2_filters_unrelated_facts_and_audits_without_changing_m1(tmp_path):
    first = start(tmp_path, "record weather data")
    asyncio.run(first.memory_command("add", kind="fact", key="weather.sun", text="weather.sun is sunny"))
    m1 = start(tmp_path, "invoice revenue summary", "M1", first.thread.thread_id)
    m2 = start(tmp_path, "invoice revenue summary", "M2", first.thread.thread_id)
    assert any("weather.sun" in r["content"] for r in m1.context_engine.last_packet.thread_memory)
    assert not any("weather.sun" in r["content"] for r in m2.context_engine.last_packet.thread_memory)
    diagnostic = next(d for d in reversed(m2.context_engine.diagnostics) if d["kind"] == "memory_selection")
    assert any(d["reason"] == "query_unrelated" and d["sources"] for d in diagnostic["candidates"])
    assert m2.state.completion_scope == m1.state.completion_scope


def test_m2_preserves_preferences_and_user_corrections(tmp_path):
    first = start(tmp_path, "以后叫我c", "M2")
    second = start(tmp_path, "以后叫我d", "M2", first.thread.thread_id)
    third = start(tmp_path, "hello", "M2")
    assert [r["content"] for r in third.context_engine.last_packet.recalled_memories] == ["以后叫我d"]
    assert second.context_engine.store.state(first.thread.thread_id).items[-1].content == "以后叫我d"


def test_legacy_renderers_cannot_silently_bypass_experimental_memory_modes(tmp_path):
    from kong.context import ContextBuilder
    workspace = Workspace(tmp_path)
    for mode in ("M0", "M2"):
        with pytest.raises(ValueError, match="standard ContextEngine"):
            ContextEngine(builder=ContextBuilder(), policy=ContinuityPolicy(memory_mode=mode))
        with pytest.raises(ValueError, match="standard ContextEngine"):
            Runtime.new("no effects", ScriptedModel([]), workspace, default_registry(workspace),
                        context_builder=ContextBuilder(), memory_mode=mode)
