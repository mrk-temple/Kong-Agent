"""Versioned, selected Memory Bottle exchange. Imported text grants no authority."""
import hashlib
import hmac
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_serializer, model_validator

from kong.memory.domain import Capability, DTO, Episode, MemoryDraft, Scope, Source, canonical
from kong.memory.errors import PolicyViolation, RevisionConflict
from kong.memory.policy import require, require_scope, validate_draft


MAX_PACKAGE_BYTES = 4 * 1024 * 1024


class PackageMetadata(DTO):
    origin_store: str = Field(min_length=1, max_length=64)
    origin_profile: str = Field(min_length=1, max_length=200)
    bottle_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    bottle_type: Literal["memory"] = "memory"
    created_at: str = Field(min_length=1, max_length=64)


class ExportProvenance(DTO):
    source_id: str = Field(min_length=1, max_length=200)
    origin: Literal["user_command", "interaction", "artifact", "import", "model"]
    speaker: Literal["user", "assistant", "tool", "external"]


class ExchangeRecord(DTO):
    record_id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    draft: MemoryDraft
    provenance: tuple[ExportProvenance, ...] = Field(min_length=1, max_length=64)
    episode: Episode | None = None
    pinned: bool = False

    @model_serializer(mode="wrap")
    def compatible_payload(self, handler):
        data = handler(self)
        if self.episode is None:
            data.pop("episode", None)
        if not self.pinned:
            data.pop("pinned", None)
        return data  # Legacy v1 checksums do not gain new empty/default fields.


class ExchangePrivacy(DTO):
    scope: Literal["profile"] = "profile"
    content_untrusted: Literal[True] = True
    activate_on_import: Literal[False] = False
    includes_chat_history: Literal[False] = False


class BottlePayload(DTO):
    format: Literal["kong.memory-bottle"] = "kong.memory-bottle"
    schema_version: Literal[1, 2] = 1
    package_id: str = Field(min_length=1, max_length=200)
    metadata: PackageMetadata
    records: tuple[ExchangeRecord, ...] = Field(max_length=500)
    # These fields are reserved rather than silently applying an imported Persona
    # or claiming that Relationship Evolution is implemented.
    identity_references: tuple[()] = ()
    relationship_preferences: tuple[()] = ()
    bottle_membership: tuple[str, ...] = Field(max_length=500)
    privacy: ExchangePrivacy = ExchangePrivacy()


class BottlePackage(BottlePayload):
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_envelope(self):
        ids = tuple(r.record_id for r in self.records)
        if len(set(ids)) != len(ids) or self.bottle_membership != ids:
            raise ValueError("Package membership must name each exported record exactly once")
        if any(r.draft.scope != Scope() for r in self.records):
            raise ValueError("Portable MVP supports Profile-scoped records only")
        if self.schema_version == 1 and any(r.episode is not None or r.pinned for r in self.records):
            raise ValueError("Structured episodes and pinned selections require Bottle schema v2")
        digest = hashlib.sha256(canonical(self.model_dump(mode="json", exclude={"checksum"})).encode()).hexdigest()
        if not hmac.compare_digest(digest, self.checksum):
            raise ValueError("Package checksum mismatch; it is integrity, not a signature or trust grant")
        return self

    @classmethod
    def create(cls, **values):
        # Populate defaults through Pydantic before computing the canonical digest.
        values = BottlePayload.model_validate(values).model_dump(mode="json")
        values["checksum"] = hashlib.sha256(canonical(values).encode()).hexdigest()
        return cls.model_validate(values)


def parse_package(text: str | bytes) -> BottlePackage:
    if len(text.encode("utf-8") if isinstance(text, str) else text) > MAX_PACKAGE_BYTES:
        raise ValueError("Memory Bottle exceeds the 4 MiB limit")
    return BottlePackage.model_validate_json(text)


class ImportItem(DTO):
    origin_record: str
    disposition: Literal["new", "duplicate", "conflict", "forgotten", "blocked"]
    local_record: str | None = None
    draft: MemoryDraft
    reason: str = ""
    episode: Episode | None = None
    pinned: bool = False


class ImportPreview(DTO):
    profile_id: str
    package_checksum: str
    selected_records: tuple[str, ...]
    items: tuple[ImportItem, ...]
    approved_digest: str
    default_active: Literal[False] = False
    identity_changes: Literal[False] = False


class ExchangeOperations:
    @staticmethod
    def _same_episode(left, right):
        if left is None or right is None:
            return left is right
        return left.model_dump(exclude={"confirmed_by"}) == right.model_dump(exclude={"confirmed_by"})

    @staticmethod
    def _draft_of(record):
        fields = ("kind", "category", "subject_id", "subject_kind", "scope", "assertion_mode", "sensitivity", "occurred_at", "expires_at")
        return MemoryDraft(content=record.current.content, **{k: getattr(record, k) for k in fields})

    def export_bottle(self, auth, bottle_id, *, selected_records=None):
        require(auth, Capability.EXCHANGE, explicit=True)
        require(auth, Capability.READ)
        with self._store.transaction(write=False) as db:
            access = self._access(db, auth)
            bottle = self._bottle(db, auth, bottle_id)
            if bottle["kind"] != "memory" or (bottle["scope_kind"], bottle["scope_id"]) != ("profile", ""):
                raise PolicyViolation("Only Profile-scoped Memory Bottles are portable in this MVP")
            selection_sql, params = "", [auth.profile_id, bottle_id]
            if selected_records is not None:
                selected_records = tuple(sorted(set(selected_records)))
                if len(selected_records) > 500:
                    raise PolicyViolation("Select at most 500 records")
                selection_sql = " AND r.record_id IN (" + ",".join("?" for _ in selected_records) + ")" if selected_records else " AND 0"
                params.extend(selected_records)
            rows = db.execute("""SELECT r.* FROM memory_records r JOIN record_collections c
                ON c.profile_id=r.profile_id AND c.record_id=r.record_id
                WHERE r.profile_id=? AND c.collection_id=? AND r.state='active'""" + selection_sql + " ORDER BY r.record_id LIMIT 501", params).fetchall()
            available = {row["record_id"]: row for row in rows}
            ids = tuple(available) if selected_records is None else tuple(sorted(set(selected_records)))
            if len(ids) > 500 or not set(ids).issubset(available):
                raise PolicyViolation("Select at most 500 active records belonging to this Bottle")
            records = []
            for record_id in ids:
                record = self._record(db, auth, self._row(db, auth, record_id))
                records.append(ExchangeRecord(record_id=record_id, version=record.current.version, draft=self._draft_of(record),
                    provenance=tuple(ExportProvenance(source_id=s.source_id, origin=s.origin, speaker=s.speaker) for s in record.current.sources),
                    episode=record.current.episode, pinned=bool(db.execute("SELECT 1 FROM memory_pins WHERE profile_id=? AND record_id=? AND bottle_id=?", (auth.profile_id, record_id, bottle_id)).fetchone())))
            package = BottlePackage.create(package_id=uuid4().hex,
                schema_version=2 if any(r.episode is not None or r.pinned for r in records) else 1,
                metadata=PackageMetadata(origin_store=access.store_id, origin_profile=auth.profile_id,
                    bottle_id=bottle_id, title=bottle["title"], created_at=self.clock()),
                records=tuple(records), bottle_membership=ids)
            if len(package.model_dump_json().encode()) > MAX_PACKAGE_BYTES:
                raise PolicyViolation("Selected export exceeds the 4 MiB limit")
        # Detect a revocation that committed while the export snapshot was read.
        if self.get_access_state(auth) != access:
            raise RevisionConflict("Memory authorization changed during export")
        return package

    def _preview_import(self, db, auth, package, selected_records):
        access = self._access(db, auth)
        require_scope(auth, Scope())
        records = {r.record_id: r for r in package.records}
        ids = tuple(sorted(records)) if selected_records is None else tuple(sorted(set(selected_records)))
        if not ids or not set(ids).issubset(records):
            raise PolicyViolation("Select nonempty record IDs from the previewed package")
        origin = canonical([package.metadata.origin_store, package.metadata.origin_profile])
        items = []
        for record_id in ids:
            entry = records[record_id]
            try:
                validate_draft(entry.draft)
                if entry.episode:
                    validate_draft(entry.draft.model_copy(update={"content": entry.episode.model_dump_json()}))
            except PolicyViolation:
                items.append(ImportItem(origin_record=record_id, disposition="blocked", draft=entry.draft, reason="Content policy rejected this record"))
                continue
            digest = self._store.digest(entry.model_dump(mode="json"))
            old = db.execute("SELECT * FROM import_origins WHERE profile_id=? AND origin_profile=? AND origin_record=?",
                             (auth.profile_id, origin, record_id)).fetchone()
            local_id = old["local_record"] if old else None
            # An export brought back to its own Profile must also respect the
            # original record's tombstone, even before the first import.
            if old is None and (package.metadata.origin_store, package.metadata.origin_profile) == (access.store_id, auth.profile_id):
                local_id = record_id
            row = db.execute("SELECT * FROM memory_records WHERE profile_id=? AND record_id=?", (auth.profile_id, local_id)).fetchone() if local_id else None
            if row is not None:
                if row["state"] == "deleted":
                    disposition, reason = "forgotten", "Known origin was forgotten; old packages cannot restore it"
                elif row["state"] != "active" or (old is not None and old["digest"] != digest):
                    disposition, reason = "conflict", "Known origin changed or is inactive; resolve explicitly outside import"
                elif self._draft_of(self._record(db, auth, self._row(db, auth, local_id))) != entry.draft or not self._same_episode(self._record(db, auth, row).current.episode, entry.episode):
                    disposition, reason = "conflict", "Local content differs; import never overwrites a local record"
                else:
                    disposition, reason = "duplicate", "Same approved content and classification"
            else:
                # Exact structured equality only. Never merge using model guesses
                # or silently widen a private classification during deduplication.
                matches = db.execute("""SELECT r.* FROM memory_records r JOIN memory_versions v
                    ON v.profile_id=r.profile_id AND v.record_id=r.record_id AND v.version=r.head_version
                    WHERE r.profile_id=? AND r.state='active' AND r.scope_kind='profile' AND r.scope_id='' AND v.content=?""",
                                     (auth.profile_id, entry.draft.content)).fetchall()
                local_id = next((r["record_id"] for r in matches if self._draft_of(self._record(db, auth, r)) == entry.draft and self._same_episode(self._record(db, auth, r).current.episode, entry.episode)), None)
                disposition, reason = ("duplicate", "Exact local content and classification") if local_id else ("new", "New imported evidence")
            items.append(ImportItem(origin_record=record_id, disposition=disposition, local_record=local_id, draft=entry.draft, reason=reason, episode=entry.episode, pinned=entry.pinned))
        payload = {"access": access.model_dump(mode="json"), "checksum": package.checksum, "selected_records": ids,
                   "items": [i.model_dump(mode="json") for i in items]}
        return ImportPreview(profile_id=auth.profile_id, package_checksum=package.checksum, selected_records=ids,
                             items=tuple(items), approved_digest=self._store.digest(payload))

    def preview_import(self, auth, package: BottlePackage, *, selected_records=None):
        require(auth, Capability.EXCHANGE)
        require(auth, Capability.READ)
        if auth.actor != "owner":
            raise PolicyViolation("Import preview requires the owner")
        package = parse_package(package.model_dump_json())
        with self._store.transaction(write=False) as db:
            return self._preview_import(db, auth, package, selected_records)

    def import_bottle(self, auth, package: BottlePackage, *, approved_digest, command_id, selected_records=None, bottle_id=None):
        require(auth, Capability.READ)
        package = parse_package(package.model_dump_json())
        ids = tuple(sorted(r.record_id for r in package.records)) if selected_records is None else tuple(sorted(set(selected_records)))
        if bottle_id is not None and (not bottle_id.strip() or len(bottle_id) > 200):
            raise ValueError("Invalid destination Bottle ID")
        def action(db):
            preview = self._preview_import(db, auth, package, ids)
            if not hmac.compare_digest(preview.approved_digest, approved_digest):
                raise RevisionConflict("Import preview is stale or does not match the approved selection")
            if any(i.disposition not in {"new", "duplicate"} for i in preview.items):
                raise PolicyViolation("Resolve or deselect blocked, conflicting and forgotten records before import")
            destination = bottle_id or uuid4().hex
            if db.execute("SELECT 1 FROM bottles WHERE profile_id=? AND bottle_id=?", (auth.profile_id, destination)).fetchone():
                raise PolicyViolation("Import creates a new inactive Bottle; destination already exists")
            db.execute("INSERT INTO bottles VALUES(?,?,?,'memory','profile','',0,1)", (auth.profile_id, destination, package.metadata.title))
            origin = canonical([package.metadata.origin_store, package.metadata.origin_profile])
            entries = {r.record_id: r for r in package.records}
            for item in preview.items:
                entry = entries[item.origin_record]
                digest = self._store.digest(entry.model_dump(mode="json"))
                local_id = item.local_record
                if item.disposition == "new":
                    source = Source(source_id="import:" + hashlib.sha256(canonical([origin, item.origin_record, digest]).encode()).hexdigest(),
                        origin="import", speaker="external", excerpt=entry.draft.content,
                        locator="bottle-import:" + hashlib.sha256(canonical([origin, item.origin_record]).encode()).hexdigest())
                    local_id = self._new_record(db, auth, entry.draft, (source,), evidence="imported")["record_id"]
                    if entry.episode:
                        # Foreign approval is data, never local authority; provenance remains external.
                        episode = entry.episode.model_copy(update={"confirmed_by": None})
                        db.execute("INSERT INTO episodic_details VALUES(?,?,1,?)", (auth.profile_id, local_id, episode.model_dump_json()))
                db.execute("INSERT OR IGNORE INTO import_origins VALUES(?,?,?,?,?)", (auth.profile_id, origin, item.origin_record, digest, local_id))
                db.execute("INSERT INTO record_collections VALUES(?,?,?)", (auth.profile_id, local_id, destination))
                if entry.pinned:
                    db.execute("INSERT INTO memory_pins VALUES(?,?,?,?)", (auth.profile_id, local_id, destination, auth.input_id))
            return {"bottle_id": destination, "revision": 1}
        return self._execute(auth, command_id, "import_bottle", {"checksum": package.checksum, "selected_records": ids,
                             "bottle_id": bottle_id, "approved_digest": approved_digest}, Capability.EXCHANGE, action)
