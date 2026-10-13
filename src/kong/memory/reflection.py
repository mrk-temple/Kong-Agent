"""Bounded Memory maintenance and explainable, owner-approved evolution.

No Runtime reflection, model loop, tool executor, sockets or MCP authority.
All analysis is deterministic; proposals never write a configuration head.
"""
from datetime import datetime, timedelta
import hmac
import re
import time
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from kong.memory.domain import Capability, DTO, Identity, ParentVersion, Relationship, Scope
from kong.memory.errors import ForgottenSource, NotFound, PermissionDenied, PolicyViolation, RevisionConflict
from kong.memory.policy import require, require_scope


class ReflectionBudget(DTO):
    max_records: int = Field(default=20, ge=1, le=50)
    max_proposals: int = Field(default=4, ge=0, le=4)
    max_seconds: float = Field(default=2, gt=0, le=30)


class EvolutionProposal(DTO):
    proposal_id: str
    profile_id: str
    target: Literal["identity", "relationship"]
    revision: int = Field(default=1, ge=1)
    state: Literal["pending", "approved", "rejected", "revoked", "cancelled", "expired"] = "pending"
    base_version: int = Field(ge=0)
    current: Identity | Relationship | None
    proposed: Identity | Relationship
    parents: tuple[ParentVersion, ...] = Field(default=(), max_length=32)
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    candidate_id: str | None = None
    reason: str = Field(min_length=1, max_length=600)
    behavior_impact: str = Field(min_length=1, max_length=600)
    scopes: tuple[Scope, ...]
    deletion_epoch: int
    activation_revision: int
    approved_digest: str
    created_at: str
    expires_at: str
    approved_by: str | None = None

    @model_validator(mode="after")
    def correct_domain(self):
        expected = Identity if self.target == "identity" else Relationship
        if not isinstance(self.proposed, expected) or self.current is not None and not isinstance(self.current, expected):
            raise ValueError("Evolution must stay within its declared state domain")
        return self


class ReflectionReport(DTO):
    run_id: str
    parents: tuple[ParentVersion, ...]
    scopes: tuple[Scope, ...]
    deletion_epoch: int
    activation_revision: int
    episode_groups: tuple[dict, ...]
    conflicts: tuple[dict, ...]
    proposal_ids: tuple[str, ...]
    model_calls: Literal[0] = 0
    elapsed_seconds: float
    created_at: str


class RollbackPlan(DTO):
    proposal_id: str
    target: Literal["identity", "relationship"]
    expected_revision: int
    current: Identity | Relationship
    restored: Identity | Relationship
    approved_digest: str


def _style(text):
    concise = bool(re.search(r"简洁|简短|concise|brief", text, re.I))
    detailed = bool(re.search(r"详细|展开|detailed", text, re.I))
    return "concise" if concise and not detailed else "detailed" if detailed and not concise else None


def analyze_reflection(context, *, max_proposals):
    """Read authorized records; link explicit projects, detect rather than resolve conflicts."""
    groups, values, preferences = {}, {}, []
    for record in context.records:
        if record.assertion_mode != "real" or record.sensitivity not in {"public", "personal"}:
            continue
        if record.current.evidence != "user_authorized":
            continue  # Imported/model/derived assertions cannot evolve configuration.
        if record.current.episode and record.current.episode.project_id:
            groups.setdefault(record.current.episode.project_id, []).append(record)
        if record.kind == "semantic" and record.subject_kind == "owner":
            style = _style(record.current.content)
            if style and re.search(r"偏好|喜欢|更希望|prefer", record.current.content, re.I):
                preferences.append(record)
                # Approval receipts do not manufacture repeated independent feedback.
                observed = {s.source_id for s in record.current.sources if s.origin == "interaction" and s.speaker == "user"}
                if not observed:
                    observed = {next((s.source_id for s in record.current.sources if s.origin == "user_command" and s.speaker == "user"), record.record_id)}
                values.setdefault(style, set()).update(observed)
    conflicts = ({"record_ids": [r.record_id for r in preferences], "reason": "Conflicting current communication preferences; owner correction required."},) if len(values) > 1 else ()
    proposals = []
    if len(values) == 1 and Scope() in context.access.scopes and max_proposals:
        style, events = next(iter(values.items()))
        if len(events) >= 2:
            relationship = context.relationship.relationship if context.relationship else Relationship()
            parents = tuple(ParentVersion(record_id=r.record_id, version=r.current.version) for r in preferences[:32])
            if relationship.communication_style != style:
                proposals.append(("relationship", relationship.model_copy(update={"communication_style": style}), parents,
                    "At least two independent authorized user feedback sources consistently prefer " + style + " communication.",
                    "Use " + style + " responses; role, proactivity and historical mention boundaries remain user controlled."))
            identity = context.identity.identity if context.identity else Identity()
            # A stable Identity change is always a separate suggestion; all other fields are copied unchanged.
            desired = "Prefer concise, calm responses." if style == "concise" else "Prefer detailed, calm responses."
            if identity.communication_style != desired:
                proposals.append(("identity", identity.model_copy(update={"communication_style": desired}), parents,
                    "Repeated authorized feedback suggests a communication-style adaptation; no automatic Identity change.",
                    "Adapt the stable expression style only after owner approval; personality, principles and role retain their approved values."))
    episode_groups = tuple({"project_id": key, "record_ids": [r.record_id for r in records],
        "reason": "Authorized shared project experiences; grouping does not assert causal or emotional relationships."}
        for key, records in sorted(groups.items()) if len(records) >= 2)
    return episode_groups, conflicts, tuple(proposals[:max_proposals])


class ReflectionOperations:
    def _identity_value(self, db, auth):
        profile = self._profile(db, auth)
        row = db.execute("SELECT content FROM identity_versions WHERE profile_id=? AND version=?", (auth.profile_id, profile["identity_version"])).fetchone()
        return Identity.model_validate_json(row["content"]) if row else None

    def _checked_records(self, db, auth, parents):
        records = []
        for parent in parents:
            row = self._row(db, auth, parent.record_id)
            if not self._usable(row) or row["head_version"] != parent.version:
                raise RevisionConflict("Reflection evidence is no longer an active current version")
            if not db.execute("SELECT 1 FROM record_collections c JOIN bottles b ON c.profile_id=b.profile_id AND c.collection_id=b.bottle_id WHERE c.profile_id=? AND c.record_id=? AND b.active=1 AND b.kind='memory'", (auth.profile_id, parent.record_id)).fetchone():
                raise RevisionConflict("Reflection evidence is outside the active Bottle view")
            record = self._record(db, auth, row)
            if record.current.evidence != "user_authorized" or record.assertion_mode != "real" or record.sensitivity not in {"public", "personal"}:
                raise PolicyViolation("Evolution requires real, authorized, non-sensitive local evidence")
            records.append(record)
        return tuple(records)

    def _insert_proposal(self, db, auth, target, value, parents, reason, impact, *, candidate_id=None):
        require_scope(auth, Scope())  # Profile evolution cannot widen project-only authority.
        profile = self._profile(db, auth)
        access = self._access(db, auth)
        records = self._checked_records(db, auth, parents)
        sources = {s.source_id for r in records for s in r.current.sources}
        if candidate_id:
            plan = self._formation_plan(db, auth, candidate_id)
            if plan.operation != "proposal":
                raise PolicyViolation("Only domain-change candidates can create an evolution proposal")
            job = db.execute("SELECT j.* FROM formation_jobs j JOIN formation_candidates f USING(profile_id,job_id) WHERE f.profile_id=? AND f.candidate_id=?", (auth.profile_id, candidate_id)).fetchone()
            if job["activation_revision"] != access.activation_revision:
                raise RevisionConflict("Candidate Bottle authorization changed")
            sources.add(plan.item.source.source_id)
        if not sources or len(sources) > 64:
            raise PolicyViolation("Evolution evidence exceeds its bounded source budget")
        if target == "identity":
            current, base = self._identity_value(db, auth), profile["identity_version"]
            value = Identity.model_validate(value.model_dump())
        else:
            old = self._relationship(db, auth)
            current, base = old.relationship if old else None, profile["relationship_version"]
            value = Relationship.model_validate(value.model_dump())
        if current == value:
            raise PolicyViolation("Evolution proposal has no change")
        data = dict(target=target, base_version=base, current=current.model_dump(mode="json") if current else None,
            proposed=value.model_dump(mode="json"), parents=[p.model_dump() for p in parents], source_ids=sorted(sources),
            candidate_id=candidate_id, reason=reason, behavior_impact=impact, scopes=[s.model_dump() for s in auth.scopes],
            deletion_epoch=access.deletion_epoch, activation_revision=access.activation_revision)
        digest = self._store.digest(data)
        existing = db.execute("SELECT proposal_id FROM evolution_proposals WHERE profile_id=? AND fingerprint=?", (auth.profile_id, digest)).fetchone()
        if existing:
            return existing["proposal_id"]  # A rejection is not repeatedly re-proposed for the same evidence.
        now = self.clock()
        proposal = EvolutionProposal(proposal_id=uuid4().hex, profile_id=auth.profile_id, approved_digest=digest,
            created_at=now, expires_at=(datetime.fromisoformat(now) + timedelta(days=7)).isoformat(), **data)
        db.execute("INSERT INTO evolution_proposals VALUES(?,?,?,'pending',1,?,?,?,?,?)", (auth.profile_id,
            proposal.proposal_id, target, base, digest, proposal.model_dump_json(), now, proposal.expires_at))
        for parent in parents:
            db.execute("INSERT INTO proposal_inputs VALUES(?,?,?,?)", (auth.profile_id, proposal.proposal_id, parent.record_id, parent.version))
        for source_id in sources:
            db.execute("INSERT INTO proposal_sources VALUES(?,?,?)", (auth.profile_id, proposal.proposal_id, source_id))
        if candidate_id:
            db.execute("INSERT INTO proposal_candidates VALUES(?,?,?)", (auth.profile_id, proposal.proposal_id, candidate_id))
        self._audit(db, auth, proposal.proposal_id, "evolution_proposed", proposal.proposal_id, None, "pending")
        return proposal.proposal_id

    def _read_proposal(self, db, auth, proposal_id):
        self._profile(db, auth)
        require_scope(auth, Scope())
        row = db.execute("SELECT * FROM evolution_proposals WHERE profile_id=? AND proposal_id=?", (auth.profile_id, proposal_id)).fetchone()
        if row is None or row["content"] is None:
            raise NotFound("Evolution proposal was purged or is unavailable")
        proposal = EvolutionProposal.model_validate_json(row["content"])
        fields = {"target", "base_version", "current", "proposed", "parents", "source_ids", "candidate_id", "reason", "behavior_impact", "scopes", "deletion_epoch", "activation_revision"}
        digest = self._store.digest(proposal.model_dump(mode="json", include=fields))
        if (proposal.profile_id, proposal.proposal_id, proposal.target, proposal.base_version) != (auth.profile_id, proposal_id, row["target"], row["base_version"]) or not hmac.compare_digest(digest, row["fingerprint"]) or not hmac.compare_digest(digest, proposal.approved_digest):
            raise PolicyViolation("Evolution content no longer matches its review binding")
        return proposal.model_copy(update={"revision": row["revision"], "state": row["state"]})

    def _check_proposal(self, db, auth, proposal, *, pending=False):
        if pending and proposal.state != "pending":
            raise RevisionConflict("Only pending evolution can be approved")
        if proposal.state == "pending" and proposal.expires_at <= self.clock():
            raise NotFound("Evolution proposal expired")
        access = self._access(db, auth)
        if proposal.deletion_epoch != access.deletion_epoch or proposal.activation_revision != access.activation_revision:
            raise RevisionConflict("Evolution authorization changed; create a fresh proposal")
        if any(s not in auth.scopes for s in proposal.scopes):
            raise PermissionDenied("Evolution review requires its original scopes")
        self._checked_records(db, auth, proposal.parents)
        if proposal.candidate_id and proposal.state == "pending":
            self._formation_plan(db, auth, proposal.candidate_id)
        for source_id in proposal.source_ids:
            row = db.execute("SELECT purged_at FROM sources WHERE profile_id=? AND source_id=?", (auth.profile_id, source_id)).fetchone()
            if row is None or row["purged_at"]:
                raise ForgottenSource("Evolution provenance was purged")
        if proposal.state == "pending" and self._profile(db, auth)[proposal.target + "_version"] != proposal.base_version:
            raise RevisionConflict("Evolution base version changed")

    def get_proposal(self, auth, proposal_id):
        require(auth, Capability.READ)
        if auth.actor != "owner":
            raise PermissionDenied("Evolution review requires the owner")
        with self._store.transaction(write=False) as db:
            proposal = self._read_proposal(db, auth, proposal_id)
            self._check_proposal(db, auth, proposal)
            return proposal

    def list_proposals(self, auth, *, state="pending", limit=50):
        require(auth, Capability.READ)
        if auth.actor != "owner" or state not in {"pending", "approved", "rejected", "revoked"} or not 1 <= limit <= 100:
            raise PermissionDenied("Evolution review is owner-only and bounded")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            rows = db.execute("SELECT proposal_id FROM evolution_proposals WHERE profile_id=? AND state=? AND content IS NOT NULL ORDER BY created_at,proposal_id LIMIT ?", (auth.profile_id, state, limit)).fetchall()
            proposals = []
            for row in rows:
                try:
                    proposal = self._read_proposal(db, auth, row[0])
                    self._check_proposal(db, auth, proposal)
                    proposals.append(proposal)
                except (PermissionDenied, RevisionConflict, NotFound, ForgottenSource):
                    continue
            return tuple(proposals)

    def proposal_from_candidate(self, auth, candidate_id, *, command_id):
        require(auth, Capability.READ)
        def action(db):
            plan = self._formation_plan(db, auth, candidate_id)
            text = plan.item.draft.content
            if plan.item.candidate_type == "identity_change_proposal":
                style = _style(text)
                if style is None:
                    raise PolicyViolation("Identity suggestion needs a specific supported style; ambiguous personality changes are not inferred")
                current = self._identity_value(db, auth) or Identity()
                proposed = current.model_copy(update={"communication_style": "Prefer concise, calm responses." if style == "concise" else "Prefer detailed, calm responses."})
                target = "identity"
            elif plan.item.candidate_type == "relationship_preference_proposal":
                old = self._relationship(db, auth)
                current = old.relationship if old else Relationship()
                patch = {}
                if re.search(r"不要主动提", text):
                    patch["history_mentions"] = "on_request"
                if re.search(r"导师|mentor", text, re.I):
                    patch["role"] = "mentor"
                if re.search(r"敏感话题", text):
                    topics = tuple(topic for pattern, topic in (("健康|health", "health"), ("财务|financ", "finances"), ("家庭|family", "family"), ("亲密|intimacy", "intimacy")) if re.search(pattern, text, re.I))
                    if topics:
                        patch["sensitive_topics"] = tuple(dict.fromkeys((*current.sensitive_topics, *topics)))
                if not patch:
                    raise PolicyViolation("Relationship candidate needs an explicit supported boundary")
                proposed, target = current.model_copy(update=patch), "relationship"
            else:
                raise PolicyViolation("Ordinary Memory candidates cannot become configuration changes")
            proposal_id = self._insert_proposal(db, auth, target, proposed, (),
                "An authorized user observation contains a specific configuration preference; explicit approval is still required.",
                "Apply only the displayed configuration difference; no new tool permissions or Bottle activation.", candidate_id=candidate_id)
            return {"proposal_id": proposal_id, "status": "pending"}
        return self._execute(auth, command_id, "proposal_from_candidate", {"candidate_id": candidate_id}, Capability.PROPOSE, action)

    def _write_evolution(self, db, auth, target, proposed, base, proposal_id):
        if target == "relationship":
            return self._write_relationship(db, auth, proposed, base, proposal_id)
        require_scope(auth, Scope())
        if self._profile(db, auth)["identity_version"] != base:
            raise RevisionConflict("Identity version changed before approval")
        number = base + 1
        db.execute("INSERT INTO identity_versions VALUES(?,?,?,?,?)", (auth.profile_id, number, proposed.model_dump_json(), auth.input_id, self.clock()))
        db.execute("UPDATE profiles SET identity_version=? WHERE profile_id=?", (number, auth.profile_id))
        return {"revision": number, "version": number, "proposal_id": proposal_id}

    def approve_proposal(self, auth, proposal_id, *, expected_revision, approved_digest, command_id):
        require(auth, Capability.READ)
        # Resolve the target without a mutation; authority is checked again by _execute.
        with self._store.transaction(write=False) as db:
            target = self._read_proposal(db, auth, proposal_id).target
        def action(db):
            proposal = self._read_proposal(db, auth, proposal_id)
            if proposal.revision != expected_revision or not hmac.compare_digest(proposal.approved_digest, approved_digest):
                raise RevisionConflict("Approval does not match the reviewed evolution")
            self._check_proposal(db, auth, proposal, pending=True)
            result = self._write_evolution(db, auth, proposal.target, proposal.proposed, proposal.base_version, proposal_id)
            approved = proposal.model_copy(update={"approved_by": auth.input_id})
            db.execute("UPDATE evolution_proposals SET state='approved',revision=revision+1,content=? WHERE profile_id=? AND proposal_id=?", (approved.model_dump_json(), auth.profile_id, proposal_id))
            if proposal.candidate_id:
                db.execute("UPDATE candidates SET state='consolidated',revision=revision+1 WHERE profile_id=? AND candidate_id=?", (auth.profile_id, proposal.candidate_id))
            return result
        return self._execute(auth, command_id, "approve_evolution", {"proposal_id": proposal_id, "expected_revision": expected_revision,
            "approved_digest": approved_digest}, Capability.IDENTITY if target == "identity" else Capability.RELATIONSHIP, action)

    def reject_proposal(self, auth, proposal_id, *, expected_revision, command_id):
        def action(db):
            proposal = self._read_proposal(db, auth, proposal_id)
            if proposal.state != "pending" or proposal.revision != expected_revision:
                raise RevisionConflict("Only the current pending proposal can be rejected")
            if any(s not in auth.scopes for s in proposal.scopes):
                raise PermissionDenied("Rejection requires original scopes")
            db.execute("UPDATE evolution_proposals SET state='rejected',revision=revision+1 WHERE profile_id=? AND proposal_id=?", (auth.profile_id, proposal_id))
            return {"proposal_id": proposal_id, "revision": proposal.revision + 1}
        return self._execute(auth, command_id, "reject_evolution", {"proposal_id": proposal_id, "expected_revision": expected_revision}, Capability.PROPOSE, action)

    def _rollback_plan(self, db, auth, proposal_id):
        proposal = self._read_proposal(db, auth, proposal_id)
        if proposal.state != "approved":
            raise RevisionConflict("Only an approved evolution can be rolled back")
        profile = self._profile(db, auth)
        current = self._identity_value(db, auth) if proposal.target == "identity" else self._relationship(db, auth).relationship
        number = profile[proposal.target + "_version"]
        if number != proposal.base_version + 1 or current != proposal.proposed:
            raise RevisionConflict("Later configuration changes must be reviewed independently before rollback")
        restored = proposal.current or (Identity() if proposal.target == "identity" else Relationship())
        data = dict(proposal_id=proposal_id, target=proposal.target, expected_revision=number, current=current, restored=restored)
        return RollbackPlan(**data, approved_digest=self._store.digest({**data, "current": current.model_dump(mode="json"), "restored": restored.model_dump(mode="json")}))

    def preview_rollback_proposal(self, auth, proposal_id):
        require(auth, Capability.READ, explicit=True)
        with self._store.transaction(write=False) as db:
            return self._rollback_plan(db, auth, proposal_id)

    def rollback_proposal(self, auth, proposal_id, *, approved_digest, command_id):
        require(auth, Capability.READ, explicit=True)
        with self._store.transaction(write=False) as db:
            target = self._read_proposal(db, auth, proposal_id).target
        def action(db):
            plan = self._rollback_plan(db, auth, proposal_id)
            if not hmac.compare_digest(plan.approved_digest, approved_digest):
                raise RevisionConflict("Rollback changed after review")
            result = self._write_evolution(db, auth, plan.target, plan.restored, plan.expected_revision, proposal_id)
            db.execute("UPDATE evolution_proposals SET state='revoked',revision=revision+1 WHERE profile_id=? AND proposal_id=?", (auth.profile_id, proposal_id))
            return result
        return self._execute(auth, command_id, "rollback_evolution", {"proposal_id": proposal_id, "approved_digest": approved_digest},
            Capability.IDENTITY if target == "identity" else Capability.RELATIONSHIP, action)

    def _analyze_reflection(self, context, budget):
        return analyze_reflection(context, max_proposals=budget.max_proposals)

    def run_reflection(self, auth, *, command_id, budget=ReflectionBudget()):
        require(auth, Capability.REFLECTION)
        require(auth, Capability.PROPOSE)
        require(auth, Capability.READ)
        self.expire_reflection(auth)
        started = time.monotonic()
        context = self.retrieve_context(auth, "", limit=budget.max_records)
        records = tuple(r for r in context.records if r.current.evidence == "user_authorized" and r.assertion_mode == "real" and r.sensitivity in {"public", "personal"})
        context = context.model_copy(update={"records": records})
        groups, conflicts, proposals = self._analyze_reflection(context, budget)
        parents = tuple(ParentVersion(record_id=r.record_id, version=r.current.version) for r in records)
        def action(db):
            current = self._access(db, auth)
            if current != context.access:
                raise RevisionConflict("Reflection inputs changed during analysis; retry with a fresh snapshot")
            if time.monotonic() - started > budget.max_seconds:
                raise PolicyViolation("Reflection exceeded its time budget; no changes committed")
            self._checked_records(db, auth, parents)
            ids = tuple(self._insert_proposal(db, auth, target, value, refs, reason, impact) for target, value, refs, reason, impact in proposals)
            run_id = uuid4().hex
            report = ReflectionReport(run_id=run_id, parents=parents, scopes=auth.scopes, deletion_epoch=current.deletion_epoch,
                activation_revision=current.activation_revision, episode_groups=groups, conflicts=conflicts, proposal_ids=ids,
                elapsed_seconds=time.monotonic() - started, created_at=self.clock())
            expires = (datetime.fromisoformat(self.clock()) + timedelta(days=7)).isoformat()
            db.execute("INSERT INTO reflection_runs VALUES(?,?,?,?,?)", (auth.profile_id, run_id, report.model_dump_json(), self.clock(), expires))
            for parent in parents:
                db.execute("INSERT INTO reflection_inputs VALUES(?,?,?,?)", (auth.profile_id, run_id, parent.record_id, parent.version))
            by_id = {p.record_id: p.version for p in parents}
            for group in groups:
                first, *others = group["record_ids"]
                for other in others:
                    left, right = sorted((first, other))
                    db.execute("INSERT OR IGNORE INTO episode_links VALUES(?,?,?,?,?,'shared_project',?)", (auth.profile_id, left, by_id[left], right, by_id[right], run_id))
            if time.monotonic() - started > budget.max_seconds:
                raise PolicyViolation("Reflection exceeded its commit budget; no changes committed")
            return {"reflection_id": run_id}
        return self._execute(auth, command_id, "memory_reflection", {"budget": budget.model_dump()}, Capability.REFLECTION, action, explicit=False)

    def get_reflection_report(self, auth, run_id):
        require(auth, Capability.READ)
        if auth.actor != "owner":
            raise PermissionDenied("Reflection inspection requires the owner")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            row = db.execute("SELECT * FROM reflection_runs WHERE profile_id=? AND run_id=?", (auth.profile_id, run_id)).fetchone()
            if row is None or not row["payload"] or row["expires_at"] <= self.clock():
                raise NotFound("Reflection report was purged or expired")
            report = ReflectionReport.model_validate_json(row["payload"])
            current = self._access(db, auth)
            if report.deletion_epoch != current.deletion_epoch or report.activation_revision != current.activation_revision or any(s not in auth.scopes for s in report.scopes):
                raise RevisionConflict("Reflection report authorization changed")
            self._checked_records(db, auth, report.parents)
            return report

    def expire_reflection(self, auth):
        require(auth, Capability.REFLECTION)
        with self._store.transaction() as db:
            self._profile(db, auth)
            expired = db.execute("UPDATE evolution_proposals SET content=NULL,state='expired',revision=revision+1 WHERE profile_id=? AND state IN ('pending','rejected') AND expires_at<=?", (auth.profile_id, self.clock())).rowcount
            db.execute("UPDATE reflection_runs SET payload=NULL WHERE profile_id=? AND expires_at<=?", (auth.profile_id, self.clock()))
            db.execute("UPDATE interaction_inbox SET payload=NULL,status='expired' WHERE profile_id=? AND payload IS NOT NULL AND expires_at<=?", (auth.profile_id, self.clock()))
            # Approved rules retain their own versions, not an indefinite transcript.
            clauses = " OR ".join("(j.scope_kind=? AND j.scope_id=?)" for s in auth.scopes) or "0"
            params = [v for s in auth.scopes for v in (s.kind, s.id)]
            rows = db.execute("SELECT c.* FROM candidates c JOIN formation_candidates f USING(profile_id,candidate_id) JOIN formation_jobs j USING(profile_id,job_id) WHERE c.profile_id=? AND c.state='consolidated' AND c.record_id IS NULL AND c.draft IS NOT NULL AND f.expires_at<=? AND (" + clauses + ")", (auth.profile_id, self.clock(), *params)).fetchall()
            for row in rows:
                db.execute("UPDATE candidates SET draft=NULL WHERE profile_id=? AND candidate_id=?", (auth.profile_id, row["candidate_id"]))
                db.execute("UPDATE formation_candidates SET episode=NULL,logical_key='' WHERE profile_id=? AND candidate_id=?", (auth.profile_id, row["candidate_id"]))
                if not db.execute("SELECT 1 FROM memory_sources WHERE profile_id=? AND source_id=?", (auth.profile_id, row["source_id"])).fetchone() and not db.execute("SELECT 1 FROM candidates WHERE profile_id=? AND source_id=? AND state='pending' AND draft IS NOT NULL", (auth.profile_id, row["source_id"])).fetchone():
                    db.execute("UPDATE sources SET excerpt=NULL,locator=NULL,purged_at=? WHERE profile_id=? AND source_id=?", (self.clock(), auth.profile_id, row["source_id"]))
            return expired

    def _purge_reflection(self, db, auth, records, sources):
        # Conservative profile-wide report/pending cancellation, like inbox revocation.
        db.execute("UPDATE reflection_runs SET payload=NULL WHERE profile_id=?", (auth.profile_id,))
        db.execute("UPDATE evolution_proposals SET content=NULL,state='cancelled',revision=revision+1 WHERE profile_id=? AND state='pending'", (auth.profile_id,))
        for source_id in sources:
            db.execute("UPDATE evolution_proposals SET content=NULL WHERE profile_id=? AND proposal_id IN (SELECT proposal_id FROM proposal_sources WHERE profile_id=? AND source_id=?)", (auth.profile_id, auth.profile_id, source_id))
        for record_id in records:
            db.execute("DELETE FROM episode_links WHERE profile_id=? AND (left_id=? OR right_id=?)", (auth.profile_id, record_id, record_id))
