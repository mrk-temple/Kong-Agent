"""Small standalone command/query service. All writes are atomic and authorized.

This synchronous API does not run an Agent Loop, call a model, or open sockets.
Phase 2 hosts can call it through a bounded I/O adapter instead of blocking their
event loop. The host supplies authenticated Authority objects, not model JSON.
"""
from collections.abc import Callable
from datetime import datetime
import hmac
from pathlib import Path
from uuid import uuid4

from kong.lexical import lexical_terms
from kong.memory.domain import (
    Authority, Candidate, Capability, CommandReceipt, DeletionPlan, DeletionReceipt, Identity,
    IdentityVersion, MemoryDraft, MemoryRecord, MemoryVersion, ParentVersion, Episode,
    Profile, Retrieval, Scope, Source, candidate_digest, identity_digest, utc_now,
)
from kong.memory.errors import (
    ForgottenSource, IdempotencyConflict, NotFound, PermissionDenied,
    PolicyViolation, RevisionConflict,
)
from kong.memory.policy import require, require_scope, validate_draft, validate_explicit_source
from kong.memory.sqlite_store import SQLiteStore
from kong.memory.bottles import BottleOperations
from kong.memory.observations import ObservationOperations
from kong.memory.exchange import ExchangeOperations
from kong.memory.formation import FormationOperations
from kong.memory.relationship import RelationshipOperations
from kong.memory.reflection import ReflectionOperations


class MemoryService(BottleOperations, ObservationOperations, ExchangeOperations, FormationOperations,
                    RelationshipOperations, ReflectionOperations):
    def __init__(self, path: Path | str, *, clock: Callable[[], str] = utc_now):
        self._store = SQLiteStore(path)
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._store.close()

    @staticmethod
    def _profile(db, auth):
        row = db.execute("SELECT * FROM profiles WHERE profile_id=?", (auth.profile_id,)).fetchone()
        if row is None or row["owner_id"] != auth.owner_id:
            raise PermissionDenied("Profile is not owned by this principal")
        return row

    @staticmethod
    def _row(db, auth, record_id, *, allow_deleted=False):
        row = db.execute("SELECT * FROM memory_records WHERE profile_id=? AND record_id=?",
                         (auth.profile_id, record_id)).fetchone()
        if row is None or (row["state"] == "deleted" and not allow_deleted):
            raise NotFound("Memory is unavailable")
        require_scope(auth, Scope(kind=row["scope_kind"], id=row["scope_id"]))
        return row

    @staticmethod
    def _revision(row, expected):
        if row["revision"] != expected:
            raise RevisionConflict(f"Expected revision {expected}; current revision is {row['revision']}")

    def _audit(self, db, auth, command_id, operation, object_id=None, before=None, after=None):
        db.execute("""INSERT INTO audit_entries(profile_id,command_id,actor,input_id,operation,object_id,from_state,to_state,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (auth.profile_id, command_id, auth.actor, auth.input_id, operation,
                                           object_id, before, after, self.clock()))

    def _execute(self, auth, command_id, operation, payload, capability, action, *, explicit=True, creating=False):
        require(auth, capability, explicit=explicit)
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 200:
            raise ValueError("command_id must be a stable nonempty ID of at most 200 characters")
        digest = self._store.digest({"operation": operation, "payload": payload, "actor": auth.actor,
                                     "input_id": auth.input_id, "scopes": sorted((scope.kind, scope.id) for scope in auth.scopes)})
        with self._store.transaction() as db:
            existing = db.execute("SELECT owner_id FROM profiles WHERE profile_id=?", (auth.profile_id,)).fetchone()
            if not creating or existing is not None:
                self._profile(db, auth)
            saved = db.execute("SELECT digest,result FROM command_receipts WHERE profile_id=? AND command_id=?",
                               (auth.profile_id, command_id)).fetchone()
            if saved:
                if not hmac.compare_digest(saved["digest"], digest):
                    raise IdempotencyConflict("command_id was already used for a different request")
                cls = DeletionReceipt if operation == "forget" else CommandReceipt
                receipt = cls.model_validate_json(saved["result"])
                if receipt.record_id:
                    record = self._row(db, auth, receipt.record_id, allow_deleted=True)
                    if operation != "forget" and record["state"] == "deleted":
                        raise ForgottenSource("The original command's memory was forgotten")
                if receipt.candidate_id:
                    candidate = db.execute("SELECT state,draft FROM candidates WHERE profile_id=? AND candidate_id=?",
                                           (auth.profile_id, receipt.candidate_id)).fetchone()
                    if candidate["state"] == "cancelled":
                        raise ForgottenSource("The original candidate was forgotten")
                    if candidate["draft"]:
                        require_scope(auth, MemoryDraft.model_validate_json(candidate["draft"]).scope)
                if receipt.proposal_id:
                    proposal = db.execute("SELECT content FROM evolution_proposals WHERE profile_id=? AND proposal_id=?", (auth.profile_id, receipt.proposal_id)).fetchone()
                    if proposal is None or proposal["content"] is None:
                        raise ForgottenSource("The original proposal's evidence was purged")
                if receipt.reflection_id:
                    report = db.execute("SELECT payload FROM reflection_runs WHERE profile_id=? AND run_id=?", (auth.profile_id, receipt.reflection_id)).fetchone()
                    if report is None or report["payload"] is None:
                        raise ForgottenSource("The original reflection was purged")
                return receipt.model_copy(update={"replayed": True})
            result = action(db)
            receipt = result if isinstance(result, CommandReceipt) else CommandReceipt(
                profile_id=auth.profile_id, command_id=command_id, operation=operation, **result)
            db.execute("UPDATE profiles SET memory_revision=memory_revision+1 WHERE profile_id=?", (auth.profile_id,))
            profile = self._profile(db, auth)
            db.execute("INSERT INTO command_receipts VALUES(?,?,?,?)",
                       (auth.profile_id, command_id, digest, receipt.model_dump_json()))
            object_id = receipt.record_id or receipt.candidate_id or receipt.bottle_id or receipt.proposal_id or receipt.reflection_id
            self._audit(db, auth, command_id, operation, object_id)
            db.execute("""INSERT INTO memory_outbox(profile_id,operation,object_id,memory_revision,deletion_epoch,created_at)
                VALUES(?,?,?,?,?,?)""", (auth.profile_id, operation, object_id,
                                         profile["memory_revision"], profile["deletion_epoch"], self.clock()))
            return receipt

    def create_profile(self, auth: Authority, *, command_id: str) -> CommandReceipt:
        def action(db):
            if db.execute("SELECT 1 FROM profiles WHERE profile_id=?", (auth.profile_id,)).fetchone():
                raise RevisionConflict("Profile already exists")
            db.execute("INSERT INTO profiles(profile_id,owner_id) VALUES(?,?)", (auth.profile_id, auth.owner_id))
            return {}
        return self._execute(auth, command_id, "create_profile", {}, Capability.PROFILE, action, creating=True)

    def get_profile(self, auth: Authority) -> Profile:
        require(auth, Capability.READ)
        with self._store.transaction(write=False) as db:
            return Profile.model_validate(dict(self._profile(db, auth)))

    def set_identity(self, auth: Authority, identity: Identity, *, expected_revision: int,
                     approved_digest: str, command_id: str) -> CommandReceipt:
        """Host approval binds the exact configuration and current identity version."""
        payload = {"identity": identity.model_dump(mode="json"), "expected_revision": expected_revision,
                   "approved_digest": approved_digest}
        def action(db):
            profile = self._profile(db, auth)
            if profile["identity_version"] != expected_revision:
                raise RevisionConflict("Identity version changed before approval")
            if not hmac.compare_digest(identity_digest(identity), approved_digest):
                raise PolicyViolation("Approval does not match the exact Identity")
            version = expected_revision + 1
            db.execute("INSERT INTO identity_versions VALUES(?,?,?,?,?)",
                       (auth.profile_id, version, identity.model_dump_json(), auth.input_id, self.clock()))
            db.execute("UPDATE profiles SET identity_version=? WHERE profile_id=?", (version, auth.profile_id))
            return {"revision": version, "version": version}
        return self._execute(auth, command_id, "set_identity", payload, Capability.IDENTITY, action)

    def get_identity(self, auth: Authority, *, version: int | None = None) -> IdentityVersion | None:
        require(auth, Capability.READ)
        if version is not None and auth.actor != "owner":
            raise PermissionDenied("Historical Identity inspection requires the owner")
        with self._store.transaction(write=False) as db:
            profile = self._profile(db, auth)
            row = db.execute("SELECT * FROM identity_versions WHERE profile_id=? AND version=?",
                             (auth.profile_id, profile["identity_version"] if version is None else version)).fetchone()
            if row is None:
                if version is not None:
                    raise NotFound("Identity version is unavailable")
                return None  # Born empty; no invented personality or relationship.
            return IdentityVersion(profile_id=auth.profile_id, version=row["version"],
                                   identity=Identity.model_validate_json(row["content"]),
                                   approved_by=row["approved_by"], created_at=row["created_at"])

    def _source(self, db, auth, source):
        if db.execute("SELECT 1 FROM source_suppressions WHERE profile_id=? AND source_id=?",
                      (auth.profile_id, source.source_id)).fetchone():
            raise ForgottenSource("Source was suppressed by a deletion")
        digest = self._store.digest(source.model_dump(mode="json"))
        old = db.execute("SELECT digest,purged_at FROM sources WHERE profile_id=? AND source_id=?",
                         (auth.profile_id, source.source_id)).fetchone()
        if old:
            if old["purged_at"]:
                raise ForgottenSource("Source content was purged")
            if not hmac.compare_digest(old["digest"], digest):
                raise IdempotencyConflict("source_id already identifies different evidence")
        else:
            db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?,NULL)", (auth.profile_id, source.source_id,
                       source.origin, source.speaker, source.excerpt, source.locator, digest))

    @staticmethod
    def _source_dto(row):
        return Source(**{key: row[key] for key in ("source_id", "origin", "speaker", "excerpt", "locator")})

    def _version(self, db, auth, record_id, version):
        row = db.execute("SELECT * FROM memory_versions WHERE profile_id=? AND record_id=? AND version=?",
                         (auth.profile_id, record_id, version)).fetchone()
        if row is None or row["content"] is None:
            raise NotFound("Memory version is unavailable")
        sources = db.execute("""SELECT s.* FROM sources s JOIN memory_sources m
            ON s.profile_id=m.profile_id AND s.source_id=m.source_id
            WHERE m.profile_id=? AND m.record_id=? AND m.version=? ORDER BY s.source_id""",
                             (auth.profile_id, record_id, version)).fetchall()
        if any(source["purged_at"] for source in sources):
            raise NotFound("A source is unavailable")
        details = db.execute("SELECT payload FROM episodic_details WHERE profile_id=? AND record_id=? AND version=?", (auth.profile_id, record_id, version)).fetchone()
        return MemoryVersion(record_id=record_id, version=version, content=row["content"], evidence=row["evidence"],
                             sources=tuple(self._source_dto(s) for s in sources), created_at=row["created_at"],
                             supersedes=row["supersedes"], episode=Episode.model_validate_json(details["payload"]) if details and details["payload"] else None)

    def _record(self, db, auth, row):
        fields = ("record_id", "profile_id", "owner_id", "revision", "state", "kind", "category", "subject_id",
                  "subject_kind", "assertion_mode", "sensitivity", "occurred_at", "expires_at")
        return MemoryRecord(**{key: row[key] for key in fields}, scope=Scope(kind=row["scope_kind"], id=row["scope_id"]),
                            current=self._version(db, auth, row["record_id"], row["head_version"]))

    def _new_record(self, db, auth, draft, sources, *, evidence="user_authorized"):
        record_id = uuid4().hex
        db.execute("""INSERT INTO memory_records VALUES(?,?,?,1,'active',?,?,?,?,?,?,?,?,?,?,1)""",
                   (auth.profile_id, record_id, auth.owner_id, draft.kind, draft.category, draft.subject_id,
                    draft.subject_kind, draft.scope.kind, draft.scope.id, draft.assertion_mode, draft.sensitivity,
                    draft.occurred_at.isoformat() if draft.occurred_at else None,
                    draft.expires_at.isoformat() if draft.expires_at else None))
        db.execute("INSERT INTO memory_versions VALUES(?,?,1,?,?,?,NULL,NULL)",
                   (auth.profile_id, record_id, draft.content, evidence, self.clock()))
        for source in sources:
            self._source(db, auth, source)
            db.execute("INSERT OR IGNORE INTO memory_sources VALUES(?,?,1,?)", (auth.profile_id, record_id, source.source_id))
        self._store.index(db, auth.profile_id, record_id, draft.content)
        return {"record_id": record_id, "revision": 1, "version": 1}

    def remember(self, auth: Authority, draft: MemoryDraft, source: Source, *, command_id: str) -> CommandReceipt:
        def action(db):
            require_scope(auth, draft.scope)
            validate_draft(draft)
            validate_explicit_source(auth, draft, source)
            result = self._new_record(db, auth, draft, (source,))
            self._audit(db, auth, command_id, "record_state", result["record_id"], None, "active")
            return result
        return self._execute(auth, command_id, "remember", {"draft": draft.model_dump(mode="json"),
                             "source": source.model_dump(mode="json")}, Capability.WRITE, action)

    def propose_memory(self, auth: Authority, draft: MemoryDraft, source: Source, *, command_id: str) -> CommandReceipt:
        def action(db):
            require_scope(auth, draft.scope)
            validate_draft(draft)
            validate_draft(draft.model_copy(update={"content": source.excerpt}))
            self._source(db, auth, source)
            candidate_id = uuid4().hex
            db.execute("INSERT INTO candidates VALUES(?,?,1,'pending',?,?,NULL)",
                       (auth.profile_id, candidate_id, draft.model_dump_json(), source.source_id))
            self._audit(db, auth, command_id, "candidate_state", candidate_id, None, "pending")
            return {"candidate_id": candidate_id, "revision": 1, "status": "pending"}
        return self._execute(auth, command_id, "propose", {"draft": draft.model_dump(mode="json"),
                             "source": source.model_dump(mode="json")}, Capability.PROPOSE, action, explicit=False)

    def _candidate(self, db, auth, candidate_id):
        row = db.execute("SELECT * FROM candidates WHERE profile_id=? AND candidate_id=?", (auth.profile_id, candidate_id)).fetchone()
        if row is None or row["draft"] is None:
            raise NotFound("Candidate is unavailable")
        draft = MemoryDraft.model_validate_json(row["draft"])
        require_scope(auth, draft.scope)
        source = db.execute("SELECT * FROM sources WHERE profile_id=? AND source_id=?", (auth.profile_id, row["source_id"])).fetchone()
        if source["purged_at"]:
            raise ForgottenSource("Candidate source was forgotten")
        return row, draft, self._source_dto(source)

    def get_candidate(self, auth: Authority, candidate_id: str) -> Candidate:
        require(auth, Capability.READ)
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            row, draft, source = self._candidate(db, auth, candidate_id)
            return Candidate(candidate_id=candidate_id, profile_id=auth.profile_id, revision=row["revision"],
                             state=row["state"], draft=draft, source=source, record_id=row["record_id"])

    def confirm_candidate(self, auth: Authority, candidate_id: str, *, expected_revision: int,
                          approved_digest: str, command_id: str) -> CommandReceipt:
        def action(db):
            row, draft, source = self._candidate(db, auth, candidate_id)
            if db.execute("SELECT 1 FROM formation_candidates WHERE profile_id=? AND candidate_id=?", (auth.profile_id, candidate_id)).fetchone():
                raise PolicyViolation("Formation candidates require an approved consolidation plan")
            self._revision(row, expected_revision)
            if row["state"] != "pending":
                raise PolicyViolation("Only pending candidates can be confirmed")
            if not hmac.compare_digest(candidate_digest(draft), approved_digest):
                raise PolicyViolation("Approval does not match the candidate")
            validate_draft(draft)
            self._audit(db, auth, command_id, "candidate_state", candidate_id, "pending", "validated")
            # This is approved canonical content, not a fabricated verbatim chat.
            approval = Source(source_id=auth.input_id, origin="user_command", speaker="user", excerpt=draft.content,
                              locator="candidate-approval:" + candidate_id)
            result = self._new_record(db, auth, draft, (source, approval))
            db.execute("UPDATE candidates SET state='consolidated',revision=revision+1,record_id=? WHERE profile_id=? AND candidate_id=?",
                       (result["record_id"], auth.profile_id, candidate_id))
            self._audit(db, auth, command_id, "candidate_state", candidate_id, "validated", "consolidated")
            return {**result, "candidate_id": candidate_id}
        return self._execute(auth, command_id, "confirm", {"candidate_id": candidate_id,
                             "expected_revision": expected_revision, "approved_digest": approved_digest}, Capability.CONFIRM, action)

    def reject_candidate(self, auth: Authority, candidate_id: str, *, expected_revision: int, command_id: str) -> CommandReceipt:
        def action(db):
            row, _, _ = self._candidate(db, auth, candidate_id)
            self._revision(row, expected_revision)
            if row["state"] != "pending":
                raise PolicyViolation("Only pending candidates can be rejected")
            db.execute("UPDATE candidates SET state='rejected',revision=revision+1 WHERE profile_id=? AND candidate_id=?",
                       (auth.profile_id, candidate_id))
            self._audit(db, auth, command_id, "candidate_state", candidate_id, "pending", "rejected")
            return {"candidate_id": candidate_id, "revision": expected_revision + 1}
        return self._execute(auth, command_id, "reject", {"candidate_id": candidate_id,
                             "expected_revision": expected_revision}, Capability.CONFIRM, action)

    def get_record(self, auth: Authority, record_id: str, *, include_inactive: bool = False) -> MemoryRecord:
        require(auth, Capability.READ)
        if include_inactive and auth.actor != "owner":
            raise PermissionDenied("Only the owner can inspect inactive memories")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            row = self._row(db, auth, record_id)
            if not include_inactive and not self._usable(row):
                raise NotFound("Memory is not currently active")
            return self._record(db, auth, row)

    def get_versions(self, auth: Authority, record_id: str) -> tuple[MemoryVersion, ...]:
        """Owner-only inspection. Historical versions never enter ordinary retrieval."""
        require(auth, Capability.READ)
        if auth.actor != "owner":
            raise PermissionDenied("Historical inspection requires the owner")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            self._row(db, auth, record_id)
            versions = db.execute("SELECT version FROM memory_versions WHERE profile_id=? AND record_id=? ORDER BY version",
                                  (auth.profile_id, record_id)).fetchall()
            return tuple(self._version(db, auth, record_id, row["version"]) for row in versions)

    def _usable(self, row):
        return row["state"] == "active" and (row["expires_at"] is None or
               datetime.fromisoformat(row["expires_at"]) > datetime.fromisoformat(self.clock()))

    def retrieve(self, auth: Authority, query: str, *, scopes: tuple[Scope, ...] | None = None,
                 kind: str | None = None, limit: int = 12) -> Retrieval:
        """Authorized SQL filtering precedes ranking/LIMIT. No write side effects."""
        require(auth, Capability.READ)
        if not isinstance(query, str) or len(query) > 2000 or not 1 <= limit <= 100:
            raise ValueError("Query is limited to 2000 characters and 1..100 results")
        if kind not in (None, "semantic", "episodic"):
            raise ValueError("Unknown memory kind")
        selected = auth.scopes if scopes is None else scopes
        for scope in selected:
            require_scope(auth, scope)
        with self._store.transaction(write=False) as db:
            profile = self._profile(db, auth)
            clauses = ["r.profile_id=?", "r.owner_id=?", "r.state='active'",
                       "(r.expires_at IS NULL OR julianday(r.expires_at)>julianday(?))"]
            params = [auth.profile_id, auth.owner_id, self.clock()]
            if selected:
                clauses.append("(" + " OR ".join("(r.scope_kind=? AND r.scope_id=?)" for _ in selected) + ")")
                for scope in selected:
                    params.extend((scope.kind, scope.id))
            else:
                clauses.append("0")
            if kind:
                clauses.append("r.kind=?")
                params.append(kind)
            terms = lexical_terms(query)[:64]
            if query.strip() and not terms:
                return Retrieval(records=(), memory_revision=profile["memory_revision"], deletion_epoch=profile["deletion_epoch"])
            if terms:
                expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                sql = """SELECT r.* FROM memory_fts JOIN memory_records r
                    ON memory_fts.profile_id=r.profile_id AND memory_fts.record_id=r.record_id
                    WHERE memory_fts MATCH ? AND """ + " AND ".join(clauses) + " ORDER BY bm25(memory_fts),r.record_id LIMIT ?"
                params = [expression, *params, limit]
            else:
                sql = "SELECT r.* FROM memory_records r WHERE " + " AND ".join(clauses) + " ORDER BY r.record_id LIMIT ?"
                params.append(limit)
            rows = db.execute(sql, params).fetchall()
            return Retrieval(records=tuple(self._record(db, auth, row) for row in rows),
                             memory_revision=profile["memory_revision"], deletion_epoch=profile["deletion_epoch"])

    def list_records(self, auth: Authority, *, limit: int = 100) -> Retrieval:
        return self.retrieve(auth, "", limit=limit)

    def _quarantine_descendants(self, db, auth, parent_id, command_id):
        rows = db.execute("""WITH RECURSIVE children(id) AS (
            SELECT child_id FROM derivations WHERE profile_id=? AND parent_id=?
            UNION SELECT d.child_id FROM derivations d JOIN children c ON d.parent_id=c.id WHERE d.profile_id=?)
            SELECT r.* FROM memory_records r JOIN children c ON c.id=r.record_id
            WHERE r.profile_id=? AND r.state IN ('active','archived')""",
                          (auth.profile_id, parent_id, auth.profile_id, auth.profile_id)).fetchall()
        for row in rows:
            require_scope(auth, Scope(kind=row["scope_kind"], id=row["scope_id"]))
            db.execute("UPDATE memory_records SET state='quarantined',revision=revision+1 WHERE profile_id=? AND record_id=?",
                       (auth.profile_id, row["record_id"]))
            self._store.index(db, auth.profile_id, row["record_id"], None)
            self._audit(db, auth, command_id, "record_state", row["record_id"], row["state"], "quarantined")

    def revise_memory(self, auth: Authority, record_id: str, content: str, source: Source, *,
                      expected_revision: int, command_id: str) -> CommandReceipt:
        def action(db):
            row = self._row(db, auth, record_id)
            self._revision(row, expected_revision)
            if db.execute("SELECT 1 FROM derivations WHERE profile_id=? AND child_id=?", (auth.profile_id, record_id)).fetchone():
                raise PolicyViolation("Revise source memories and derive again; do not edit a dependent summary")
            draft = MemoryDraft(kind=row["kind"], content=content, scope=Scope(kind=row["scope_kind"], id=row["scope_id"]),
                                category=row["category"], subject_id=row["subject_id"], subject_kind=row["subject_kind"],
                                assertion_mode=row["assertion_mode"], sensitivity=row["sensitivity"],
                                occurred_at=row["occurred_at"], expires_at=row["expires_at"])
            validate_draft(draft)
            validate_explicit_source(auth, draft, source)
            self._source(db, auth, source)
            version = row["head_version"] + 1
            db.execute("INSERT INTO memory_versions VALUES(?,?,?,?,?,?,?,NULL)",
                       (auth.profile_id, record_id, version, content, "user_authorized", self.clock(), row["head_version"]))
            db.execute("INSERT INTO memory_sources VALUES(?,?,?,?)", (auth.profile_id, record_id, version, source.source_id))
            db.execute("UPDATE memory_records SET head_version=?,revision=revision+1 WHERE profile_id=? AND record_id=?",
                       (version, auth.profile_id, record_id))
            self._store.index(db, auth.profile_id, record_id, content if row["state"] == "active" else None)
            self._quarantine_descendants(db, auth, record_id, command_id)
            return {"record_id": record_id, "revision": expected_revision + 1, "version": version}
        return self._execute(auth, command_id, "revise", {"record_id": record_id, "content": content,
                             "source": source.model_dump(mode="json"), "expected_revision": expected_revision}, Capability.WRITE, action)

    def _change_state(self, auth, record_id, expected_revision, command_id, before, after):
        def action(db):
            row = self._row(db, auth, record_id)
            self._revision(row, expected_revision)
            if row["state"] != before:
                raise PolicyViolation(f"Transition requires state {before}")
            db.execute("UPDATE memory_records SET state=?,revision=revision+1 WHERE profile_id=? AND record_id=?",
                       (after, auth.profile_id, record_id))
            content = self._version(db, auth, record_id, row["head_version"]).content if after == "active" else None
            self._store.index(db, auth.profile_id, record_id, content)
            if after == "archived":
                self._quarantine_descendants(db, auth, record_id, command_id)
            self._audit(db, auth, command_id, "record_state", record_id, before, after)
            return {"record_id": record_id, "revision": expected_revision + 1, "version": row["head_version"]}
        return self._execute(auth, command_id, "archive" if after == "archived" else "unarchive",
                             {"record_id": record_id, "expected_revision": expected_revision}, Capability.WRITE, action)

    def archive_memory(self, auth: Authority, record_id: str, *, expected_revision: int, command_id: str) -> CommandReceipt:
        return self._change_state(auth, record_id, expected_revision, command_id, "active", "archived")

    def unarchive_memory(self, auth: Authority, record_id: str, *, expected_revision: int, command_id: str) -> CommandReceipt:
        return self._change_state(auth, record_id, expected_revision, command_id, "archived", "active")

    def derive_memory(self, auth: Authority, draft: MemoryDraft, parents: tuple[ParentVersion, ...], *, command_id: str) -> CommandReceipt:
        """Known derived records inherit all parent source links and exact scope.

        This is deterministic persistence, not an automatic Reflection/model call.
        Parents must be active current versions. Stale/deleted inputs cannot commit.
        """
        def action(db):
            require_scope(auth, draft.scope)
            validate_draft(draft)
            if not parents or len(parents) > 32:
                raise PolicyViolation("Derivation requires 1..32 parent versions")
            sources = {}
            for parent in parents:
                row = self._row(db, auth, parent.record_id)
                if not self._usable(row) or row["head_version"] != parent.version:
                    raise RevisionConflict("Derivation input is no longer an active current version")
                if (row["scope_kind"], row["scope_id"], row["sensitivity"], row["assertion_mode"], row["subject_kind"], row["subject_id"]) != (
                        draft.scope.kind, draft.scope.id, draft.sensitivity, draft.assertion_mode, draft.subject_kind, draft.subject_id):
                    raise PolicyViolation("Cross-scope, subject, sensitivity or assertion-mode derivation is unsupported")
                if row["expires_at"] and (draft.expires_at is None or draft.expires_at > datetime.fromisoformat(row["expires_at"])):
                    raise PolicyViolation("A derivation cannot outlive its parent")
                for source in self._version(db, auth, parent.record_id, parent.version).sources:
                    sources[source.source_id] = source
            result = self._new_record(db, auth, draft, tuple(sources.values()), evidence="derived")
            for parent in parents:
                db.execute("INSERT OR IGNORE INTO derivations VALUES(?,?,?,?)",
                           (auth.profile_id, parent.record_id, parent.version, result["record_id"]))
            return result
        return self._execute(auth, command_id, "derive", {"draft": draft.model_dump(mode="json"),
                             "parents": [parent.model_dump() for parent in parents]}, Capability.DERIVE, action, explicit=False)

    def _deletion_closure(self, db, auth, record_id):
        """Conservative source-event granularity: shared sources are one unit.

        Grow through all versions, shared sources, and transitive derived records.
        Scope authority for the complete closure is checked before any mutation.
        """
        records, sources, frontier = set(), set(), {record_id}
        while frontier:
            current = frontier.pop()
            if current in records:
                continue
            row = self._row(db, auth, current, allow_deleted=True)
            if row["state"] == "deleted":
                continue
            records.add(current)
            refs = db.execute("SELECT DISTINCT source_id FROM memory_sources WHERE profile_id=? AND record_id=?",
                              (auth.profile_id, current)).fetchall()
            for ref in refs:
                source_id = ref["source_id"]
                if source_id not in sources:
                    sources.add(source_id)
                    linked = db.execute("SELECT DISTINCT record_id FROM memory_sources WHERE profile_id=? AND source_id=?",
                                        (auth.profile_id, source_id)).fetchall()
                    frontier.update(link["record_id"] for link in linked if link["record_id"] not in records)
            children = db.execute("SELECT child_id FROM derivations WHERE profile_id=? AND parent_id=?",
                                  (auth.profile_id, current)).fetchall()
            frontier.update(child["child_id"] for child in children if child["child_id"] not in records)
        return records, sources

    def _deletion_plan(self, db, auth, record_id):
        target = self._row(db, auth, record_id)
        records, sources = self._deletion_closure(db, auth, record_id)
        descendants = db.execute("""WITH RECURSIVE children(id) AS (
            SELECT child_id FROM derivations WHERE profile_id=? AND parent_id=?
            UNION SELECT d.child_id FROM derivations d JOIN children c ON d.parent_id=c.id WHERE d.profile_id=?)
            SELECT id FROM children""", (auth.profile_id, record_id, auth.profile_id)).fetchall()
        ordinary = {record_id, *(row["id"] for row in descendants)}
        targets = tuple((item, self._row(db, auth, item)["revision"]) for item in sorted(records))
        data = {"record_id": record_id, "expected_revision": target["revision"], "targets": targets,
                "source_ids": tuple(sorted(sources)), "deletion_epoch": self._profile(db, auth)["deletion_epoch"],
                "requires_expanded_approval": bool(records - ordinary)}
        return DeletionPlan(**data, digest=self._store.digest(data))

    def preview_forget(self, auth: Authority, record_id: str) -> DeletionPlan:
        require(auth, Capability.FORGET)
        if auth.actor != "owner":
            raise PermissionDenied("Deletion preview requires the owner")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            return self._deletion_plan(db, auth, record_id)

    def forget_memory(self, auth: Authority, record_id: str, *, expected_revision: int,
                      command_id: str, approved_plan_digest: str | None = None) -> DeletionReceipt:
        def action(db):
            row = self._row(db, auth, record_id)
            self._revision(row, expected_revision)
            plan = self._deletion_plan(db, auth, record_id)
            if approved_plan_digest is not None and not hmac.compare_digest(plan.digest, approved_plan_digest):
                raise RevisionConflict("Deletion impact changed after approval; preview again")
            if plan.requires_expanded_approval and approved_plan_digest is None:
                raise PolicyViolation("Shared-source deletion requires an explicitly approved preview")
            records = {item[0] for item in plan.targets}
            sources = set(plan.source_ids)
            db.execute("UPDATE profiles SET deletion_epoch=deletion_epoch+1 WHERE profile_id=?", (auth.profile_id,))
            epoch = self._profile(db, auth)["deletion_epoch"]
            versions, cancelled = 0, 0
            now = self.clock()
            for target in sorted(records):
                versions += db.execute("SELECT count(*) FROM memory_versions WHERE profile_id=? AND record_id=? AND content IS NOT NULL",
                                       (auth.profile_id, target)).fetchone()[0]
                db.execute("UPDATE memory_records SET state='deleted',head_version=NULL,revision=revision+1,category='',subject_id='',subject_kind='',assertion_mode='',sensitivity='',occurred_at=NULL,expires_at=NULL WHERE profile_id=? AND record_id=?",
                           (auth.profile_id, target))
                db.execute("UPDATE memory_versions SET content=NULL,purged_at=? WHERE profile_id=? AND record_id=?", (now, auth.profile_id, target))
                db.execute("DELETE FROM record_collections WHERE profile_id=? AND record_id=?", (auth.profile_id, target))
                self._store.index(db, auth.profile_id, target, None)
                self._audit(db, auth, command_id, "record_state", target, None, "deleted")
            for source_id in sorted(sources):
                db.execute("INSERT OR IGNORE INTO source_suppressions VALUES(?,?,?,?)", (auth.profile_id, source_id, epoch, command_id))
                cancelled += db.execute("SELECT count(*) FROM candidates WHERE profile_id=? AND source_id=? AND state!='cancelled'",
                                        (auth.profile_id, source_id)).fetchone()[0]
                db.execute("UPDATE candidates SET state='cancelled',draft=NULL,revision=revision+1 WHERE profile_id=? AND source_id=? AND state!='cancelled'",
                           (auth.profile_id, source_id))
                db.execute("UPDATE sources SET excerpt=NULL,locator=NULL,purged_at=? WHERE profile_id=? AND source_id=?", (now, auth.profile_id, source_id))
            purged_observations = db.execute("UPDATE interaction_inbox SET payload=NULL,status='suppressed' WHERE profile_id=? AND payload IS NOT NULL",
                                             (auth.profile_id,)).rowcount
            self._purge_formation(db, auth, records, sources)
            self._purge_reflection(db, auth, records, sources)
            receipt = DeletionReceipt(profile_id=auth.profile_id, command_id=command_id, operation="forget", record_id=record_id,
                                      revision=expected_revision + 1, deletion_epoch=epoch, affected_records=len(records),
                                      purged_versions=versions, purged_sources=len(sources), cancelled_candidates=cancelled,
                                      purged_observations=purged_observations)
            db.execute("INSERT INTO deletion_receipts VALUES(?,?,?,?)", (auth.profile_id, command_id, record_id, receipt.model_dump_json()))
            return receipt
        return self._execute(auth, command_id, "forget", {"record_id": record_id, "expected_revision": expected_revision,
                             "approved_plan_digest": approved_plan_digest}, Capability.FORGET, action)

    def get_deletion_receipt(self, auth: Authority, command_id: str) -> DeletionReceipt:
        require(auth, Capability.FORGET)
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            row = db.execute("SELECT record_id,result FROM deletion_receipts WHERE profile_id=? AND command_id=?",
                             (auth.profile_id, command_id)).fetchone()
            if row is None:
                raise NotFound("Deletion receipt is unavailable")
            self._row(db, auth, row["record_id"], allow_deleted=True)
            affected = db.execute("""SELECT r.scope_kind,r.scope_id FROM audit_entries a JOIN memory_records r
                ON a.profile_id=r.profile_id AND a.object_id=r.record_id
                WHERE a.profile_id=? AND a.command_id=? AND a.operation='record_state' AND a.to_state='deleted'""",
                                  (auth.profile_id, command_id)).fetchall()
            for target in affected:
                require_scope(auth, Scope(kind=target["scope_kind"], id=target["scope_id"]))
            return DeletionReceipt.model_validate_json(row["result"])

    def page_records(self, auth, *, after=None, query="", bottle_id=None, limit=100):
        """Owner keyset pagination; query and scope are bound to the cursor."""
        import hashlib
        require(auth, Capability.READ)
        if auth.actor != "owner" or not 1 <= limit <= 100 or len(query) > 200:
            raise PolicyViolation("Owner inspection requires a bounded page")
        binding = hashlib.sha256(repr((auth.profile_id, auth.scopes, query, bottle_id)).encode()).hexdigest()[:16]
        key = ""
        if after:
            prefix, separator, key = after.partition(":")
            if not separator or prefix != binding:
                raise PolicyViolation("Cursor does not belong to this query")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            clauses = ["r.profile_id=?", "r.owner_id=?", "r.state!='deleted'", "r.record_id>?",
                       "(r.expires_at IS NULL OR julianday(r.expires_at)>julianday(?))"]
            values = [auth.profile_id, auth.owner_id, key, self.clock()]
            clauses.append("(" + " OR ".join("(r.scope_kind=? AND r.scope_id=?)" for _ in auth.scopes) + ")" if auth.scopes else "0")
            for scope in auth.scopes:
                values.extend((scope.kind, scope.id))
            if bottle_id:
                self._bottle(db, auth, bottle_id)
                clauses.append("EXISTS(SELECT 1 FROM record_collections c WHERE c.profile_id=r.profile_id AND c.record_id=r.record_id AND c.collection_id=?)")
                values.append(bottle_id)
            if query.strip():
                clauses.append("EXISTS(SELECT 1 FROM memory_versions v WHERE v.profile_id=r.profile_id AND v.record_id=r.record_id AND v.version=r.head_version AND instr(lower(v.content),lower(?))>0)")
                values.append(query.strip())
            rows = db.execute("SELECT r.* FROM memory_records r WHERE " + " AND ".join(clauses) + " ORDER BY r.record_id LIMIT ?", (*values, limit+1)).fetchall()
            return {"items": [self._record(db, auth, row).model_dump(mode="json") for row in rows[:limit]],
                    "next_cursor": binding + ":" + rows[limit-1]["record_id"] if len(rows)>limit else None}

    def record_memberships(self, auth, record_id):
        require(auth, Capability.READ)
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            self._row(db, auth, record_id)
            rows = db.execute("SELECT b.* FROM bottles b JOIN record_collections c ON b.profile_id=c.profile_id AND b.bottle_id=c.collection_id WHERE c.profile_id=? AND c.record_id=? ORDER BY b.bottle_id", (auth.profile_id,record_id)).fetchall()
            return tuple(self._bottle_dto(row) for row in rows if Scope(kind=row["scope_kind"],id=row["scope_id"]) in auth.scopes)

    def read_events(self, auth, *, after=0, limit=100):
        """Authorized metadata only. Never returns source or forgotten content."""
        require(auth, Capability.READ)
        if auth.actor != "owner" or not 0 <= after or not 1 <= limit <= 256:
            raise PolicyViolation("Owner event query requires a bounded cursor")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            rows = db.execute("SELECT * FROM memory_outbox WHERE profile_id=? AND sequence>? ORDER BY sequence LIMIT ?", (auth.profile_id,after,limit)).fetchall()
            result = []
            for row in rows:
                object_id = row["object_id"]
                if object_id:
                    record = db.execute("SELECT scope_kind,scope_id FROM memory_records WHERE profile_id=? AND record_id=?", (auth.profile_id,object_id)).fetchone()
                    bottle = db.execute("SELECT scope_kind,scope_id FROM bottles WHERE profile_id=? AND bottle_id=?", (auth.profile_id,object_id)).fetchone()
                    scoped = record or bottle
                    if scoped and Scope(kind=scoped["scope_kind"],id=scoped["scope_id"]) not in auth.scopes:
                        continue
                    candidate = db.execute("SELECT draft FROM candidates WHERE profile_id=? AND candidate_id=?", (auth.profile_id,object_id)).fetchone()
                    if candidate and candidate[0] and MemoryDraft.model_validate_json(candidate[0]).scope not in auth.scopes:
                        continue
                    if not scoped and not candidate:
                        continue
                result.append(dict(row))
            return {"items":result,"next_cursor":rows[-1]["sequence"] if rows else after}
