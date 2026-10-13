from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from kong.memory import (
    Bottle, FormationBudget, InteractionObservation, MemoryDraft, MemoryService,
    NotFound, PermissionDenied, PolicyViolation, Scope, Source, candidate_digest,
)


def observe(core, auth, text, event_id, *, capture="on", role="user", bottle=None):
    event = InteractionObservation(event_id=event_id, capture=capture, original_access=core.get_access_state(auth), role=role, text=text if capture == "on" else None)
    core.observe_interaction(auth, event)
    return core.queue_formation(auth, event_id, bottle_id=bottle) if capture == "on" else event


def approve(core, auth, item, command, **kwargs):
    plan = core.preview_consolidation(auth, item.candidate_id)
    result = core.consolidate_candidate(auth.model_copy(update={"input_id": command}), item.candidate_id,
        approved_digest=plan.digest, command_id=command, **kwargs)
    return result, plan


def test_grounded_episode_review_restart_and_selected_inactive_bottle(core, auth):
    core.create_bottle(auth, Bottle(bottle_id="shared", profile_id=auth.profile_id, title="Cherished"), command_id="bottle")
    text = "2026-10-02 我和Kong共同完成项目 Cedar 经历 prototype 的验证，我觉得放心，结果修复了崩溃。"
    job = observe(core, auth, text, "episode-1", bottle="shared")
    assert core.queue_formation(auth, "episode-1", bottle_id="shared") == job
    report = core.run_formation(auth)
    assert report["model_calls"] == 0 and len(report["jobs"]) == 1
    assert not core.list_records(auth).records
    item, = core.list_formation_candidates(auth)
    assert item.episode.project_id == "Cedar"
    assert item.episode.participants == ("owner", "kong")
    assert item.episode.when.date().isoformat() == "2026-10-02"
    assert item.episode.evidence_source_ids == ("episode-1",)
    assert item.episode.user_expression == ("我觉得放心",)
    assert item.episode.outcomes == ("结果修复了崩溃",)
    assert "结果" not in item.episode.what and item.episode.importance_reason
    with pytest.raises(PolicyViolation, match="consolidation"):
        core.confirm_candidate(auth, item.candidate_id, expected_revision=1, approved_digest=candidate_digest(item.draft), command_id="wrong-path")
    result, _ = approve(core, auth, item, "approve-episode")
    assert core.list_bottles(auth)[0].active is False
    assert not core.retrieve_context(auth, "Cedar").records
    with MemoryService(core._store.path) as restarted:
        record = restarted.get_record(auth, result.record_id)
        assert record.current.episode.confirmed_by == "approve-episode"
        assert {s.source_id for s in record.current.sources} == {"episode-1", "approve-episode"}
    assert core.run_formation(auth)["jobs"] == []


@pytest.mark.parametrize("text,role", [
    ("小说故事：我和Kong共同完成项目 Cedar", "user"),
    ("如果我和Kong共同完成项目 Cedar", "user"),
    ("他说我喜欢简洁回答", "user"),
    ("我猜用户喜欢简洁回答", "assistant"),
    ("今天的天气挺好", "user"),
    ("我喜欢简洁回答吗？", "user"),
    ("我住哪里？", "user"),
    ("请和我共同完成项目 Cedar 经历 verify", "user"),
    ("我和Kong将共同完成项目 Cedar 经历 verify", "user"),
    ("明天我和Kong共同决定项目 Cedar 的架构", "user"),
    ("Please help me confirm the project Cedar deadline", "user"),
    ("下周我和Kong共同完成项目 Cedar 验证。", "user"),
    ("下个月我和Kong一起决定项目 Cedar 的方案。", "user"),
    ("我希望和你共同解决项目 Cedar 的故障。", "user"),
    ("Next week with you we together resolved project Cedar.", "user"),
    ("我今天喜欢简洁回答，只限今天。", "user"),
    ("I prefer concise responses for today only.", "user"),
    ("我朋友林某患有抑郁症，我喜欢简洁回答。", "user"),
    ("My colleague has a medical diagnosis; I prefer concise responses.", "user"),
])
def test_fiction_quote_inference_and_chatter_never_become_real_facts(core, auth, text, role):
    observe(core, auth, text, "ambiguous", role=role)
    assert core.run_formation(auth)["jobs"][0]["candidates"] == 0
    assert not core.list_formation_candidates(auth)


def test_completed_episode_with_later_future_wish_still_uses_actual_evidence(core, auth):
    observe(core, auth, "昨天我和Kong共同解决项目 Cedar 的故障，我希望下周休息，结果恢复服务。", "completed")
    assert core.run_formation(auth)["jobs"][0]["candidates"] == 1
    item, = core.list_formation_candidates(auth)
    assert item.episode.what == "昨天我和Kong共同解决项目 Cedar 的故障"
    assert item.episode.outcomes == ("结果恢复服务",)
    assert not core.list_records(auth).records


def test_capture_off_scope_and_sensitivity_are_not_bypassed(core, auth):
    observe(core, auth, "我喜欢简洁回答", "off", capture="off")
    with pytest.raises(PolicyViolation):
        core.queue_formation(auth, "off")
    secret = InteractionObservation(event_id="secret", capture="on", original_access=core.get_access_state(auth), role="user", text="api_key: FAKE_SECRET_123456")
    assert core.observe_interaction(auth, secret).status == "skipped"
    with pytest.raises(PolicyViolation):
        core.queue_formation(auth, "secret")
    observe(core, auth, "我喜欢简洁回答", "scope")
    with pytest.raises(PermissionDenied):
        core.queue_formation(auth, "scope", scope=Scope(kind="project", id="other"))


def test_duplicate_conflict_correction_and_pin_protection(core, auth):
    core.create_bottle(auth, Bottle(bottle_id="b", profile_id=auth.profile_id, title="Memories"), command_id="b")
    text = "我喜欢你用简洁的回答。"
    observe(core, auth, text, "pref-1", bottle="b")
    core.run_formation(auth)
    first, _ = approve(core, auth, core.list_formation_candidates(auth)[0], "approve-1")
    core.pin_memory(auth, first.record_id, "b", command_id="pin")
    observe(core, auth, text, "pref-2", bottle="b")
    core.run_formation(auth)
    duplicate, = core.list_formation_candidates(auth)
    again, plan = approve(core, auth, duplicate, "approve-2")
    assert plan.operation == "duplicate" and again.record_id == first.record_id
    assert len(core.list_records(auth).records) == 1
    assert core.list_pinned_memories(auth, "b")[0].record_id == first.record_id
    observe(core, auth, "我更喜欢详细的回答。", "pref-3", bottle="b")
    core.run_formation(auth)
    conflict, = core.list_formation_candidates(auth)
    plan = core.preview_consolidation(auth, conflict.candidate_id)
    assert plan.operation == "conflict" and plan.previous_content == text
    with pytest.raises(PolicyViolation):
        core.consolidate_candidate(auth, conflict.candidate_id, approved_digest=plan.digest, command_id="no-resolution")
    changed, _ = approve(core, auth, conflict, "approve-change", resolve_conflict=True)
    assert changed.version == 3 and "详细" in core.get_record(auth, first.record_id).current.content
    assert core.list_pinned_memories(auth, "b")


def test_same_event_extension_and_different_events_remain_distinct(core, auth):
    observe(core, auth, "我和Kong共同完成项目 Cedar 经历 prototype，结果通过三项验证。", "event-1")
    core.run_formation(auth)
    first, _ = approve(core, auth, core.list_formation_candidates(auth)[0], "first")
    observe(core, auth, "我和Kong共同完成项目 Cedar 经历 prototype 的补充，结果通过五项验证。", "event-2")
    core.run_formation(auth)
    item, = core.list_formation_candidates(auth)
    extended, plan = approve(core, auth, item, "extend")
    assert plan.operation == "extend_episode" and extended.record_id == first.record_id
    episode = core.get_record(auth, first.record_id).current.episode
    assert episode.when is None and episode.user_expression == ()
    assert len(episode.outcomes) == 2
    assert episode.evidence_source_ids == ("event-1", "event-2")
    observe(core, auth, "我和Kong共同完成项目 Cedar 的发布。", "separate")
    core.run_formation(auth)
    item, = core.list_formation_candidates(auth)
    separate, plan = approve(core, auth, item, "create-separate")
    assert plan.operation == "create" and separate.record_id != first.record_id


def test_identity_and_boundary_changes_are_only_proposals(core, auth):
    before = core.get_profile(auth).identity_version
    observe(core, auth, "我想改变你的性格。", "identity")
    observe(core, auth, "不要主动提起我的历史经历。", "boundary")
    core.run_formation(auth)
    items = core.list_formation_candidates(auth)
    assert len(items) == 2
    for item in items:
        with pytest.raises(PolicyViolation):
            approve(core, auth, item, "approve-" + item.candidate_id)
    assert core.get_profile(auth).identity_version == before
    assert not core.list_records(auth).records


def test_retry_lease_crash_recovery_and_expiry(core, auth, monkeypatch):
    instant = datetime(2026, 10, 9, tzinfo=timezone.utc)
    core.clock = lambda: instant.isoformat()
    observe(core, auth, "我喜欢简洁回答", "retry")
    original = core._extract_event
    def crash(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(core, "_extract_event", crash)
    with pytest.raises(KeyboardInterrupt):
        core.run_formation(auth)
    instant += timedelta(seconds=46)
    monkeypatch.setattr(core, "_extract_event", original)
    assert core.run_formation(auth, budget=FormationBudget(max_jobs=1))["jobs"][0]["status"] == "done"
    assert len(core.list_formation_candidates(auth)) == 1
    instant += timedelta(days=8)
    assert core.expire_formation_candidates(auth) == 1
    assert not core.list_formation_candidates(auth)
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT count(*) FROM candidates WHERE draft IS NOT NULL").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM sources WHERE excerpt IS NOT NULL").fetchone()[0] == 0


def test_delete_during_extraction_cancels_result_and_purges_episode(core, auth, monkeypatch):
    saved = core.remember(auth, MemoryDraft(kind="semantic", content="ROOT_MARKER"), Source(source_id=auth.input_id, origin="user_command", speaker="user", excerpt="ROOT_MARKER"), command_id="root")
    observe(core, auth, "我和Kong共同完成项目 Cedar 的验证。", "race")
    original = core._extract_event
    def racing(event, scope):
        with MemoryService(core._store.path) as other:
            other.forget_memory(auth, saved.record_id, expected_revision=1, command_id="forget")
        return original(event, scope)
    monkeypatch.setattr(core, "_extract_event", racing)
    report = core.run_formation(auth)
    assert report["jobs"][0]["status"] == "cancelled"
    assert not core.list_records(auth).records and not core.list_formation_candidates(auth)


def test_stale_preview_is_rejected_without_overwriting(core, auth):
    observe(core, auth, "我喜欢简洁回答", "first")
    core.run_formation(auth)
    item, = core.list_formation_candidates(auth)
    plan = core.preview_consolidation(auth, item.candidate_id)
    core.reject_candidate(auth, item.candidate_id, expected_revision=1, command_id="reject")
    with pytest.raises(NotFound):
        core.consolidate_candidate(auth, item.candidate_id, approved_digest=plan.digest, command_id="stale")
    assert not core.list_records(auth).records


def test_episode_pin_exchange_old_schema_compatibility_and_forget_purge(core, auth):
    import hashlib
    import json
    from kong.memory import parse_package
    from kong.memory.domain import canonical
    core.create_bottle(auth, Bottle(bottle_id="b", profile_id=auth.profile_id, title="Portable"), command_id="b")
    observe(core, auth, "我和Kong共同完成项目 Cedar 经历 verify，结果通过。", "portable", bottle="b")
    core.run_formation(auth)
    saved, _ = approve(core, auth, core.list_formation_candidates(auth)[0], "approve")
    core.pin_memory(auth, saved.record_id, "b", command_id="pin")
    package = core.export_bottle(auth, "b")
    assert package.schema_version == 2 and package.records[0].pinned and package.records[0].episode
    other_auth = auth.model_copy(update={"profile_id": "other", "input_id": "import"})
    core.create_profile(other_auth, command_id="create-other")
    preview = core.preview_import(other_auth, package)
    imported = core.import_bottle(other_auth, package, approved_digest=preview.approved_digest, command_id="import")
    record, = core.list_pinned_memories(other_auth, imported.bottle_id)
    assert record.current.episode.what == package.records[0].episode.what
    assert record.current.episode.confirmed_by is None
    assert core.preview_import(other_auth, package).items[0].disposition == "duplicate"
    legacy = json.loads(package.model_dump_json())
    for entry in legacy["records"]:
        entry.pop("episode", None)
        entry.pop("pinned", None)
    legacy.pop("checksum")
    legacy["schema_version"] = 1
    legacy["checksum"] = hashlib.sha256(canonical(legacy).encode()).hexdigest()
    assert parse_package(json.dumps(legacy)).records[0].episode is None
    core.forget_memory(auth, saved.record_id, expected_revision=1, command_id="forget")
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT payload FROM episodic_details WHERE profile_id=?", (auth.profile_id,)).fetchone()[0] is None
        assert db.execute("SELECT count(*) FROM memory_pins WHERE profile_id=?", (auth.profile_id,)).fetchone()[0] == 0


def test_failures_are_bounded_and_restart_cannot_leave_last_lease_running(core, auth, monkeypatch):
    instant = datetime(2026, 10, 9, tzinfo=timezone.utc)
    core.clock = lambda: instant.isoformat()
    observe(core, auth, "我喜欢简洁回答", "failing")
    def broken(*args):
        raise OSError("transient synthetic failure")
    monkeypatch.setattr(core, "_extract_event", broken)
    for expected in ("retry", "retry", "failed"):
        assert core.run_formation(auth)["jobs"][0]["status"] == expected
        instant += timedelta(seconds=10)
    assert core.run_formation(auth)["jobs"] == []
    assert not core.list_records(auth).records


def test_existing_approved_fact_is_deduplicated_and_ambiguous_targets_block(core, auth):
    text = "我喜欢简洁回答"
    first = core.remember(auth, MemoryDraft(kind="semantic", content=text),
        Source(source_id=auth.input_id, origin="user_command", speaker="user", excerpt=text), command_id="legacy")
    observe(core, auth, text, "new-evidence")
    core.run_formation(auth)
    item, = core.list_formation_candidates(auth)
    duplicate, plan = approve(core, auth, item, "deduplicate")
    assert plan.operation == "duplicate" and duplicate.record_id == first.record_id
    assert len(core.list_records(auth).records) == 1
    other = auth.model_copy(update={"input_id": "other"})
    core.remember(other, MemoryDraft(kind="semantic", content=text),
        Source(source_id="other", origin="user_command", speaker="user", excerpt=text), command_id="other")
    # Force a legacy duplicate with no traits, so neither arbitrary merge nor another INSERT is safe.
    with core._store.transaction() as db:
        db.execute("DELETE FROM formation_traits WHERE profile_id=?", (auth.profile_id,))
    observe(core, auth, text, "ambiguous-evidence")
    core.run_formation(auth)
    item, = core.list_formation_candidates(auth)
    with pytest.raises(PolicyViolation, match="Multiple matching"):
        core.preview_consolidation(auth, item.candidate_id)
    assert len(core.list_records(auth).records) == 2


def test_candidate_review_and_expiry_respect_narrow_scope_grants(core, auth):
    project = Scope(kind="project", id="Cedar")
    broad = auth.model_copy(update={"scopes": (Scope(), project)})
    event = InteractionObservation(event_id="scoped", capture="on", original_access=core.get_access_state(broad),
        role="user", text="我喜欢简洁回答")
    core.observe_interaction(broad, event)
    core.queue_formation(broad, "scoped", scope=project)
    core.run_formation(broad)
    assert len(core.list_formation_candidates(broad)) == 1
    assert core.list_formation_candidates(auth) == ()
    core.clock = lambda: (datetime.now(timezone.utc) + timedelta(days=8)).isoformat()
    assert core.expire_formation_candidates(auth) == 0
    assert core.expire_formation_candidates(broad) == 1


def test_rich_episode_cannot_be_disguised_as_legacy_package(core, auth):
    import hashlib
    import json
    from kong.memory import parse_package
    from kong.memory.domain import canonical
    core.create_bottle(auth, Bottle(bottle_id="v2", profile_id=auth.profile_id, title="Versioned"), command_id="v2")
    observe(core, auth, "我和Kong共同完成项目 Cedar 的验证。", "rich", bottle="v2")
    core.run_formation(auth)
    approve(core, auth, core.list_formation_candidates(auth)[0], "approved")
    data = json.loads(core.export_bottle(auth, "v2").model_dump_json())
    data["schema_version"] = 1
    data.pop("checksum")
    data["checksum"] = hashlib.sha256(canonical(data).encode()).hexdigest()
    with pytest.raises(ValueError, match="schema v2"):
        parse_package(json.dumps(data))
