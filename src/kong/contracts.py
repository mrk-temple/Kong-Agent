"""Public, serializable V0.1 contracts. No provider or environment dependencies."""
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic_core import PydanticCustomError


INVARIANT_MESSAGES = {
    "criterion_event_artifact_conflict": "Historical event evidence cannot certify current artifacts",
    "criterion_missing_path": "File criteria require path",
    "criterion_missing_contains": "file_contains requires nonempty contains",
    "criterion_missing_expected": "file_equals requires expected_text; file_json requires json_match",
    "criterion_file_filters_kind": "expected_text only applies to file_equals; json_match only applies to file_json",
    "criterion_missing_tool": "Tool criteria require tool_name",
    "criterion_after_write_kind": "after_last_write only applies to tool criteria",
    "criterion_result_filters_kind": "Result filters require kind=tool_result; omit them for other kinds",
    "criterion_missing_result_match": "tool_result requires output_match or output_contains",
    "criterion_empty_operation": "Omit operation or supply a nonempty operation name",
    "criterion_invalid_pointer": "output_contains requires JSON Pointer keys and nonempty strings",
    "plan_duplicate_ids": "IDs must be unique within steps and within criteria",
}


def invariant_error(code):
    return PydanticCustomError(code, INVARIANT_MESSAGES[code])


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
    kind: Literal["evidence", "file_exists", "file_contains", "file_equals", "file_json", "tool_success", "tool_result"] = "evidence"
    path: str | None = None
    contains: str | None = None
    expected_text: str | None = Field(default=None, max_length=1_000_000,
        description="file_equals only: exact UTF-8 text, including whitespace; empty string is allowed.")
    json_match: dict[str, Any] | None = Field(default=None,
        description="file_json only: exact JSON object, including keys and value types; whitespace is ignored.")
    tool_name: str | None = None
    after_last_write: bool = Field(default=False, description="Only for tool_success/tool_result. Omit or false for other kinds.")
    evidence_scope: Literal["current", "event"] = Field(default="current",
        description="current verifies current artifacts/resource state; event only proves a past operation happened.")
    artifact_paths: list[str] = Field(default_factory=list, max_length=100,
        description="Local files whose current versions must match the verification; empty uses the workspace.")
    operation: str | None = Field(default=None, description="Only for tool_result. Omit or null for other kinds; never use an empty string.")
    arguments_match: dict[str, Any] = Field(default_factory=dict)
    output_match: dict[str, Any] = Field(default_factory=dict)
    output_contains: dict[str, str] = Field(default_factory=dict,
        description="JSON Pointer -> required nonempty substring, e.g. /text -> Saved: Kong")

    @model_validator(mode="after")
    def check_verifier(self):
        if self.evidence_scope == "event" and (self.after_last_write or self.artifact_paths):
            raise invariant_error("criterion_event_artifact_conflict")
        if self.kind in {"file_exists", "file_contains", "file_equals", "file_json"} and not self.path:
            raise invariant_error("criterion_missing_path")
        if self.kind == "file_contains" and not self.contains:
            raise invariant_error("criterion_missing_contains")
        if self.kind == "file_equals" and self.expected_text is None or self.kind == "file_json" and self.json_match is None:
            raise invariant_error("criterion_missing_expected")
        if self.expected_text is not None and self.kind != "file_equals" or self.json_match is not None and self.kind != "file_json":
            raise invariant_error("criterion_file_filters_kind")
        if self.kind in {"tool_success", "tool_result"} and not self.tool_name:
            raise invariant_error("criterion_missing_tool")
        if self.after_last_write and self.kind not in {"tool_success", "tool_result"}:
            raise invariant_error("criterion_after_write_kind")
        if self.kind != "tool_result" and (self.operation is not None or self.arguments_match or self.output_match or self.output_contains):
            raise invariant_error("criterion_result_filters_kind")
        if self.kind == "tool_result" and not (self.output_match or self.output_contains):
            raise invariant_error("criterion_missing_result_match")
        if self.operation == "":
            raise invariant_error("criterion_empty_operation")
        if any(not key.startswith("/") or not value for key, value in self.output_contains.items()):
            raise invariant_error("criterion_invalid_pointer")
        return self


class ToolTiming(Contract):
    action_id: str
    tool: str
    operation: str = "run"
    elapsed_seconds: float | None = Field(default=None, ge=0)
    success: bool | None = None
    cancelled: bool = False
    outcome_unknown: bool = False


class RunMetrics(Contract):
    active_seconds: float = 0
    model_seconds: float = 0
    model_calls: int = 0
    usage_calls: int = 0
    tokens: dict[str, int] = Field(default_factory=dict)
    legacy_unknown: bool = False
    tool_calls: int = Field(default=0, ge=0)
    tool_seconds: float = Field(default=0, ge=0)
    tool_wall_seconds: float = Field(default=0, ge=0)
    tool_timings: list[ToolTiming] = Field(default_factory=list)
    tool_timing_complete: bool = True


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
                raise invariant_error("plan_duplicate_ids")
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
    requires_replan: bool = False
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
    outcome_unknown: bool = False
    fingerprint: str
    output_hash: str
    evidence_version: Literal[1, 2] = 1
    artifact_versions: dict[str, str | None] = Field(default_factory=dict)
    artifact_versions_before: dict[str, str | None] = Field(default_factory=dict)
    effect: Literal["read", "mutation", "unknown"] = "unknown"


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
