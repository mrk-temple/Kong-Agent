from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from pydantic import ValidationError

from kong.memory import (
    Bottle, Capability, Identity, InteractionObservation, MemoryDraft, MemoryService,
    NotFound, PermissionDenied, PolicyViolation, ReflectionBudget, Relationship,
    RevisionConflict, Scope, Source, ForgottenSource, identity_digest, relationship_digest,
)


def stable_preference(core, auth):
    core.create_bottle(auth, Bottle(bottle_id="A", profile_id=auth.profile_id, title="Authorized"), command_id="A")
    core.activate_bottle(auth, "A", expected_revision=1, command_id="activate")
    record_id = None
    for i in range(2):
        event = InteractionObservation(event_id=f"feedback-{i}", capture="on", original_access=core.get_access_state(auth),
            role="user", text="我喜欢简洁回答")
        core.observe_interaction(auth, event)
        core.queue_formation(auth, event.event_id, bottle_id="A")
        core.run_formation(auth)
        item, = core.list_formation_candidates(auth)
        plan = core.preview_consolidation(auth, item.candidate_id)
        own = auth.model_copy(update={"input_id": f"approve-{i}"})
        result = core.consolidate_candidate(own, item.candidate_id, approved_digest=plan.digest, command_id=f"approve-{i}")
        record_id = result.record_id
    return record_id


def choose(core, auth, target):
    return next(p for p in core.list_proposals(auth) if p.target == target)


def approve(core, auth, proposal, command="approved"):
    return core.approve_proposal(auth.model_copy(update={"input_id": command}), proposal.proposal_id,
        expected_revision=proposal.revision, approved_digest=proposal.approved_digest, command_id=command)


def test_relationship_empty_explicit_versioned_scope_and_rollback_by_version(core, auth):
    assert core.get_relationship(auth) is None
    value = Relationship(role="collaborator", addressing="respectful", communication_style="rational",
        history_mentions="on_request", sensitive_topics=("health",), agreements=("allow_disagreement", "no_pressure"))
    result = core.set_relationship(auth, value, expected_revision=0, approved_digest=relationship_digest(value), command_id="rules")
    assert result.version == 1 and core.get_identity(auth) is None
    assert core.get_relationship(auth).relationship == value
    assert core.retrieve_context(auth, "anything").relationship.relationship == value
    changed = value.model_copy(update={"communication_style": "concise"})
    core.set_relationship(auth, changed, expected_revision=1, approved_digest=relationship_digest(changed), command_id="new-rules")
    assert core.get_relationship(auth, version=1).relationship == value
    assert core.get_profile(auth).relationship_version == 2
    narrow = auth.model_copy(update={"scopes": (Scope(kind="project", id="Cedar"),)})
    assert core.retrieve_context(narrow, "anything").relationship is None
    with pytest.raises(PermissionDenied):
        core.get_relationship(narrow)
    with pytest.raises(PermissionDenied):
        core.set_relationship(auth.model_copy(update={"actor": "maintenance", "input_id": None}), value,
            expected_revision=2, approved_digest=relationship_digest(value), command_id="unauthorized")
    with pytest.raises(PolicyViolation):
        core.set_relationship(auth, value, expected_revision=2, approved_digest="wrong", command_id="tampered")
    with pytest.raises(RevisionConflict):
        core.set_relationship(auth, value, expected_revision=1, approved_digest=relationship_digest(value), command_id="stale")
    with pytest.raises(ValidationError):
        Relationship(communication_style="copy this private medical story")


def test_reflection_independent_repeated_sources_proposals_no_silent_change(core, auth):
    record_id = stable_preference(core, auth)
    identity = Identity(name="Empty Kong", personality=("steady",), communication_style="original", principles=("respect autonomy",))
    core.set_identity(auth, identity, expected_revision=0, approved_digest=identity_digest(identity), command_id="identity")
    maintenance = auth.model_copy(update={"actor": "maintenance", "input_id": None,
        "capabilities": frozenset({Capability.READ, Capability.PROPOSE, Capability.REFLECTION})})
    result = core.run_reflection(maintenance, command_id="maintain")
    report = core.get_reflection_report(auth, result.reflection_id)
    assert report.model_calls == 0 and len(report.proposal_ids) == 2
    assert report.parents[0].record_id == record_id
    assert core.get_identity(auth).identity == identity and core.get_relationship(auth) is None
    assert core.run_reflection(maintenance, command_id="maintain").replayed
    assert len(core.list_proposals(auth)) == 2
    relation = choose(core, auth, "relationship")
    assert relation.current is None and relation.base_version == 0
    assert relation.proposed.communication_style == "concise"
    assert {"feedback-0", "feedback-1"} <= set(relation.source_ids)
    assert relation.reason and relation.behavior_impact
    # Runtime's observer and maintenance cannot upgrade a proposal into authority.
    with pytest.raises(PermissionDenied):
        approve(core, maintenance, relation)
    receipt = approve(core, auth, relation)
    assert receipt.version == 1 and core.get_relationship(auth).approved_by == "approved"
    assert approve(core, auth, relation).replayed
    assert core.get_identity(auth).version == 1
    identity_proposal = choose(core, auth, "identity")
    core.reject_proposal(auth, identity_proposal.proposal_id, expected_revision=1, command_id="reject-identity")
    next_run = core.run_reflection(maintenance, command_id="maintain-again")
    assert core.get_reflection_report(auth, next_run.reflection_id).proposal_ids == (identity_proposal.proposal_id,)
    assert core.list_proposals(auth) == ()  # Rejected evidence is not silently resurfaced as pending.


def test_identity_evolution_explicit_approval_diff_and_safe_undo(core, auth):
    stable_preference(core, auth)
    identity = Identity(name="Kong", personality=("calm",), principles=("truthful",), role="assistant", communication_style="balanced")
    core.set_identity(auth, identity, expected_revision=0, approved_digest=identity_digest(identity), command_id="identity")
    core.run_reflection(auth, command_id="reflection")
    proposal = choose(core, auth, "identity")
    assert proposal.proposed.personality == identity.personality and proposal.proposed.principles == identity.principles
    with pytest.raises(RevisionConflict):
        core.approve_proposal(auth, proposal.proposal_id, expected_revision=1, approved_digest="altered", command_id="bad-approve")
    approve(core, auth, proposal, "approve-style")
    assert core.get_identity(auth).version == 2 and core.get_identity(auth).approved_by == "approve-style"
    plan = core.preview_rollback_proposal(auth, proposal.proposal_id)
    assert plan.current != plan.restored and plan.restored == identity
    result = core.rollback_proposal(auth, proposal.proposal_id, approved_digest=plan.approved_digest, command_id="undo-style")
    assert result.version == 3 and core.get_identity(auth).identity == identity
    assert core.get_proposal(auth, proposal.proposal_id).state == "revoked"
    assert core.rollback_proposal(auth, proposal.proposal_id, approved_digest=plan.approved_digest, command_id="undo-style").replayed


def test_later_identity_write_prevents_undo_or_old_proposal_approval(core, auth):
    stable_preference(core, auth)
    core.run_reflection(auth, command_id="reflect")
    proposal = choose(core, auth, "identity")
    approve(core, auth, proposal)
    plan = core.preview_rollback_proposal(auth, proposal.proposal_id)
    changed = Identity(name="User choice", communication_style="custom")
    core.set_identity(auth, changed, expected_revision=1, approved_digest=identity_digest(changed), command_id="explicit-new")
    with pytest.raises(RevisionConflict):
        core.rollback_proposal(auth, proposal.proposal_id, approved_digest=plan.approved_digest, command_id="stale-undo")
    assert core.get_identity(auth).identity == changed


def test_deactivated_bottle_and_corrected_version_block_old_suggestions(core, auth):
    record_id = stable_preference(core, auth)
    core.run_reflection(auth, command_id="reflect")
    proposal = choose(core, auth, "relationship")
    value = core.get_record(auth, record_id)
    text = "我现在喜欢详细回答"
    core.revise_memory(auth.model_copy(update={"input_id": "corrected"}), record_id, text,
        Source(source_id="corrected", origin="user_command", speaker="user", excerpt=text), expected_revision=value.revision, command_id="corrected")
    with pytest.raises(RevisionConflict):
        approve(core, auth, proposal)
    assert core.get_relationship(auth) is None
    core.run_reflection(auth, command_id="reflect-corrected")
    assert not core.list_proposals(auth)  # One new preference source alone is not stable repeated feedback.
    with core._store.transaction() as db:
        db.execute("DELETE FROM formation_traits WHERE profile_id=?", (auth.profile_id,))
    bottle, = core.list_bottles(auth)
    core.deactivate_bottle(auth, "A", expected_revision=bottle.revision, command_id="off")
    report = core.run_reflection(auth, command_id="disabled")
    assert core.get_reflection_report(auth, report.reflection_id).parents == ()
    with pytest.raises(RevisionConflict):
        core.get_proposal(auth, proposal.proposal_id)


def test_forget_purges_reports_pending_proposals_and_cannot_replay(core, auth):
    record_id = stable_preference(core, auth)
    report = core.run_reflection(auth, command_id="reflect")
    proposal = choose(core, auth, "relationship")
    value = core.get_record(auth, record_id)
    core.forget_memory(auth, record_id, expected_revision=value.revision, command_id="forget")
    with pytest.raises(NotFound):
        core.get_reflection_report(auth, report.reflection_id)
    with pytest.raises(NotFound):
        approve(core, auth, proposal)
    with pytest.raises(ForgottenSource):
        core.run_reflection(auth, command_id="reflect")
    assert core.list_proposals(auth) == () and core.get_relationship(auth) is None
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT count(*) FROM evolution_proposals WHERE content IS NOT NULL").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM reflection_runs WHERE payload IS NOT NULL").fetchone()[0] == 0


def test_delete_during_reflection_prevents_commit(core, auth, monkeypatch):
    record_id = stable_preference(core, auth)
    original = core._analyze_reflection
    def racing(context, budget):
        with MemoryService(core._store.path) as other:
            record = other.get_record(auth, record_id)
            other.forget_memory(auth, record_id, expected_revision=record.revision, command_id="racing-forget")
        return original(context, budget)
    monkeypatch.setattr(core, "_analyze_reflection", racing)
    with pytest.raises(RevisionConflict):
        core.run_reflection(auth, command_id="stale-result")
    assert core.list_proposals(auth) == () and core.get_relationship(auth) is None
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT count(*) FROM reflection_runs").fetchone()[0] == 0


def test_reflection_budget_and_expiry_leave_no_partial_state(core, auth, monkeypatch):
    stable_preference(core, auth)
    original = core._analyze_reflection
    def timeout(context, budget):
        import time
        time.sleep(.02)
        return original(context, budget)
    monkeypatch.setattr(core, "_analyze_reflection", timeout)
    with pytest.raises(PolicyViolation, match="budget"):
        core.run_reflection(auth, command_id="over-budget", budget=ReflectionBudget(max_seconds=.001))
    assert core.list_proposals(auth) == ()
    monkeypatch.setattr(core, "_analyze_reflection", original)
    report = core.run_reflection(auth, command_id="small", budget=ReflectionBudget(max_proposals=0, max_records=1))
    assert core.get_reflection_report(auth, report.reflection_id).proposal_ids == ()
    core.run_reflection(auth, command_id="full")
    core.clock = lambda: (datetime.now(timezone.utc) + timedelta(days=8)).isoformat()
    assert core.expire_reflection(auth) == 2
    assert core.list_proposals(auth) == ()
    with pytest.raises(NotFound):
        core.get_reflection_report(auth, report.reflection_id)


def test_domain_candidates_need_separate_exact_approval(core, auth):
    for event_id, text in (("boundary", "不要主动提起我的历史经历。"), ("identity", "我偏好你的性格表达简洁。")):
        event = InteractionObservation(event_id=event_id, capture="on", role="user", text=text, original_access=core.get_access_state(auth))
        core.observe_interaction(auth, event)
        core.queue_formation(auth, event_id)
        core.run_formation(auth)
    candidates = core.list_formation_candidates(auth)
    for item in candidates:
        result = core.proposal_from_candidate(auth, item.candidate_id, command_id="proposal-" + item.candidate_id)
        proposal = core.get_proposal(auth, result.proposal_id)
        assert proposal.candidate_id == item.candidate_id and proposal.parents == ()
        assert core.get_relationship(auth) is None and core.get_identity(auth) is None
    boundary = choose(core, auth, "relationship")
    assert boundary.proposed.history_mentions == "on_request"
    approve(core, auth, boundary, "accept-boundary")
    assert core.get_relationship(auth).relationship.history_mentions == "on_request"
    assert core.get_identity(auth) is None


def test_unrecognized_identity_candidate_does_not_invent_personality(core, auth):
    event = InteractionObservation(event_id="vague", capture="on", role="user", text="我想改变你的性格。", original_access=core.get_access_state(auth))
    core.observe_interaction(auth, event)
    core.queue_formation(auth, event.event_id)
    core.run_formation(auth)
    item, = core.list_formation_candidates(auth)
    with pytest.raises(PolicyViolation, match="specific"):
        core.proposal_from_candidate(auth, item.candidate_id, command_id="vague-proposal")
    assert core.get_identity(auth) is None


def test_pending_identity_base_compare_and_set_requires_fresh_review(core, auth):
    stable_preference(core, auth)
    core.run_reflection(auth, command_id="reflect")
    proposal = choose(core, auth, "identity")
    explicit = Identity(name="Owner choice")
    core.set_identity(auth, explicit, expected_revision=0, approved_digest=identity_digest(explicit), command_id="owner-choice")
    with pytest.raises(RevisionConflict, match="base version"):
        approve(core, auth, proposal)
    assert core.get_identity(auth).identity == explicit


def test_project_only_reflection_never_evolves_profile_or_reads_profile(core, auth):
    stable_preference(core, auth)
    project = auth.model_copy(update={"scopes": (Scope(kind="project", id="Cedar"),)})
    run = core.run_reflection(project, command_id="project-maintenance")
    report = core.get_reflection_report(project, run.reflection_id)
    assert report.parents == () and report.proposal_ids == ()
    assert core.list_proposals(project) == ()


def test_episode_linking_conflict_detection_and_inactive_inputs(core, auth):
    core.create_bottle(auth, Bottle(bottle_id="A", profile_id=auth.profile_id, title="Shared"), command_id="A")
    core.create_bottle(auth, Bottle(bottle_id="B", profile_id=auth.profile_id, title="Inactive"), command_id="B")
    core.activate_bottle(auth, "A", expected_revision=1, command_id="active")
    for event, text in (("one", "我和Kong共同完成项目 Cedar 经历 prototype，结果通过。"),
                        ("two", "我和Kong共同完成项目 Cedar 经历 release，结果发布。")):
        observation = InteractionObservation(event_id=event, capture="on", role="user", text=text, original_access=core.get_access_state(auth))
        core.observe_interaction(auth, observation)
        core.queue_formation(auth, event, bottle_id="A")
        core.run_formation(auth)
        item, = core.list_formation_candidates(auth)
        plan = core.preview_consolidation(auth, item.candidate_id)
        core.consolidate_candidate(auth.model_copy(update={"input_id": event + "-approved"}), item.candidate_id,
            approved_digest=plan.digest, command_id=event + "-approved")
    for key, text, bottle in (("concise", "我喜欢简洁回答", "A"), ("detailed", "我喜欢详细回答", "A"),
                               ("hidden", "我喜欢简洁回答 HIDDEN_REFLECTION_832", "B")):
        own = auth.model_copy(update={"input_id": key})
        receipt = core.remember(own, MemoryDraft(kind="semantic", content=text), Source(source_id=key, origin="user_command", speaker="user", excerpt=text), command_id=key)
        core.add_memory_to_bottle(own, bottle, receipt.record_id, command_id=key + "-member")
    run = core.run_reflection(auth, command_id="timeline")
    report = core.get_reflection_report(auth, run.reflection_id)
    assert len(report.episode_groups) == 1 and report.episode_groups[0]["project_id"] == "Cedar"
    assert len(report.episode_groups[0]["record_ids"]) == 2
    assert report.conflicts and not report.proposal_ids
    assert all(p.record_id != receipt.record_id for p in report.parents)
    assert "HIDDEN_REFLECTION_832" not in report.model_dump_json()
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT count(*) FROM episode_links").fetchone()[0] == 1


def test_reflection_partial_failure_rolls_back_and_same_command_can_retry(core, auth, monkeypatch):
    stable_preference(core, auth)
    before = core.get_access_state(auth)
    original = core._insert_proposal
    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic interrupted transaction")
    monkeypatch.setattr(core, "_insert_proposal", fail_after_insert)
    with pytest.raises(OSError):
        core.run_reflection(auth, command_id="retryable")
    assert core.get_access_state(auth) == before and core.list_proposals(auth) == ()
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT count(*) FROM evolution_proposals").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM reflection_runs").fetchone()[0] == 0
    monkeypatch.setattr(core, "_insert_proposal", original)
    result = core.run_reflection(auth, command_id="retryable")
    assert len(core.get_reflection_report(auth, result.reflection_id).proposal_ids) == 2
    with MemoryService(core._store.path) as other:
        assert other.run_reflection(auth, command_id="retryable").replayed


def test_approved_rules_are_explicit_configuration_not_a_hidden_memory_copy(core, auth):
    record_id = stable_preference(core, auth)
    core.run_reflection(auth, command_id="reflect")
    proposal = choose(core, auth, "relationship")
    approve(core, auth, proposal)
    record = core.get_record(auth, record_id)
    core.forget_memory(auth, record_id, expected_revision=record.revision, command_id="forget-basis")
    relationship = core.get_relationship(auth)
    assert relationship.relationship.communication_style == "concise"
    assert "feedback" not in relationship.relationship.model_dump_json()
    assert core.list_proposals(auth, state="approved") == ()
    reset = Relationship()
    core.set_relationship(auth, reset, expected_revision=1, approved_digest=relationship_digest(reset), command_id="clear-rules")
    assert core.get_relationship(auth).relationship == reset


def test_approved_domain_rule_does_not_retain_raw_observation_indefinitely(core, auth):
    event = InteractionObservation(event_id="bounded", capture="on", role="user", text="不要主动提起我的历史经历。", original_access=core.get_access_state(auth))
    core.observe_interaction(auth, event)
    core.queue_formation(auth, event.event_id)
    core.run_formation(auth)
    item, = core.list_formation_candidates(auth)
    result = core.proposal_from_candidate(auth, item.candidate_id, command_id="proposal")
    approve(core, auth, core.get_proposal(auth, result.proposal_id))
    core.clock = lambda: (datetime.now(timezone.utc) + timedelta(days=8)).isoformat()
    core.expire_reflection(auth)
    assert core.get_relationship(auth).relationship.history_mentions == "on_request"
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT payload FROM interaction_inbox").fetchone()[0] is None
        assert db.execute("SELECT draft FROM candidates").fetchone()[0] is None
        assert db.execute("SELECT excerpt FROM sources").fetchone()[0] is None
    with pytest.raises(NotFound):
        core.get_candidate(auth, item.candidate_id)


def test_idle_observation_scan_is_bounded_authorized_and_does_not_enable_bottle(core, auth):
    core.create_bottle(auth, Bottle(bottle_id="A", profile_id=auth.profile_id, title="Owner-selected"), command_id="A")
    for i, capture in enumerate(("off", "on", "on")):
        observation = InteractionObservation(event_id=f"scan-{i}", capture=capture, role="user", text="我喜欢简洁回答" if capture == "on" else None,
            original_access=core.get_access_state(auth))
        core.observe_interaction(auth, observation)
    first = core.queue_observations(auth, bottle_id="A", limit=1)
    second = core.queue_observations(auth, bottle_id="A", limit=1)
    assert len(first["job_ids"]) == len(second["job_ids"]) == 1
    assert core.queue_observations(auth, bottle_id="A")["job_ids"] == []
    assert core.list_bottles(auth)[0].active is False
    assert core.run_formation(auth)["model_calls"] == 0
    assert len(core.list_formation_candidates(auth)) == 2
    assert not core.list_records(auth).records
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT count(*) FROM formation_jobs WHERE event_id='scan-0'").fetchone()[0] == 0


def test_evolution_payload_corruption_cannot_reuse_an_old_approved_digest(core, auth):
    import json
    stable_preference(core, auth)
    core.run_reflection(auth, command_id="reflect")
    proposal = choose(core, auth, "relationship")
    with sqlite3.connect(core._store.path) as db:
        body = json.loads(db.execute("SELECT content FROM evolution_proposals WHERE proposal_id=?", (proposal.proposal_id,)).fetchone()[0])
        body["proposed"]["proactivity"] = "allowed"
        db.execute("UPDATE evolution_proposals SET content=? WHERE proposal_id=?", (json.dumps(body), proposal.proposal_id))
    with pytest.raises(PolicyViolation, match="review binding"):
        approve(core, auth, proposal)
    assert core.get_relationship(auth) is None
