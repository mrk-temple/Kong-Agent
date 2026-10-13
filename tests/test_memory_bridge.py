"""Offline product-loop tests. Context-reading fake replies are not live LLM accuracy."""
import asyncio
import json
import sqlite3
import threading

import pytest

from kong.application.memory_bridge import MemoryBridge
from kong.continuity.engine import ContextEngine
from kong.continuity.memory_store import MemoryStore
from kong.contracts import Act, Action, Criterion, FinalResponse, Mode, Plan, PlanProposal, PlanStep, Status
from kong.memory import Authority, Bottle, Capability, Identity, MemoryDraft, MemoryService, PermissionDenied, Source, identity_digest
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def test_observation_persisted_consent_restart_replay_and_no_automatic_memory(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            off = runtime(tmp_path, ScriptedModel([FinalResponse(content="off reply")]), bridge, goal="off input")
            await off.run()
            on = runtime(tmp_path, ScriptedModel([FinalResponse(content="on reply")]), bridge, goal="on input", capture=True)
            await on.run()
            off_snapshot = off.store.load(off.state.run_id)
            on_snapshot = on.store.load(on.state.run_id)
            assert off_snapshot.memory_binding["capture"] == "off"
            assert on_snapshot.memory_binding["capture"] == "on"
            with MemoryService(db) as core:
                rows = core.list_observations(owner())
                assert len(rows) == 4
                assert len([o for o in rows if o["payload"] is None]) == 2
                assert any("on reply" in (o["payload"] or "") for o in rows)
                assert not core.list_records(owner()).records
            workspace = Workspace(tmp_path)
            with pytest.raises(ValueError, match="original Memory capture"):
                Runtime.restore(off_snapshot, ScriptedModel([]), workspace, default_registry(workspace),
                                store=off.store, memory_bridge=bridge, memory_capture=True)
        # New adapter/process cannot backfill old off events or duplicate on ones.
        async with MemoryBridge(db, owner()) as restarted:
            assert await restarted.observe_snapshot(off_snapshot)
            assert await restarted.observe_snapshot(on_snapshot)
            with MemoryService(db) as core:
                rows = core.list_observations(owner())
                assert len(rows) == 4 and all(o["capture"] == "off" for o in rows if o["event_id"].startswith(off.state.run_id))
    asyncio.run(scenario())


def test_observation_excludes_rejected_questions_and_completion_attempts(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([]), bridge, capture=True)
            await prepare(run)
            run.history.append("decision", 1, decision={"kind": "ask", "question": "REJECTED_QUESTION"})
            run.history.append("feedback", 1, ask_rejected=["missing prerequisite"])
            run.history.append("decision", 2, decision={"kind": "respond", "content": "REJECTED_REPLY"})
            run.history.append("feedback", 2, completion_rejected=["missing evidence"])
            run.history.append("decision", 3, decision={"kind": "ask", "question": "ACCEPTED_QUESTION"})
            run.history.append("feedback", 3, transition={"reason": "user_input_required"})
            run.save()
            assert await bridge.observe_snapshot(run.store.load(run.state.run_id))
            with MemoryService(db) as core:
                payload = str(core.list_observations(owner()))
                assert "ACCEPTED_QUESTION" in payload
                assert "REJECTED_QUESTION" not in payload and "REJECTED_REPLY" not in payload
    asyncio.run(scenario())


def test_observation_keeps_original_goal_when_accepted_goal_changes(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()
        class WaitingModel:
            calls = 0
            async def generate(self, packet):
                self.calls += 1
                if self.calls == 1:
                    ready.set()
                    await release.wait()
                return FinalResponse(content="Accepted answer")
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, WaitingModel(), bridge, goal="Original goal", capture=True)
            task = asyncio.create_task(run.run())
            await asyncio.wait_for(ready.wait(), 5)
            run.save()
            assert await bridge.observe_snapshot(run.store.load(run.state.run_id))
            run.submit_input("goal", "Updated goal")
            release.set()
            await asyncio.wait_for(task, 5)
            with MemoryService(db) as core:
                events = core.list_observations(owner())
                assert len(events) == 3
                goal = next(e for e in events if e["event_id"].endswith(":goal"))
                assert "Original goal" in goal["payload"] and "Updated goal" not in goal["payload"]
                assert any("Updated goal" in (e["payload"] or "") for e in events if e["event_id"] != goal["event_id"])
    asyncio.run(scenario())


def test_corrupt_identity_data_degrades_without_reusing_personalized_context(tmp_path):
    db = tmp_path / "core.db"
    initialize(db, identity=Identity(name="Private identity"))
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE identity_versions SET content='invalid json'")
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([FinalResponse(content="safe reply")]), bridge)
            await run.run()
            packet = run.context_engine.last_packet
            assert packet.memory_degraded and not packet.recalled_memories
            assert "Private identity" not in json.dumps(packet.model_dump())
            assert run.state.final_output == "safe reply"
    asyncio.run(scenario())


def test_bridge_never_grants_capabilities_missing_from_host_authority(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    with pytest.raises(PermissionDenied):
        MemoryBridge(db, owner().model_copy(update={"capabilities": frozenset()}))
    async def scenario():
        readonly = owner().model_copy(update={"capabilities": frozenset({Capability.READ})})
        async with MemoryBridge(db, readonly) as bridge:
            assert bridge.authority.capabilities == frozenset({Capability.READ}) and not bridge.can_observe
            assert await bridge.get_access_state()
            with pytest.raises(ValueError, match="memory.observe"):
                runtime(tmp_path, ScriptedModel([]), bridge, capture=True)
            run = runtime(tmp_path, ScriptedModel([FinalResponse(content="readonly reply")]), bridge)
            await run.run()
            assert not await bridge.observe_snapshot(run.store.load(run.state.run_id))
            with MemoryService(db) as core:
                assert core.list_observations(owner()) == ()
    asyncio.run(scenario())


def test_unbound_failed_read_cannot_reauthorize_old_text_on_recovery_or_restore(tmp_path):
    db = tmp_path / "late-core.db"
    phrase = "Cedar UNKNOWN_ORIGIN_945"
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([]), bridge, goal=phrase)
            degraded = await prepare(run)
            assert degraded.memory_degraded and phrase in json.dumps(degraded.model_dump())
            assert run.context_engine.memory_binding["access"] is None
            run.save()
            unknown = run.store.load(run.state.run_id)
            initialize(db)
            record = save(db, phrase)
            with MemoryService(db) as core:
                core.forget_memory(owner(), record.record_id, expected_revision=1, command_id="forget")
            recovered = await prepare(run)
            assert phrase not in json.dumps(recovered.model_dump())
            assert run.context_engine.memory_binding["access"] is None
        async with MemoryBridge(db, owner()) as bridge:
            workspace = Workspace(tmp_path)
            restored = Runtime.restore(unknown, ScriptedModel([]), workspace, default_registry(workspace),
                                       store=run.store, memory_bridge=bridge)
            packet = await prepare(restored)
            assert phrase not in json.dumps(packet.model_dump())
            assert restored.context_engine.memory_binding["access"] is None
            assert not (await restored.registry.get("recall_history").run(run.state.run_id)).success
    asyncio.run(scenario())


def test_core_database_and_sidecars_are_reserved_from_ordinary_file_tools(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([]), bridge)
            for suffix in ("", "-wal", "-shm", "-journal"):
                with pytest.raises(ValueError, match="reserved"):
                    run.workspace.resolve("core.db" + suffix)
            reading = await run.registry.get("read_file").run(path="core.db")
            assert not reading.success and "reserved" in reading.error
            with pytest.raises(ValueError, match="reserved"):
                await run.registry.get("write_file").run(path="core.db", content="not allowed")
            with MemoryService(db) as core:
                assert core.get_profile(owner()).profile_id == "kong-a"
    asyncio.run(scenario())


def test_legacy_restore_cannot_reauthorize_history_or_enable_retroactive_capture(tmp_path):
    phrase = "Cedar LEGACY_RESTORE_PRIVATE_561"
    workspace = Workspace(tmp_path)
    store = RunStore(tmp_path / ".kong/runs")
    legacy = Runtime.new(phrase, ScriptedModel([FinalResponse(content=phrase)]), workspace, default_registry(workspace), store=store)
    asyncio.run(legacy.run())
    snapshot = store.load(legacy.state.run_id)
    assert snapshot.memory_binding is None
    db = tmp_path / "core.db"
    initialize(db)
    saved = save(db, phrase)
    with MemoryService(db) as core:
        core.forget_memory(owner(), saved.record_id, expected_revision=1, command_id="forget")
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            with pytest.raises(ValueError, match="Legacy Runs"):
                Runtime.restore(snapshot, ScriptedModel([]), workspace, default_registry(workspace),
                                store=store, memory_bridge=bridge, memory_capture=True)
            restored = Runtime.restore(snapshot, ScriptedModel([]), workspace, default_registry(workspace), store=store, memory_bridge=bridge)
            packet = await prepare(restored)
            assert phrase not in json.dumps(packet.model_dump())
            assert restored.context_engine.memory_binding["access"] is None
            assert restored.context_engine.memory_binding["capture"] == "off"
            assert not (await restored.registry.get("recall_history").run(legacy.state.run_id)).success
            restored.save()
            assert await bridge.observe_snapshot(store.load(legacy.state.run_id))
            with MemoryService(db) as core:
                assert all(o["payload"] is None and o["capture"] == "off" for o in core.list_observations(owner()))
    asyncio.run(scenario())


def owner(profile="kong-a", input_id="input"):
    return Authority(owner_id="test-owner", profile_id=profile, input_id=input_id, capabilities=frozenset(Capability))


def initialize(path, profile="kong-a", identity=None):
    with MemoryService(path) as core:
        auth = owner(profile)
        core.create_profile(auth, command_id="profile")
        if identity:
            core.set_identity(auth, identity, expected_revision=0, approved_digest=identity_digest(identity), command_id="identity")
        for name in ("A", "B"):
            core.create_bottle(auth, Bottle(bottle_id=name, profile_id=profile, title=name), command_id="create-" + name)
        core.activate_bottle(auth, "A", expected_revision=1, command_id="activate-A")


def save(path, text, *, bottle="A", profile="kong-a", source_id="event-one"):
    with MemoryService(path) as core:
        auth = owner(profile, source_id)
        result = core.remember(auth, MemoryDraft(kind="episodic", content=text),
                               Source(source_id=source_id, origin="user_command", speaker="user", excerpt=text), command_id="save-" + source_id)
        core.add_memory_to_bottle(auth, bottle, result.record_id, command_id="member-" + source_id)
        return result


def runtime(path, model, bridge, goal="Cedar project", *, thread_id=None, mode=Mode.AUTO, capture=None):
    workspace = Workspace(path)
    return Runtime.new(goal, model, workspace, default_registry(workspace), mode,
                       store=RunStore(path / ".kong/runs"), thread_id=thread_id, memory_bridge=bridge, memory_capture=capture)


async def prepare(run):
    return await run.context_engine.prepare(thread=run.thread, state=run.state, history=run.history, tools=run.registry.definitions())


def test_empty_profile_and_stable_identity_across_restart(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    async def first():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([FinalResponse(content="hello")]), bridge)
            await run.run()
            assert not any(i.category == "core_identity" for i in run.context_engine.last_packet.items)
            assert not run.context_engine.last_packet.recalled_memories
    asyncio.run(first())
    identity = Identity(name="Cedar Kong", communication_style="calm and concise")
    with MemoryService(db) as core:
        core.set_identity(owner(), identity, expected_revision=0, approved_digest=identity_digest(identity), command_id="identity")
    async def second():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([FinalResponse(content="hello")]), bridge)
            await run.run()
            assert any("Cedar Kong" in i.content for i in run.context_engine.last_packet.items if i.category == "core_identity")
    asyncio.run(second())


def test_episodic_retrieval_and_actual_offline_reply_are_separate_assertions(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    saved = save(db, "Cedar project shipped the blue milestone.")
    class ContextReadingModel:
        async def generate(self, packet):
            self.packet = packet
            text = next((r["content"] for r in packet.recalled_memories if r["id"] == saved.record_id), "No authorized recollection")
            return FinalResponse(content=text)
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            model = ContextReadingModel()
            run = runtime(tmp_path, model, bridge)
            await run.run()
            assert [r["id"] for r in model.packet.recalled_memories] == [saved.record_id]
            assert run.state.final_output == "Cedar project shipped the blue milestone."
            return run.thread.thread_id
    thread = asyncio.run(scenario())
    async def restarted():
        async with MemoryBridge(db, owner()) as bridge:
            model = ContextReadingModel()
            run = runtime(tmp_path, model, bridge, thread_id=thread)
            await run.run()
            assert run.state.final_output == "Cedar project shipped the blue milestone."
    asyncio.run(restarted())


def test_switching_bottles_invalidates_packet_and_history(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    a = save(db, "Cedar A visible milestone", source_id="a")
    b = save(db, "Cedar B hidden milestone", bottle="B", source_id="b")
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([FinalResponse(content="Cedar A visible milestone")]), bridge)
            await run.run()
            packet = run.context_engine.last_packet
            assert [r["id"] for r in packet.recalled_memories] == [a.record_id]
            assert "B hidden" not in json.dumps(packet.model_dump())
            with MemoryService(db) as core:
                bottles = {v.bottle_id: v for v in core.list_bottles(owner())}
                core.deactivate_bottle(owner(), "A", expected_revision=bottles["A"].revision, command_id="off-A")
                core.activate_bottle(owner(), "B", expected_revision=bottles["B"].revision, command_id="on-B")
            assert not await run.context_engine.validate_packet(packet)
            assert run.context_engine.last_packet is None
            next_run = runtime(tmp_path, ScriptedModel([]), bridge, thread_id=run.thread.thread_id)
            current = await prepare(next_run)
            assert [r["id"] for r in current.recalled_memories] == [b.record_id]
            assert "A visible" not in json.dumps(current.model_dump())
            recall = await next_run.registry.get("recall_history").run(run.state.run_id)
            assert not recall.success
    asyncio.run(scenario())


def test_forget_blocks_recent_raw_legacy_packets_and_restored_history(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    phrase = "Cedar PRIVATE_EVENT_937"
    saved = save(db, phrase)
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([FinalResponse(content=phrase)]), bridge, goal=phrase)
            await run.run()
            old = run.context_engine.last_packet
            assert phrase in json.dumps(old.model_dump())
            # Even an old packet manually placed in the legacy store is never reused.
            legacy = MemoryStore(tmp_path / ".kong/context.db")
            legacy.save_packet(old)
            with MemoryService(db) as core:
                receipt = core.forget_memory(owner(), saved.record_id, expected_revision=1, command_id="forget")
                assert receipt.deletion_epoch == 1 and not receipt.runtime_history_purged
            assert not await run.context_engine.validate_packet(old)
            fresh = await prepare(run)
            assert phrase not in json.dumps(fresh.model_dump())
            next_run = runtime(tmp_path, ScriptedModel([]), bridge, thread_id=run.thread.thread_id, goal="Cedar status")
            fresh = await prepare(next_run)
            assert phrase not in json.dumps(fresh.model_dump())
            assert fresh.episodes == [] and fresh.thread_memory == []
            assert not (await next_run.registry.get("recall_history").run(run.state.run_id, event_index=0)).success
            snapshot = run.store.load(run.state.run_id)
            restored = Runtime.restore(snapshot, ScriptedModel([]), run.workspace, default_registry(run.workspace), store=run.store, memory_bridge=bridge)
            assert phrase not in json.dumps((await prepare(restored)).model_dump())
            with pytest.raises(ValueError):
                Runtime.restore(snapshot, ScriptedModel([]), run.workspace, default_registry(run.workspace), store=run.store)
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", list(Mode))
def test_runtime_three_modes_keep_controller_lease_and_permissions(tmp_path, mode):
    db = tmp_path / "core.db"
    initialize(db)
    async def scenario():
        # This case checks controller policy with a healthy Core, not cold disk
        # timing. Timeout/fail-closed behavior has its own explicit deadline test.
        async with MemoryBridge(db, owner(), timeout=5) as bridge:
            assert await bridge.get_access_state() is not None
            decision = (PlanProposal(plan=Plan(goal="hello", rationale="review", steps=[PlanStep(id="s1", title="work")],
                        success_criteria=[Criterion(id="c1", description="artifact", kind="file_exists", path="artifact.txt")]))
                        if mode == Mode.PLAN else FinalResponse(content="plain answer"))
            model = ScriptedModel([decision])
            run = runtime(tmp_path, model, bridge, goal="hello", mode=mode)
            before = (run.state.turn_budget, run.state.hard_turn_limit)
            await run.run()
            expected = (4, 4) if mode == Mode.FAST else (4, 40) if mode == Mode.AUTO else (6, 60)
            assert before == expected
            assert run.state.status == (Status.WAITING_USER if mode == Mode.PLAN else Status.COMPLETED), run.state.stop_reason
            if mode == Mode.PLAN:
                assert run.state.pending_approval.revision == 1
                assert run.state.plan.approved_revision is None
                assert not run.history.observations
            assert run.executor is not None and run.controller is not None
            assert run.context_engine.store is None
            assert run.store.load(run.state.run_id).schema_version == 6
    asyncio.run(scenario())


def test_wrong_profile_and_wrong_database_do_not_supply_identity_or_history(tmp_path):
    db = tmp_path / "core.db"
    initialize(db, identity=Identity(name="A private name"))
    initialize(db, profile="kong-b", identity=Identity(name="B private name"))
    async def scenario():
        async with MemoryBridge(db, owner()) as a, MemoryBridge(db, owner("kong-b")) as b:
            run = runtime(tmp_path, ScriptedModel([FinalResponse(content="ok")]), a)
            await run.run()
            with pytest.raises(ValueError):
                runtime(tmp_path, ScriptedModel([]), b, thread_id=run.thread.thread_id)
            packet = await prepare(runtime(tmp_path, ScriptedModel([]), b))
            assert "A private name" not in json.dumps(packet.model_dump())
            assert "B private name" in json.dumps(packet.model_dump())
            return run.store.load(run.state.run_id), run
    snapshot, original = asyncio.run(scenario())
    wrong = tmp_path / "different.db"
    initialize(wrong, identity=Identity(name="WRONG_DATABASE_NAME"))
    async def restore_wrong():
        async with MemoryBridge(wrong, owner()) as bridge:
            run = Runtime.restore(snapshot, ScriptedModel([]), original.workspace, default_registry(original.workspace), store=original.store, memory_bridge=bridge)
            packet = await prepare(run)
            assert "WRONG_DATABASE_NAME" not in json.dumps(packet.model_dump())
            assert packet.memory_degraded
    asyncio.run(restore_wrong())


def test_unavailable_memory_safe_degradation_and_no_legacy_double_write(tmp_path):
    async def scenario():
        async with MemoryBridge(tmp_path / "missing.db", owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([FinalResponse(content="current input answered")]), bridge, goal="ordinary hello")
            await run.run()
            assert run.state.status == Status.COMPLETED
            assert run.context_engine.last_packet.memory_degraded
            assert not (tmp_path / "missing.db").exists()
            db = sqlite3.connect(tmp_path / ".kong/context.db")
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='long_term_memories'").fetchone()
            db.close()
            with pytest.raises(ValueError):
                await run.memory_command("rebuild")
    asyncio.run(scenario())


def test_context_assembly_delete_race_and_inflight_decision_are_guarded(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    saved = save(db, "Cedar race private")
    class DeletingModel:
        async def generate(self, packet):
            with MemoryService(db) as core:
                core.forget_memory(owner(), saved.record_id, expected_revision=1, command_id="during-model")
            return Act(actions=[Action(name="write_file", arguments={"path": "should-not-exist.txt", "content": "private"})])
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, DeletingModel(), bridge)
            await run.run()
            assert run.state.status == Status.WAITING_USER
            assert not (tmp_path / "should-not-exist.txt").exists()
            assert not any(e.type == "decision" for e in run.history.events)
            assert run.context_engine.last_packet is None
    asyncio.run(scenario())


def test_io_service_created_used_and_closed_on_one_worker(tmp_path, monkeypatch):
    db = tmp_path / "core.db"
    initialize(db)
    main = threading.get_ident()
    calls = []
    class Tracked(MemoryService):
        def __init__(self, *args, **kwargs):
            calls.append(threading.get_ident())
            super().__init__(*args, **kwargs)
        def get_access_state(self, auth):
            calls.append(threading.get_ident())
            return super().get_access_state(auth)
        def close(self):
            calls.append(threading.get_ident())
            super().close()
    monkeypatch.setattr("kong.application.memory_bridge.MemoryService", Tracked)
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            assert await bridge.get_access_state()
            assert await bridge.get_access_state()
    asyncio.run(scenario())
    assert len(set(calls)) == 1 and calls[0] != main


def test_core_rejects_legacy_builder_and_store(tmp_path):
    from kong.context import ContextBuilder
    bridge = MemoryBridge(tmp_path / "core.db", owner())
    try:
        with pytest.raises(ValueError):
            ContextEngine(ContextBuilder(), memory_bridge=bridge)
        with pytest.raises(ValueError):
            workspace = Workspace(tmp_path)
            Runtime.new("hello", ScriptedModel([]), workspace, default_registry(workspace),
                        store=RunStore(tmp_path / ".kong/runs"), memory_bridge=bridge, memory_store=MemoryStore(tmp_path / "legacy.db"))
    finally:
        asyncio.run(bridge.aclose())


def test_legacy_sources_are_quarantined_and_mixed_thread_cannot_downgrade(tmp_path):
    db = tmp_path / "core.db"
    initialize(db)
    workspace = Workspace(tmp_path)
    store = RunStore(tmp_path / ".kong/runs")
    old = Runtime.new("以后叫我LEGACY_PRIVATE_87", ScriptedModel([FinalResponse(content="LEGACY_PRIVATE_87")]),
                      workspace, default_registry(workspace), store=store)
    asyncio.run(old.run())
    legacy = MemoryStore(tmp_path / ".kong/context.db")
    assert legacy.episodes(old.thread.thread_id)  # Real old compaction output.
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([FinalResponse(content="new answer")]), bridge, goal="hello", thread_id=old.thread.thread_id)
            await run.run()
            assert "LEGACY_PRIVATE_87" not in json.dumps(run.context_engine.last_packet.model_dump())
            assert not (await run.registry.get("recall_history").run(old.state.run_id)).success
            # First Run was legacy; scanning only the first membership is unsafe.
            with pytest.raises(ValueError):
                Runtime.new("hello", ScriptedModel([]), workspace, default_registry(workspace), store=store, thread_id=old.thread.thread_id)
    asyncio.run(scenario())


def test_assembly_race_rebuilds_instead_of_releasing_stale_packet(tmp_path, monkeypatch):
    db = tmp_path / "core.db"
    initialize(db)
    saved = save(db, "Cedar RACE_AT_ASSEMBLY_62")
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            run = runtime(tmp_path, ScriptedModel([]), bridge)
            allocate = run.context_engine.budget_manager.allocate
            once = [False]
            def race(*args):
                if not once[0]:
                    once[0] = True
                    with MemoryService(db) as core:
                        core.forget_memory(owner(), saved.record_id, expected_revision=1, command_id="race")
                return allocate(*args)
            monkeypatch.setattr(run.context_engine.budget_manager, "allocate", race)
            packet = await prepare(run)
            assert packet.memory_access["deletion_epoch"] == 1
            assert "RACE_AT_ASSEMBLY_62" not in json.dumps(packet.model_dump())
    asyncio.run(scenario())


def test_io_timeout_has_no_unbounded_queue_or_stale_result(tmp_path, monkeypatch):
    import time
    db = tmp_path / "core.db"
    initialize(db)
    started = threading.Event()
    release = threading.Event()
    class Slow(MemoryService):
        def get_access_state(self, auth):
            started.set()
            release.wait(5)
            return super().get_access_state(auth)
    monkeypatch.setattr("kong.application.memory_bridge.MemoryService", Slow)
    async def scenario():
        async with MemoryBridge(db, owner(), timeout=0.01) as bridge:
            # Start real worker I/O before measuring its deadline; constructing
            # SQLite and scheduling a new thread need not finish within 10 ms.
            submit = bridge._pool.submit
            pending = submit(bridge._invoke, "get_access_state", ())
            assert await asyncio.to_thread(started.wait, 5)
            submissions = 0

            def started_io(operation, arguments):
                nonlocal submissions
                assert operation == bridge._invoke and arguments == ("get_access_state", ())
                submissions += 1
                return pending

            # ThreadPoolExecutor.submit receives the callable and its arguments.
            def submit_pending(operation, *arguments):
                return started_io(operation, arguments)

            monkeypatch.setattr(bridge._pool, "submit", submit_pending)
            try:
                before = time.perf_counter()
                assert await bridge.get_access_state() is None
                for _ in range(20):
                    assert await bridge.get_access_state() is None
                assert time.perf_counter() - before < 0.5
                assert submissions == 1
            finally:
                release.set()
            assert await asyncio.wrap_future(pending) is not None
            monkeypatch.setattr(bridge._pool, "submit", submit)
            revoked = threading.Event()

            def unavailable(*_):
                revoked.set()
                raise PermissionDenied("fixture access revoked after late I/O")

            monkeypatch.setattr(bridge, "_invoke", unavailable)
            bridge.timeout = 5
            assert await bridge.get_access_state() is None
            assert revoked.is_set()  # The previous late success was not reused.
    asyncio.run(scenario())


def test_bridge_does_not_grant_tool_permission(tmp_path):
    from kong.tools.permissions import PermissionPolicy
    db = tmp_path / "core.db"
    initialize(db)
    async def scenario():
        async with MemoryBridge(db, owner()) as bridge:
            workspace = Workspace(tmp_path)
            model = ScriptedModel([Act(actions=[Action(name="write_file", arguments={"path": "denied.txt", "content": "x"})]),
                                   FinalResponse(content="write denied")])
            run = Runtime.new("write if allowed", model, workspace, default_registry(workspace), store=RunStore(tmp_path / ".kong/runs"),
                              memory_bridge=bridge, permissions=PermissionPolicy(allowed_tools=["read_file"]))
            await run.run()
            assert not (tmp_path / "denied.txt").exists()
            assert any(not o.success for o in run.history.observations)
    asyncio.run(scenario())
