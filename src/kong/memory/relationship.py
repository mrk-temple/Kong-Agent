"""Versioned user-approved interaction rules, independent of Runtime and Identity."""
import hmac

from kong.memory.domain import Capability, Relationship, RelationshipVersion, Scope, relationship_digest
from kong.memory.errors import NotFound, PermissionDenied, PolicyViolation, RevisionConflict
from kong.memory.policy import require, require_scope


class RelationshipOperations:
    def _relationship(self, db, auth, version=None):
        require_scope(auth, Scope())
        profile = self._profile(db, auth)
        number = profile["relationship_version"] if version is None else version
        row = db.execute("SELECT * FROM relationship_versions WHERE profile_id=? AND version=?", (auth.profile_id, number)).fetchone()
        if row is None:
            if version is not None:
                raise NotFound("Relationship version is unavailable")
            return None  # Born empty; do not invent relationship history.
        return RelationshipVersion(profile_id=auth.profile_id, version=number,
            relationship=Relationship.model_validate_json(row["content"]), approved_by=row["approved_by"],
            created_at=row["created_at"], proposal_id=row["proposal_id"])

    def get_relationship(self, auth, *, version=None):
        require(auth, Capability.READ)
        if version is not None and auth.actor != "owner":
            raise PermissionDenied("Historical Relationship inspection requires the owner")
        with self._store.transaction(write=False) as db:
            return self._relationship(db, auth, version)

    def _write_relationship(self, db, auth, value, base_version, proposal_id=None):
        require_scope(auth, Scope())
        if self._profile(db, auth)["relationship_version"] != base_version:
            raise RevisionConflict("Relationship version changed before approval")
        number = base_version + 1
        db.execute("INSERT INTO relationship_versions VALUES(?,?,?,?,?,?)", (auth.profile_id, number,
            value.model_dump_json(), auth.input_id, self.clock(), proposal_id))
        db.execute("UPDATE profiles SET relationship_version=? WHERE profile_id=?", (number, auth.profile_id))
        return {"revision": number, "version": number, "proposal_id": proposal_id}

    def set_relationship(self, auth, relationship: Relationship, *, expected_revision, approved_digest, command_id):
        def action(db):
            if not hmac.compare_digest(relationship_digest(relationship), approved_digest):
                raise PolicyViolation("Approval does not match the exact Relationship")
            return self._write_relationship(db, auth, relationship, expected_revision)
        return self._execute(auth, command_id, "set_relationship", {"relationship": relationship.model_dump(mode="json"),
            "expected_revision": expected_revision, "approved_digest": approved_digest}, Capability.RELATIONSHIP, action)
