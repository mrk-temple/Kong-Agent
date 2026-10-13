"""Host-owned, content-addressed skill validation. Models cannot mutate this ledger."""
import os
import hashlib
import json
from functools import wraps
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from kong.contracts import Contract
from kong.storage import RunStore, atomic_replace


def serialized_mutation(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        run_id = hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()[:32]
        with RunStore(self.path.parent / "skill-locks").lock(run_id):
            return method(self, *args, **kwargs)
    return locked


class ValidationRecord(Contract):
    task_id: str = Field(min_length=1, max_length=160)
    contract_version: str = Field(min_length=1, max_length=160)
    passed: StrictBool
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_trial: str = Field(min_length=1, max_length=300)


class SkillRevision(Contract):
    skill_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=80)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_hashes: dict[str, str]
    applies_to: list[str] = Field(min_length=1, max_length=20)
    excludes: list[str] = Field(default_factory=list, max_length=20)
    required_tasks: list[str] = Field(min_length=1, max_length=100)
    contract_version: str = Field(min_length=1, max_length=160)
    source_trials: list[str] = Field(min_length=1, max_length=100)
    status: Literal["candidate", "validated", "active", "disabled", "retired"] = "candidate"
    validations: list[ValidationRecord] = Field(default_factory=list, max_length=1000)
    decisions: list[dict] = Field(default_factory=list, max_length=1000)

    @field_validator("resource_hashes")
    @classmethod
    def hashes(cls, value):
        import re
        if "SKILL.md" not in value or any(not re.fullmatch(r"[0-9a-f]{64}", h) for h in value.values()):
            raise ValueError("All declared resources require SHA-256 identities")
        return value

    @field_validator("applies_to", "excludes", "required_tasks", "source_trials")
    @classmethod
    def identifiers(cls, values):
        if len(values) != len(set(values)) or any(not v.strip() or len(v) > 300 for v in values):
            raise ValueError("Expected unique, nonempty bounded identifiers")
        return values

    @model_validator(mode="after")
    def content_identity(self):
        expected = hashlib.sha256(json.dumps({"version": self.version, "resources": self.resource_hashes},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if self.fingerprint != expected:
            raise ValueError("Skill fingerprint does not match version and resource hashes")
        return self

    def verified(self):
        latest = {r.task_id: r for r in self.validations}
        return all(task in latest and latest[task].passed for task in self.required_tasks)


class LifecycleFile(Contract):
    schema_version: Literal[1] = 1
    revisions: list[SkillRevision] = Field(default_factory=list, max_length=1000)


class SkillLifecycle:
    def __init__(self, path: Path):
        self.path = path

    def read(self):
        if not self.path.exists():
            return LifecycleFile()
        with self.path.open("rb") as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Skill lifecycle ledger exceeds its bound")
        return LifecycleFile.model_validate_json(raw)

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = data.model_dump_json(indent=2)
        if len(payload.encode("utf-8")) > 2_000_000:
            raise ValueError("Skill lifecycle ledger exceeds its bound")
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def get(self, skill_id, fingerprint):
        return next((r for r in self.read().revisions if r.skill_id == skill_id and r.fingerprint == fingerprint), None)

    @serialized_mutation
    def register(self, **kwargs):
        revision = SkillRevision.model_validate(kwargs)
        if revision.status != "candidate" or revision.validations or revision.decisions:
            raise ValueError("New revisions start as unvalidated candidates")
        data = self.read()
        prior = next((r for r in data.revisions if r.skill_id == revision.skill_id and r.fingerprint == revision.fingerprint), None)
        if prior:
            identity = {"status", "validations", "decisions"}
            if prior.model_dump(exclude=identity) != revision.model_dump(exclude=identity):
                raise ValueError("Registered revision metadata is immutable")
            return prior
        data.revisions.append(revision)
        self._save(data)
        return revision

    @serialized_mutation
    def record(self, skill_id, fingerprint, validation):
        data = self.read()
        revision = self._find(data, skill_id, fingerprint)
        record = ValidationRecord.model_validate(validation)
        if record.contract_version != revision.contract_version or record.task_id not in revision.required_tasks:
            raise ValueError("Validation must match the frozen task and contract version")
        if revision.status == "retired":
            raise ValueError("Retired revision cannot receive new validations")
        revision.validations.append(record)
        if revision.status in {"candidate", "validated"}:
            revision.status = "validated" if revision.verified() else "candidate"
        elif revision.status == "active" and not revision.verified():
            revision.status = "disabled"
            revision.decisions.append({"operation": "disable", "reason": "Latest validation failed"})
        self._save(LifecycleFile.model_validate(data.model_dump()))
        return revision

    @staticmethod
    def _find(data, skill_id, fingerprint):
        revision = next((r for r in data.revisions if r.skill_id == skill_id and r.fingerprint == fingerprint), None)
        if revision is None:
            raise ValueError("Unknown skill revision")
        return revision

    @serialized_mutation
    def select(self, skill_id, fingerprint, operation, *, reason):
        if operation not in {"activate", "disable", "retire"} or not reason.strip() or len(reason) > 1000:
            raise ValueError("Expected an explicit lifecycle operation and bounded reason")
        data = self.read()
        revision = self._find(data, skill_id, fingerprint)
        if revision.status == "retired":
            raise ValueError("Retired revision is immutable")
        if operation == "activate":
            if not revision.verified():
                raise ValueError("Activation requires passing current validation for every declared task")
            for old in data.revisions:
                if old.skill_id == skill_id and old.status == "active" and old.fingerprint != fingerprint:
                    old.status = "disabled"
                    old.decisions.append({"operation": "disable", "reason": "Host selected another validated version"})
        revision.status = {"activate": "active", "disable": "disabled", "retire": "retired"}[operation]
        revision.decisions.append({"operation": operation, "reason": reason})
        self._save(LifecycleFile.model_validate(data.model_dump()))
        return revision
