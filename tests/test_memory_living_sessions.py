"""Seven logical days, real Runtime/Bridge, synthetic data and simulated models.

Engineering assertions verify state/packets. Fake replies are not live model eval.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from kong.application.memory_bridge import MemoryBridge
from kong.contracts import FinalResponse, Mode
from kong.memory import (
    Authority, Bottle, Capability, Identity, MemoryDraft, MemoryService, ReflectionBudget,
    Source, identity_digest, Relationship, relationship_digest,
)
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


class ContextReader:
    """Explicit test double: reads permitted assets, no pretrained model behavior."""
    async def generate(self, packet):
        self.packet = packet
        query = packet.working_set.goal
        memories = packet.recalled_memories
        if "deadline" in query:
            answer = next((r["content"] for r in memories if "deadline" in r["content"]), "No authorized deadline.")
        elif "shared experience" in query:
            answer = next((r["episode"]["outcomes"][0] for r in memories if r.get("episode")), "No authorized shared experience.")
        elif "style sample" in query:
            rules = next((json.loads(i.content.split("\n", 1)[1]) for i in packet.items if i.category == "core_relationship"), {})
            answer = "简洁回答。" if rules.get("communication_style") == "concise" else "I use balanced replies. I include relevant background and a useful explanation."
        else:
            answer = "已完成这次讨论。"
        return FinalResponse(content=answer)


class AlternateContextReader(ContextReader):
    """Second simulated adapter; portability checks assets rather than claiming equal prose."""


@pytest.mark.parametrize("accept_style", [True, False])
@pytest.mark.parametrize("interrupt_delivery", [False, True])
def test_seven_day_formation_reflection_and_model_asset_portability(tmp_path, monkeypatch, accept_style, interrupt_delivery):
    database = tmp_path / "synthetic-memory.db"
    instant = datetime(2026, 10, 1, tzinfo=timezone.utc)
    def clock():
        return instant.isoformat()
    auth = Authority(owner_id="synthetic-owner", profile_id="synthetic-kong", input_id="day-one",
        capabilities=frozenset(Capability))
    # The existing worker still constructs/uses/closes its connection on the same thread.
    monkeypatch.setattr("kong.application.memory_bridge.MemoryService", lambda path: MemoryService(path, clock=clock))
    if interrupt_delivery:
        observe = MemoryBridge.observe_snapshot

        async def interrupt_commit(self, snapshot, *, max_seconds=0.1):
            if max_seconds <= 0.1:
                return False  # Simulate a missed bounded commit, without a sleep.
            return await observe(self, snapshot, max_seconds=max_seconds)

        monkeypatch.setattr(MemoryBridge, "observe_snapshot", interrupt_commit)
    packets, sessions = [], []
    identity = Identity(name="Kong", personality=("steady",), principles=("respect autonomy",), communication_style="calm")

    def owner(command):
        return auth.model_copy(update={"input_id": command})

    async def session(text, *, capture=False, model_type=ContextReader, thread_id=None):
        async with MemoryBridge(database, auth, timeout=5) as bridge:
            assert await bridge.get_access_state() is not None
            assert bridge.authority.capabilities == frozenset({Capability.READ, Capability.OBSERVE})
            (tmp_path / "workspace").mkdir(exist_ok=True)
            workspace = Workspace(tmp_path / "workspace")
            model = model_type()
            run = Runtime.new(text, model, workspace, default_registry(workspace), store=RunStore(workspace.root / ".kong/runs"),
                memory_bridge=bridge, memory_capture=capture, thread_id=thread_id)
            await run.run()
            snapshot = run.store.load(run.state.run_id)
            assert snapshot.memory_binding["capture"] == ("on" if capture else "off")
            assert snapshot.memory_binding["access"] is not None
        # Closing drains any late worker. Explicitly finish observation delivery
        # from the persisted original consent; this never resumes an Agent Loop.
        async with MemoryBridge(database, auth, timeout=5) as recovery:
            assert await recovery.get_access_state() is not None
            assert await recovery.observe_snapshot(snapshot, max_seconds=5)
        packets.append(model.packet)
        sessions.append({"run_id": run.state.run_id, "thread_id": run.thread.thread_id, "day": instant.date().isoformat(),
            "adapter": model_type.__name__, "response_simulated": run.state.final_output})
        return run

    with MemoryService(database, clock=clock) as core:
        core.create_profile(auth, command_id="born-empty")
        assert core.get_identity(auth) is None and core.get_relationship(auth) is None
        assert not core.list_records(auth).records
        core.set_identity(owner("identity"), identity, expected_revision=0, approved_digest=identity_digest(identity), command_id="identity")
        for bottle in ("A", "hidden"):
            core.create_bottle(auth, Bottle(bottle_id=bottle, profile_id=auth.profile_id, title=bottle), command_id="bottle-" + bottle)
        core.activate_bottle(auth, "A", expected_revision=1, command_id="activate")

        def explicit_memory(key, text, bottle="A"):
            own = owner(key)
            saved = core.remember(own, MemoryDraft(kind="semantic", content=text),
                Source(source_id=key, origin="user_command", speaker="user", excerpt=text), command_id=key)
            core.add_memory_to_bottle(own, bottle, saved.record_id, command_id=key + "-member")
            return saved

        explicit_memory("hidden-fact", "PRIVATE_INACTIVE_631", "hidden")
        instant += timedelta(days=1)
        deadline = explicit_memory("deadline", "Cedar deadline DATE_OLD_09")
        first = asyncio.run(session("2026-10-02 我和Kong共同决定项目 Cedar 经历 architecture 的回滚方案，我觉得挑战可控，结果选择 EPISODE_LIVING_23 分阶段验证。", capture=True))
        first_event = first.state.run_id + ":goal"
        core.queue_formation(auth, first_event, bottle_id="A")
        core.run_formation(auth)
        assert not any(r.kind == "episodic" for r in core.list_records(auth).records)

        instant += timedelta(days=1)
        for i in range(2):
            feedback = asyncio.run(session("我喜欢简洁回答。", capture=True))
            event_id = feedback.state.run_id + ":goal"
            core.queue_formation(auth, event_id, bottle_id="A")
            core.run_formation(auth)
            item = next(c for c in core.list_formation_candidates(auth) if c.source.source_id == event_id)
            plan = core.preview_consolidation(auth, item.candidate_id)
            core.consolidate_candidate(owner(f"approve-feedback-{i}"), item.candidate_id, approved_digest=plan.digest, command_id=f"approve-feedback-{i}")
        supplement = asyncio.run(session("2026-10-02 我和Kong共同决定项目 Cedar 经历 architecture 的补充，结果建立五项回滚验证标准。", capture=True))
        core.queue_formation(auth, supplement.state.run_id + ":goal", bottle_id="A")
        core.run_formation(auth)

        instant += timedelta(days=1)
        corrected = "Cedar deadline DATE_NEW_19"
        core.revise_memory(owner("correction"), deadline.record_id, corrected,
            Source(source_id="correction", origin="user_command", speaker="user", excerpt=corrected), expected_revision=1, command_id="correction")
        corrected_session = asyncio.run(session("What is the Cedar deadline?", thread_id=first.thread.thread_id))
        assert corrected_session.state.final_output == corrected
        assert "DATE_OLD_09" not in json.dumps(packets[-1].messages)

        instant += timedelta(days=1)
        episode_id = None
        for event_id in (first_event, supplement.state.run_id + ":goal"):
            item = next(c for c in core.list_formation_candidates(auth) if c.source.source_id == event_id)
            plan = core.preview_consolidation(auth, item.candidate_id)
            result = core.consolidate_candidate(owner("approve-" + event_id), item.candidate_id, approved_digest=plan.digest, command_id="approve-" + event_id)
            if episode_id:
                assert plan.operation == "extend_episode" and result.record_id == episode_id
            episode_id = result.record_id
        core.pin_memory(owner("cherish"), episode_id, "A", command_id="cherish")
        episode = core.get_record(auth, episode_id)
        assert episode.current.version == 2 and len(episode.current.episode.evidence_source_ids) == 2
        assert episode.current.episode.user_expression == ("我觉得挑战可控",)
        recalled = asyncio.run(session("What was our Cedar shared experience?"))
        assert "EPISODE_LIVING_23" in recalled.state.final_output
        assert any(r.get("episode") for r in packets[-1].recalled_memories)

        instant += timedelta(days=1)
        maintenance = auth.model_copy(update={"actor": "maintenance", "input_id": None,
            "capabilities": frozenset({Capability.READ, Capability.PROPOSE, Capability.REFLECTION})})
        reflection = core.run_reflection(maintenance, command_id="day-six", budget=ReflectionBudget())
        report = core.get_reflection_report(auth, reflection.reflection_id)
        assert report.model_calls == 0 and len(report.proposal_ids) == 2
        assert core.get_relationship(auth) is None and core.get_identity(auth).identity == identity

        instant += timedelta(days=1)
        for proposal in core.list_proposals(auth):
            if proposal.target == "relationship" and accept_style:
                core.approve_proposal(owner("approve-style"), proposal.proposal_id, expected_revision=1,
                    approved_digest=proposal.approved_digest, command_id="approve-style")
            else:
                core.reject_proposal(owner("reject-" + proposal.target), proposal.proposal_id, expected_revision=1, command_id="reject-" + proposal.target)
        assert core.get_identity(auth).identity == identity
        assert (core.get_relationship(auth) is not None) == accept_style
        boundary_run = asyncio.run(session("不要主动提起我的历史经历。", capture=True))
        boundary_event = boundary_run.state.run_id + ":goal"
        core.queue_formation(auth, boundary_event)
        core.run_formation(auth)
        boundary = next(c for c in core.list_formation_candidates(auth) if c.source.source_id == boundary_event)
        proposed = core.proposal_from_candidate(owner("boundary-proposal"), boundary.candidate_id, command_id="boundary-proposal")
        proposal = core.get_proposal(auth, proposed.proposal_id)
        core.approve_proposal(owner("boundary-approve"), proposal.proposal_id, expected_revision=1,
            approved_digest=proposal.approved_digest, command_id="boundary-approve")
        relationship = core.get_relationship(auth)
        assert relationship.relationship.history_mentions == "on_request"
        assert relationship.relationship.communication_style == ("concise" if accept_style else "balanced")
        style_session = asyncio.run(session("Give a style sample for our interaction."))
        assert (style_session.state.final_output == "简洁回答。") == accept_style

    # Restart the Core and use two distinct simulated adapters across new Sessions.
    for model_type in (ContextReader, AlternateContextReader):
        asyncio.run(session("What was our Cedar shared experience?", model_type=model_type))
        packet = packets[-1]
        assert any("Kong" in i.content for i in packet.items if i.category == "core_identity")
        assert any('"history_mentions":"on_request"' in i.content for i in packet.items if i.category == "core_relationship")
        assert any(r.get("episode") for r in packet.recalled_memories)
    with MemoryService(database, clock=clock) as restarted:
        bottle = next(b for b in restarted.list_bottles(auth) if b.bottle_id == "A")
        restarted.deactivate_bottle(owner("off"), "A", expected_revision=bottle.revision, command_id="off")
        hidden = asyncio.run(session("What was our Cedar shared experience?", thread_id=recalled.thread.thread_id))
        assert hidden.state.final_output == "No authorized shared experience."
        assert "EPISODE_LIVING_23" not in json.dumps(packets[-1].messages)
        bottle = next(b for b in restarted.list_bottles(auth) if b.bottle_id == "A")
        restarted.activate_bottle(owner("on"), "A", expected_revision=bottle.revision, command_id="on")
        plan = restarted.preview_forget(auth, episode_id)
        restarted.forget_memory(owner("forget"), episode_id, expected_revision=plan.expected_revision,
            approved_plan_digest=plan.digest, command_id="forget")
        assert restarted.get_relationship(auth).relationship.history_mentions == "on_request"
    forgotten = asyncio.run(session("What was our Cedar shared experience?", thread_id=recalled.thread.thread_id))
    assert forgotten.state.final_output == "No authorized shared experience."
    assert "EPISODE_LIVING_23" not in json.dumps(packets[-1].messages)
    assert all("PRIVATE_INACTIVE_631" not in json.dumps(p.messages) for p in packets)
    result = {"scenario": "seven-logical-days-v1", "data": "synthetic", "live_model_evaluation": False,
        "behavior_source": "simulated", "accept_style": accept_style,
        "commit_delivery_interrupted": interrupt_delivery, "sessions": sessions,
        "engineering_assertions": {"empty_start": True, "grounded_episode": True, "episode_extension": True,
            "corrected_fact": True, "identity_stable": True, "relationship_review": True,
            "assets_after_adapter_switch": True, "bottle_isolation": True, "forget_context_block": True, "approved_rule_injection": True},
        "model_behavior": {"simulated_replies_only": True, "real_answer_correctness": None, "real_persona_consistency": None}}
    (tmp_path / "seven-day-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("mode", [Mode.FAST, Mode.AUTO, Mode.PLAN])
def test_approved_relationship_uses_shared_context_path_in_all_runtime_modes(tmp_path, mode):
    auth = Authority(owner_id="synthetic-owner", profile_id="kong", input_id="owner", capabilities=frozenset(Capability))
    database = tmp_path / "modes.db"
    relationship = Relationship(role="collaborator", communication_style="concise", history_mentions="on_request")
    with MemoryService(database) as core:
        core.create_profile(auth, command_id="profile")
        core.set_relationship(auth, relationship, expected_revision=0, approved_digest=relationship_digest(relationship), command_id="rules")
    async def build():
        async with MemoryBridge(database, auth, timeout=5) as bridge:
            assert await bridge.get_access_state() is not None
            workspace = Workspace(tmp_path)
            run = Runtime.new("current task", ContextReader(), workspace, default_registry(workspace), mode,
                store=RunStore(tmp_path / ".kong/runs"), memory_bridge=bridge)
            return await run.context_engine.prepare(thread=run.thread, state=run.state, history=run.history, tools=run.registry.definitions())
    packet = asyncio.run(build())
    rule, = [i for i in packet.items if i.category == "core_relationship"]
    assert rule.priority == 0 and '"history_mentions":"on_request"' in rule.content
    assert rule.sources[0].path == "relationship/kong/v1"
    assert packet.memory_degraded is False


def test_corrupt_relationship_degrades_entire_personalized_context(tmp_path):
    import sqlite3
    auth = Authority(owner_id="synthetic-owner", profile_id="kong", input_id="owner", capabilities=frozenset(Capability))
    database = tmp_path / "corrupt.db"
    identity = Identity(name="PRIVATE_IDENTITY_399")
    relationship = Relationship(role="friend")
    with MemoryService(database) as core:
        core.create_profile(auth, command_id="profile")
        core.set_identity(auth, identity, expected_revision=0, approved_digest=identity_digest(identity), command_id="identity")
        core.set_relationship(auth, relationship, expected_revision=0, approved_digest=relationship_digest(relationship), command_id="rules")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE relationship_versions SET content='corrupt json'")
    async def scenario():
        async with MemoryBridge(database, auth, timeout=5) as bridge:
            await bridge.get_access_state()
            workspace = Workspace(tmp_path)
            model = ContextReader()
            run = Runtime.new("ordinary task", model, workspace, default_registry(workspace), store=RunStore(tmp_path / ".kong/runs"), memory_bridge=bridge)
            await run.run()
            return model.packet
    packet = asyncio.run(scenario())
    assert packet.memory_degraded and not packet.recalled_memories
    assert not any(i.category in {"core_identity", "core_relationship"} for i in packet.items)
    assert "PRIVATE_IDENTITY_399" not in json.dumps(packet.messages)
