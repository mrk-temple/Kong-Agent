import asyncio

from kong.continuity.contracts import RecentContextPolicy
from kong.continuity.history import HistorySelector
from kong.contracts import FinalResponse, NeedUserInput
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def new(tmp_path, goal, thread_id=None, model=None, **kwargs):
    workspace = Workspace(tmp_path)
    return Runtime.new(goal, model or ScriptedModel([FinalResponse(content="收到")]), workspace,
                       default_registry(workspace), store=RunStore(tmp_path / ".kong" / "runs"),
                       thread_id=thread_id, **kwargs)


def execute(runtime):
    asyncio.run(runtime.run())
    return runtime


def test_immediate_recall_uses_actual_raw_context(tmp_path):
    first = execute(new(tmp_path, "你好我是c"))
    class ContextReadingModel:
        async def generate(self, packet):
            assert [(m.role, m.content) for m in packet.recent_conversation] == [("user", "你好我是c"), ("assistant", "收到")]
            assert all(m.source.thread_id == first.thread.thread_id and m.source.event_id for m in packet.recent_conversation)
            assert all(m.source.run_id == first.state.run_id for m in packet.recent_conversation)
            assert sum(m["content"].count("我是谁") for m in packet.messages) == 1
            selected = [m for m in packet.messages if "RECENT THREAD CONVERSATION" in m["content"]]
            assert '"role":"user"' in selected[0]["content"]
            assert '"role":"assistant"' in selected[1]["content"]
            return FinalResponse(content="你刚才告诉我你是 c。")
    second = execute(new(tmp_path, "我是谁", first.thread.thread_id, ContextReadingModel()))
    assert second.state.final_output == "你刚才告诉我你是 c。"
    assert first.history.events != second.history.events


def test_multiple_runs_order_isolation_and_restore(tmp_path):
    first = execute(new(tmp_path, "A"))
    execute(new(tmp_path, "B", first.thread.thread_id))
    third = execute(new(tmp_path, "C", first.thread.thread_id))
    messages = third.context_engine.last_packet.recent_conversation
    assert [m.content for m in messages] == ["A", "收到", "B", "收到"]
    other = execute(new(tmp_path, "D"))
    assert other.context_engine.last_packet.recent_conversation == []
    assert not other.context_engine.last_packet.thread_memory
    restored = Runtime.restore(first.snapshot(), ScriptedModel([]), first.workspace, first.registry, store=first.store)
    selected, _ = restored.context_engine.history_selector.select(first.thread, first.state.run_id)
    assert selected == []  # Later Runs cannot enter an earlier Run on restore.
    assert restored.context_engine.last_packet == first.context_engine.last_packet


def test_recent_bound_and_noise_does_not_displace_dialog(tmp_path):
    first = execute(new(tmp_path, "first"))
    for index in range(100):
        first.history.append("feedback", index, text="noise" * 2000)
    first.save()
    second = execute(new(tmp_path, "second", first.thread.thread_id))
    selector = HistorySelector(first.store, first.thread_store, RecentContextPolicy(max_messages=2, max_chars=20))
    messages, _ = selector.select(second.thread, second.state.run_id)
    assert [m.content for m in messages] == ["first", "收到"]
    assert sum(len(m.content) for m in messages) <= 20
    selector.policy = RecentContextPolicy(max_messages=1, max_chars=2)
    messages, _ = selector.select(second.thread, second.state.run_id)
    assert [m.content for m in messages] == ["收到"]


def test_missing_prior_snapshot_is_reported_not_fatal(tmp_path):
    first = execute(new(tmp_path, "first"))
    first.store.path(first.state.run_id).unlink()
    second = execute(new(tmp_path, "second", first.thread.thread_id))
    assert second.state.final_output
    assert any("unreadable" in note for note in second.context_engine.last_packet.selection_notes)


def test_current_human_answer_once_and_source_remains_raw(tmp_path):
    runtime = new(tmp_path, "goal", model=ScriptedModel([NeedUserInput(question="which?"), FinalResponse(content="done")]))
    execute(runtime)
    runtime.human("answer", text="unique_answer_781")
    execute(runtime)
    packet = runtime.context_engine.last_packet
    assert sum(m["content"].count("unique_answer_781") for m in packet.messages) == 1
    assert "unique_answer_781" in runtime.history.model_dump_json()
    assert runtime.context_engine.store.packet(runtime.state.run_id) == packet


def test_rejected_answer_not_replayed_as_accepted_conversation(tmp_path):
    first = new(tmp_path, "goal")
    first.history.append("decision", 1, decision={"kind": "respond", "content": "false claim"})
    first.history.append("feedback", 1, completion_rejected=["no evidence"])
    execute(first)
    second = execute(new(tmp_path, "next", first.thread.thread_id))
    assert "false claim" not in [m.content for m in second.context_engine.last_packet.recent_conversation]


def test_thread_database_does_not_duplicate_full_raw_history(tmp_path):
    runtime = new(tmp_path, "goal")
    runtime.history.append("feedback", 0, text="RAW_ONLY_MARKER_762" * 5000)
    execute(runtime)
    import sqlite3
    with sqlite3.connect(runtime.context_engine.store.path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "history" not in tables and "events" not in tables
        rows = db.execute("SELECT data FROM episodes").fetchall()
        assert all(len(row[0]) < 20000 for row in rows)
    assert len(runtime.history.events[0].payload["text"]) > 80000
