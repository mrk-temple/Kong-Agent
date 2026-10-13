"""Bounded, durable rule-based formation; no Agent Loop, model or tools.

Capture permits temporary processing, not automatic Identity or record changes.
Owner approval binds consolidation, the version, scope and chosen Bottle.
"""
from datetime import datetime, timedelta, timezone
import hmac
import re
import time
from uuid import uuid4

from pydantic import Field

from kong.memory.domain import (
    Capability, DTO, Episode, InteractionObservation, MemoryDraft, Scope, Source,
)
from kong.memory.errors import ForgottenSource, NotFound, PermissionDenied, PolicyViolation, RevisionConflict
from kong.memory.policy import require, require_scope, validate_draft


class FormationBudget(DTO):
    max_jobs: int = Field(default=10, ge=1, le=20)
    max_seconds: float = Field(default=2, gt=0, le=30)


class FormationItem(DTO):
    candidate_id: str
    revision: int
    candidate_type: str
    logical_key: str
    importance: float
    draft: MemoryDraft
    episode: Episode | None = None
    source: Source


class ConsolidationPlan(DTO):
    candidate_id: str
    candidate_revision: int
    operation: str
    target_id: str | None
    target_revision: int | None
    bottle_id: str | None
    deletion_epoch: int
    previous_content: str | None
    proposed_content: str
    proposed_episode: Episode | None
    bottle_revision: int | None
    digest: str
    item: FormationItem


def extract_event(event, scope):
    """Conservative, inspectable MVP classifier; ambiguous narration is skipped."""
    text = event.text or ""
    if event.role != "user" or re.search(r"小说|故事|假设|如果|角色扮演|他[说问]|她[说问]|引用|我猜|也许|可能|fiction|hypothetical|[“”\"]|[?？]", text, re.I):
        return ()
    if re.match(r"\s*(?:如何|怎样|是否|哪里|什么|how\b|what\b|where\b|could you\b|can you\b)", text, re.I):
        return ()
    # Narrow, evidenced mixed-source guard. This is not a general privacy classifier.
    if re.search(r"(?:朋友|同事|邻居|亲戚|friend|colleague).{0,80}(?:患有|病史|诊断|抑郁|diagnos|medical)", text, re.I):
        return ()
    project = re.search(r"(?:项目\s*|project\s+)([A-Za-z][A-Za-z0-9_-]{1,60})", text, re.I)
    project_id = project.group(1) if project else None
    episode = None
    if re.search(r"人格|性格|personality", text, re.I):
        category, key = "identity_change_proposal", "identity.style"
    elif re.search(r"不要主动提|敏感话题|相处约定|扮演导师|as a mentor", text, re.I):
        category, key = "relationship_preference_proposal", "relationship.boundary"
    elif re.search(r"一起|共同|together", text, re.I) and re.search(r"完成|决定|解决|纠正|验证|completed|resolved|decided", text, re.I):
        category = "project_milestone" if project_id else "episodic_experience"
        label = re.search(r"(?:经历\s*|event\s+)([A-Za-z][A-Za-z0-9_-]{1,60})", text, re.I)
        key = "episode:" + (project_id or "owner") + ":" + (label.group(1) if label else event.event_id)
        date = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        try:
            when = datetime.fromisoformat(date.group(1)).replace(tzinfo=timezone.utc) if date else None
        except ValueError:
            when = None
        participants = ("owner", "kong") if re.search(r"和\s*Kong|Kong\s*和|和你|with you", text, re.I) else ("owner",)
        clauses = tuple(c.strip()[:1200] for c in re.split(r"[，。；,;！？!?]", text) if c.strip())
        expressions = tuple(c for c in clauses if re.search(r"我觉得|我感到|我认为|我希望|I (?:felt|feel|think|hope)", c, re.I))
        outcomes = tuple(c for c in clauses if re.match(r"结果|最终|因此|后来|outcome|result", c, re.I))
        what = next((c for c in clauses if re.search(r"一起|共同|together", c, re.I)), text[:1200])
        episode = Episode(event_key=key, what=what, when=when, participants=participants,
            project_id=project_id, user_expression=expressions[:16], outcomes=outcomes[:16],
            importance_reason="Explicit joint decision, resolution or milestone; requires owner review.",
            evidence_source_ids=(event.event_id,))
    elif re.search(r"偏好|喜欢|更希望|prefer", text, re.I):
        if re.search(r"只限今天|仅限今天|只限这次|仅限这次|只在今天|today only|for (?:today|now) only", text, re.I):
            return ()  # An explicitly temporary request is not a stable preference.
        category, key = "stable_preference", "preference.communication" if re.search(r"简洁|回答|交流|concise|response", text, re.I) else "preference.general"
    elif re.search(r"我住|我是|截止|事实|I live|deadline|fact", text, re.I):
        category, key = "semantic_fact", "fact:" + (project_id or "owner")
    else:
        return ()
    if "proposal" not in category:
        remember_requested = bool(re.search(r"记住|记下来|remember that", text, re.I))
        if not remember_requested and re.match(r"\s*(?:请|帮我|需要你|希望你|想让你|please\b|help me\b|let'?s\b)", text, re.I):
            return ()  # A task request is not evidence of its eventual completion.
        if episode:
            clause = re.split(r"[，。；,;]", text, maxsplit=1)[0]
            if re.search(r"将要|打算|将共同|将一起|计划共同|计划一起|明天|未来|下周|下星期|下个?月|明年|我希望|我期待|准备共同|准备一起|\bwill\b|\bplan to\b|\bwant to\b|\bhope to\b|\bnext (?:week|month|year)\b", clause, re.I):
                return ()  # A future goal is not an observed shared experience.
    draft = MemoryDraft(kind="episodic" if episode else "semantic", category=category, content=text[:1200],
                        scope=scope, subject_kind="project" if project_id else "owner", subject_id=project_id or "owner",
                        occurred_at=episode.when if episode else None)
    validate_draft(draft)
    return ((draft, episode, key, .85 if episode else .7),)


class FormationOperations:
    def queue_observations(self, auth, *, scope=Scope(), bottle_id=None, limit=10):
        """Explicit idle/session-end host batch; never backfills capture-off history."""
        require(auth, Capability.FORMATION)
        require_scope(auth, scope)
        if not 1 <= limit <= 20:
            raise ValueError("Observation scan is bounded to 1..20 events")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            if bottle_id:
                bottle = self._bottle(db, auth, bottle_id)
                if bottle["kind"] != "memory" or (bottle["scope_kind"], bottle["scope_id"]) != (scope.kind, scope.id):
                    raise PolicyViolation("Formation Bottle must match the selected scope")
            rows = db.execute("""SELECT i.event_id FROM interaction_inbox i WHERE i.profile_id=? AND i.capture='on'
                AND i.status='observed' AND i.payload IS NOT NULL AND i.expires_at>?
                AND NOT EXISTS(SELECT 1 FROM formation_jobs j WHERE j.profile_id=i.profile_id AND j.event_id=i.event_id AND j.scope_kind=? AND j.scope_id=?)
                ORDER BY i.created_at,i.event_id LIMIT ?""", (auth.profile_id, self.clock(), scope.kind, scope.id, limit)).fetchall()
        queued, skipped = [], 0
        for row in rows:
            try:
                queued.append(self.queue_formation(auth, row[0], scope=scope, bottle_id=bottle_id))
            except (ForgottenSource, PermissionDenied):
                skipped += 1  # A changed original grant cannot authorize automatic backfill.
        return {"job_ids": queued, "skipped": skipped}

    def queue_formation(self, auth, event_id, *, scope=Scope(), bottle_id=None):
        require(auth, Capability.FORMATION)
        require_scope(auth, scope)
        with self._store.transaction() as db:
            access = self._access(db, auth)
            row = db.execute("SELECT * FROM interaction_inbox WHERE profile_id=? AND event_id=?", (auth.profile_id, event_id)).fetchone()
            if row is None or row["capture"] != "on" or row["status"] != "observed" or not row["payload"] or row["expires_at"] <= self.clock():
                raise PolicyViolation("Only retained, authorized capture-on observations can form memories")
            event = InteractionObservation.model_validate_json(row["payload"])
            original = event.original_access
            if original is None or (original.store_id, original.owner_id, original.profile_id, original.deletion_epoch, original.activation_revision) != (
                    access.store_id, auth.owner_id, auth.profile_id, access.deletion_epoch, access.activation_revision):
                raise ForgottenSource("Observation authorization changed")
            if scope not in original.scopes or not all(s in auth.scopes for s in original.scopes):
                raise PermissionDenied("Formation cannot widen original capture scopes")
            if bottle_id:
                bottle = self._bottle(db, auth, bottle_id)
                if bottle["kind"] != "memory" or (bottle["scope_kind"], bottle["scope_id"]) != (scope.kind, scope.id):
                    raise PolicyViolation("Formation Bottle must match its source scope")
            old = db.execute("SELECT * FROM formation_jobs WHERE profile_id=? AND event_id=? AND scope_kind=? AND scope_id=?",
                             (auth.profile_id, event_id, scope.kind, scope.id)).fetchone()
            if old:
                if old["bottle_id"] != bottle_id:
                    raise PolicyViolation("A queued event cannot silently change its selected Bottle")
                return old["job_id"]
            job_id = uuid4().hex
            db.execute("INSERT INTO formation_jobs VALUES(?,?,?,?,?,?,?,?,'queued',0,?,NULL,NULL,NULL)",
                       (auth.profile_id, job_id, event_id, scope.kind, scope.id, bottle_id, access.deletion_epoch, access.activation_revision, self.clock()))
            self._audit(db, auth, job_id, "formation_queued", job_id)
            return job_id

    def _extract_event(self, event, scope):
        return extract_event(event, scope)

    def run_formation(self, auth, *, budget=FormationBudget()):
        require(auth, Capability.FORMATION)
        require(auth, Capability.PROPOSE)
        self.expire_formation_candidates(auth)
        with self._store.transaction() as db:
            self._profile(db, auth)
            db.execute("UPDATE formation_jobs SET status='failed',lease_token=NULL,lease_until=NULL,error_type='LeaseExpired' WHERE profile_id=? AND status='running' AND attempts>=3 AND lease_until<=?", (auth.profile_id, self.clock()))
        started, results = time.monotonic(), []
        for _ in range(budget.max_jobs):
            if time.monotonic() - started >= budget.max_seconds:
                break
            with self._store.transaction() as db:
                access = self._access(db, auth)
                now = self.clock()
                clauses = " OR ".join("(scope_kind=? AND scope_id=?)" for s in auth.scopes) or "0"
                params = [value for s in auth.scopes for value in (s.kind, s.id)]
                job = db.execute("SELECT * FROM formation_jobs WHERE profile_id=? AND (" + clauses + ") AND attempts<3 AND ((status IN ('queued','retry') AND available_at<=?) OR (status='running' AND lease_until<=?)) ORDER BY available_at,job_id LIMIT 1",
                                 (auth.profile_id, *params, now, now)).fetchone()
                if job is None:
                    break
                job = dict(job)
                token = uuid4().hex
                lease = (datetime.fromisoformat(now) + timedelta(seconds=45)).isoformat()
                db.execute("UPDATE formation_jobs SET status='running',attempts=attempts+1,lease_token=?,lease_until=? WHERE profile_id=? AND job_id=?", (token, lease, auth.profile_id, job["job_id"]))
                row = db.execute("SELECT * FROM interaction_inbox WHERE profile_id=? AND event_id=?", (auth.profile_id, job["event_id"])).fetchone()
                event = InteractionObservation.model_validate_json(row["payload"]) if row and row["payload"] and row["expires_at"] > now else None
            try:
                if event is None or job["deletion_epoch"] != access.deletion_epoch or job["activation_revision"] != access.activation_revision:
                    raise ForgottenSource("Formation input expired or was revoked")
                values = self._extract_event(event, Scope(kind=job["scope_kind"], id=job["scope_id"]))
                with self._store.transaction() as db:
                    current = self._access(db, auth)
                    locked = db.execute("SELECT status,lease_token FROM formation_jobs WHERE profile_id=? AND job_id=?", (auth.profile_id, job["job_id"])).fetchone()
                    retained = db.execute("SELECT payload,status,expires_at FROM interaction_inbox WHERE profile_id=? AND event_id=?", (auth.profile_id, job["event_id"])).fetchone()
                    if locked["status"] != "running" or locked["lease_token"] != token or current.deletion_epoch != job["deletion_epoch"] or current.activation_revision != job["activation_revision"] or not retained["payload"] or retained["expires_at"] <= self.clock():
                        raise ForgottenSource("Formation result cannot commit after revocation")
                    if len(values) > 8:
                        raise PolicyViolation("Formation output exceeds per-event budget")
                    source = Source(source_id=event.event_id, origin="interaction", speaker=event.role, excerpt=event.text)
                    for draft, episode, key, importance in values:
                        require_scope(auth, draft.scope)
                        validate_draft(draft)
                        if draft.content not in event.text or event.role != "user":
                            raise PolicyViolation("Formation must be grounded in the user observation")
                        self._source(db, auth, source)
                        candidate_id = uuid4().hex
                        db.execute("INSERT INTO candidates VALUES(?,?,1,'pending',?,?,NULL)", (auth.profile_id, candidate_id, draft.model_dump_json(), source.source_id))
                        expires = (datetime.fromisoformat(self.clock()) + timedelta(days=7)).isoformat()
                        db.execute("INSERT INTO formation_candidates VALUES(?,?,?,?,?,?,?,?)", (auth.profile_id, candidate_id, job["job_id"], draft.category, key, importance, episode.model_dump_json() if episode else None, expires))
                    db.execute("UPDATE formation_jobs SET status='done',lease_token=NULL,lease_until=NULL,error_type=NULL WHERE profile_id=? AND job_id=?", (auth.profile_id, job["job_id"]))
                    self._audit(db, auth, job["job_id"], "formation_done", job["job_id"])
                    db.execute("INSERT INTO memory_outbox(profile_id,operation,object_id,memory_revision,deletion_epoch,created_at) VALUES(?,?,?,?,?,?)", (auth.profile_id, "formation_candidates", job["job_id"], current.memory_revision, current.deletion_epoch, self.clock()))
                    results.append({"job_id": job["job_id"], "status": "done", "candidates": len(values)})
            except Exception as exc:
                with self._store.transaction() as db:
                    locked = db.execute("SELECT * FROM formation_jobs WHERE profile_id=? AND job_id=?", (auth.profile_id, job["job_id"])).fetchone()
                    if locked["lease_token"] == token:
                        status = "cancelled" if isinstance(exc, ForgottenSource) else "failed" if isinstance(exc, (PolicyViolation, PermissionDenied)) or locked["attempts"] >= 3 else "retry"
                        later = (datetime.fromisoformat(self.clock()) + timedelta(seconds=2 ** locked["attempts"])).isoformat()
                        db.execute("UPDATE formation_jobs SET status=?,available_at=?,lease_token=NULL,lease_until=NULL,error_type=? WHERE profile_id=? AND job_id=?", (status, later, type(exc).__name__, auth.profile_id, job["job_id"]))
                    else:
                        status = locked["status"]
                    results.append({"job_id": job["job_id"], "status": status, "candidates": 0})
        return {"jobs": results, "model_calls": 0, "elapsed_seconds": time.monotonic() - started}

    def list_formation_candidates(self, auth, *, limit=100):
        require(auth, Capability.READ)
        if auth.actor != "owner" or not 1 <= limit <= 100:
            raise PermissionDenied("Candidate review is owner-only and bounded")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            clauses = " OR ".join("(json_extract(c.draft,'$.scope.kind')=? AND json_extract(c.draft,'$.scope.id')=?)" for s in auth.scopes) or "0"
            params = [v for s in auth.scopes for v in (s.kind, s.id)]
            rows = db.execute("SELECT f.* FROM formation_candidates f JOIN candidates c USING(profile_id,candidate_id) WHERE f.profile_id=? AND c.state='pending' AND f.expires_at>? AND (" + clauses + ") ORDER BY f.candidate_id LIMIT ?", (auth.profile_id, self.clock(), *params, limit)).fetchall()
            items = []
            for meta in rows:
                row, draft, source = self._candidate(db, auth, meta["candidate_id"])
                items.append(FormationItem(candidate_id=row["candidate_id"], revision=row["revision"], candidate_type=meta["candidate_type"], logical_key=meta["logical_key"], importance=meta["importance"], draft=draft, source=source, episode=Episode.model_validate_json(meta["episode"]) if meta["episode"] else None))
            return tuple(items)

    def _formation_plan(self, db, auth, candidate_id):
        row, draft, source = self._candidate(db, auth, candidate_id)
        meta = db.execute("SELECT * FROM formation_candidates WHERE profile_id=? AND candidate_id=?", (auth.profile_id, candidate_id)).fetchone()
        if meta is None or meta["expires_at"] <= self.clock() or row["state"] != "pending":
            raise NotFound("Formation candidate expired or was already reviewed")
        job = db.execute("SELECT * FROM formation_jobs WHERE profile_id=? AND job_id=?", (auth.profile_id, meta["job_id"])).fetchone()
        access = self._access(db, auth)
        if job["deletion_epoch"] != access.deletion_epoch:
            raise ForgottenSource("Candidate access changed; capture again or provide a new explicit source")
        item = FormationItem(candidate_id=candidate_id, revision=row["revision"], candidate_type=meta["candidate_type"], logical_key=meta["logical_key"], importance=meta["importance"], draft=draft, source=source, episode=Episode.model_validate_json(meta["episode"]) if meta["episode"] else None)
        matches = db.execute("""SELECT r.* FROM formation_traits t JOIN memory_records r USING(profile_id,record_id)
            WHERE t.profile_id=? AND t.logical_key=? AND r.state='active' AND r.scope_kind=? AND r.scope_id=?
            AND r.subject_id=? AND r.subject_kind=? AND r.sensitivity=? AND r.assertion_mode=? AND r.kind=? ORDER BY r.record_id LIMIT 101""", (auth.profile_id, meta["logical_key"], draft.scope.kind, draft.scope.id, draft.subject_id, draft.subject_kind, draft.sensitivity, draft.assertion_mode, draft.kind)).fetchall()
        matches = [r for r in matches if self._usable(r)]
        if not matches and "proposal" not in meta["candidate_type"]:
            # Upgrade existing approved records without inventing semantic equivalence.
            matches = db.execute("""SELECT r.* FROM memory_records r JOIN memory_versions v
                ON v.profile_id=r.profile_id AND v.record_id=r.record_id AND v.version=r.head_version
                WHERE r.profile_id=? AND r.state='active' AND r.scope_kind=? AND r.scope_id=?
                AND r.subject_id=? AND r.subject_kind=? AND r.sensitivity=? AND r.assertion_mode=? AND r.kind=?
                AND v.content=? ORDER BY r.record_id LIMIT 101""", (auth.profile_id, draft.scope.kind, draft.scope.id,
                draft.subject_id, draft.subject_kind, draft.sensitivity, draft.assertion_mode, draft.kind, draft.content)).fetchall()
            matches = [r for r in matches if self._usable(r)]
        if len(matches) > 1:
            raise PolicyViolation("Multiple matching records require explicit owner correction before consolidation")
        target = matches[0] if matches else None
        operation = "create"
        previous, proposed_episode = None, item.episode
        if "proposal" in meta["candidate_type"]:
            operation = "proposal"
        elif target:
            old = self._version(db, auth, target["record_id"], target["head_version"])
            previous = old.content
            operation = "duplicate" if old.content.strip() == draft.content.strip() else "extend_episode" if item.episode and old.episode and old.episode.event_key == item.episode.event_key else "conflict"
            if operation == "extend_episode":
                values = old.episode.model_dump()
                for field in ("user_expression", "outcomes", "evidence_source_ids", "related_records"):
                    values[field] = tuple(dict.fromkeys((*getattr(old.episode, field), *getattr(item.episode, field))))
                proposed_episode = Episode.model_validate(values)
            elif operation == "duplicate":
                proposed_episode = old.episode
        proposed_content = previous if operation == "duplicate" else previous + "\n" + draft.content if operation == "extend_episode" else draft.content
        if len(proposed_content) > 6000:
            raise PolicyViolation("Consolidated content exceeds its budget")
        bottle = self._bottle(db, auth, job["bottle_id"]) if job["bottle_id"] else None
        data = dict(candidate_id=candidate_id, candidate_revision=row["revision"], operation=operation,
            target_id=target["record_id"] if target else None, target_revision=target["revision"] if target else None,
            bottle_id=job["bottle_id"], deletion_epoch=access.deletion_epoch, item=item,
            previous_content=previous, proposed_content=proposed_content, proposed_episode=proposed_episode,
            bottle_revision=bottle["revision"] if bottle else None)
        return ConsolidationPlan(**data, digest=self._store.digest({**data, "item": item.model_dump(mode="json"), "proposed_episode": proposed_episode.model_dump(mode="json") if proposed_episode else None}))

    def preview_consolidation(self, auth, candidate_id):
        require(auth, Capability.READ)
        if auth.actor != "owner":
            raise PermissionDenied("Only the owner may review a consolidation change set")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            return self._formation_plan(db, auth, candidate_id)

    def consolidate_candidate(self, auth, candidate_id, *, approved_digest, command_id, resolve_conflict=False):
        def action(db):
            plan = self._formation_plan(db, auth, candidate_id)
            if not hmac.compare_digest(plan.digest, approved_digest):
                raise RevisionConflict("Consolidation changed after review")
            if plan.operation == "proposal" or (plan.operation == "conflict" and not resolve_conflict):
                raise PolicyViolation("Domain proposals or conflicts require an explicit review decision")
            item, draft = plan.item, plan.item.draft
            validate_draft(draft)
            approval = Source(source_id=auth.input_id, origin="user_command", speaker="user", excerpt=draft.content, locator="formation-approval:" + candidate_id)
            if plan.target_id is None:
                result = self._new_record(db, auth, draft, (item.source, approval))
                db.execute("INSERT INTO formation_traits VALUES(?,?,?)", (auth.profile_id, result["record_id"], item.logical_key))
                episode = plan.proposed_episode
            else:
                target = self._row(db, auth, plan.target_id)
                self._revision(target, plan.target_revision)
                old = self._version(db, auth, plan.target_id, target["head_version"])
                content = plan.proposed_content
                if len(content) > 6000:
                    raise PolicyViolation("Consolidated content exceeds record budget")
                version = old.version + 1
                sources = {s.source_id: s for s in old.sources} if plan.operation in {"duplicate", "extend_episode"} else {}
                sources.update({item.source.source_id: item.source, approval.source_id: approval})
                db.execute("INSERT INTO memory_versions VALUES(?,?,?,?,?,?,?,NULL)", (auth.profile_id, plan.target_id, version, content, "user_authorized", self.clock(), old.version))
                for src in sources.values():
                    self._source(db, auth, src)
                    db.execute("INSERT INTO memory_sources VALUES(?,?,?,?)", (auth.profile_id, plan.target_id, version, src.source_id))
                expiry = draft.expires_at.isoformat() if draft.expires_at else target["expires_at"]
                if target["expires_at"] and expiry and datetime.fromisoformat(expiry) > datetime.fromisoformat(target["expires_at"]):
                    expiry = target["expires_at"]  # Consolidation cannot silently relax validity.
                db.execute("UPDATE memory_records SET head_version=?,revision=revision+1,expires_at=? WHERE profile_id=? AND record_id=?", (version, expiry, auth.profile_id, plan.target_id))
                self._store.index(db, auth.profile_id, plan.target_id, content)
                self._quarantine_descendants(db, auth, plan.target_id, command_id)
                result = {"record_id": plan.target_id, "revision": target["revision"] + 1, "version": version}
                episode = plan.proposed_episode
                db.execute("INSERT OR IGNORE INTO formation_traits VALUES(?,?,?)", (auth.profile_id, plan.target_id, item.logical_key))
            if episode:
                episode = episode.model_copy(update={"confirmed_by": auth.input_id})
                db.execute("INSERT INTO episodic_details VALUES(?,?,?,?)", (auth.profile_id, result["record_id"], result["version"], episode.model_dump_json()))
            if plan.bottle_id:
                bottle = self._bottle(db, auth, plan.bottle_id)
                if (bottle["scope_kind"], bottle["scope_id"]) != (draft.scope.kind, draft.scope.id):
                    raise PolicyViolation("Bottle scope changed")
                db.execute("INSERT OR IGNORE INTO record_collections VALUES(?,?,?)", (auth.profile_id, result["record_id"], plan.bottle_id))
                db.execute("UPDATE profiles SET activation_revision=activation_revision+1 WHERE profile_id=?", (auth.profile_id,))
            db.execute("UPDATE candidates SET state='consolidated',record_id=?,revision=revision+1 WHERE profile_id=? AND candidate_id=?", (result["record_id"], auth.profile_id, candidate_id))
            return {**result, "candidate_id": candidate_id}
        return self._execute(auth, command_id, "consolidate", {"candidate_id": candidate_id, "approved_digest": approved_digest, "resolve_conflict": resolve_conflict}, Capability.CONFIRM, action)

    def pin_memory(self, auth, record_id, bottle_id, *, command_id, pinned=True):
        if not isinstance(pinned, bool):
            raise ValueError("pinned must be a boolean")
        def action(db):
            record = self._row(db, auth, record_id)
            self._bottle(db, auth, bottle_id)
            if not db.execute("SELECT 1 FROM record_collections WHERE profile_id=? AND record_id=? AND collection_id=?", (auth.profile_id, record_id, bottle_id)).fetchone():
                raise PolicyViolation("Only an existing Bottle member can be cherished")
            if pinned:
                db.execute("INSERT OR REPLACE INTO memory_pins VALUES(?,?,?,?)", (auth.profile_id, record_id, bottle_id, auth.input_id))
            else:
                db.execute("DELETE FROM memory_pins WHERE profile_id=? AND record_id=? AND bottle_id=?", (auth.profile_id, record_id, bottle_id))
            return {"record_id": record_id, "bottle_id": bottle_id, "revision": record["revision"]}
        return self._execute(auth, command_id, "pin_memory", {"record_id": record_id, "bottle_id": bottle_id, "pinned": pinned}, Capability.BOTTLE, action)

    def list_pinned_memories(self, auth, bottle_id):
        records = self.list_bottle_memories(auth, bottle_id)
        with self._store.transaction(write=False) as db:
            ids = {row[0] for row in db.execute("SELECT record_id FROM memory_pins WHERE profile_id=? AND bottle_id=?", (auth.profile_id, bottle_id))}
            return tuple(record for record in records if record.record_id in ids)

    def expire_formation_candidates(self, auth):
        require(auth, Capability.FORMATION)
        with self._store.transaction() as db:
            self._profile(db, auth)
            clauses = " OR ".join("(json_extract(c.draft,'$.scope.kind')=? AND json_extract(c.draft,'$.scope.id')=?)" for s in auth.scopes) or "0"
            params = [v for s in auth.scopes for v in (s.kind, s.id)]
            rows = db.execute("SELECT c.* FROM candidates c JOIN formation_candidates f USING(profile_id,candidate_id) WHERE c.profile_id=? AND c.state IN ('pending','rejected') AND f.expires_at<=? AND (" + clauses + ")", (auth.profile_id, self.clock(), *params)).fetchall()
            for row in rows:
                draft = MemoryDraft.model_validate_json(row["draft"]) if row["draft"] else None
                if draft is not None:
                    require_scope(auth, draft.scope)
                db.execute("UPDATE candidates SET state='cancelled',draft=NULL,revision=revision+1 WHERE profile_id=? AND candidate_id=?", (auth.profile_id, row["candidate_id"]))
                db.execute("UPDATE formation_candidates SET episode=NULL,logical_key='' WHERE profile_id=? AND candidate_id=?", (auth.profile_id, row["candidate_id"]))
                if not db.execute("SELECT 1 FROM memory_sources WHERE profile_id=? AND source_id=?", (auth.profile_id, row["source_id"])).fetchone():
                    db.execute("UPDATE sources SET excerpt=NULL,locator=NULL,purged_at=? WHERE profile_id=? AND source_id=?", (self.clock(), auth.profile_id, row["source_id"]))
            db.execute("UPDATE interaction_inbox SET payload=NULL,status='expired' WHERE profile_id=? AND payload IS NOT NULL AND expires_at<=?", (auth.profile_id, self.clock()))
            return len(rows)

    def _purge_formation(self, db, auth, records, sources):
        db.execute("UPDATE formation_jobs SET status='cancelled',lease_token=NULL,lease_until=NULL,error_type=NULL WHERE profile_id=? AND status!='done'", (auth.profile_id,))
        for record_id in records:
            db.execute("UPDATE episodic_details SET payload=NULL WHERE profile_id=? AND record_id=?", (auth.profile_id, record_id))
            db.execute("DELETE FROM formation_traits WHERE profile_id=? AND record_id=?", (auth.profile_id, record_id))
        for source_id in sources:
            db.execute("UPDATE formation_candidates SET episode=NULL,logical_key='' WHERE profile_id=? AND candidate_id IN (SELECT candidate_id FROM candidates WHERE profile_id=? AND source_id=?)", (auth.profile_id, auth.profile_id, source_id))
