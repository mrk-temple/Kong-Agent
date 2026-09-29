"""Derived context contracts, separate from V0.1 control state and raw history."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from kong.contracts import Approval, Contract, PlanStep


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Thread(Contract):
    thread_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    workspace: str
    title: str = ""
    status: Literal["active", "archived"] = "active"
    created_at: str = Field(default_factory=now)


class ContextBudget(Contract):
    """Output-first budget. The V0.2 compiler enforces its conservative estimate."""
    window_tokens: int = Field(default=32768, gt=0)
    output_reserved_tokens: int = Field(default=4096, ge=0)

    @model_validator(mode="after")
    def reserve_output(self):
        if self.output_reserved_tokens >= self.window_tokens:
            raise ValueError("Output reservation must leave room for input")
        return self

    @property
    def input_tokens(self) -> int:
        return self.window_tokens - self.output_reserved_tokens


class ContextSource(Contract):
    """Event offsets are zero-based in the append-only Run History.

    State/system sources are explicit: V0.1 does not emit a goal-created event.
    These are not memory records and must not pretend to have event provenance.
    """
    kind: Literal["system", "state", "tools", "event", "observation", "custom"]
    thread_id: str | None = None
    event_id: str | None = None
    run_id: str | None = None
    event_index: int | None = Field(default=None, ge=0)
    action_id: str | None = None
    path: str | None = None
    snapshot_path: str | None = None

    @model_validator(mode="after")
    def event_reference(self):
        if self.kind == "event" and (self.run_id is None or self.event_index is None):
            raise ValueError("Event sources require run_id and event_index")
        return self


class ContextItem(Contract):
    category: str
    priority: int = Field(ge=0, le=5)
    content: str
    sources: list[ContextSource] = Field(min_length=1)


class WorkingSet(Contract):
    run_id: str
    goal: str
    current_plan_step: PlanStep | None = None
    pending_approval: Approval | None = None
    pending_question: str | None = None
    items: list[ContextItem] = Field(default_factory=list)
    source_event_count: int = Field(default=0, ge=0)
    current_focus: str | None = None
    progress_summary: str = ""
    active_constraints: list[str] = Field(default_factory=list)
    important_observation_ids: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    recent_failures: list[str] = Field(default_factory=list)


class ContextPacket(Contract):
    """Exactly the messages passed to one generate(), plus inspectable provenance."""
    thread_id: str
    run_id: str
    turn: int
    messages: list[dict[str, str]]
    working_set: WorkingSet
    budget: ContextBudget
    items: list[ContextItem]
    estimated_input_tokens: int = Field(ge=0)
    token_estimator: str = "utf8_bytes_upper_estimate"
    budget_enforced: bool = False
    omitted_event_indices: list[int] = Field(default_factory=list)
    truncated_event_indices: list[int] = Field(default_factory=list)
    recent_conversation: list[ConversationMessage] = Field(default_factory=list)
    selection_notes: list[str] = Field(default_factory=list)
    category_tokens: dict[str, int] = Field(default_factory=dict)
    dropped: list[dict] = Field(default_factory=list)
    thread_memory: list[dict] = Field(default_factory=list)
    recalled_memories: list[dict] = Field(default_factory=list)
    episodes: list[dict] = Field(default_factory=list)
    observations: list[dict] = Field(default_factory=list)

    @property
    def over_budget(self) -> bool:
        return self.estimated_input_tokens > self.budget.input_tokens


class ConversationMessage(Contract):
    role: Literal["user", "assistant"]
    content: str
    source: ContextSource


class RecentContextPolicy(Contract):
    max_messages: int = Field(default=12, ge=0)
    max_chars: int = Field(default=12000, ge=0)


ContextPacket.model_rebuild()
