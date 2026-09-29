import asyncio
import json

import httpx
import pytest

from kong.config import ModelConfig
from kong.continuity.compaction import Compactor
from kong.continuity.compiler import BudgetManager, ContextBudgetExceeded, ContextRenderer, item, token_estimate
from kong.continuity.contracts import ContextBudget, ContextSource
from kong.continuity.memory_contracts import ContinuityPolicy, MemoryStatus
from kong.contracts import Act, Action, Criterion, FinalResponse, History, Plan, PlanStep, RunState, Status
from kong.models.compatible import CompatibleModel
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.base import ToolCall
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def runtime_for(path, model, goal="goal"):
    workspace = Workspace(path)
    return Runtime.new(goal, model, workspace, default_registry(workspace), store=RunStore(path / ".kong" / "runs"))


def test_priority_budget_output_reservation_and_no_p0_truncation():
    src = [ContextSource(kind="system")]
    candidates = [item("system", "required", 0, src), item("old", "x" * 500, 5, src),
                  item("working_set", "important", 1, src)]
    allocator = BudgetManager(ContextRenderer())
    budget = ContextBudget(window_tokens=500, output_reserved_tokens=200)
    selected, messages, used, categories, dropped = allocator.allocate(candidates, budget)
    assert [i.category for i in selected] == ["system", "working_set"]
    assert used <= budget.input_tokens
    assert sum(categories.values()) == used == sum(token_estimate(m["content"]) for m in messages)
    assert dropped[0]["category"] == "old"
    with pytest.raises(ContextBudgetExceeded):
        allocator.allocate([item("system", "x" * 1000, 0, src)], budget)


def test_large_observation_digested_raw_retained_and_recall_bounded(tmp_path):
    raw = "large source " * 15000
    (tmp_path / "large.txt").write_text(raw, encoding="utf-8")
    model = ScriptedModel([Act(actions=[Action(name="read_file", arguments={"path": "large.txt"})]),
                           FinalResponse(content="read", evidence_ids=["a1"])])
    runtime = runtime_for(tmp_path, model)
    asyncio.run(runtime.run())
    packet = runtime.context_engine.last_packet
    assert packet.budget_enforced and not packet.over_budget
    assert len(packet.observations) == 1
    assert packet.observations[0]["truncated"] is True
    assert packet.observations[0]["size"] >= len(raw)
    assert runtime.history.observations[0].output == raw
    index = packet.observations[0]["raw_ref"]["event_index"]
    result = asyncio.run(runtime.executor.execute(ToolCall(name="recall_history", arguments={
        "run_id": runtime.state.run_id, "event_index": index, "offset": 1000, "max_chars": 600})))
    assert result.success and result.output["truncated"]
    assert len(result.output["content"]) == 600
    assert result.output["historical_only"]
    assert len("".join(m["content"] for m in packet.messages)) < len(raw)


def test_raw_recall_rejects_another_thread(tmp_path):
    first = runtime_for(tmp_path, ScriptedModel([FinalResponse(content="first")]))
    asyncio.run(first.run())
    second = runtime_for(tmp_path, ScriptedModel([]))
    result = asyncio.run(second.executor.execute(ToolCall(name="recall_history", arguments={"run_id": first.state.run_id})))
    assert not result.success and "Thread" in result.error


@pytest.mark.parametrize("trigger", ["turn_threshold", "token_pressure", "run_complete", "phase_end"])
def test_episode_triggers_are_idempotent_and_preserve_raw(tmp_path, trigger):
    runtime = runtime_for(tmp_path, ScriptedModel([]))
    history = History()
    history.append("human", 0, command="goal", text="some work")
    for turn in range(2):
        history.append("decision", turn, decision={"kind": "assess", "diagnosis": "observed progress"})
    state = RunState(goal="goal", run_id=runtime.state.run_id)
    if trigger == "run_complete":
        state.status = Status.COMPLETED
    if trigger == "phase_end":
        state.plan = Plan(goal="goal", rationale="reason", steps=[PlanStep(id="s1", title="done", done=True)],
                          success_criteria=[Criterion(id="c", description="evidence")])
        history.append("decision", 2, decision={"kind": "complete_step", "step_id": "s1"})
    before = history.model_dump_json()
    compactor = Compactor(runtime.context_engine.store, ContinuityPolicy(compact_turns=2 if trigger == "turn_threshold" else 20))
    episode = compactor.compact(runtime.thread.thread_id, state, history, trigger == "token_pressure")
    assert episode.trigger == trigger
    assert compactor.compact(runtime.thread.thread_id, state, history, True) is None
    assert history.model_dump_json() == before
    assert len(episode.sources) == len(history.events)
    assert all(s.event_id for s in episode.sources)


def test_rejected_step_does_not_trigger_phase_compaction(tmp_path):
    runtime = runtime_for(tmp_path, ScriptedModel([]))
    history = History()
    history.append("decision", 1, decision={"kind": "complete_step", "step_id": "fake"})
    history.append("feedback", 1, error="unapproved")
    assert runtime.context_engine.compactor.compact(runtime.thread.thread_id, runtime.state, history) is None


def test_required_context_overflow_pauses_without_model_call(tmp_path):
    model = ScriptedModel([FinalResponse(content="must not run")])
    runtime = runtime_for(tmp_path, model)
    runtime.context_engine.budget = ContextBudget(window_tokens=200, output_reserved_tokens=100)
    asyncio.run(runtime.run())
    assert model.calls == 0
    assert runtime.state.status == Status.WAITING_USER
    assert runtime.state.turn_count == 1
    assert "context" in runtime.state.pending_question


@pytest.mark.parametrize("valid", [True, False])
def test_model_proposes_delta_in_same_http_call_and_cannot_confirm_decision(tmp_path, valid):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        manifest = next(m["content"] for m in body["messages"] if m["content"].startswith("MEMORY DELTA TARGET:"))
        metadata = json.loads(manifest.split("\n", 1)[1])
        goal = next(m["content"] for m in body["messages"] if m["content"].startswith("ORIGINAL GOAL:"))
        source = json.loads(goal.split("\nSOURCE: ", 1)[1])
        delta = {"thread_id": metadata["thread_id"], "base_revision": metadata["base_revision"],
                 "mutations": [{"key": "project.database", "kind": "decision", "content": "Use SQLite",
                                "sources": [source]}]}
        if not valid:
            delta["mutations"][0]["confirmed_by"] = source
        content = json.dumps({"kind": "respond", "content": "Here is the proposal", "memory_delta": delta})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = CompatibleModel(ModelConfig(base_url="http://localhost/v1", model="fake"), client)
            runtime = runtime_for(tmp_path, model, "We could use SQLite")
            await runtime.run()
            assert runtime.state.status == Status.COMPLETED
            state = runtime.context_engine.store.state(runtime.thread.thread_id)
            if valid:
                assert state.items[0].status == MemoryStatus.CANDIDATE
                assert not state.items[0].confirmed_by
            else:
                assert not state.items
            kinds = [entry["kind"] for entry in runtime.context_engine.store.audits(runtime.state.run_id)]
            assert "memory_delta_proposed" in kinds
            assert ("memory_delta_merged" if valid else "memory_delta_rejected") in kinds
            assert model.take_memory_delta() is None
    asyncio.run(scenario())
    assert len(calls) == 1


def test_budget_tracks_provider_output_setting(tmp_path):
    model = CompatibleModel(ModelConfig(base_url="http://localhost/v1", model="fake", max_tokens=8000,
                                        context_window_tokens=32000))
    runtime = runtime_for(tmp_path, model)
    assert runtime.context_engine.budget.input_tokens == 24000


def test_derived_database_failure_does_not_change_runtime_completion(tmp_path, monkeypatch):
    import sqlite3
    model = ScriptedModel([FinalResponse(content="still answered")])
    runtime = runtime_for(tmp_path, model)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(runtime.context_engine.store, "state", unavailable)
    monkeypatch.setattr(runtime.context_engine.store, "audit", unavailable)
    asyncio.run(runtime.run())
    assert runtime.state.status == Status.COMPLETED
    assert model.calls == 1
    assert any("unavailable" in note for note in model.contexts[0].selection_notes)
    assert runtime.context_engine.diagnostics


def test_old_snapshot_answer_keeps_priority_without_fabricated_goal_event(tmp_path):
    from kong.storage import Snapshot
    history = History()
    history.append("human", 2, command="answer", text="LATEST_USER_729")
    state = RunState(goal="old goal", turn_count=2)
    snapshot = Snapshot(workspace=str(tmp_path), state=state, history=history)
    workspace = Workspace(tmp_path)
    model = ScriptedModel([FinalResponse(content="ok")])
    runtime = Runtime.restore(snapshot, model, workspace, default_registry(workspace),
                              store=RunStore(tmp_path / ".kong" / "runs"))
    asyncio.run(runtime.run())
    assert any(i.priority == 0 and "LATEST_USER_729" in i.content for i in model.contexts[0].items)
    assert not any(e.payload.get("command") == "goal" for e in runtime.history.events)


def test_retriever_is_replaceable_without_runtime_changes(tmp_path):
    from kong.continuity.engine import ContextEngine
    class AlternativeRetriever:
        def __init__(self):
            self.queries = []
        def retrieve(self, query, thread, project_id=None, limit=12):
            self.queries.append(query)
            return []
    replacement = AlternativeRetriever()
    engine = ContextEngine(retriever=replacement)
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("retrieval goal", ScriptedModel([FinalResponse(content="ok")]), workspace,
                          default_registry(workspace), store=RunStore(tmp_path / ".kong" / "runs"), context_engine=engine)
    asyncio.run(runtime.run())
    assert engine.retriever is replacement
    assert len(replacement.queries) == 1 and "retrieval goal" in replacement.queries[0]
