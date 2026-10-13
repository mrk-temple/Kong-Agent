"""Replay of observed recall failure patterns; simulated decisions, no live LLM."""
import asyncio
import json
from pathlib import Path

import pytest

from kong.application.memory_bridge import MemoryBridge
from kong.completion_contracts import CompletionContract
from kong.contracts import Act, Action, FinalResponse, Mode, NeedDiscovery, NeedUserInput, Status
from kong.evaluation.memory_behavior import owner
from kong.memory import Bottle, MemoryDraft, MemoryService, Source
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.runtime.recall_feedback import RECALL_LIMIT, recall_no_new_count
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.tools.permissions import PermissionPolicy
from kong.workspace import Workspace


FIXTURE = json.loads((Path(__file__).parent / "fixtures/memory_recall_holdout_v1.json").read_text(encoding="utf-8"))
PRIVATE = "PRIVATE_EPISODE_997"


def test_available_memory_feedback_supports_recall_without_execution_authority(tmp_path):
    database = setup(tmp_path, "unknown")
    text = "Cedar recovery finished with rollback and badge VERIFIED_23."
    with MemoryService(database) as core:
        core.create_bottle(owner(), Bottle(bottle_id="public", profile_id=owner().profile_id, title="Public"), command_id="bottle")
        receipt = core.remember(owner("source"), MemoryDraft(kind="episodic", content=text),
            Source(source_id="source", origin="user_command", speaker="user", excerpt=text), command_id="save")
        core.add_memory_to_bottle(owner(), "public", receipt.record_id, command_id="member")
        bottle, = core.list_bottles(owner())
        core.activate_bottle(owner(), "public", expected_revision=bottle.revision, command_id="activate")

    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            model = ScriptedModel([FinalResponse(content="Cedar used rollback, with badge VERIFIED_23.")])
            run = make_run(tmp_path, bridge, model, goal="Recall Cedar recovery and its badge.")
            await run.run()
            packet = model.contexts[0]
            assert {r["id"] for r in packet.recalled_memories} == {receipt.record_id}
            feedback, = [i.content for i in packet.items if i.content.startswith("HISTORY RECALL STATUS:")]
            instruction = json.loads(feedback.split("\n", 1)[1])["instruction"]
            assert "Use relevant AUTHORIZED MEMORY" in instruction
            assert "Express uncertainty" not in instruction
            assert "not execution evidence" in instruction
            assert run.state.status == Status.COMPLETED and not run.history.observations
    asyncio.run(execute())


def test_no_new_read_feedback_recommends_clarification_without_claiming_tool_proof(tmp_path):
    database = setup(tmp_path, "unknown")
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            run = make_run(tmp_path, bridge)
            model = ScriptedModel([query(run.state.run_id, 0), NeedUserInput(question="Which event do you mean?")])
            run.model = model
            await run.run()
            packet = model.contexts[1]
            feedback, = [i.content for i in packet.items if i.content.startswith("HISTORY RECALL STATUS:")]
            instruction = json.loads(feedback.split("\n", 1)[1])["instruction"]
            assert "Ask for a clue" in instruction and "do not cite question echoes" in instruction
            assert "Claim tool reads only when observations show them" in instruction
            assert "Do not narrate counters" in instruction
            assert run.state.status == Status.WAITING_USER and run.state.final_output is None
            assert len(run.history.observations) == 1
    asyncio.run(execute())


def setup(path, condition):
    database = path / "core.db"
    with MemoryService(database) as core:
        core.create_profile(owner(), command_id="profile")
        if condition in {"inactive", "forgotten"}:
            core.create_bottle(owner(), Bottle(bottle_id="private", profile_id=owner().profile_id, title="Private"), command_id="bottle")
            text = "The Birch Maple Orion experience ended with " + PRIVATE
            receipt = core.remember(owner("source"), MemoryDraft(kind="episodic", content=text),
                Source(source_id="source", origin="user_command", speaker="user", excerpt=text), command_id="save")
            core.add_memory_to_bottle(owner(), "private", receipt.record_id, command_id="member")
            if condition == "forgotten":
                core.forget_memory(owner("forget"), receipt.record_id, expected_revision=1, command_id="forget")
    return database


def make_run(path, bridge, model=None, **kwargs):
    workspace = Workspace(path)
    return Runtime.new(kwargs.pop("goal", "What was our Maple experience?"), model or ScriptedModel([]),
        workspace, default_registry(workspace), store=RunStore(path / ".kong/runs"), memory_bridge=bridge, **kwargs)


def query(run_id, event_index=None, **extra):
    arguments = {"run_id":run_id, **extra}
    if event_index is not None:
        arguments["event_index"] = event_index
    return NeedDiscovery(reason="Retrieve historical information", actions=[Action(name="recall_history", arguments=arguments)])


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_twenty_missing_or_unusable_cases_end_after_two_no_new_reads(tmp_path, case):
    database = setup(tmp_path, case["condition"])
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            if case["condition"] == "unavailable":
                async def unavailable(*args, **kwargs):
                    return None
                bridge.retrieve_context = unavailable
            run = make_run(tmp_path, bridge, goal=case["query"])
            model = ScriptedModel([query(run.state.run_id), query(run.state.run_id, 0), query(run.state.run_id, 0, offset=1)])
            run.model = model
            await run.run()
            assert run.state.status == Status.WAITING_USER and run.state.final_output is None
            assert len(run.history.observations) == RECALL_LIMIT
            assert run.state.requires_evidence and recall_no_new_count(run.state, run.history) == RECALL_LIMIT
            assert PRIVATE not in json.dumps([packet.messages for packet in model.contexts])
            assert PRIVATE not in str([o.output for o in run.history.observations])
            assert all("HISTORY RECALL STATUS" in str(packet.messages) for packet in model.contexts)
            assert not run.pending_action_ids
            result = {"case":case, "behavior_source":"engineering-scripted", "live_requests":0,
                "status":run.state.status.value,"reads":len(run.history.observations),
                "question":run.state.pending_question,"history":run.history.model_dump(mode="json"),
                "packets":[p.model_dump(mode="json") for p in model.contexts]}
            (tmp_path / "recall-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    asyncio.run(execute())


def test_multi_action_batch_cannot_dispatch_third_query_or_following_write(tmp_path):
    database = setup(tmp_path, "unknown")
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            run = make_run(tmp_path, bridge)
            action = query(run.state.run_id).actions[0]
            run.model = ScriptedModel([Act(actions=[action, action, action, Action(name="write_file", arguments={"path":"must-not-exist.txt","content":"bad"})])])
            await run.run()
            assert len(run.history.observations) == 2 and run.state.status == Status.WAITING_USER
            assert not (tmp_path / "must-not-exist.txt").exists()
    asyncio.run(execute())


@pytest.mark.parametrize("condition,query_text", [
    ("unknown", "What was our Cedar experience?"), ("unknown", "What happened at the Vega milestone?"),
    ("unknown", "那回经历的结果呢？"), ("unknown", "How did that event finish?"),
    ("inactive", "Recall the Birch milestone."), ("inactive", "Maple 的共同经历是什么？"),
    ("forgotten", "Recall the Maple experience."), ("forgotten", "Birch 项目后来怎样？")])
def test_supplement_reaches_twenty_unknown_inactive_forgotten_cases(tmp_path, condition, query_text):
    # The initial frozen matrix had 12 such cases plus ambiguity/unavailability.
    # Add coverage without replacing its results or changing its expected answers.
    test_twenty_missing_or_unusable_cases_end_after_two_no_new_reads(tmp_path,
        {"id":"supplement-" + condition + "-" + query_text, "condition":condition, "query":query_text})


@pytest.mark.parametrize("text", ["那次如何收尾？", "How did it end?", "那回的结果呢？", "What happened in the end?"])
def test_true_ambiguity_can_clarify_without_a_history_query(tmp_path, text):
    database = setup(tmp_path, "unknown")
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            run = make_run(tmp_path, bridge, ScriptedModel([NeedUserInput(question="Which project or event do you mean?")]), goal=text)
            await run.run()
            assert run.context_engine.last_packet.memory_query_status == "ambiguous"
            assert run.state.status == Status.WAITING_USER and not run.history.observations
    asyncio.run(execute())


def test_unmapped_and_revoked_sources_have_identical_nonrevealing_results(tmp_path):
    database = setup(tmp_path, "inactive")
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            prior = make_run(tmp_path, bridge, ScriptedModel([FinalResponse(content="ok")]), goal=PRIVATE)
            await prior.run()
            with MemoryService(database) as core:
                record, = core.list_records(owner()).records
                core.forget_memory(owner("forget"), record.record_id, expected_revision=1, command_id="forget")
            current = make_run(tmp_path, bridge, thread_id=prior.thread.thread_id)
            from kong.tools.base import ToolCall
            unknown = await current.executor.execute(ToolCall(name="recall_history", arguments={"run_id":"f" * 32}))
            revoked = await current.executor.execute(ToolCall(name="recall_history", arguments={"run_id":prior.state.run_id}))
            assert unknown == revoked and not revoked.success and PRIVATE not in str(revoked)
    asyncio.run(execute())


def test_legitimate_new_source_then_repeated_reads_new_pagination_and_human_reset(tmp_path):
    database = setup(tmp_path, "unknown")
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            prior = make_run(tmp_path, bridge, ScriptedModel([FinalResponse(content="ok")]), goal="An explicit past account " + "details " * 20)
            await prior.run()
            run = make_run(tmp_path, bridge, thread_id=prior.thread.thread_id)
            run.model = ScriptedModel([query(prior.state.run_id, max_chars=20), query(prior.state.run_id, offset=20, max_chars=20),
                query(prior.state.run_id, offset=20, max_chars=20), query(prior.state.run_id, offset=20, max_chars=20),
                query(prior.state.run_id, offset=20, max_chars=20)])
            run.state.turn_budget = 8
            await run.run()
            assert len(run.history.observations) == 4 and recall_no_new_count(run.state, run.history) == 2
            run.controller.human(run.state, run.history, "answer", text="New project clue")
            assert recall_no_new_count(run.state, run.history) == 0
            run.save()
            restored = Runtime.restore(run.store.load(run.state.run_id), ScriptedModel([]), run.workspace,
                default_registry(run.workspace), store=run.store, memory_bridge=bridge)
            assert recall_no_new_count(restored.state, restored.history) == 0
    asyncio.run(execute())


def test_no_new_count_survives_restart_and_completion_evidence_is_still_required(tmp_path):
    database = setup(tmp_path, "unknown")
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            run = make_run(tmp_path, bridge)
            run.model = ScriptedModel([query(run.state.run_id), query(run.state.run_id, 0), query(run.state.run_id)])
            await run.run()
            restored = Runtime.restore(run.store.load(run.state.run_id), ScriptedModel([]), run.workspace,
                default_registry(run.workspace), store=run.store, memory_bridge=bridge)
            assert recall_no_new_count(restored.state, restored.history) == 2
            assert restored.controller.completion_gate.check(restored.state, FinalResponse(content="done"), restored.history)
    asyncio.run(execute())


def test_new_authorized_task_read_can_be_recalled_after_two_question_echoes(tmp_path):
    database = setup(tmp_path, "unknown")
    (tmp_path / "source.txt").write_text("AUTHORIZED_TASK_DATA " + "x" * 5000, encoding="utf-8")
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            run = make_run(tmp_path, bridge)
            class NewEvidenceModel:
                calls = 0
                async def generate(self, packet):
                    self.calls += 1
                    if self.calls <= 2:
                        return query(run.state.run_id)
                    if self.calls == 3:
                        return Act(actions=[Action(name="read_file", arguments={"path":"source.txt"})])
                    if self.calls == 4:
                        index = next(i for i, event in enumerate(run.history.events) if event.type == "observation" and
                            event.payload["observation"]["action"]["name"] == "read_file")
                        return query(run.state.run_id, index, max_chars=12000)
                    return NeedUserInput(question="Further task instruction?")
            run.model, run.state.turn_budget = NewEvidenceModel(), 8
            await run.run()
            assert len(run.history.observations) == 4 and run.history.observations[-1].success
            assert "AUTHORIZED_TASK_DATA" in run.history.observations[-1].output["content"]
            assert recall_no_new_count(run.state, run.history) == 0
    asyncio.run(execute())


def test_read_before_ask_contract_is_not_waived_by_recall_limit(tmp_path):
    database = setup(tmp_path, "unknown")
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            run = make_run(tmp_path, bridge, completion_contract=CompletionContract(family="data", allow_completion=False, read_before_ask=["required.txt"]))
            run.model = ScriptedModel([query(run.state.run_id), query(run.state.run_id, 0), query(run.state.run_id)])
            await run.run()
            assert len(run.history.observations) == 2 and run.state.status == Status.STOPPED
            assert run.state.stop_reason == "recall_limit_requires_task_evidence" and run.state.final_output is None
            assert any("ask_rejected" in e.payload for e in run.history.events)
    asyncio.run(execute())


@pytest.mark.parametrize("mode", [Mode.FAST, Mode.AUTO, Mode.PLAN])
def test_controller_and_tool_permissions_remain_authoritative(tmp_path, mode):
    database = setup(tmp_path, "unknown")
    async def execute():
        async with MemoryBridge(database, owner()) as bridge:
            run = make_run(tmp_path, bridge, mode=mode, permissions=PermissionPolicy(allowed_tools=[]))
            run.model = ScriptedModel([query(run.state.run_id), query(run.state.run_id), NeedUserInput(question="No information; clarify?")])
            await run.run()
            assert run.state.status == Status.WAITING_USER and run.state.final_output is None
            if mode == Mode.PLAN:
                assert not run.history.observations
            else:
                assert all(not o.success and "permission" in o.error for o in run.history.observations)
    asyncio.run(execute())
