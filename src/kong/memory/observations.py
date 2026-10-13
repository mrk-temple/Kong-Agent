"""Durable observation transport, not automatic long-term memory extraction."""
from datetime import datetime, timedelta
import json

from kong.memory.domain import Capability, MemoryDraft, ObservationReceipt
from kong.memory.errors import IdempotencyConflict, PermissionDenied, PolicyViolation
from kong.memory.policy import require, validate_draft


class ObservationOperations:
    def observe_interaction(self, auth, event):
        require(auth, Capability.OBSERVE)
        digest = self._store.digest(event.model_dump(mode="json"))
        with self._store.transaction() as db:
            access = self._access(db, auth)
            now = datetime.fromisoformat(self.clock())
            db.execute("UPDATE interaction_inbox SET payload=NULL,status='expired' WHERE profile_id=? AND payload IS NOT NULL AND julianday(expires_at)<=julianday(?)",
                       (auth.profile_id, now.isoformat()))
            old = db.execute("SELECT * FROM interaction_inbox WHERE profile_id=? AND event_id=?", (auth.profile_id, event.event_id)).fetchone()
            if old is not None:
                if old["digest"] != digest:
                    raise IdempotencyConflict("Observation cannot change its original capture policy or content")
                return ObservationReceipt(event_id=event.event_id, capture=old["capture"], status=old["status"], replayed=True)
            status, payload = "skipped", None
            if event.capture == "on" and event.original_access == access and event.text:
                try:
                    validate_draft(MemoryDraft(kind="semantic", content=event.text))
                except PolicyViolation:
                    pass  # Keep only non-content receipt, never extra secrets in audit.
                else:
                    status = "observed"
                    payload = event.model_dump_json()
            if db.execute("SELECT count(*) FROM interaction_inbox WHERE profile_id=? AND payload IS NOT NULL", (auth.profile_id,)).fetchone()[0] >= 1000:
                status, payload = "skipped", None
            db.execute("INSERT INTO interaction_inbox VALUES(?,?,?,?,?,?,?,?)", (auth.profile_id, event.event_id, digest, event.capture,
                       status, payload, now.isoformat(), (now + timedelta(hours=48)).isoformat()))
            # Transport does not bump Memory/Activation revision: it grants no
            # active record and must not invalidate every conversation by itself.
            return ObservationReceipt(event_id=event.event_id, status=status, capture=event.capture)

    def list_observations(self, auth, *, limit=100):
        require(auth, Capability.READ)
        if auth.actor != "owner" or not 1 <= limit <= 1000:
            raise PermissionDenied("Observation inspection requires an owner and a bounded limit")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            rows = db.execute("SELECT * FROM interaction_inbox WHERE profile_id=? ORDER BY created_at,event_id LIMIT ?", (auth.profile_id, limit)).fetchall()
            now = datetime.fromisoformat(self.clock())
            result = []
            for row in rows:
                payload = row["payload"] if datetime.fromisoformat(row["expires_at"]) > now else None
                if payload:
                    observed_scopes = json.loads(payload)["original_access"]["scopes"]
                    if not all(scope in [s.model_dump(mode="json") for s in auth.scopes] for scope in observed_scopes):
                        payload = None
                result.append({"event_id": row["event_id"], "capture": row["capture"], "status": row["status"], "payload": payload})
            return tuple(result)
