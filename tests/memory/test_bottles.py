import pytest

from kong.memory import Bottle, MemoryDraft, ParentVersion, Scope, Source
from kong.memory.errors import NotFound, PermissionDenied, PolicyViolation, RevisionConflict


def save(core, auth, text, *, input_id="source-a", scope=Scope()):
    user = auth.model_copy(update={"input_id": input_id})
    return core.remember(user, MemoryDraft(kind="episodic", content=text, scope=scope),
                         Source(source_id=input_id, origin="user_command", speaker="user", excerpt=text), command_id=input_id)


def bottle(core, auth, name):
    core.create_bottle(auth, Bottle(bottle_id=name, profile_id=auth.profile_id, title=name), command_id="create-" + name)


def activate(core, auth, name, enabled=True):
    current = next(b for b in core.list_bottles(auth) if b.bottle_id == name)
    method = core.activate_bottle if enabled else core.deactivate_bottle
    return method(auth, name, expected_revision=current.revision, command_id=f"active-{name}-{current.revision}")


def test_empty_inactive_and_switching_context(core, auth):
    bottle(core, auth, "A")
    bottle(core, auth, "B")
    a = save(core, auth, "Cedar A milestone", input_id="a")
    b = save(core, auth, "Cedar B private", input_id="b")
    core.add_memory_to_bottle(auth, "A", a.record_id, command_id="add-a")
    core.add_memory_to_bottle(auth, "B", b.record_id, command_id="add-b")
    assert core.retrieve_context(auth, "Cedar").records == ()
    assert len(core.retrieve(auth, "Cedar").records) == 2  # Owner CRUD is a distinct view.
    activate(core, auth, "A")
    before = core.retrieve_context(auth, "Cedar")
    assert [r.record_id for r in before.records] == [a.record_id]
    activate(core, auth, "A", False)
    activate(core, auth, "B")
    after = core.retrieve_context(auth, "Cedar")
    assert [r.record_id for r in after.records] == [b.record_id]
    assert after.access.activation_revision > before.access.activation_revision
    assert core.get_record(auth, a.record_id).current.content == "Cedar A milestone"


def test_shared_record_detach_and_forget_all_references(core, auth):
    for name in ("A", "B"):
        bottle(core, auth, name)
        activate(core, auth, name)
    saved = save(core, auth, "Cedar shared milestone")
    for name in ("A", "B"):
        core.add_memory_to_bottle(auth, name, saved.record_id, command_id="add-" + name)
    assert len(core.retrieve_context(auth, "Cedar").records) == 1
    core.remove_memory_from_bottle(auth, "A", saved.record_id, command_id="detach-a")
    assert len(core.retrieve_context(auth, "Cedar").records) == 1
    assert not core.list_bottle_memories(auth, "A")
    receipt = core.forget_memory(auth, saved.record_id, expected_revision=1, command_id="forget")
    assert receipt.internal_content_purged and not receipt.runtime_history_purged
    assert not core.list_bottle_memories(auth, "B")
    with pytest.raises(NotFound):
        core.get_record(auth, saved.record_id)


def test_derived_memory_cannot_launder_inactive_parent(core, auth):
    for name in ("A", "B"):
        bottle(core, auth, name)
    a = save(core, auth, "Cedar allowed", input_id="a")
    b = save(core, auth, "Cedar private hidden", input_id="b")
    core.add_memory_to_bottle(auth, "A", a.record_id, command_id="member-a")
    core.add_memory_to_bottle(auth, "B", b.record_id, command_id="member-b")
    child = core.derive_memory(auth, MemoryDraft(kind="episodic", content="Cedar derived private hidden"),
                               (ParentVersion(record_id=b.record_id, version=1),), command_id="derive")
    core.add_memory_to_bottle(auth, "A", child.record_id, command_id="add-child")
    activate(core, auth, "A")
    assert [r.record_id for r in core.retrieve_context(auth, "Cedar").records] == [a.record_id]
    activate(core, auth, "B")
    assert len(core.retrieve_context(auth, "Cedar").records) == 3


def test_bottle_profile_scope_authority_and_reserved_types(core, auth):
    bottle(core, auth, "A")
    foreign = auth.model_copy(update={"owner_id": "another-owner"})
    with pytest.raises(PermissionDenied):
        core.list_bottles(foreign)
    observer = auth.model_copy(update={"actor": "observer"})
    with pytest.raises(PermissionDenied):
        core.activate_bottle(observer, "A", expected_revision=1, command_id="bad")
    core.create_bottle(auth, Bottle(bottle_id="relationship", profile_id=auth.profile_id, title="reserved", kind="relationship"), command_id="reserved")
    with pytest.raises(PolicyViolation):
        core.activate_bottle(auth, "relationship", expected_revision=1, command_id="not-shipped")
    scoped = auth.model_copy(update={"scopes": (Scope(kind="project", id="P"),)})
    assert core.list_bottles(scoped) == ()


def test_activation_cas_idempotence_and_read_only_query(core, auth):
    bottle(core, auth, "A")
    one = core.activate_bottle(auth, "A", expected_revision=1, command_id="on")
    assert one.bottle_id == "A"
    replay = core.activate_bottle(auth, "A", expected_revision=1, command_id="on")
    assert replay.replayed
    with pytest.raises(RevisionConflict):
        core.deactivate_bottle(auth, "A", expected_revision=1, command_id="stale")
    changes = core._store.db.total_changes
    state = core.get_access_state(auth)
    core.retrieve_context(auth, "Cedar")
    core.list_bottles(auth)
    assert state == core.get_access_state(auth)
    assert core._store.db.total_changes == changes


def test_scope_cannot_be_widened_by_membership(core, auth):
    bottle(core, auth, "A")
    scoped = auth.model_copy(update={"scopes": (Scope(), Scope(kind="project", id="P"))})
    saved = save(core, scoped, "Cedar project", scope=Scope(kind="project", id="P"))
    with pytest.raises(PolicyViolation):
        core.add_memory_to_bottle(scoped, "A", saved.record_id, command_id="widen")
