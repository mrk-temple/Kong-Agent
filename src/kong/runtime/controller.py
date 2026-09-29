"""Authority boundary. Model decisions never mutate control state directly."""
from kong.contracts import (
    Act, Approval, CompletePlanStep, Decision, FinalResponse, History, Mode,
    NeedDiscovery, NeedUserInput, Phase, Plan, PlanProposal, ProgressAssessment,
    RunState, Status, Strategy, Transition,
)
from kong.runtime.gates import CompletionGate, ProgressGate
from kong.runtime.policy import ModePolicy


class RuntimeController:
    def __init__(self, completion_gate: CompletionGate):
        self.completion_gate = completion_gate

    def start_turn(self, state: RunState) -> bool:
        if state.status != Status.RUNNING:
            return False
        if state.turn_count >= state.hard_turn_limit:
            state.status = Status.STOPPED
            state.stop_reason = "fast_limit_suggest_auto" if state.mode == Mode.FAST else "hard_turn_limit"
            return False
        state.turn_count += 1
        return True

    def authorize(self, state: RunState, decision: Decision, *, context_only: bool = False) -> str | None:
        policy = ModePolicy.for_mode(state.mode)
        if context_only and isinstance(decision, (Act, NeedDiscovery)):
            return None  # Runtime verified every member is a host-owned SkillTool.
        if isinstance(decision, PlanProposal) and not policy.plan_allowed:
            return "FAST forbids explicit plans. Ask the user to start an AUTO or PLAN run."
        if state.pending_approval or state.phase == Phase.DISCUSS:
            if not isinstance(decision, (PlanProposal, NeedUserInput, FinalResponse)):
                return "Plan approval is pending. Only discuss, revise the plan, or ask the user."
        if policy.plan_required and (not state.plan or state.plan.approved_revision != state.plan.revision):
            if not isinstance(decision, (PlanProposal, NeedUserInput, FinalResponse)):
                return "PLAN requires an approved plan before any environment action."
            if isinstance(decision, FinalResponse) and not state.pending_approval:
                return "PLAN requires a plan proposal; a final response cannot bypass approval."
        if state.phase == Phase.ASSESS:
            if not isinstance(decision, (ProgressAssessment, PlanProposal, NeedUserInput, FinalResponse)):
                return "Checkpoint requires assess, plan, ask, or evidence-backed respond before more actions."
        if isinstance(decision, ProgressAssessment) and state.phase != Phase.ASSESS:
            return "Assessment is only valid at a runtime checkpoint."
        return None

    def propose(self, state: RunState, plan: Plan) -> Transition:
        if state.mode == Mode.FAST:
            raise ValueError("FAST forbids explicit plans")
        # Approval and completion flags provided by a model are never trusted.
        plan = plan.model_copy(deep=True)
        plan.goal = state.goal
        plan.revision = state.plan_revision + 1
        plan.approved_revision = None
        for step in plan.steps:
            step.done = False
            step.evidence_ids = []
        state.plan = plan
        state.plan_revision = plan.revision
        state.current_plan_step = plan.steps[0].id
        state.pending_approval = Approval(revision=plan.revision, reason=plan.rationale)
        state.pending_question = None
        state.strategy = Strategy.PLANNED
        state.phase = Phase.APPROVAL
        state.status = Status.WAITING_USER
        return Transition(control="pause", reason="plan_approval_required")

    def transition(self, state: RunState, decision: Decision, history: History) -> Transition:
        if isinstance(decision, PlanProposal):
            return self.propose(state, decision.plan)
        if isinstance(decision, NeedUserInput):
            state.pending_question = decision.question
            state.status = Status.WAITING_USER
            return Transition(control="pause", reason="user_input_required")
        if isinstance(decision, FinalResponse):
            if state.pending_approval:
                state.status = Status.WAITING_USER
                state.phase = Phase.APPROVAL
                state.pending_question = decision.content
                return Transition(control="pause", reason="plan_discussion")
            problems = self.completion_gate.check(state, decision, history)
            if problems:
                history.append("feedback", state.turn_count, completion_rejected=problems)
            else:
                state.final_output = decision.content
                state.status = Status.COMPLETED
                state.phase = Phase.FINISHED
                state.stop_reason = "goal_completed"
                return Transition(control="finish", reason="completion_gate_passed")
        elif isinstance(decision, (Act, NeedDiscovery)):
            state.strategy = (Strategy.PLANNED if state.plan else
                              Strategy.DISCOVER if isinstance(decision, NeedDiscovery) else Strategy.EXECUTE)
            state.phase = Phase.EXECUTE
        elif isinstance(decision, CompletePlanStep):
            plan = state.plan
            step = next((s for s in plan.steps if s.id == decision.step_id), None) if plan else None
            if (not plan or plan.approved_revision != plan.revision or not step or step.done
                    or decision.step_id != state.current_plan_step
                    or not history.evidence_valid(decision.evidence_ids)):
                history.append("feedback", state.turn_count, error="Complete only the active approved step with successful evidence.")
            else:
                step.done = True
                step.evidence_ids = decision.evidence_ids
                state.progress_state.progress_count += 1
                state.current_plan_step = next((s.id for s in plan.steps if not s.done), None)
                # PLAN gets a fresh per-step lease, but never a fresh hard budget.
                if state.mode == Mode.PLAN:
                    self._renew(state)
        elif isinstance(decision, ProgressAssessment):
            return self._assess(state, decision, history)
        return self.checkpoint(state)

    def checkpoint(self, state: RunState) -> Transition:
        if state.mode != Mode.FAST:
            reason = ProgressGate.trigger(state)
            if reason:
                state.phase = Phase.ASSESS
                state.progress_state.assessment_reason = reason
                return Transition(control="escalate", reason=reason)
        return Transition(control="continue", reason="next_decision")

    def _renew(self, state: RunState) -> None:
        state.turn_budget = min(state.hard_turn_limit, state.turn_count + ModePolicy.for_mode(state.mode).lease)
        state.progress_state.checkpoint_progress = state.progress_state.progress_count
        state.progress_state.assessment_reason = None
        state.phase = Phase.EXECUTE

    def _assess(self, state: RunState, decision: ProgressAssessment, history: History) -> Transition:
        progress = state.progress_state
        if ProgressGate.can_renew(state):
            self._renew(state)
            progress.recovery_attempts = 0
            if decision.next_strategy == "discover" and not state.plan:
                state.strategy = Strategy.DISCOVER
            return Transition(control="continue", reason="lease_renewed_with_progress")
        if decision.next_strategy in {"recover", "discover"} and progress.recovery_attempts < 1:
            self._renew(state)
            state.turn_budget = min(state.hard_turn_limit, state.turn_count + 2)
            progress.recovery_attempts += 1
            progress.consecutive_failures = 0
            progress.repeated_actions = 0
            progress.last_action_fingerprint = None
            if not state.plan:
                state.strategy = Strategy.DISCOVER if decision.next_strategy == "discover" else Strategy.EXECUTE
            return Transition(control="continue", reason="bounded_recovery_lease")
        state.status = Status.WAITING_USER
        state.pending_question = "没有足够的新进展，已暂停。请补充信息或调整方向：" + decision.diagnosis
        return Transition(control="pause", reason="no_progress")

    def human(self, state: RunState, history: History, command: str, *, text: str = "", revision: int | None = None, plan: Plan | None = None) -> None:
        if command == "interrupt":
            if state.status != Status.RUNNING:
                raise ValueError("Only a running task can be interrupted")
            state.status = Status.STOPPED
            state.stop_reason = "user_interrupted"
        elif command == "resume":
            if state.status != Status.STOPPED or state.stop_reason != "user_interrupted":
                raise ValueError("Only an interrupted task can resume; hard budgets never reset")
            state.status = Status.RUNNING
            state.stop_reason = None
        elif command == "approve":
            if (state.status != Status.WAITING_USER or not state.pending_approval
                    or revision != state.pending_approval.revision or not state.plan):
                raise ValueError("Approve the exact current pending revision")
            state.plan.approved_revision = revision
            state.pending_approval = None
            state.pending_question = None
            state.status = Status.RUNNING
            self._renew(state)
        elif command == "edit":
            if state.status != Status.WAITING_USER or not state.pending_approval or plan is None:
                raise ValueError("Edit requires a pending plan and a replacement Plan object")
            self.propose(state, plan)
        elif command == "discuss":
            if state.status != Status.WAITING_USER or not state.pending_approval or not text.strip():
                raise ValueError("Discuss requires a pending plan and a message")
            # Discuss invalidates the prior approval opportunity until the response is received.
            state.phase = Phase.DISCUSS
            state.pending_question = None
            state.status = Status.RUNNING
        elif command == "answer":
            if state.status != Status.WAITING_USER or not state.pending_question or not text.strip():
                raise ValueError("There is no pending question, or the answer is empty")
            state.pending_question = None
            state.status = Status.RUNNING
            if state.pending_approval:
                state.phase = Phase.DISCUSS
            else:
                # User supplied new information; allow one bounded recovery window.
                self._renew(state)
                state.turn_budget = min(state.hard_turn_limit, state.turn_count + 2)
                state.progress_state.recovery_attempts = 0
                state.progress_state.consecutive_failures = 0
                state.progress_state.repeated_actions = 0
        elif command == "reject":
            if state.status != Status.WAITING_USER:
                raise ValueError("Reject requires a paused task")
            state.status = Status.STOPPED
            state.stop_reason = "user_rejected"
        else:
            raise ValueError(f"Unknown human command: {command}")
        history.append("human", state.turn_count, command=command, text=text, revision=revision)
