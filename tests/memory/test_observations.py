from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from kong.memory import IdempotencyConflict, InteractionObservation, MemoryDraft, MemoryService, Scope, Source


def test_capture_off_is_immutable_metadata_and_never_backfilled(core, auth):
    access = core.get_access_state(auth)
    event = InteractionObservation(event_id="old-event", capture="off", original_access=access, role="user", text=None)
    before = core.get_profile(auth)
    first = core.observe_interaction(auth, event)
    assert first.status == "skipped"
    assert core.observe_interaction(auth, event).replayed
    assert core.get_profile(auth) == before
    assert core.list_records(auth).records == ()
    assert core.list_observations(auth)[0]["payload"] is None
    with pytest.raises(IdempotencyConflict):
        core.observe_interaction(auth, event.model_copy(update={"capture": "on", "text": "old private conversation"}))
    assert core.list_observations(auth)[0]["capture"] == "off"


def test_capture_on_requires_original_current_authorization_and_forget_purges_payload(core, auth):
    access = core.get_access_state(auth)
    event = InteractionObservation(event_id="accepted", capture="on", original_access=access, role="user", text="Cedar conversation")
    assert core.observe_interaction(auth, event).status == "observed"
    assert "Cedar" in core.list_observations(auth)[0]["payload"]
    saved = core.remember(auth, MemoryDraft(kind="episodic", content="Cedar memory"),
        Source(source_id=auth.input_id, origin="user_command", speaker="user", excerpt="Cedar memory"), command_id="save")
    assert core.observe_interaction(auth, event.model_copy(update={"event_id": "late"})).status == "skipped"
    receipt = core.forget_memory(auth, saved.record_id, expected_revision=1, command_id="forget")
    assert receipt.purged_observations == 1
    assert all(o["payload"] is None for o in core.list_observations(auth))
    replay = core.observe_interaction(auth, event)
    assert replay.status == "suppressed" and replay.replayed
    assert core.list_records(auth).records == ()


def test_observation_sensitive_filter_expiry_and_bounded_inbox(core, auth):
    instant = datetime(2026, 10, 9, tzinfo=timezone.utc)
    core.clock = lambda: instant.isoformat()
    access = core.get_access_state(auth)
    base = InteractionObservation(event_id="secret", capture="on", original_access=access, role="user", text="api_key: privatekey1234567")
    assert core.observe_interaction(auth, base).status == "skipped"
    assert "privatekey" not in str(core.list_observations(auth))
    good = base.model_copy(update={"event_id": "good", "text": "Ordinary observation"})
    assert core.observe_interaction(auth, good).status == "observed"
    instant += timedelta(hours=49)
    assert all(o["payload"] is None for o in core.list_observations(auth))
    assert core.observe_interaction(auth, good).status == "expired"
    core.observe_interaction(auth, good.model_copy(update={"event_id": "new"}))
    assert next(o for o in core.list_observations(auth) if o["event_id"] == "good")["status"] == "expired"
    # Fill only transport metadata efficiently, then verify the actual service cap.
    with core._store.transaction() as db:
        db.executemany("INSERT INTO interaction_inbox VALUES(?,?,'digest','on','observed','payload',?,?)",
            [(auth.profile_id, f"filled-{i}", instant.isoformat(), (instant + timedelta(hours=48)).isoformat()) for i in range(999)])
    assert core.observe_interaction(auth, good.model_copy(update={"event_id": "over-cap"})).status == "skipped"


def test_observation_inspection_does_not_widen_scope(core, auth):
    broader = auth.model_copy(update={"scopes": (Scope(), Scope(kind="workspace", id="private-workspace"))})
    event = InteractionObservation(event_id="scoped", capture="on", original_access=core.get_access_state(broader),
                                   role="user", text="Workspace private observation")
    core.observe_interaction(broader, event)
    assert core.list_observations(auth)[0]["payload"] is None
    assert "Workspace private" in core.list_observations(broader)[0]["payload"]


def test_observation_racing_forget_never_leaves_a_payload(core, auth):
    saved = core.remember(auth, MemoryDraft(kind="episodic", content="Cedar episode"),
        Source(source_id=auth.input_id, origin="user_command", speaker="user", excerpt="Cedar episode"), command_id="save")
    event = InteractionObservation(event_id="racing", capture="on", original_access=core.get_access_state(auth), role="user", text="Cedar episode")
    barrier = Barrier(2)
    def observe():
        with MemoryService(core._store.path) as service:
            barrier.wait(timeout=5)
            return service.observe_interaction(auth, event)
    def forget():
        with MemoryService(core._store.path) as service:
            barrier.wait(timeout=5)
            return service.forget_memory(auth, saved.record_id, expected_revision=1, command_id="forget")
    with ThreadPoolExecutor(max_workers=2) as workers:
        observing, deleting = workers.submit(observe), workers.submit(forget)
        observing.result(timeout=10)
        deleting.result(timeout=10)
    assert core.list_observations(auth)[0]["payload"] is None
    assert core.list_records(auth).records == ()
