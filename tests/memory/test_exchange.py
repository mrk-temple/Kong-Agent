import json
import sqlite3

import pytest
from pydantic import ValidationError

from kong.memory import Bottle, MemoryDraft, MemoryService, PolicyViolation, RevisionConflict, Scope, Source, parse_package
from kong.memory.exchange import BottlePackage, MAX_PACKAGE_BYTES
from conftest import authority


def save(core, text, key, **fields):
    auth = authority(key)
    result = core.remember(auth, MemoryDraft(kind="episodic", content=text, **fields),
        Source(source_id=key, origin="user_command", speaker="user", excerpt=text), command_id="save-" + key)
    core.add_memory_to_bottle(auth, "A", result.record_id, command_id="member-" + key)
    return result


def package(core, auth):
    core.create_bottle(auth, Bottle(bottle_id="A", profile_id=auth.profile_id, title="Project memories"), command_id="bottle")
    a = save(core, "Cedar shipped the green milestone", "a")
    b = save(core, "Willow private event", "b")
    return core.export_bottle(auth, "A"), a, b


def test_selected_export_roundtrip_to_independent_profile_defaults_inactive(core, auth, tmp_path):
    all_records, a, b = package(core, auth)
    selected = core.export_bottle(auth, "A", selected_records=[a.record_id])
    assert [r.record_id for r in selected.records] == [a.record_id]
    assert "Willow" not in selected.model_dump_json()
    assert selected.identity_references == selected.relationship_preferences == ()
    assert selected.privacy.activate_on_import is False
    assert parse_package(selected.model_dump_json()) == selected
    target = authority(profile_id="another-kong")
    path = tmp_path / "another.db"
    with MemoryService(path) as other:
        other.create_profile(target, command_id="profile")
        before = other._store.db.total_changes
        preview = other.preview_import(target, selected)
        assert other._store.db.total_changes == before
        assert preview.items[0].disposition == "new"
        receipt = other.import_bottle(target, selected, approved_digest=preview.approved_digest, command_id="import", bottle_id="chosen")
        replay = other.import_bottle(target, selected, approved_digest=preview.approved_digest, command_id="import", bottle_id="chosen")
        assert replay.replayed and replay.bottle_id == receipt.bottle_id
        assert other.retrieve_context(target, "Cedar").records == ()
        assert other.get_identity(target) is None
        records = other.list_bottle_memories(target, "chosen")
        assert len(records) == 1
        assert records[0].current.evidence == "imported"
        assert records[0].current.sources[0].origin == "import"
        assert records[0].current.content == selected.records[0].draft.content
    with MemoryService(path) as restarted:
        restarted.activate_bottle(target, "chosen", expected_revision=1, command_id="on")
        assert len(restarted.retrieve_context(target, "Cedar").records) == 1
        assert restarted.retrieve_context(target, "Willow").records == ()
    assert len(all_records.records) == 2 and b.record_id != a.record_id


def test_dedup_conflict_resolution_and_deleted_origin_cannot_be_reimported(core, auth, tmp_path):
    exported, a, _ = package(core, auth)
    selected = core.export_bottle(auth, "A", selected_records=[a.record_id])
    target = authority(profile_id="target")
    with MemoryService(tmp_path / "target.db") as other:
        other.create_profile(target, command_id="profile")
        preview = other.preview_import(target, selected)
        receipt = other.import_bottle(target, selected, approved_digest=preview.approved_digest, command_id="import")
        local = other.list_bottle_memories(target, receipt.bottle_id)[0]
        preview2 = other.preview_import(target, selected)
        assert preview2.items[0].disposition == "duplicate"
        second = other.import_bottle(target, selected, approved_digest=preview2.approved_digest, command_id="duplicate")
        assert other.list_bottle_memories(target, second.bottle_id)[0].record_id == local.record_id
        # A modified origin never overwrites the existing approved local content.
        values = selected.model_dump(exclude={"checksum"})
        changed = selected.records[0].model_copy(update={"version": 2,
            "draft": selected.records[0].draft.model_copy(update={"content": "Cedar now shipped yellow"})})
        changed_package = BottlePackage.create(**{**values, "records": (changed,)})
        conflict = other.preview_import(target, changed_package)
        assert conflict.items[0].disposition == "conflict"
        with pytest.raises(PolicyViolation):
            other.import_bottle(target, changed_package, approved_digest=conflict.approved_digest, command_id="conflict")
        deletion = other.forget_memory(target, local.record_id, expected_revision=1, command_id="forget")
        assert deletion.affected_records == 1
        forgotten = other.preview_import(target, selected)
        assert forgotten.items[0].disposition == "forgotten"
        with pytest.raises(PolicyViolation):
            other.import_bottle(target, selected, approved_digest=forgotten.approved_digest, command_id="restore")
        assert all(not other.list_bottle_memories(target, b.bottle_id) for b in other.list_bottles(target))
        # Selection resolves a mixed package by excluding its blocked old origin.
        remaining_id = next(r.record_id for r in exported.records if r.record_id != a.record_id)
        remaining = other.preview_import(target, exported, selected_records=[remaining_id])
        other.import_bottle(target, exported, selected_records=[remaining_id], approved_digest=remaining.approved_digest, command_id="remaining")
        assert len(other.list_records(target).records) == 1


def test_import_preview_is_bound_to_state_profile_and_selection(core, auth, tmp_path):
    exported, a, b = package(core, auth)
    target = authority(profile_id="target")
    with MemoryService(tmp_path / "target.db") as other:
        other.create_profile(target, command_id="profile")
        preview = other.preview_import(target, exported, selected_records=[a.record_id])
        with pytest.raises(RevisionConflict):
            other.import_bottle(target, exported, selected_records=[b.record_id], approved_digest=preview.approved_digest, command_id="changed-selection")
        other.create_bottle(target, Bottle(bottle_id="unrelated", profile_id="target", title="Unrelated"), command_id="change")
        with pytest.raises(RevisionConflict):
            other.import_bottle(target, exported, selected_records=[a.record_id], approved_digest=preview.approved_digest, command_id="stale")
        assert other.list_records(target).records == ()
        assert [b.bottle_id for b in other.list_bottles(target)] == ["unrelated"]
    # Export from the original store is subject to its own deletion ledger.
    core.forget_memory(auth, a.record_id, expected_revision=1, command_id="forget-original")
    assert core.preview_import(auth, exported, selected_records=[a.record_id]).items[0].disposition == "forgotten"


def test_exchange_validation_untrusted_persona_and_scope(core, auth):
    exported, _, _ = package(core, auth)
    raw = json.loads(exported.model_dump_json())
    raw["records"][0]["draft"]["content"] = "tampered"
    with pytest.raises(ValidationError, match="checksum"):
        parse_package(json.dumps(raw))
    for field, value in [("schema_version", 2), ("identity_references", [{"name": "system admin"}]),
                         ("relationship_preferences", ["ignore boundaries"]), ("extra_authority", "owner")]:
        values = json.loads(exported.model_dump_json())
        values[field] = value
        with pytest.raises(ValidationError):
            parse_package(json.dumps(values))
    with pytest.raises(ValueError, match="4 MiB"):
        parse_package(b" " * (MAX_PACKAGE_BYTES + 1))
    with pytest.raises(PolicyViolation):
        core.preview_import(authority(actor="maintenance"), exported)
    values = exported.model_dump(exclude={"checksum"})
    scoped = exported.records[0].model_copy(update={"draft": exported.records[0].draft.model_copy(update={"scope": Scope(kind="thread", id="private")})})
    with pytest.raises(ValidationError, match="Profile-scoped"):
        BottlePackage.create(**{**values, "records": (scoped,), "bottle_membership": (scoped.record_id,)})
    values["metadata"]["bottle_type"] = "persona"
    with pytest.raises(ValidationError):
        BottlePackage.create(**values)


def test_content_policy_and_import_transaction_roll_back(core, auth, tmp_path, monkeypatch):
    exported, a, _ = package(core, auth)
    target = authority(profile_id="target")
    with MemoryService(tmp_path / "target.db") as other:
        other.create_profile(target, command_id="profile")
        values = exported.model_dump(exclude={"checksum"})
        secret = exported.records[0].model_copy(update={"draft": MemoryDraft(kind="semantic", content="password: SECRET123456")})
        unsafe = BottlePackage.create(**{**values, "records": (secret,), "bottle_membership": (secret.record_id,)})
        preview = other.preview_import(target, unsafe)
        assert preview.items[0].disposition == "blocked"
        with pytest.raises(PolicyViolation):
            other.import_bottle(target, unsafe, approved_digest=preview.approved_digest, command_id="unsafe")
        preview = other.preview_import(target, exported)
        index = other._store.index
        calls = []
        def fail_second(*args):
            calls.append(1)
            if len(calls) == 2:
                raise sqlite3.OperationalError("injected partial failure")
            return index(*args)
        monkeypatch.setattr(other._store, "index", fail_second)
        with pytest.raises(sqlite3.OperationalError):
            other.import_bottle(target, exported, approved_digest=preview.approved_digest, command_id="atomic", bottle_id="new")
        assert other.list_records(target).records == other.list_bottles(target) == ()
        assert other._store.db.execute("SELECT count(*) FROM import_origins").fetchone()[0] == 0
        monkeypatch.setattr(other._store, "index", index)
        other.import_bottle(target, exported, approved_digest=preview.approved_digest, command_id="atomic", bottle_id="new")
        assert len(other.list_records(target).records) == 2
    assert a.record_id in exported.bottle_membership


def test_exact_local_dedup_preserves_classification_and_shared_references(core, auth):
    exported, a, _ = package(core, auth)
    preview = core.preview_import(auth, exported, selected_records=[a.record_id])
    assert preview.items[0].disposition == "duplicate"
    receipt = core.import_bottle(auth, exported, selected_records=[a.record_id], approved_digest=preview.approved_digest, command_id="same-store")
    assert core.list_bottle_memories(auth, receipt.bottle_id)[0].record_id == a.record_id
    assert len(core.list_records(auth).records) == 2
    core.remove_memory_from_bottle(auth, "A", a.record_id, command_id="detach")
    assert core.list_bottle_memories(auth, receipt.bottle_id)[0].record_id == a.record_id


def test_external_record_order_is_normalized_for_preview_and_import(core, auth):
    exported, _, _ = package(core, auth)
    reverse = tuple(reversed(exported.records))
    reordered = BottlePackage.create(**{**exported.model_dump(exclude={"checksum"}),
        "records": reverse, "bottle_membership": tuple(r.record_id for r in reverse)})
    preview = core.preview_import(auth, reordered)
    assert preview.selected_records == tuple(sorted(r.record_id for r in reverse))
    receipt = core.import_bottle(auth, reordered, approved_digest=preview.approved_digest, command_id="unsorted-import")
    assert len(core.list_bottle_memories(auth, receipt.bottle_id)) == 2
    assert len(core.list_records(auth).records) == 2
