"""Public, serializable V0.1 contracts. No provider or environment dependencies."""
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Mode(StrEnum):
    FAST = "fast"
    AUTO = "auto"
    PLAN = "plan"


class Strategy(StrEnum):
    DIRECT = "direct"
    EXECUTE = "execute"
    DISCOVER = "discover"
    PLANNED = "planned"


class Phase(StrEnum):
    DECIDE = "decide"
    ASSESS = "assess"
    APPROVAL = "approval"
    DISCUSS = "discuss"
    EXECUTE = "execute"
    FINISHED = "finished"


class Status(StrEnum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class Criterion(Contract):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: Literal["evidence", "file_exists", "file_contains", "tool_success", "tool_result"] = "evidence"
    path: str | None = None
    contains: str | None = None
    tool_name: str | None = None
    after_last_write: bool = False
    operation: str | None = None
    arguments_match: dict[str, Any] = Field(default_factory=dict)
    output_match: dict[str, Any] = Field(default_factory=dict)
    output_contains: dict[str, str] = Field(default_factory=dict,
        description="JSON Pointer -> required nonempty substring, e.g. /text -> Saved: Kong")

    @model_validator(mode="after")
    def check_verifier(self):
        if self.kind in {"file_exists", "file_contains"} and not self.path:
            raise ValueError("file criteria require path")
        if self.kind == "file_contains" and not self.contains:
            raise ValueError("file_contains requires nonempty contains")
        if self.kind in {"tool_success", "tool_result"} and not self.tool_name:
            raise ValueError("tool criteria require tool_name")
        if self.after_last_write and self.kind not in {"tool_success", "tool_result"}:
            raise ValueError("after_last_write only applies to tool criteria")
        if self.kind != "tool_result" and (self.operation is not None or self.arguments_match or self.output_match or self.output_contains):
            raise ValueError("result filters require kind=tool_result")
        if self.kind == "tool_result" and not (self.output_match or self.output_contains):
            raise ValueError("tool_result requires output_match or output_contains")
        if self.operation == "":
            raise ValueError("operation cannot be empty")
        if any(not key.startswith("/") or not value for key, value in self.output_contains.items()):
            raise ValueError("output_contains requires JSON Pointer keys and nonempty strings")
        return self


class RunMetrics(Contract):
    active_seconds: float = 0
    model_seconds: float = 0
    model_calls: int = 0
    usage_calls: int = 0
    tokens: dict[str, int] = Field(default_factory=dict)
    legacy_unknown: bool = False


class PlanStep(Contract):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    done: bool = False
    evidence_ids: list[str] = Field(default_factory=list)


class Plan(Contract):
    goal: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    steps: list[PlanStep] = Field(min_length=1, max_length=20)
    success_criteria: list[Criterion] = Field(min_length=1)
    revision: int = Field(default=0, ge=0)
    approved_revision: int | None = None

    @model_validator(mode="after")
    def unique_ids(self):
        for items in (self.steps, self.success_criteria):
            if len({item.id for item in items}) != len(items):
                raise ValueError("IDs must be unique within steps / criteria")
        return self


class ProgressState(Contract):
    last_action_fingerprint: str | None = None
    repeated_actions: int = 0
    consecutive_failures: int = 0
    observed_hashes: list[str] = Field(default_factory=list)
    progress_count: int = 0
    checkpoint_progress: int = 0
    recovery_attempts: int = 0
    assessment_reason: str | None = None


class Approval(Contract):
    revision: int
    reason: str


class RunState(Contract):
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    goal: str = Field(min_length=1)
    mode: Mode = Mode.AUTO
    strategy: Strategy = Strategy.DIRECT
    phase: Phase = Phase.DECIDE
    status: Status = Status.RUNNING
    turn_count: int = Field(default=0, ge=0)
    turn_budget: int = Field(default=4, ge=1)
    hard_turn_limit: int = Field(default=40, ge=1)
    requires_evidence: bool = False
    progress_state: ProgressState = Field(default_factory=ProgressState)
    success_criteria: list[Criterion] = Field(default_factory=list)
    plan: Plan | None = None
    current_plan_step: str | None = None
    plan_revision: int = 0
    pending_approval: Approval | None = None
    pending_question: str | None = None
    final_output: str | None = None
    stop_reason: str | None = None


class Action(Contract):
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class FinalResponse(Contract):
    kind: Literal["respond"] = "respond"
    content: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    criteria_evidence: dict[str, list[str]] = Field(default_factory=dict)


class Act(Contract):
    kind: Literal["act"] = "act"
    actions: list[Action] = Field(min_length=1, max_length=8)


class NeedDiscovery(Contract):
    kind: Literal["discover"] = "discover"
    reason: str = Field(min_length=1)
    actions: list[Action] = Field(min_length=1, max_length=8)


class PlanProposal(Contract):
    kind: Literal["plan"] = "plan"
    plan: Plan


class NeedUserInput(Contract):
    kind: Literal["ask"] = "ask"
    question: str = Field(min_length=1)


class CompletePlanStep(Contract):
    kind: Literal["complete_step"] = "complete_step"
    step_id: str
    evidence_ids: list[str] = Field(min_length=1)


class ProgressAssessment(Contract):
    kind: Literal["assess"] = "assess"
    diagnosis: str = Field(min_length=1)
    next_strategy: Literal["continue", "recover", "discover"]


Decision = Annotated[
    FinalResponse | Act | NeedDiscovery | PlanProposal | NeedUserInput
    | CompletePlanStep | ProgressAssessment,
    Field(discriminator="kind"),
]
DECISION_ADAPTER = TypeAdapter(Decision)


class Observation(Contract):
    action_id: str
    turn: int
    action: Action
    success: bool
    output: Any = None
    error: str | None = None
    fingerprint: str
    output_hash: str


class Event(Contract):
    type: Literal["decision", "model_error", "human", "feedback", "action_intent", "observation"]
    turn: int
    payload: dict[str, Any] = Field(default_factory=dict)


class Transition(Contract):
    control: Literal["continue", "escalate", "finish", "pause", "stop"]
    reason: str


class History(Contract):
    events: list[Event] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)

    def append(self, type: str, turn: int, **payload: Any) -> None:
        self.events.append(Event(type=type, turn=turn, payload=payload))

    def evidence_valid(self, ids: list[str]) -> bool:
        known = {o.action_id for o in self.observations if o.success}
        return bool(ids) and set(ids) <= known
