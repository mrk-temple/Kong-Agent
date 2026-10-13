"""Frozen synthetic holdout and real authorization integration, without an LLM."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from kong.application.memory_bridge import MemoryBridge
from kong.continuity.compiler import ContextPlanner
from kong.continuity.contracts import ContextSource, ConversationMessage
from kong.contracts import FinalResponse, History, RunState
from kong.evaluation.memory_behavior import owner
from kong.memory import Bottle, MemoryDraft, MemoryService, RetrievalQuery, Scope, Source
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


HOLDOUT = json.loads((Path(__file__).parent / "fixtures/memory_retrieval_holdout_v1.json").read_text(encoding="utf-8"))


def initialize(core):
    core.create_profile(owner(), command_id="profile")
    core.create_bottle(owner(), Bottle(bottle_id="public", profile_id=owner().profile_id, title="Experiences"), command_id="bottle")


def activate(core):
    bottle, = core.list_bottles(owner())
    core.activate_bottle(owner(), bottle.bottle_id, expected_revision=bottle.revision, command_id="activate")


def episode(core, key, project, date="2026-01-02", *, expires_at=None):
    content = f"On {date} we resolved the {project} recovery together. We used rollback and verified badge {key.upper()}."
    receipt = core.remember(owner(key), MemoryDraft(kind="episodic", content=content, expires_at=expires_at),
        Source(source_id=key, origin="user_command", speaker="user", excerpt=content), command_id=key)
    core.add_memory_to_bottle(owner(), "public", receipt.record_id, command_id="member-" + key)
    return receipt.record_id


def query(text, focus=""):
    recent = [ConversationMessage(role="user", content=focus, source=ContextSource(kind="state"))] if focus else []
    return ContextPlanner().memory_query(RunState(goal=text), History(), recent)


@pytest.mark.parametrize("case", HOLDOUT["cases"], ids=lambda case: case["id"])
def test_frozen_entity_reference_holdout(tmp_path, case):
    with MemoryService(tmp_path / "core.db") as core:
        initialize(core)
        target = episode(core, "target", case["project"], case.get("date", "2026-01-02"))
        episode(core, "decoy", "Foxtrot")
        if case.get("same_project_decoy_date"):
            episode(core, "dated-decoy", case["project"], case["same_project_decoy_date"])
        activate(core)
        planned = query(case["query"], case.get("focus", "").format(target_id=target))
        view = core.retrieve_context(owner(), planned)
        assert {record.record_id for record in view.records} == {target}
        assert view.query_status == "available"


@pytest.mark.parametrize("case", HOLDOUT["abstentions"], ids=lambda case: case["id"])
def test_ambiguous_or_unanchored_query_never_selects_arbitrary_episode(tmp_path, case):
    with MemoryService(tmp_path / "core.db") as core:
        initialize(core)
        episode(core, "orion", "Orion")
        episode(core, "lyra", "Lyra")
        if case.get("same_project_decoy"):
            episode(core, "orion-other", "Orion", "2026-02-02")
        activate(core)
        result = core.retrieve_context(owner(), query(case["query"], case["focus"]))
        assert not result.records and result.query_status == case["status"]


def test_current_entity_overrides_stale_topic_and_assistant_is_not_a_focus(tmp_path):
    with MemoryService(tmp_path / "core.db") as core:
        initialize(core)
        episode(core, "orion", "Orion")
        target = episode(core, "lyra", "Lyra")
        activate(core)
        view = core.retrieve_context(owner(), query("What was the Lyra recovery badge?", "Let's focus on Orion recovery."))
        assert {r.record_id for r in view.records} == {target}
        assistant = [ConversationMessage(role="assistant", content="We definitely discussed Orion.", source=ContextSource(kind="state"))]
        assert not core.retrieve_context(owner(), ContextPlanner().memory_query(RunState(goal="那次如何收尾？"), History(), assistant)).records
        planned = query("Recall the Lyra recovery.", "Let's discuss episode " + target)
        assert not planned.episode_ids  # An old pointer cannot override a new explicit topic.


@pytest.mark.parametrize("restriction", ["inactive", "forgotten", "archived", "expired", "scope"])
def test_reference_and_exact_episode_cannot_bypass_authorization(tmp_path, restriction):
    with MemoryService(tmp_path / "core.db") as core:
        initialize(core)
        target = episode(core, "target", "Orion", expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc) if restriction == "expired" else None)
        core.pin_memory(owner(), target, "public", command_id="pin")
        if restriction != "inactive":
            activate(core)
        if restriction == "forgotten":
            core.forget_memory(owner("forget"), target, expected_revision=1, command_id="forget")
        elif restriction == "archived":
            core.archive_memory(owner("archive"), target, expected_revision=1, command_id="archive")
        elif restriction == "expired":
            core.clock = lambda: "2100-01-01T00:00:00+00:00"
        auth = owner().model_copy(update={"scopes": (Scope(kind="project", id="different"),)}) if restriction == "scope" else owner()
        for planned in (query("那次如何收尾？", "Let's focus on Orion recovery."), RetrievalQuery(text="that episode", episode_ids=(target,))):
            view = core.retrieve_context(auth, planned)
            assert not view.records and not view.pinned_record_ids


def test_revoked_recent_focus_is_not_a_query_clue_or_context(tmp_path):
    database = tmp_path / "core.db"
    with MemoryService(database) as core:
        initialize(core)
        target = episode(core, "target", "Orion")
        activate(core)
    async def scenario():
        async with MemoryBridge(database, owner()) as bridge:
            workspace = Workspace(tmp_path)
            runs = RunStore(tmp_path / ".kong/runs")
            prior = Runtime.new("Let's focus on Orion recovery.", ScriptedModel([FinalResponse(content="Acknowledged.")]), workspace,
                default_registry(workspace), store=runs, memory_bridge=bridge)
            await prior.run()
            with MemoryService(database) as core:
                core.forget_memory(owner("forget"), target, expected_revision=1, command_id="forget")
            current = Runtime.new("那次如何收尾？", ScriptedModel([]), workspace, default_registry(workspace), store=runs,
                memory_bridge=bridge, thread_id=prior.thread.thread_id)
            packet = await current.context_engine.prepare(thread=current.thread, state=current.state, history=current.history, tools=current.registry.definitions())
            assert current.context_engine.last_memory_query.focus == ""
            assert "Orion" not in str(packet.messages) and not packet.recalled_memories
    asyncio.run(scenario())
