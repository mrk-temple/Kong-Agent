from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from kong.memory import (
    ForgottenSource, IdempotencyConflict, MemoryDraft, MemoryService, NotFound, ParentVersion,
    PermissionDenied, PolicyViolation, RevisionConflict, SchemaError, Scope, Source,
)
from kong.memory.sqlite_store import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION, SQLiteStore, migration_checksum
from conftest import authority


def remember(core, *, input_id="input-1", command_id="remember", text="Synthetic durable ROOT55", scope=Scope()):
    return core.remember(authority(input_id, scopes=(scope,)), MemoryDraft(kind="episodic", content=text, scope=scope),
                         Source(source_id=input_id, origin="user_command", speaker="user", excerpt=text), command_id=command_id)


def v1_database(path):
    """Real version-1 fixture with persisted data, independent of service v2."""
    with sqlite3.connect(path) as db:
        for statement in MIGRATIONS[0]:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO schema_migrations VALUES(1,?,'2026-01-01T00:00:00Z')", (migration_checksum(MIGRATIONS[0]),))
        db.execute("INSERT INTO metadata VALUES('digest_key',?)", (b"fixture-only-key-not-a-credential!!",))
        db.execute("INSERT INTO profiles(profile_id,owner_id) VALUES('kong-fixture','owner-fixture')")
        db.execute("""INSERT INTO memory_records VALUES('kong-fixture','legacy','owner-fixture',1,'active','semantic','fact',
            'owner','owner','profile','','real','personal',NULL,NULL,1)""")
        db.execute("INSERT INTO memory_versions VALUES('kong-fixture','legacy',1,'MIGRATED57','user_authorized','2026-01-01T00:00:00Z',NULL,NULL)")
        db.execute("INSERT INTO sources VALUES('kong-fixture','legacy-source','user_command','user','MIGRATED57',NULL,'fixture-digest',NULL)")
        db.execute("INSERT INTO memory_sources VALUES('kong-fixture','legacy',1,'legacy-source')")
        db.execute("INSERT INTO memory_fts VALUES('kong-fixture','legacy','migrated57')")


def test_migrate_v1_preserves_memory_and_is_repeatable(tmp_path, auth):
    path = tmp_path / "old.sqlite3"
    v1_database(path)
    for _ in range(2):
        with MemoryService(path) as core:
            assert core.retrieve(auth, "MIGRATED57").records[0].record_id == "legacy"
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("SELECT count(*) FROM record_collections").fetchone()[0] == 0


def test_migrate_phase3b_v5_preserves_episode_bottle_and_pin(tmp_path, auth):
    from kong.memory import Episode
    path = tmp_path / "phase3b.sqlite3"
    v1_database(path)
    episode = Episode(event_key="legacy:Cedar", what="MIGRATED57", project_id="Cedar", participants=("owner", "kong"),
        importance_reason="Synthetic upgrade fixture", evidence_source_ids=("legacy-source",), confirmed_by="synthetic-approval")
    with sqlite3.connect(path) as db:
        for index in range(1, 5):
            for statement in MIGRATIONS[index]:
                db.execute(statement)
            db.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (index + 1, migration_checksum(MIGRATIONS[index]), "2026-01-01T00:00:00Z"))
        db.execute("PRAGMA user_version=5")
        db.execute("UPDATE memory_records SET kind='episodic'")
        db.execute("INSERT INTO bottles VALUES('kong-fixture','A','Portable','memory','profile','',1,1)")
        db.execute("INSERT INTO record_collections VALUES('kong-fixture','legacy','A')")
        db.execute("INSERT INTO memory_pins VALUES('kong-fixture','legacy','A','synthetic-approval')")
        db.execute("INSERT INTO episodic_details VALUES('kong-fixture','legacy',1,?)", (episode.model_dump_json(),))
        before = db.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall()
    with MemoryService(path) as service:
        record, = service.list_pinned_memories(auth, "A")
        assert record.current.episode == episode
        assert service.retrieve_context(auth, "MIGRATED57").records[0].record_id == "legacy"
        assert service.get_relationship(auth) is None
        assert service.get_profile(auth).relationship_version == 0
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version,checksum FROM schema_migrations WHERE version<=5 ORDER BY version").fetchall() == before


@pytest.mark.parametrize("case", ["future", "unowned", "checksum"])
def test_refuse_unknown_or_tampered_database_without_modifying_it(tmp_path, case):
    path = tmp_path / "unsupported.sqlite3"
    if case == "unowned":
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE thread_items(id TEXT)")
    else:
        v1_database(path)
        with sqlite3.connect(path) as db:
            if case == "future":
                db.execute("PRAGMA user_version=999")
            else:
                db.execute("UPDATE schema_migrations SET checksum='tampered'")
    before = path.read_bytes()
    with pytest.raises(SchemaError):
        MemoryService(path)
    assert path.read_bytes() == before


def test_migration_failure_rolls_back_all_new_schema(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite3"
    v1_database(path)
    import kong.memory.sqlite_store as module
    monkeypatch.setattr(module, "MIGRATIONS", (MIGRATIONS[0], (*MIGRATIONS[1], "INSERT INTO missing_table VALUES(1)")))
    with pytest.raises(sqlite3.OperationalError):
        SQLiteStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='derivations'").fetchone()
    monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS)
    with MemoryService(path) as core:
        assert core.retrieve(authority(), "MIGRATED57").records


def test_partial_write_failure_has_no_record_source_receipt_or_event(core, auth, monkeypatch):
    before = core.get_profile(auth).memory_revision
    original = core._store.index
    def fail(*args):
        raise OSError("synthetic index failure")
    monkeypatch.setattr(core._store, "index", fail)
    with pytest.raises(OSError, match="synthetic"):
        remember(core)
    with sqlite3.connect(core._store.path) as db:
        for table in ("memory_records", "memory_versions", "sources", "memory_fts"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM command_receipts WHERE command_id='remember'").fetchone()
        assert not db.execute("SELECT 1 FROM memory_outbox WHERE operation='remember'").fetchone()
    assert core.get_profile(auth).memory_revision == before
    monkeypatch.setattr(core._store, "index", original)
    assert remember(core).record_id


def test_two_connections_detect_real_concurrent_revision_conflict(core, auth):
    saved = remember(core)
    barrier = Barrier(2)
    def revise(index):
        with MemoryService(core._store.path) as worker:
            assert worker.get_record(auth, saved.record_id).revision == 1
            barrier.wait(timeout=10)
            text = f"Synthetic concurrent WIN{index}"
            try:
                return worker.revise_memory(authority(f"input-{index + 10}"), saved.record_id, text,
                    Source(source_id=f"input-{index + 10}", origin="user_command", speaker="user", excerpt=text),
                    expected_revision=1, command_id=f"revision-{index}")
            except RevisionConflict:
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(revise, (0, 1)))
    assert sum(result == "conflict" for result in results) == 1
    assert core.get_record(auth, saved.record_id).revision == 2
    assert len(core.get_versions(auth, saved.record_id)) == 2


def test_delete_races_with_stale_derived_writer_and_wins(core, auth):
    saved = remember(core)
    barrier = Barrier(2)
    draft = MemoryDraft(kind="semantic", content="Synthetic derivative ROOT55")
    def worker(delete):
        with MemoryService(core._store.path) as service:
            barrier.wait(timeout=10)
            if delete:
                return service.forget_memory(auth, saved.record_id, expected_revision=1, command_id="forget-race")
            try:
                return service.derive_memory(authority(actor="maintenance"), draft,
                    (ParentVersion(record_id=saved.record_id, version=1),), command_id="stale-derived-race")
            except (NotFound, ForgottenSource):
                return "suppressed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, (True, False)))
    assert not core.list_records(auth).records
    with sqlite3.connect(core._store.path) as db:
        assert db.execute("SELECT count(*) FROM memory_versions WHERE content IS NOT NULL").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 0


def test_forgetting_shared_sources_requires_entire_scope_authority(core, auth):
    text = "Synthetic shared input DATA61"
    shared = remember(core, text=text)
    scope = Scope(kind="project", id="other-scope")
    another = remember(core, text=text, scope=scope, command_id="shared-project")
    with pytest.raises(PermissionDenied):
        core.forget_memory(auth, shared.record_id, expected_revision=1, command_id="incomplete-authority")
    assert core.get_record(auth, shared.record_id).state == "active"
    all_scopes = authority(scopes=(Scope(), scope))
    with pytest.raises(PolicyViolation):
        core.forget_memory(all_scopes, shared.record_id, expected_revision=1, command_id="unreviewed-expansion")
    plan = core.preview_forget(all_scopes, shared.record_id)
    assert plan.requires_expanded_approval
    receipt = core.forget_memory(all_scopes, shared.record_id, expected_revision=1, command_id="delete-all-shared", approved_plan_digest=plan.digest)
    assert receipt.affected_records == 2
    with pytest.raises(NotFound):
        core.get_record(all_scopes, another.record_id)
    with pytest.raises(PermissionDenied):
        core.get_deletion_receipt(auth, "delete-all-shared")


def test_source_cannot_be_reused_for_different_evidence(core):
    remember(core)
    with pytest.raises(IdempotencyConflict):
        remember(core, text="Different evidence with same source ID", command_id="other-command")


def test_deletion_plan_detects_new_shared_source_record_before_commit(core, auth):
    text = "Synthetic fact A71; synthetic fact B72; synthetic fact C73"
    source = Source(source_id="input-1", origin="user_command", speaker="user", excerpt=text)
    first = core.remember(auth, MemoryDraft(kind="semantic", content="Synthetic fact A71"), source, command_id="first")
    core.remember(auth, MemoryDraft(kind="semantic", content="synthetic fact B72"), source, command_id="second")
    preview = core.preview_forget(auth, first.record_id)
    assert preview.requires_expanded_approval and len(preview.targets) == 2
    core.remember(auth, MemoryDraft(kind="semantic", content="synthetic fact C73"), source, command_id="third")
    with pytest.raises(RevisionConflict):
        core.forget_memory(auth, first.record_id, expected_revision=1, command_id="stale-preview", approved_plan_digest=preview.digest)
    assert len(core.list_records(auth).records) == 3
    fresh = core.preview_forget(auth, first.record_id)
    receipt = core.forget_memory(auth, first.record_id, expected_revision=1, command_id="approved-current", approved_plan_digest=fresh.digest)
    assert receipt.affected_records == 3


def test_new_explicit_source_can_reintroduce_without_reviving_tombstone(core, auth):
    saved = remember(core)
    core.forget_memory(auth, saved.record_id, expected_revision=1, command_id="forget")
    new = remember(core, input_id="new-explicit-input", command_id="new-explicit-save")
    assert new.record_id != saved.record_id
    assert len(core.list_records(auth).records) == 1
    with pytest.raises(NotFound):
        core.get_record(auth, saved.record_id, include_inactive=True)


def test_hard_process_exit_mid_write_rolls_back_then_retry_commits_once(tmp_path, auth):
    database = tmp_path / "crash.sqlite3"
    with MemoryService(database) as core:
        core.create_profile(auth, command_id="create")
    script = r'''
import os
import sys
sys.path.insert(0, sys.argv[1])
from kong.memory import *
auth = Authority(owner_id="owner-fixture",profile_id="kong-fixture",input_id="crash-source",capabilities=frozenset(Capability))
with MemoryService(sys.argv[2]) as core:
    def crash(*args):
        os._exit(42)
    core._store.index = crash
    draft = MemoryDraft(kind="episodic",content="Synthetic crash fixture")
    core.remember(auth,draft,Source(source_id="crash-source",origin="user_command",speaker="user",excerpt=draft.content),command_id="crash-command")
'''
    source_path = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(source_path), str(database)], capture_output=True, timeout=30)
    assert result.returncode == 42, result.stderr
    with MemoryService(database) as core:
        assert not core.list_records(auth).records
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
            assert not db.execute("SELECT 1 FROM command_receipts WHERE command_id='crash-command'").fetchone()
        saved = remember(core, input_id="crash-source", command_id="crash-command", text="Synthetic crash fixture")
        replay = remember(core, input_id="crash-source", command_id="crash-command", text="Synthetic crash fixture")
        assert replay.record_id == saved.record_id and replay.replayed
        assert len(core.list_records(auth).records) == 1
