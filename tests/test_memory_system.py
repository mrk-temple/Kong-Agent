import asyncio

import pytest

from kong.continuity.memory import source_for
from kong.continuity.memory_contracts import MemoryDelta, MemoryMutation, MemoryScope, MemoryStatus
from kong.continuity.memory_store import MemoryStore
from kong.continuity.retrieval import SQLiteMemoryRetriever
from kong.contracts import FinalResponse
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def run(tmp_path, goal, thread_id=None, shared=None, project=None):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new(goal, ScriptedModel([FinalResponse(content="ok")]), workspace, default_registry(workspace),
                          store=RunStore(tmp_path / ".kong" / "runs"), thread_id=thread_id,
                          memory_store=shared, project_id=project)
    asyncio.run(runtime.run())
    return runtime


def test_identity_is_thread_only_and_persists_after_recent_window(tmp_path):
    first = run(tmp_path, "你好我是c")
    for index in range(7):
        run(tmp_path, f"filler {index}", first.thread.thread_id)
    final = run(tmp_path, "我前面怎么称呼自己的", first.thread.thread_id)
    packet = final.context_engine.last_packet
    assert "你好我是c" not in [m.content for m in packet.recent_conversation]
    assert any(i["key"] == "user.self_identification" and "c" in i["content"] for i in packet.thread_memory)
    assert final.context_engine.store.memories() == []
    other = run(tmp_path, "我是谁")
    assert not other.context_engine.last_packet.thread_memory


def test_preference_auto_promotes_and_recalls_across_threads(tmp_path):
    first = run(tmp_path, "以后叫我c")
    records = first.context_engine.store.memories()
    assert len(records) == 1 and records[0].status == MemoryStatus.ACTIVE
    assert records[0].confirmed_by is None
    assert all(s.kind == "event" and s.event_id for s in records[0].sources)
    second = run(tmp_path, "你好")
    assert second.thread.thread_id != first.thread.thread_id
    assert any(r["content"] == "以后叫我c" for r in second.context_engine.last_packet.recalled_memories)
    assert second.context_engine.store.get_memory(records[0].id).last_accessed_at is not None


def test_user_correction_supersedes_thread_and_long_term(tmp_path):
    first = run(tmp_path, "以后叫我c")
    second = run(tmp_path, "以后叫我d", first.thread.thread_id)
    thread_state = second.context_engine.store.state(first.thread.thread_id)
    assert [i.content for i in thread_state.items if i.status == MemoryStatus.ACTIVE] == ["以后叫我d"]
    assert thread_state.items[0].status == MemoryStatus.SUPERSEDED
    assert thread_state.items[-1].supersedes == thread_state.items[0].id
    records = second.context_engine.store.memories()
    assert [r.content for r in records if r.status == MemoryStatus.ACTIVE] == ["以后叫我d"]
    third = run(tmp_path, "hello")
    assert [r["content"] for r in third.context_engine.last_packet.recalled_memories] == ["以后叫我d"]


def test_decision_requires_exact_confirmation_without_approving_plan(tmp_path):
    runtime = run(tmp_path, "决定：项目使用 SQLite")
    engine = runtime.context_engine
    entry = engine.store.state(runtime.thread.thread_id).items[0]
    assert entry.status == MemoryStatus.CANDIDATE
    assert engine.store.memories()[0].status == MemoryStatus.CANDIDATE
    before = runtime.state.model_dump_json()
    asyncio.run(runtime.memory_command("confirm", item_id=entry.id))
    assert runtime.state.model_dump_json() == before
    confirmed = engine.store.state(runtime.thread.thread_id).items[0]
    assert confirmed.confirmed_by and confirmed.status == MemoryStatus.ACTIVE
    record = engine.store.memories()[0]
    assert record.status == MemoryStatus.ACTIVE and record.confirmed_by
    assert engine.retriever.retrieve("SQLite", runtime.thread)[0].id == record.id
    with pytest.raises(ValueError):
        engine.manager.confirm(entry.id, entry.sources[0], (runtime.state.run_id, runtime.history))


def test_reject_candidate_and_forget_do_not_resurrect_on_commit(tmp_path):
    runtime = run(tmp_path, "以后叫我c")
    record = runtime.context_engine.store.memories()[0]
    asyncio.run(runtime.memory_command("forget", item_id=record.id))
    asyncio.run(runtime.context_engine.commit(state=runtime.state, history=runtime.history))
    assert runtime.context_engine.store.get_memory(record.id).status == MemoryStatus.REJECTED
    assert not runtime.context_engine.retriever.retrieve("c", runtime.thread)
    with pytest.raises(ValueError):
        asyncio.run(runtime.memory_command("confirm", item_id=record.id))


def test_delta_idempotence_revision_and_batch_atomicity(tmp_path):
    runtime = run(tmp_path, "source statement")
    engine = runtime.context_engine
    source = source_for(runtime.thread.thread_id, runtime.state.run_id, 0)
    state = engine.store.state(runtime.thread.thread_id)
    mutation = MemoryMutation(key="project.database", kind="fact", content="SQLite", sources=[source])
    delta = MemoryDelta(thread_id=runtime.thread.thread_id, base_revision=state.revision, mutations=[mutation])
    result = engine.manager.merge(delta)
    assert engine.manager.merge(delta) == result
    assert result.revision == state.revision + 1
    with pytest.raises(ValueError, match="Stale"):
        engine.manager.merge(MemoryDelta(thread_id=delta.thread_id, base_revision=state.revision, mutations=[mutation]))
    bad_source = source.model_copy(update={"event_index": 999, "event_id": f"{runtime.state.run_id}:event:999"})
    with pytest.raises(ValueError):
        engine.manager.merge(MemoryDelta(thread_id=delta.thread_id, base_revision=result.revision,
            mutations=[mutation, MemoryMutation(key="bad", content="bad", sources=[bad_source])]))
    assert engine.store.state(delta.thread_id) == result


@pytest.mark.parametrize("operation", ["update", "supersede", "resolve", "reject"])
def test_mutation_operations_preserve_sources(tmp_path, operation):
    runtime = run(tmp_path, "你好我是c")
    engine = runtime.context_engine
    state = engine.store.state(runtime.thread.thread_id)
    old = state.items[0]
    source = old.sources[0]
    delta = MemoryDelta(thread_id=state.thread_id, base_revision=state.revision,
        mutations=[MemoryMutation(operation=operation, target_id=old.id, key=old.key, kind=old.kind,
                                  content="用户现在自称 d", sources=[source])])
    result = engine.manager.merge(delta)
    assert result.items[0].status == {"resolve": MemoryStatus.RESOLVED, "reject": MemoryStatus.REJECTED}.get(operation, MemoryStatus.SUPERSEDED)
    assert all(i.sources for i in result.items)


def test_scope_filters_global_workspace_project_and_thread(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    shared = MemoryStore(tmp_path / "global.db")
    first = run(left, "以后叫我c", shared=shared, project="project-a")
    for scope in ["workspace", "project", "thread"]:
        asyncio.run(first.memory_command("add", kind="fact", key="fact." + scope, scope=scope, text="SQLite " + scope))
        entry = first.context_engine.store.state(first.thread.thread_id).items[-1]
        asyncio.run(first.memory_command("confirm", item_id=entry.id))
    other = run(right, "hello", shared=shared, project="project-b")
    assert [r.content for r in other.context_engine.retriever.retrieve("SQLite c", other.thread, "project-b")] == ["以后叫我c"]
    retriever = SQLiteMemoryRetriever([first.context_engine.store, shared])
    same_workspace = run(left, "hello", shared=shared, project="project-a")
    found = retriever.retrieve("SQLite", same_workspace.thread, "project-a")
    assert {r.scope for r in found} == {MemoryScope.GLOBAL, MemoryScope.WORKSPACE, MemoryScope.PROJECT}
    assert MemoryScope.THREAD not in {r.scope for r in found}


def test_chinese_fts_punctuation_and_sql_are_safe(tmp_path):
    runtime = run(tmp_path, "source")
    asyncio.run(runtime.memory_command("add", kind="fact", key="database", scope="workspace", text="项目数据库使用关系模型"))
    entry = runtime.context_engine.store.state(runtime.thread.thread_id).items[-1]
    asyncio.run(runtime.memory_command("confirm", item_id=entry.id))
    found = runtime.context_engine.retriever.retrieve('数据库 " OR * ; DROP TABLE threads; --', runtime.thread)
    assert any("数据库" in r.content for r in found)
    assert runtime.context_engine.store.get(runtime.thread.thread_id)


def test_model_cannot_use_another_threads_source(tmp_path):
    first = run(tmp_path, "first")
    second = run(tmp_path, "second")
    state = second.context_engine.store.state(second.thread.thread_id)
    with pytest.raises(ValueError, match="provenance"):
        second.context_engine.manager.merge(MemoryDelta(thread_id=state.thread_id, base_revision=state.revision,
            mutations=[MemoryMutation(key="stolen", content="private", sources=[source_for(first.thread.thread_id, first.state.run_id, 0)])]))


def test_model_only_source_cannot_establish_user_fact(tmp_path):
    runtime = run(tmp_path, "hello")
    state = runtime.context_engine.store.state(runtime.thread.thread_id)
    with pytest.raises(ValueError, match="user source"):
        runtime.context_engine.manager.merge(MemoryDelta(thread_id=state.thread_id, base_revision=state.revision,
            mutations=[MemoryMutation(key="identity", content="invented", sources=[source_for(state.thread_id, runtime.state.run_id, 1)])]))


def test_rebuild_replays_confirmation_correction_and_rejection_without_model(tmp_path):
    first = run(tmp_path, "以后叫我c")
    second = run(tmp_path, "以后叫我d", first.thread.thread_id)
    asyncio.run(second.memory_command("add", kind="decision", key="database", scope="workspace", text="Use SQLite"))
    entry = second.context_engine.store.state(first.thread.thread_id).items[-1]
    asyncio.run(second.memory_command("confirm", item_id=entry.id))
    asyncio.run(second.memory_command("add", kind="open_loop", key="question", text="Which filename?"))
    question = second.context_engine.store.state(first.thread.thread_id).items[-1]
    asyncio.run(second.memory_command("resolve", item_id=question.id))
    before = second.context_engine.store.state(first.thread.thread_id)
    before_raw = second.history.model_dump_json()
    before_calls = second.model.calls
    rebuilt = asyncio.run(second.memory_command("rebuild"))
    def semantic(state):
        return {(i.id, i.key, i.content, i.status, bool(i.confirmed_by)) for i in state.items}
    assert semantic(rebuilt) == semantic(before)
    assert rebuilt.revision > before.revision
    assert second.history.model_dump_json() == before_raw
    assert second.model.calls == before_calls
    assert [r.content for r in second.context_engine.store.memories() if r.status == MemoryStatus.ACTIVE] == ["以后叫我d", "Use SQLite"]


def test_local_request_does_not_auto_promote_preference(tmp_path):
    runtime = run(tmp_path, "这次请用英文回复")
    assert not runtime.context_engine.store.memories()


def test_pending_replacement_decision_keeps_old_confirmed_until_approved(tmp_path):
    runtime = run(tmp_path, "source")
    asyncio.run(runtime.memory_command("add", kind="decision", key="database", scope="workspace", text="SQLite"))
    first = runtime.context_engine.store.state(runtime.thread.thread_id).items[-1]
    asyncio.run(runtime.memory_command("confirm", item_id=first.id))
    asyncio.run(runtime.memory_command("add", kind="decision", key="database", scope="workspace", text="Postgres"))
    state = runtime.context_engine.store.state(runtime.thread.thread_id)
    second = state.items[-1]
    assert state.items[0].status == MemoryStatus.ACTIVE and second.status == MemoryStatus.CANDIDATE
    asyncio.run(runtime.memory_command("confirm", item_id=second.id))
    assert [i.content for i in runtime.context_engine.store.state(runtime.thread.thread_id).items if i.status == MemoryStatus.ACTIVE] == ["Postgres"]


def test_expired_memory_not_retrieved(tmp_path):
    runtime = run(tmp_path, "以后叫我c")
    record = runtime.context_engine.store.memories()[0]
    record.expires_at = "2000-01-01T00:00:00+00:00"
    runtime.context_engine.store.save_memory(record)
    assert not runtime.context_engine.retriever.retrieve("c", runtime.thread)


def test_stale_delta_cannot_resurrect_rejected_source(tmp_path):
    runtime = run(tmp_path, "以后叫我c")
    state = runtime.context_engine.store.state(runtime.thread.thread_id)
    old = state.items[0]
    asyncio.run(runtime.memory_command("reject", item_id=old.id))
    state = runtime.context_engine.store.state(runtime.thread.thread_id)
    with pytest.raises(ValueError, match="new user source"):
        runtime.context_engine.manager.merge(MemoryDelta(thread_id=state.thread_id, base_revision=state.revision,
            mutations=[MemoryMutation(key=old.key, kind=old.kind, content="paraphrased stale preference", sources=old.sources)]))


def test_project_scope_survives_restore(tmp_path):
    runtime = run(tmp_path, "hello", project="kong")
    restored = Runtime.restore(runtime.snapshot(), ScriptedModel([]), runtime.workspace, runtime.registry, store=runtime.store)
    assert restored.context_engine.project_id == "kong"
    with pytest.raises(ValueError, match="project scope"):
        Runtime.restore(runtime.snapshot(), ScriptedModel([]), runtime.workspace, runtime.registry, store=runtime.store, project_id="other")


def test_project_specific_preference_is_not_global(tmp_path):
    runtime = run(tmp_path, "我偏好这个项目使用 SQLite")
    record = runtime.context_engine.store.memories()[0]
    assert record.scope == MemoryScope.WORKSPACE and record.status == MemoryStatus.ACTIVE


def test_model_cannot_retype_or_reject_confirmed_decision_to_bypass_confirmation(tmp_path):
    runtime = run(tmp_path, "source")
    asyncio.run(runtime.memory_command("add", kind="decision", key="database", scope="workspace", text="SQLite"))
    entry = runtime.context_engine.store.state(runtime.thread.thread_id).items[0]
    asyncio.run(runtime.memory_command("confirm", item_id=entry.id))
    state = runtime.context_engine.store.state(runtime.thread.thread_id)
    for mutation in [MemoryMutation(key="database", kind="preference", content="Postgres", sources=entry.sources),
                     MemoryMutation(operation="reject", target_id=entry.id, key="database", kind="decision", content="SQLite", sources=entry.sources)]:
        with pytest.raises(ValueError):
            runtime.context_engine.manager.merge(MemoryDelta(thread_id=state.thread_id, base_revision=state.revision, mutations=[mutation]))
    assert runtime.context_engine.store.state(state.thread_id) == state


def test_thread_project_scope_is_inherited_and_cannot_silently_change(tmp_path):
    first = run(tmp_path, "project work", project="project-a")
    second = run(tmp_path, "continue", first.thread.thread_id)
    assert second.context_engine.project_id == "project-a"
    with pytest.raises(ValueError, match="different project"):
        run(tmp_path, "other project", first.thread.thread_id, project="project-b")
