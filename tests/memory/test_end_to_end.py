import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from kong.memory import (
    Authority, Capability, ForgottenSource, Identity, MemoryDraft, MemoryService,
    NotFound, ParentVersion, Scope, ServiceClosed, Source, identity_digest,
)


def owner(input_id, scope=Scope()):
    return Authority(owner_id="synthetic-owner", profile_id="empty-kong", input_id=input_id,
                     capabilities=frozenset(Capability), scopes=(scope,))


def source(source_id, text):
    return Source(source_id=source_id, origin="user_command", speaker="user", excerpt=text)


def test_empty_identity_episode_restart_correct_forget_and_replay(tmp_path):
    """The requested twelve-step product loop, including known derivatives."""
    database = tmp_path / "core.sqlite3"
    scope = Scope(kind="project", id="cedar-synthetic")
    original = "Synthetic Cedar milestone ALPHA5 completed with five fixtures."
    corrected = "Synthetic Cedar milestone OMEGA7 completed with seven fixtures."
    draft = MemoryDraft(kind="episodic", category="milestone", content=original, scope=scope,
                        subject_kind="project", subject_id="cedar-synthetic")
    first_source = source("event-first", original)
    with MemoryService(database) as first:
        first.create_profile(owner("create", scope), command_id="profile-create")
        assert first.get_identity(owner("read", scope)) is None
        assert first.get_profile(owner("read", scope)).identity_version == 0
        assert not first.list_records(owner("read", scope)).records
        identity = Identity(name="Kong", communication_style="Clear and calm")
        first.set_identity(owner("identity-input", scope), identity, expected_revision=0,
                           approved_digest=identity_digest(identity), command_id="identity-first")
        saved = first.remember(owner("event-first", scope), draft, first_source, command_id="remember-first")
        assert first.remember(owner("event-first", scope), draft, first_source, command_id="remember-first").replayed
    with pytest.raises(ServiceClosed):
        first.get_profile(owner("read", scope))

    with MemoryService(database) as second:
        result = second.retrieve(owner("read", scope), "ALPHA5", kind="episodic")
        assert len(result.records) == 1
        assert result.records[0].current.content == original
        assert result.records[0].current.sources == (first_source,)
        old_summary = second.derive_memory(owner("derive-old", scope), draft.model_copy(update={"content": "Old summary ALPHA5"}),
                                           (ParentVersion(record_id=saved.record_id, version=1),), command_id="derive-old")
        revised = second.revise_memory(owner("event-correction", scope), saved.record_id, corrected,
                                       source("event-correction", corrected), expected_revision=1, command_id="correct")
        assert revised.version == 2
        assert second.get_record(owner("read", scope), saved.record_id).current.content == corrected
        assert not second.retrieve(owner("read", scope), "ALPHA5").records
        assert second.get_record(owner("read", scope), old_summary.record_id, include_inactive=True).state == "quarantined"
        assert [v.content for v in second.get_versions(owner("read", scope), saved.record_id)] == [original, corrected]
        new_draft = draft.model_copy(update={"content": "New summary OMEGA7"})
        summary = second.derive_memory(owner("derive-new", scope), new_draft,
                                       (ParentVersion(record_id=saved.record_id, version=2),), command_id="derive-new")
        pending = second.propose_memory(owner("observe-old", scope), draft, first_source, command_id="pending-old-source")
        identity_before = second.get_identity(owner("read", scope))
        deleted = second.forget_memory(owner("forget", scope), saved.record_id, expected_revision=2, command_id="forget-first")
        assert deleted.affected_records == 3
        assert deleted.purged_versions == 4
        assert deleted.cancelled_candidates == 1
        assert deleted.blocked_from_use and deleted.internal_content_purged
        assert not deleted.runtime_history_purged
        assert second.get_identity(owner("read", scope)) == identity_before
        assert not second.list_records(owner("read", scope)).records
        for memory_id in (saved.record_id, summary.record_id, old_summary.record_id):
            with pytest.raises(NotFound):
                second.get_record(owner("read", scope), memory_id, include_inactive=True)
        with pytest.raises(NotFound):
            second.get_candidate(owner("read", scope), pending.candidate_id)
        with pytest.raises(NotFound):
            second.get_versions(owner("read", scope), saved.record_id)

    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM memory_versions WHERE content IS NOT NULL").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM sources WHERE excerpt IS NOT NULL OR locator IS NOT NULL").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM candidates WHERE draft IS NOT NULL").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        # Audit, idempotency receipts and outbox do not retain deleted content.
        for table in ("audit_entries", "command_receipts", "memory_outbox", "deletion_receipts"):
            assert "ALPHA5" not in str(db.execute(f"SELECT * FROM {table}").fetchall())
            assert "OMEGA7" not in str(db.execute(f"SELECT * FROM {table}").fetchall())

    with MemoryService(database) as third:
        assert third.get_deletion_receipt(owner("read", scope), "forget-first").deletion_epoch == 1
        assert third.forget_memory(owner("forget", scope), saved.record_id, expected_revision=2,
                                   command_id="forget-first").replayed
        with pytest.raises(ForgottenSource):
            third.remember(owner("event-first", scope), draft, first_source, command_id="remember-first")
        with pytest.raises(ForgottenSource):
            third.remember(owner("event-first", scope), draft, first_source, command_id="retry-new-command-id")
        with pytest.raises(NotFound):
            third.derive_memory(owner("stale-job", scope), draft,
                                (ParentVersion(record_id=saved.record_id, version=1),), command_id="stale-job")
        assert not third.list_records(owner("read", scope)).records


def test_separate_processes_no_runtime_network_or_api_key(tmp_path):
    """A fresh interpreter, not just module mocks, proves dependency isolation."""
    script = r'''
import importlib.abc
import socket
import sys
sys.path.insert(0, sys.argv[1])
blocked = ("kong.runtime", "kong.storage", "kong.contracts", "kong.tools", "kong.continuity", "httpx")
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("forbidden import: " + fullname)
sys.meta_path.insert(0, Guard())
def no_network(*args, **kwargs):
    raise AssertionError("network is forbidden")
socket.socket = no_network
socket.create_connection = no_network
from kong.memory import *
auth = Authority(owner_id="fixture", profile_id="fixture", input_id="event-one", capabilities=frozenset(Capability))
draft = MemoryDraft(kind="episodic", content="Synthetic restart episode RUN42")
src = Source(source_id="event-one", origin="user_command", speaker="user", excerpt=draft.content)
with MemoryService(sys.argv[2]) as core:
    if sys.argv[3] == "write":
        core.create_profile(auth, command_id="profile")
        identity = Identity(name="Kong")
        core.set_identity(auth, identity, expected_revision=0, approved_digest=identity_digest(identity), command_id="identity")
        core.remember(auth, draft, src, command_id="remember")
    elif sys.argv[3] == "forget":
        record = core.retrieve(auth, "RUN42").records[0]
        assert record.current.content == draft.content
        new = "Synthetic restart episode RUN99"
        core.revise_memory(auth.model_copy(update={"input_id":"correct"}), record.record_id, new,
            Source(source_id="correct", origin="user_command", speaker="user", excerpt=new), expected_revision=1, command_id="correct")
        assert not core.retrieve(auth, "RUN42").records
        core.forget_memory(auth, record.record_id, expected_revision=2, command_id="forget")
    else:
        assert not core.list_records(auth).records
        try:
            core.remember(auth, draft, src, command_id="remember")
        except ForgottenSource:
            pass
        else:
            raise AssertionError("forgotten command resurrected")
assert not any(name in sys.modules for name in blocked)
print("independent-offline-ok")
'''
    root = Path(__file__).resolve().parents[2] / "src"
    env = {key: value for key, value in os.environ.items() if "API_KEY" not in key.upper()}
    for stage in ("write", "forget", "retry"):
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(root), str(tmp_path / "process.sqlite3"), stage],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "independent-offline-ok"
