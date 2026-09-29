import asyncio

import pytest
from pydantic import ValidationError

from kong.context import Context, ContextBuilder
from kong.continuity.contracts import ContextBudget, ContextPacket, ContextSource, Thread
from kong.continuity.engine import ContextEngine
from kong.continuity.storage import ThreadStore
from kong.contracts import FinalResponse, History, Mode, NeedUserInput, RunState, Status
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore, Snapshot
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def make_runtime(path, *, thread_id=None, model=None, mode=Mode.AUTO):
    workspace = Workspace(path)
    return Runtime.new("goal", model or ScriptedModel([FinalResponse(content="done")]),
                       workspace, default_registry(workspace), mode,
                       store=RunStore(path / ".kong" / "runs"), thread_id=thread_id)


def test_thread_lifecycle_and_unique_run_membership(tmp_path):
    store = ThreadStore(tmp_path / "context.db")
    first = store.create(tmp_path, "conversation")
    second = store.create(tmp_path, "other")
    store.attach_run(first.thread_id, "run1", tmp_path)
    store.attach_run(first.thread_id, "run1", tmp_path)
    store.attach_run(first.thread_id, "run2", tmp_path)
    assert store.runs(first.thread_id) == ["run1", "run2"]
    with pytest.raises(ValueError, match="another thread"):
        store.attach_run(second.thread_id, "run1", tmp_path)
    assert store.set_archived(first.thread_id, True).status == "archived"
    store.attach_run(first.thread_id, "run1", tmp_path)
    with pytest.raises(ValueError, match="archived"):
        store.attach_run(first.thread_id, "run3", tmp_path)
    store.set_archived(first.thread_id, False)
    store.attach_run(first.thread_id, "run3", tmp_path)
    reopened = ThreadStore(tmp_path / "context.db")
    assert reopened.runs(first.thread_id) == ["run1", "run2", "run3"]
    assert len(reopened.list_threads(tmp_path)) == 2
    with pytest.raises(ValueError, match="workspace"):
        reopened.attach_run(first.thread_id, "outside", tmp_path / "other")


def test_thread_has_multiple_independent_runs(tmp_path):
    first = make_runtime(tmp_path)
    asyncio.run(first.run())
    second = make_runtime(tmp_path, thread_id=first.thread.thread_id, mode=Mode.FAST)
    assert second.state.turn_count == 0
    assert second.state.hard_turn_limit == 4
    assert second.history.events == []
    asyncio.run(second.run())
    assert first.state.status == second.state.status == Status.COMPLETED
    assert first.context_engine.working_set.run_id != second.context_engine.working_set.run_id
    assert second.thread_store.runs(first.thread.thread_id) == [first.state.run_id, second.state.run_id]
    assert second.snapshot().thread_id == first.thread.thread_id
    third = make_runtime(tmp_path)
    assert third.thread.thread_id != first.thread.thread_id
    with pytest.raises(ValueError, match="Unknown thread"):
        make_runtime(tmp_path, thread_id="f" * 32)


def test_restore_old_snapshot_is_lazy_and_idempotent(tmp_path):
    state = RunState(goal="legacy", turn_count=2)
    snapshot = Snapshot.model_validate({"workspace": str(tmp_path), "state": state.model_dump()})
    workspace = Workspace(tmp_path)
    store = RunStore(tmp_path / ".kong" / "runs")
    before = snapshot.model_dump_json()
    restored = Runtime.restore(snapshot, ScriptedModel([]), workspace, default_registry(workspace), store=store)
    assert restored.thread.thread_id == state.run_id
    assert snapshot.model_dump_json() == before
    restored.save()
    again = Runtime.restore(store.load(state.run_id), ScriptedModel([]), workspace, default_registry(workspace), store=store)
    assert again.state.turn_count == 2
    assert again.thread_store.runs(restored.thread.thread_id) == [state.run_id]
    assert len(again.thread_store.list_threads(tmp_path)) == 1


def test_restore_reconstructs_missing_derived_database(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.save()
    runtime.thread_store.path.unlink()
    restored = Runtime.restore(runtime.store.load(runtime.state.run_id), ScriptedModel([]),
                               runtime.workspace, runtime.registry, store=runtime.store)
    assert restored.thread.thread_id == runtime.thread.thread_id
    assert restored.thread_store.runs(runtime.thread.thread_id) == [runtime.state.run_id]


@pytest.mark.parametrize("mode", list(Mode))
def test_engine_preserves_exact_legacy_messages_and_truth(tmp_path, mode):
    state = RunState(goal="original", mode=mode)
    history = History()
    history.append("human", 0, text="keep this correction")
    for i in range(12):
        history.append("feedback", i, text="x" * 2000)
    history.append("feedback", 12, text="y" * 15000)
    before = (state.model_dump_json(), history.model_dump_json())
    builder = ContextBuilder(max_history_chars=16000)
    engine = ContextEngine(builder, ContextBudget(window_tokens=100, output_reserved_tokens=10))
    packet = asyncio.run(engine.prepare(thread=Thread(workspace=str(tmp_path)), state=state, history=history, tools=[]))
    assert packet.messages == builder.build(state, history, []).messages
    assert packet.over_budget and not packet.budget_enforced
    assert packet.truncated_event_indices == [13]
    selected = {s.event_index for item in packet.items for s in item.sources if s.kind == "event"}
    assert selected | set(packet.omitted_event_indices) == set(range(len(history.events)))
    assert not selected & set(packet.omitted_event_indices)
    assert "keep this correction" in packet.working_set.items[-1].content
    assert ContextPacket.model_validate_json(packet.model_dump_json()) == packet
    asyncio.run(engine.commit(state=state, history=history))
    assert before == (state.model_dump_json(), history.model_dump_json())
    assert engine.last_packet == packet
    history.append("human", 14, text="new answer")
    first = asyncio.run(engine.commit(state=state, history=history))
    assert asyncio.run(engine.commit(state=state, history=history)) == first
    assert engine.last_packet.working_set.source_event_count == 14
    assert engine.working_set.source_event_count == 15


def test_packet_is_actual_model_input_and_commit_tracks_final_state(tmp_path):
    model = ScriptedModel([NeedUserInput(question="which?"), FinalResponse(content="done")])
    runtime = make_runtime(tmp_path, model=model)
    asyncio.run(runtime.run())
    assert isinstance(model.contexts[0], ContextPacket)
    assert runtime.context_engine.working_set.pending_question == "which?"
    assert runtime.context_engine.last_packet == model.contexts[0]
    runtime.human("answer", text="this one")
    asyncio.run(runtime.run())
    assert model.calls == 2
    assert "this one" in model.contexts[1].messages[-1]["content"]
    assert runtime.context_engine.working_set.source_event_count == len(runtime.history.events)
    assert model.contexts[0].working_set.pending_question is None


def test_context_contract_validation():
    assert ContextBudget(window_tokens=100, output_reserved_tokens=20).input_tokens == 80
    for fields in ({"window_tokens": 0}, {"output_reserved_tokens": -1},
                   {"window_tokens": 10, "output_reserved_tokens": 10}):
        with pytest.raises(ValidationError):
            ContextBudget(**fields)
    with pytest.raises(ValidationError):
        ContextSource(kind="event")


def test_custom_context_builder_still_controls_messages(tmp_path):
    class CustomBuilder(ContextBuilder):
        def build(self, state, history, tools):
            return Context(messages=[{"role": "user", "content": "custom"}])
    workspace = Workspace(tmp_path)
    model = ScriptedModel([FinalResponse(content="ok")])
    runtime = Runtime.new("goal", model, workspace, default_registry(workspace), context_builder=CustomBuilder())
    asyncio.run(runtime.run())
    assert model.contexts[0].messages == [{"role": "user", "content": "custom"}]
    assert model.contexts[0].items[0].sources[0].kind == "custom"
    assert not (tmp_path / ".kong").exists()
