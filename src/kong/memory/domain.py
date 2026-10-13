"""Memory-owned immutable DTOs; no execution or model contracts."""
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scope(DTO):
    kind: Literal["profile", "workspace", "project", "thread"] = "profile"
    id: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def valid_scope(self):
        if (self.kind == "profile") != (self.id == ""):
            raise ValueError("Profile scope has no ID; other scopes require an ID")
        return self


class Capability(StrEnum):
    PROFILE = "profile.manage"
    IDENTITY = "identity.write"
    READ = "memory.read"
    PROPOSE = "memory.propose"
    CONFIRM = "memory.confirm"
    WRITE = "memory.write"
    FORGET = "memory.forget"
    DERIVE = "memory.derive"
    BOTTLE = "bottle.manage"
    OBSERVE = "memory.observe"
    EXCHANGE = "bottle.exchange"
    FORMATION = "memory.formation"
    RELATIONSHIP = "relationship.write"
    REFLECTION = "memory.reflection"


class Authority(DTO):
    """Trusted host input, never accepted from model/import JSON as authority.

    Authentication belongs to the embedding application. A capability grants
    only the exact scopes listed here; profile scope is not a wildcard.
    """
    owner_id: str = Field(min_length=1, max_length=200)
    profile_id: str = Field(min_length=1, max_length=200)
    actor: Literal["owner", "observer", "maintenance"] = "owner"
    capabilities: frozenset[Capability]
    scopes: tuple[Scope, ...] = (Scope(),)
    input_id: str | None = Field(default=None, min_length=1, max_length=200)


class Identity(DTO):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    personality: tuple[str, ...] = Field(default=(), max_length=16)
    communication_style: str = Field(default="", max_length=1000)
    principles: tuple[str, ...] = Field(default=(), max_length=16)
    role: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def bounded_traits(self):
        if any(not value.strip() or len(value) > 300 for value in self.personality + self.principles):
            raise ValueError("Identity traits must be nonempty and at most 300 characters")
        return self


def identity_digest(identity: Identity) -> str:
    return hashlib.sha256(identity.model_dump_json().encode()).hexdigest()


class IdentityVersion(DTO):
    profile_id: str
    version: int
    identity: Identity
    approved_by: str
    created_at: str


class Relationship(DTO):
    """Interaction rules, never an intimacy score or a second Persona prompt.

    Controlled values prevent Reflection from copying forgotten private facts
    into permanent configuration. Names and stories remain Memory records.
    """
    role: Literal["neutral", "collaborator", "mentor", "friend"] = "neutral"
    addressing: Literal["neutral", "respectful", "casual"] = "neutral"
    communication_style: Literal["balanced", "concise", "detailed", "rational", "relaxed"] = "balanced"
    proactivity: Literal["user_led", "on_request", "allowed"] = "user_led"
    history_mentions: Literal["when_relevant", "on_request", "never"] = "when_relevant"
    sensitive_topics: tuple[Literal["health", "finances", "family", "intimacy", "identity"], ...] = Field(default=(), max_length=5)
    agreements: tuple[Literal["ask_before_advice", "allow_disagreement", "respect_boundaries", "no_pressure", "no_exclusivity"], ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def unique_rules(self):
        if len(set(self.sensitive_topics)) != len(self.sensitive_topics) or len(set(self.agreements)) != len(self.agreements):
            raise ValueError("Relationship rules must not contain duplicates")
        return self


def relationship_digest(relationship: Relationship) -> str:
    return hashlib.sha256(relationship.model_dump_json().encode()).hexdigest()


class RelationshipVersion(DTO):
    profile_id: str
    version: int
    relationship: Relationship
    approved_by: str
    created_at: str
    proposal_id: str | None = None


class Profile(DTO):
    profile_id: str
    owner_id: str
    identity_version: int = 0
    memory_revision: int = 0
    deletion_epoch: int = 0
    activation_revision: int = 0
    relationship_version: int = 0


class Source(DTO):
    """Minimal host-supplied evidence, not a claim that the world is verified."""
    source_id: str = Field(min_length=1, max_length=200)
    origin: Literal["user_command", "interaction", "artifact", "import", "model"]
    speaker: Literal["user", "assistant", "tool", "external"]
    excerpt: str = Field(min_length=1, max_length=6000)
    locator: str | None = Field(default=None, max_length=500)


class MemoryDraft(DTO):
    kind: Literal["semantic", "episodic"]
    content: str = Field(min_length=1, max_length=6000)
    category: str = Field(default="fact", min_length=1, max_length=100)
    subject_id: str = Field(default="owner", min_length=1, max_length=200)
    subject_kind: Literal["owner", "kong", "project", "third_party"] = "owner"
    scope: Scope = Scope()
    assertion_mode: Literal["real", "fictional", "hypothetical", "quoted", "unknown"] = "real"
    sensitivity: Literal["public", "personal", "sensitive", "secret"] = "personal"
    occurred_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def nonblank_and_time(self):
        if not self.content.strip():
            raise ValueError("Memory content cannot be blank")
        if any(value is not None and value.utcoffset() is None for value in (self.occurred_at, self.expires_at)):
            raise ValueError("Memory timestamps require a timezone")
        return self


def candidate_digest(draft: MemoryDraft) -> str:
    return hashlib.sha256(draft.model_dump_json().encode()).hexdigest()


class RecordState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    QUARANTINED = "quarantined"
    DELETED = "deleted"


class Candidate(DTO):
    candidate_id: str
    profile_id: str
    revision: int
    state: Literal["pending", "validated", "consolidated", "rejected", "cancelled"]
    draft: MemoryDraft | None
    source: Source | None
    record_id: str | None = None


class Episode(DTO):
    event_key: str = Field(min_length=1, max_length=200)
    what: str = Field(min_length=1, max_length=1200)
    when: datetime | None = None
    participants: tuple[str, ...] = Field(default=(), max_length=16)
    project_id: str | None = Field(default=None, max_length=200)
    user_expression: tuple[str, ...] = Field(default=(), max_length=16)
    outcomes: tuple[str, ...] = Field(default=(), max_length=16)
    importance_reason: str = Field(min_length=1, max_length=600)
    related_records: tuple[str, ...] = Field(default=(), max_length=16)
    evidence_source_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    confirmed_by: str | None = None

    @model_validator(mode="after")
    def bounded_episode(self):
        if self.when is not None and self.when.utcoffset() is None:
            raise ValueError("Episode time requires a timezone")
        if any(not value.strip() or len(value) > 1200 for value in self.user_expression + self.outcomes):
            raise ValueError("Episode fields must be bounded, nonblank evidence")
        if len(self.model_dump_json()) > 6000:
            raise ValueError("Episode structure exceeds its bounded evidence budget")
        return self


class MemoryVersion(DTO):
    record_id: str
    version: int
    content: str
    evidence: Literal["user_authorized", "derived", "imported"]
    sources: tuple[Source, ...] = Field(min_length=1)
    created_at: str
    supersedes: int | None = None
    episode: Episode | None = None


class MemoryRecord(DTO):
    record_id: str
    profile_id: str
    owner_id: str
    revision: int
    state: RecordState
    kind: Literal["semantic", "episodic"]
    category: str
    subject_id: str
    subject_kind: str
    scope: Scope
    assertion_mode: str
    sensitivity: str
    occurred_at: datetime | None
    expires_at: datetime | None
    current: MemoryVersion


class ParentVersion(DTO):
    record_id: str
    version: int = Field(ge=1)


class CommandReceipt(DTO):
    profile_id: str
    command_id: str
    operation: str
    record_id: str | None = None
    candidate_id: str | None = None
    bottle_id: str | None = None
    proposal_id: str | None = None
    reflection_id: str | None = None
    revision: int = 0
    version: int | None = None
    status: Literal["committed", "pending"] = "committed"
    replayed: bool = False


class DeletionReceipt(CommandReceipt):
    deletion_epoch: int
    affected_records: int
    purged_versions: int
    purged_sources: int
    cancelled_candidates: int
    blocked_from_use: bool = True
    internal_content_purged: bool = True
    runtime_history_purged: bool = False
    purged_observations: int = 0


class DeletionPlan(DTO):
    record_id: str
    expected_revision: int
    targets: tuple[tuple[str, int], ...]
    source_ids: tuple[str, ...]
    deletion_epoch: int
    requires_expanded_approval: bool
    digest: str


class Retrieval(DTO):
    records: tuple[MemoryRecord, ...]
    memory_revision: int
    deletion_epoch: int


class BottlePolicy(DTO):
    scope: Scope = Scope()


class Bottle(DTO):
    bottle_id: str
    profile_id: str
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["persona", "memory", "relationship"] = "memory"
    policy: BottlePolicy = BottlePolicy()
    active: bool = False
    revision: int = 1


class AccessState(DTO):
    store_id: str
    owner_id: str
    profile_id: str
    scopes: tuple[Scope, ...]
    memory_revision: int
    deletion_epoch: int
    activation_revision: int


class MemoryContext(DTO):
    access: AccessState
    identity: IdentityVersion | None
    relationship: RelationshipVersion | None = None
    records: tuple[MemoryRecord, ...]
    query_status: Literal["available", "no_usable_information", "ambiguous"] = "available"
    query_reason: str = "lexical"
    pinned_record_ids: tuple[str, ...] = ()


class RetrievalQuery(DTO):
    """Bounded clues, never authority or permission to activate a collection."""
    text: str = Field(max_length=2000)
    focus: str = Field(default="", max_length=240)
    project_id: str | None = Field(default=None, max_length=200)
    episode_ids: tuple[str, ...] = Field(default=(), max_length=4)


class InteractionObservation(DTO):
    event_id: str = Field(min_length=1, max_length=200)
    capture: Literal["off", "on"]
    original_access: AccessState | None
    role: Literal["user", "assistant"]
    text: str | None = Field(default=None, max_length=6000)


class ObservationReceipt(DTO):
    event_id: str
    status: Literal["observed", "skipped", "suppressed", "expired"]
    capture: Literal["off", "on"]
    replayed: bool = False


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
