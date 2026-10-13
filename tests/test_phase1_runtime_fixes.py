"""Phase 1 regressions for Memory handoff and completion classification."""
import asyncio
import json

import pytest

from kong.application.memory_bridge import MemoryBridge
from kong.context import SYSTEM_PROMPT
from kong.contracts import Act, Action, Criterion, FinalResponse, NeedDiscovery, NeedUserInput, RunState, Status
from kong.evaluation.memory_behavior import owner
from kong.memory import Bottle, MemoryDraft, MemoryService, Source
from kong.models.fake import ScriptedModel
from kong.runtime.acceptance import has_declared_acceptance
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def _core_with_record(path):
    database = path / "core.db"
    with MemoryService(database) as core:
        core.create_profile(owner(), command_id="profile")
        core.create_bottle(owner(), Bottle(bottle_id="public", profile_id=owner().profile_id, title="Public"), command_id="public")
        core.create_bottle(owner(), Bottle(bottle_id="other", profile_id=owner().profile_id, title="Other"), command_id="other")
        content = "Cedar verification badge is BLUE-41."
        record = core.remember(owner("source"), MemoryDraft(kind="episodic", content=content),
            Source(source_id="source", origin="user_command", speaker="user", excerpt=content), command_id="save")
        core.add_memory_to_bottle(owner(), "public", record.record_id, command_id="member")
        core.activate_bottle(owner(), "public", expected_revision=2, command_id="activate-public")
    return database, record.record_id


def _mutate_core(database, record_id, kind):
    with MemoryService(database) as core:
        if kind == "pin":
            core.pin_memory(owner("pin"), record_id, "public", command_id="pin")
        elif kind == "activate":
            core.activate_bottle(owner("activate"), "other", expected_revision=1, command_id="activate-other")
        elif kind == "revise":
            content = "Cedar verification badge is GREEN-42."
            core.revise_memory(owner("revise"), record_id, content,
                Source(source_id="revise", origin="user_command", speaker="user", excerpt=content),
                expected_revision=1, command_id="revise")
        else:
            core.forget_memory(owner("forget"), record_id, expected_revision=1, command_id="forget")


@pytest.mark.parametrize("mutation", ["pin", "activate", "revise", "forget"])
@pytest.mark.parametrize("fresh_goal", ["The answer is 42.", "Please summarize the Cedar badge again."])
def test_core_mutation_requires_explicit_fresh_run_with_only_new_input(tmp_path, mutation, fresh_goal):
    database, record_id = _core_with_record(tmp_path)
    old_secret = "OLD_RUN_GOAL_PRIVATE_481"
    async def scenario():
        async with MemoryBridge(database, owner()) as bridge:
            workspace = Workspace(tmp_path)
            store = RunStore(tmp_path / ".kong/runs")
            old = Runtime.new(old_secret, ScriptedModel([NeedUserInput(question="What next?")]),
                workspace, default_registry(workspace), store=store, memory_bridge=bridge)
            assert (await old.run()).status == Status.WAITING_USER
            old_packet = old.context_engine.last_packet
            old_binding = dict(old.context_engine.memory_binding)
            assert old_binding["access"] is not None
            _mutate_core(database, record_id, mutation)
            assert not await old.context_engine.validate_packet(old_packet)
            new_model = ScriptedModel([FinalResponse(content="Fresh reply")])
            fresh = await old.start_fresh_after_memory_change(fresh_goal, model=new_model)
            assert fresh.state.run_id != old.state.run_id
            assert fresh.thread.thread_id == old.thread.thread_id
            assert fresh.state.goal == fresh_goal
            assert fresh.history.events == [] and fresh.history.observations == []
            assert fresh.state.plan is None and fresh.state.success_criteria == []
            assert old.state.stop_reason == "memory_access_changed"
            assert store.load(old.state.run_id).memory_binding == old_binding
            assert (await fresh.run()).status == Status.COMPLETED
            visible = json.dumps(new_model.contexts[0].model_dump(mode="json"), ensure_ascii=False)
            assert fresh_goal in visible and old_secret not in visible
            assert all(message.source.run_id != old.state.run_id for message in new_model.contexts[0].recent_conversation)
            if mutation == "forget":
                assert "BLUE-41" not in visible
    asyncio.run(scenario())


def test_fresh_run_handoff_rejects_unknown_effect_and_unchanged_access(tmp_path):
    database, record_id = _core_with_record(tmp_path)
    async def scenario():
        async with MemoryBridge(database, owner()) as bridge:
            workspace = Workspace(tmp_path)
            old = Runtime.new("Old task", ScriptedModel([NeedUserInput(question="More?")]),
                workspace, default_registry(workspace), store=RunStore(tmp_path / ".kong/runs"), memory_bridge=bridge)
            await old.run()
            with pytest.raises(ValueError, match="Memory access"):
                await old.start_fresh_after_memory_change("New goal")
            _mutate_core(database, record_id, "pin")
            old.pending_action_ids = ["unknown-write"]
            with pytest.raises(ValueError, match="unknown|uncertain"):
                await old.start_fresh_after_memory_change("New goal")
    asyncio.run(scenario())


def test_successful_historical_recall_can_complete_chat_with_read_evidence(tmp_path):
    workspace = Workspace(tmp_path)
    store = RunStore(tmp_path / ".kong/runs")
    previous = Runtime.new("The prior meeting ended with badge BLUE-41.",
        ScriptedModel([FinalResponse(content="Recorded.")]), workspace, default_registry(workspace), store=store)
    assert asyncio.run(previous.run()).status == Status.COMPLETED
    recall = NeedDiscovery(reason="Check prior account", actions=[
        Action(name="recall_history", arguments={"run_id": previous.state.run_id})])
    current = Runtime.new("How did the prior meeting end?",
        ScriptedModel([recall, FinalResponse(content="It ended with badge BLUE-41.", evidence_ids=["a1"])]),
        workspace, default_registry(workspace), store=store, thread_id=previous.thread.thread_id)
    assert asyncio.run(current.run()).status == Status.COMPLETED
    assert len(current.history.observations) == 1
    assert current.history.observations[0].effect == "read"
    assert current.state.final_output == "It ended with badge BLUE-41."


def test_read_only_file_explanation_can_complete_with_evidence(tmp_path):
    (tmp_path / "note.txt").write_text("Cedar badge BLUE-41", encoding="utf-8")
    workspace = Workspace(tmp_path)
    model = ScriptedModel([
        Act(actions=[Action(name="read_file", arguments={"path": "note.txt"})]),
        FinalResponse(content="The note says BLUE-41.", evidence_ids=["a1"]),
    ])
    run = Runtime.new("What does note.txt say?", model, workspace, default_registry(workspace))
    assert asyncio.run(run.run()).status == Status.COMPLETED
    assert run.history.observations[0].effect == "read"


def test_prompt_plan_example_meets_the_same_default_acceptance_rule_as_validator():
    line = next(line for line in SYSTEM_PROMPT.splitlines() if line.startswith("Minimal PLAN shape:"))
    plan = json.loads(line.removeprefix("Minimal PLAN shape: ").removesuffix("."))
    criterion = Criterion.model_validate(plan["plan"]["success_criteria"][0])
    state = RunState(goal="Create the expected file", success_criteria=[criterion])
    assert has_declared_acceptance(state)
    assert "file_exists/file_contains when deterministic" not in SYSTEM_PROMPT
    assert "PLAN requires approval before any tool execution" not in SYSTEM_PROMPT
    assert "host-owned context-only SkillTool preparation" in SYSTEM_PROMPT


@pytest.mark.parametrize("terminal_status", ["completed", "stopped"])
def test_fresh_run_from_terminal_old_run_preserves_old_result(tmp_path, terminal_status):
    database, record_id = _core_with_record(tmp_path)
    async def scenario():
        async with MemoryBridge(database, owner()) as bridge:
            workspace = Workspace(tmp_path)
            store = RunStore(tmp_path / ".kong/runs")
            old = Runtime.new("PRIVATE_OLD_GOAL_792",
                ScriptedModel([FinalResponse(content="Old result")] if terminal_status == "completed"
                              else [NeedUserInput(question="Continue?")]),
                workspace, default_registry(workspace), store=store, memory_bridge=bridge)
            await old.run()
            if terminal_status == "stopped":
                old.human("reject")
            original_status = old.state.status
            original_reason = old.state.stop_reason
            _mutate_core(database, record_id, "pin")
            model = ScriptedModel([FinalResponse(content="Fresh result")])
            fresh = await old.start_fresh_after_memory_change("State the new answer", model=model)
            assert old.state.status == original_status
            assert old.state.stop_reason == original_reason
            assert store.load(old.state.run_id).state.status == original_status
            assert (await fresh.run()).status == Status.COMPLETED
            visible = json.dumps(model.contexts[0].model_dump(mode="json"), ensure_ascii=False)
            assert "State the new answer" in visible
            assert "PRIVATE_OLD_GOAL_792" not in visible
    asyncio.run(scenario())


def test_fresh_run_binding_does_not_renew_after_another_mutation(tmp_path):
    database, record_id = _core_with_record(tmp_path)
    async def scenario():
        async with MemoryBridge(database, owner()) as bridge:
            workspace = Workspace(tmp_path)
            old = Runtime.new("Old secret", ScriptedModel([NeedUserInput(question="Continue?")]),
                workspace, default_registry(workspace), store=RunStore(tmp_path / ".kong/runs"),
                memory_bridge=bridge)
            await old.run()
            _mutate_core(database, record_id, "pin")
            first = await old.start_fresh_after_memory_change("First fresh goal")
            _mutate_core(database, record_id, "activate")
            assert await first.memory_access_changed()
            packet = await first.context_engine.prepare(thread=first.thread, state=first.state,
                history=first.history, tools=first.registry.definitions())
            assert "First fresh goal" not in packet.model_dump_json()
            second = await first.start_fresh_after_memory_change("Second fresh goal",
                model=ScriptedModel([FinalResponse(content="Second answer")]))
            assert (await second.run()).status == Status.COMPLETED
            assert second.thread.thread_id == old.thread.thread_id
    asyncio.run(scenario())

def test_read_bypass_retains_evidence_and_business_guards(tmp_path):
    from kong.contracts import History, Observation
    from kong.runtime.acceptance import acceptance_problem
    from kong.runtime.gates import CompletionGate

    gate = CompletionGate(Workspace(tmp_path))
    state = RunState(goal="Explain a source", requires_evidence=True)
    empty = gate.check(state, FinalResponse(content="Done"), History())
    assert any("successful tool observation" in problem for problem in empty)

    def observed(effect, success=True):
        observation = Observation(action_id="a1", turn=1,
            action=Action(name="read_file", arguments={"path": "report.txt"}),
            success=success, error=None if success else "read failed",
            fingerprint="fixture", output_hash="fixture", effect=effect)
        return History(observations=[observation])

    failed = gate.check(state, FinalResponse(content="Done", evidence_ids=["a1"]),
                        observed("read", success=False))
    assert acceptance_problem(state, observed("read", success=False))
    assert any("Last action failed" in problem for problem in failed)

    unknown = gate.check(state, FinalResponse(content="Done", evidence_ids=["a1"]),
                         observed("unknown"))
    assert any("Acceptance conditions required" in problem for problem in unknown)

    (tmp_path / "report.txt").write_text("Exists", encoding="utf-8")
    weak = Criterion(id="weak", description="Report exists", kind="file_exists", path="report.txt")
    state.success_criteria = [weak]
    complex_read = gate.check(state, FinalResponse(content="Done", evidence_ids=["a1"]),
                              observed("read"))
    assert any("Acceptance conditions required" in problem for problem in complex_read)


def test_cli_core_changed_run_uses_fresh_goal(tmp_path):
    from kong.cli import drive

    class Reader:
        def __init__(self):
            self.lines = ["/fresh State the Cedar badge"]
            self.deferred = []
            self.eof = False

        def poll(self):
            return False, None

        async def get(self, prompt=""):
            if self.lines:
                return self.lines.pop(0)
            raise EOFError()

    database, record_id = _core_with_record(tmp_path)
    async def scenario():
        async with MemoryBridge(database, owner()) as bridge:
            workspace = Workspace(tmp_path)
            store = RunStore(tmp_path / ".kong/runs")
            model = ScriptedModel([NeedUserInput(question="Continue?"),
                                   FinalResponse(content="Fresh reply")])
            old = Runtime.new("PRIVATE_PREVIOUS_CLI_GOAL", model, workspace,
                default_registry(workspace), store=store, memory_bridge=bridge)
            assert (await old.run()).status == Status.WAITING_USER
            _mutate_core(database, record_id, "pin")
            assert await drive(old, True, Reader()) == 0
            assert old.state.status == Status.STOPPED
            runs = store.list_runs()
            assert len(runs) == 2
            fresh_id = next(item["run_id"] for item in runs if item["run_id"] != old.state.run_id)
            assert store.load(fresh_id).state.goal == "State the Cedar badge"
            visible = json.dumps(model.contexts[-1].model_dump(mode="json"), ensure_ascii=False)
            assert "State the Cedar badge" in visible
            assert "PRIVATE_PREVIOUS_CLI_GOAL" not in visible
    asyncio.run(scenario())


def test_fresh_run_does_not_inherit_previous_runs_capture_consent(tmp_path):
    database, record_id = _core_with_record(tmp_path)
    async def scenario():
        async with MemoryBridge(database, owner()) as bridge:
            workspace = Workspace(tmp_path)
            old = Runtime.new("Old explicitly captured conversation",
                ScriptedModel([NeedUserInput(question="Continue?")]), workspace,
                default_registry(workspace), store=RunStore(tmp_path / ".kong/runs"),
                memory_bridge=bridge, memory_capture=True)
            await old.run()
            assert old.context_engine.memory_binding["capture"] == "on"
            _mutate_core(database, record_id, "pin")
            fresh = await old.start_fresh_after_memory_change("Fresh private conversation")
            assert fresh.context_engine.memory_binding["capture"] == "off"
            assert old.context_engine.memory_binding["capture"] == "on"
            assert fresh.store.load(fresh.state.run_id).memory_binding["capture"] == "off"
    asyncio.run(scenario())
