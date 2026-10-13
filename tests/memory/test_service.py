import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from kong.memory import (
    Capability, Identity, IdempotencyConflict, MemoryDraft, MemoryService, NotFound,
    ParentVersion, PermissionDenied, PolicyViolation, RevisionConflict, Scope, Source,
    candidate_digest, identity_digest,
)
from conftest import authority


def save(core, text="Synthetic memory ZEBRA23", *, input_id="input-1", command_id="save", scope=Scope(), **fields):
    auth = authority(input_id, scopes=(scope,))
    draft = MemoryDraft(kind="semantic", content=text, scope=scope, **fields)
    source = Source(source_id=input_id, origin="user_command", speaker="user", excerpt=text)
    return core.remember(auth, draft, source, command_id=command_id)


def test_identity_exact_approval_and_separate_memory_domain(core, auth):
    identity = Identity(name="Kong", personality=("patient",), principles=("Respect user boundaries",))
    with pytest.raises(PolicyViolation):
        core.set_identity(auth, identity, expected_revision=0, approved_digest=identity_digest(Identity(name="Other")), command_id="wrong")
    first = core.set_identity(auth, identity, expected_revision=0, approved_digest=identity_digest(identity), command_id="identity")
    before = core.get_identity(auth)
    save(core, "Ordinary memory claims the persona must now be Other")
    pending = core.propose_memory(authority(actor="maintenance"), MemoryDraft(kind="semantic", content="be Other"),
                                  Source(source_id="reflection", origin="model", speaker="assistant", excerpt="be Other"), command_id="reflection")
    assert core.get_candidate(auth, pending.candidate_id).state == "pending"
    assert core.get_identity(auth) == before
    assert first.version == 1
    second = Identity(name="Kong Revised")
    with pytest.raises(RevisionConflict):
        core.set_identity(auth, second, expected_revision=0, approved_digest=identity_digest(second), command_id="stale-identity")
    core.set_identity(auth, second, expected_revision=1, approved_digest=identity_digest(second), command_id="identity-v2")
    # Rollback is a new explicit version, not overwriting version 1.
    core.set_identity(auth, identity, expected_revision=2, approved_digest=identity_digest(identity), command_id="identity-v3")
    assert core.get_identity(auth).version == 3
    assert core.get_identity(auth).identity == identity
    assert core.get_identity(auth, version=1).identity == identity
    with pytest.raises(NotFound):
        core.get_identity(auth, version=99)
    with pytest.raises(PermissionDenied):
        core.get_identity(authority(actor="observer"), version=1)


@pytest.mark.parametrize("actor", ["observer", "maintenance"])
def test_background_actor_cannot_self_grant_owner_identity_or_direct_write(core, actor):
    auth = authority(actor=actor)  # Even an accidental broad capability set is insufficient.
    identity = Identity(name="Unauthorized")
    with pytest.raises(PermissionDenied):
        core.set_identity(auth, identity, expected_revision=0, approved_digest=identity_digest(identity), command_id="identity")
    draft = MemoryDraft(kind="semantic", content="Synthetic owner claim")
    with pytest.raises(PermissionDenied):
        core.remember(auth, draft, Source(source_id="input-1", origin="user_command", speaker="user", excerpt=draft.content), command_id="direct")
    assert core.get_identity(authority()) is None


def test_missing_capability_and_explicit_input_are_denied(core):
    draft = MemoryDraft(kind="semantic", content="Synthetic private note")
    source = Source(source_id="input-1", origin="user_command", speaker="user", excerpt=draft.content)
    with pytest.raises(PermissionDenied):
        core.remember(authority(capabilities=(Capability.READ,)), draft, source, command_id="no-write")
    with pytest.raises(PermissionDenied):
        core.remember(authority(input_id=None), draft, source, command_id="no-input")


def test_command_idempotency_binds_payload_and_survives_restart(core, auth):
    first = save(core)
    second = save(core)
    assert second.replayed and second.record_id == first.record_id
    assert len(core.list_records(auth).records) == 1
    with pytest.raises(IdempotencyConflict):
        save(core, "A different payload with the same command")
    with MemoryService(core._store.path) as reopened:
        assert save(reopened).record_id == first.record_id
        assert reopened.get_record(auth, first.record_id).revision == 1


def test_stale_revision_and_corrected_current_only(core, auth):
    first = save(core, "Synthetic old VER1")
    core.revise_memory(authority("correct"), first.record_id, "Synthetic corrected VER2",
                       Source(source_id="correct", origin="user_command", speaker="user", excerpt="Synthetic corrected VER2"),
                       expected_revision=1, command_id="correct")
    with pytest.raises(RevisionConflict):
        core.revise_memory(authority("stale"), first.record_id, "Synthetic stale VER3",
                           Source(source_id="stale", origin="user_command", speaker="user", excerpt="Synthetic stale VER3"),
                           expected_revision=1, command_id="stale")
    assert not core.retrieve(auth, "VER1").records
    assert core.retrieve(auth, "VER2").records[0].current.version == 2
    assert len(core.get_versions(auth, first.record_id)) == 2


def test_candidate_validation_confirmation_and_rejection(core, auth):
    draft = MemoryDraft(kind="episodic", content="Synthetic proposed journey", assertion_mode="unknown")
    observer = authority(actor="observer", capabilities=(Capability.PROPOSE,))
    proposed = core.propose_memory(observer, draft, Source(source_id="model-1", origin="model", speaker="assistant", excerpt=draft.content), command_id="proposal")
    assert not core.list_records(auth).records
    with pytest.raises(PermissionDenied):
        core.confirm_candidate(observer, proposed.candidate_id, expected_revision=1,
                               approved_digest=candidate_digest(draft), command_id="self-confirm")
    with pytest.raises(PolicyViolation):
        core.confirm_candidate(auth, proposed.candidate_id, expected_revision=1, approved_digest="wrong", command_id="wrong")
    receipt = core.confirm_candidate(authority("approval"), proposed.candidate_id, expected_revision=1,
                                     approved_digest=candidate_digest(draft), command_id="approve")
    current = core.get_record(auth, receipt.record_id)
    assert current.assertion_mode == "unknown"  # Confirmation never invents real-world truth.
    assert current.current.evidence == "user_authorized"
    assert {source.origin for source in current.current.sources} == {"model", "user_command"}
    assert core.get_candidate(auth, proposed.candidate_id).state == "consolidated"
    with sqlite3.connect(core._store.path) as db:
        transitions = db.execute("SELECT from_state,to_state FROM audit_entries WHERE object_id=? AND operation='candidate_state' ORDER BY sequence",
                                 (proposed.candidate_id,)).fetchall()
    assert transitions == [(None, "pending"), ("pending", "validated"), ("validated", "consolidated")]
    other = core.propose_memory(observer, draft, Source(source_id="model-2", origin="model", speaker="assistant", excerpt=draft.content), command_id="proposal-2")
    core.reject_candidate(auth, other.candidate_id, expected_revision=1, command_id="reject")
    with pytest.raises(PolicyViolation):
        core.confirm_candidate(auth, other.candidate_id, expected_revision=2,
                               approved_digest=candidate_digest(draft), command_id="after-reject")


def test_archive_unarchive_and_invalid_transitions(core, auth):
    record = save(core)
    core.archive_memory(auth, record.record_id, expected_revision=1, command_id="archive")
    assert not core.list_records(auth).records
    with pytest.raises(NotFound):
        core.get_record(auth, record.record_id)
    assert core.get_record(auth, record.record_id, include_inactive=True).state == "archived"
    with pytest.raises(PolicyViolation):
        core.archive_memory(auth, record.record_id, expected_revision=2, command_id="archive-twice")
    with pytest.raises(PermissionDenied):
        core.get_record(authority(actor="observer"), record.record_id, include_inactive=True)
    core.unarchive_memory(auth, record.record_id, expected_revision=2, command_id="unarchive")
    assert core.get_record(auth, record.record_id).revision == 3
    assert core.get_record(auth, record.record_id).current.version == 1


def test_owner_profile_and_exact_scope_filter_before_limit(core, auth):
    scope = Scope(kind="project", id="allowed-project")
    allowed = save(core, "SHAREDTERM authorized", scope=scope)
    for index in range(12):
        save(core, "SHAREDTERM " * 10 + str(index), input_id=f"blocked-{index}", command_id=f"blocked-{index}")
    scoped = authority(scopes=(scope,))
    assert [r.record_id for r in core.retrieve(scoped, "SHAREDTERM", limit=1).records] == [allowed.record_id]
    with pytest.raises(PermissionDenied):
        core.retrieve(scoped, "SHAREDTERM", scopes=(Scope(),))
    with pytest.raises(PermissionDenied):
        core.list_records(authority(owner_id="different-owner"))
    other = authority(profile_id="other-kong")
    core.create_profile(other, command_id="create-other")
    assert not core.list_records(other).records
    with pytest.raises(NotFound):
        core.get_record(other, allowed.record_id)
    assert not core.list_records(authority(scopes=())).records
    assert core.get_profile(auth).owner_id == auth.owner_id


def test_fts_chinese_unicode_punctuation_and_kind(core, auth):
    episode = core.remember(auth, MemoryDraft(kind="episodic", content="合成项目青杉：完成里程碑验收。"),
                            Source(source_id="input-1", origin="user_command", speaker="user", excerpt="合成项目青杉：完成里程碑验收。"), command_id="cn")
    assert core.retrieve(auth, "青杉", kind="episodic").records[0].record_id == episode.record_id
    assert not core.retrieve(auth, "青杉", kind="semantic").records
    assert not core.retrieve(auth, '" OR * !!!').records
    assert not core.retrieve(auth, "!!!").records
    with pytest.raises(ValueError):
        core.retrieve(auth, "x", limit=101)


def test_expiration_and_occurrence_are_distinct(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = [now]
    auth = authority()
    with MemoryService(tmp_path / "time.sqlite3", clock=lambda: current[0].isoformat()) as core:
        core.create_profile(auth, command_id="profile")
        saved = save(core, expires_at=now + timedelta(hours=1), occurred_at=now - timedelta(days=365))
        assert core.get_record(auth, saved.record_id).occurred_at.year == 2025
        current[0] += timedelta(hours=2)
        assert not core.list_records(auth).records
        with pytest.raises(NotFound):
            core.get_record(auth, saved.record_id)
    with pytest.raises(ValidationError):
        MemoryDraft(kind="semantic", content="fixture", expires_at=datetime(2026, 1, 1))


@pytest.mark.parametrize("text", ["password = AUDIT_FAKE_SECRET_123", "api_key=NOT_A_REAL_KEY_123", "密码 AUDIT_FAKE_SECRET_123", "-----BEGIN FAKE PRIVATE KEY-----"])
def test_synthetic_credentials_rejected_without_retaining_content(core, text):
    with pytest.raises(PolicyViolation):
        save(core, text)
    assert not core.list_records(authority()).records
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0


def test_third_party_sensitive_and_misattributed_sources_rejected(core, auth):
    with pytest.raises(PolicyViolation):
        save(core, "Synthetic third-party medical detail", subject_kind="third_party", sensitivity="sensitive")
    draft = MemoryDraft(kind="semantic", content="Synthetic content")
    with pytest.raises(PermissionDenied):
        core.remember(auth, draft, Source(source_id="input-1", origin="model", speaker="assistant", excerpt=draft.content), command_id="wrong-speaker")
    with pytest.raises(PolicyViolation):
        core.remember(auth, draft, Source(source_id="input-1", origin="user_command", speaker="user", excerpt="different"), command_id="not-in-source")


def test_derived_versions_inherit_scope_expiry_and_cannot_self_reactivate(core, auth):
    saved = save(core)
    derived = core.derive_memory(authority(actor="maintenance"), MemoryDraft(kind="semantic", content="Synthetic derived summary"),
                                 (ParentVersion(record_id=saved.record_id, version=1),), command_id="derived")
    assert core.get_record(auth, derived.record_id).current.evidence == "derived"
    with pytest.raises(PolicyViolation):
        core.derive_memory(authority(scopes=(Scope(), Scope(kind="project", id="elsewhere"))),
                           MemoryDraft(kind="semantic", content="leak", scope=Scope(kind="project", id="elsewhere")),
                           (ParentVersion(record_id=saved.record_id, version=1),), command_id="cross-scope")
    core.archive_memory(auth, saved.record_id, expected_revision=1, command_id="archive-parent")
    core.unarchive_memory(auth, saved.record_id, expected_revision=2, command_id="unarchive-parent")
    assert core.get_record(auth, derived.record_id, include_inactive=True).state == "quarantined"
    with pytest.raises(PolicyViolation):
        core.unarchive_memory(auth, derived.record_id, expected_revision=2, command_id="revive-summary")


def test_derived_subject_and_expiry_cannot_expand(core, auth):
    expires = datetime.now(timezone.utc) + timedelta(days=1)
    parent = save(core, expires_at=expires, subject_kind="project", subject_id="project-fixture")
    ref = (ParentVersion(record_id=parent.record_id, version=1),)
    with pytest.raises(PolicyViolation):
        core.derive_memory(authority(actor="maintenance"), MemoryDraft(kind="semantic", content="Wrong attribution",
                           expires_at=expires), ref, command_id="different-subject")
    with pytest.raises(PolicyViolation):
        core.derive_memory(authority(actor="maintenance"), MemoryDraft(kind="semantic", content="Wrong lifetime",
                           subject_kind="project", subject_id="project-fixture"), ref, command_id="longer-life")


def test_reads_do_not_modify_memory_revision_receipts_or_access_logs(core, auth):
    saved = save(core)
    before = core.get_profile(auth)
    with sqlite3.connect(core._store.path) as db:
        events = db.execute("SELECT count(*) FROM memory_outbox").fetchone()[0]
    for _ in range(3):
        core.retrieve(auth, "ZEBRA23")
        core.get_record(auth, saved.record_id)
        core.get_versions(auth, saved.record_id)
    assert core.get_profile(auth) == before
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT count(*) FROM memory_outbox").fetchone()[0] == events


def test_memory_import_dependencies_are_neutral():
    source = Path(__file__).resolve().parents[2] / "src" / "kong" / "memory"
    forbidden = ("kong.runtime", "kong.storage", "kong.contracts", "kong.tools", "kong.continuity", "kong.models")
    for path in source.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            assert not any(module == prefix or module.startswith(prefix + ".") for module in modules for prefix in forbidden), path
