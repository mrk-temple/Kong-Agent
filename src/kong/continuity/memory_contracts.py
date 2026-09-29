"""Versioned, source-grounded derived memory. No execution authority."""
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from kong.contracts import Contract
from kong.continuity.contracts import ContextSource, now


class MemoryScope(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    PROJECT = "project"
    THREAD = "thread"


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    STABLE_FACT = "stable_fact"
    PROJECT = "project"
    DECISION = "decision"
    PROCEDURE = "procedure"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    CANDIDATE = "candidate"


class Grounded(Contract):
    sources: list[ContextSource] = Field(min_length=1)

    @model_validator(mode="after")
    def event_provenance(self):
        if any(s.kind != "event" or not s.thread_id or not s.event_id for s in self.sources):
            raise ValueError("Memory requires thread/run/event provenance")
        return self


class ThreadItem(Grounded):
    id: str = Field(default_factory=lambda: uuid4().hex)
    thread_id: str
    key: str = Field(min_length=1, max_length=200)
    kind: Literal["fact", "preference", "constraint", "decision", "open_loop", "artifact", "procedure", "project"]
    content: str = Field(min_length=1, max_length=6000)
    status: MemoryStatus = MemoryStatus.ACTIVE
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.8, ge=0, le=1)
    supersedes: str | None = None
    confirmed_by: ContextSource | None = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


class ThreadState(Contract):
    thread_id: str
    revision: int = 0
    items: list[ThreadItem] = Field(default_factory=list)


class MemoryMutation(Grounded):
    operation: Literal["add", "update", "supersede", "resolve", "reject"] = "add"
    target_id: str | None = None
    key: str = Field(min_length=1, max_length=200)
    kind: Literal["fact", "preference", "constraint", "decision", "open_loop", "artifact", "procedure", "project"] = "fact"
    content: str = Field(min_length=1, max_length=6000)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.8, ge=0, le=1)


class MemoryDelta(Contract):
    id: str = Field(default_factory=lambda: uuid4().hex)
    thread_id: str
    base_revision: int = Field(ge=0)
    mutations: list[MemoryMutation] = Field(default_factory=list, max_length=100)


class EpisodeRef(Contract):
    episode_id: str
    thread_id: str
    run_id: str


class Episode(Grounded):
    id: str
    thread_id: str
    run_id: str
    first_event: int
    last_event: int
    summary: str
    trigger: Literal["turn_threshold", "token_pressure", "run_complete", "phase_end"]
    created_at: str = Field(default_factory=now)


class MemoryCandidate(Grounded):
    id: str = Field(default_factory=lambda: uuid4().hex)
    thread_item_id: str
    thread_id: str
    key: str
    content: str
    kind: MemoryKind
    scope: MemoryScope
    scope_id: str
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.8, ge=0, le=1)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    confirmed_by: ContextSource | None = None


class MemoryRecord(MemoryCandidate):
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    last_accessed_at: str | None = None
    expires_at: str | None = None
    supersedes: str | None = None


class ObservationDigest(Contract):
    action_id: str
    success: bool
    summary: str
    important_excerpts: list[str]
    raw_ref: ContextSource
    size: int
    truncated: bool


class ContinuityPolicy(Contract):
    recent_messages: int = Field(default=12, ge=0)
    recent_chars: int = Field(default=12000, ge=0)
    observation_chars: int = Field(default=2200, ge=100)
    compact_turns: int = Field(default=8, ge=1)
    pressure_ratio: float = Field(default=0.8, gt=0, le=1)
    episode_chars: int = Field(default=4000, ge=200)
    retrieval_limit: int = Field(default=12, ge=1)
    thread_item_limit: int = Field(default=24, ge=1)
    episode_limit: int = Field(default=4, ge=0)
    critical_observations: int = Field(default=3, ge=0)


class RankingConfig(Contract):
    lexical: float = 3.0
    importance: float = 1.0
    recency: float = 0.5
    scope_match: float = 1.0
    entity_overlap: float = 1.0
    unresolved: float = 0.8
