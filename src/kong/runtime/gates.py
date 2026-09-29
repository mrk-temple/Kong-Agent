from kong.contracts import FinalResponse, History, RunState
from kong.workspace import Workspace


def subset(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(actual[k], v) for k,v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(subset(a,b) for a,b in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def pointer(value, path):
    try:
        for key in path[1:].split("/"):
            key = key.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def result_evidence(criterion, history):
    candidates = [(i,o) for i,o in enumerate(history.observations)
        if o.action.name == criterion.tool_name
        and (criterion.operation is None or o.action.arguments.get("operation") == criterion.operation)
        and subset(o.action.arguments, criterion.arguments_match)]
    if not candidates:
        return []
    # A newer failure or changed result supersedes earlier success for the same selector.
    index, observation = candidates[-1]
    last_write = max((i for i,o in enumerate(history.observations)
        if o.action.name in {"write_file", "patch_file"}), default=-1)
    if not observation.success or criterion.after_last_write and index <= last_write:
        return []
    if not subset(observation.output, criterion.output_match):
        return []
    for path, required in criterion.output_contains.items():
        value = pointer(observation.output, path)
        if not isinstance(value, str) or required not in value:
            return []
    return [observation.action_id]


class ProgressGate:
    @staticmethod
    def observe(state: RunState, history: History) -> None:
        observation = history.observations[-1]
        progress = state.progress_state
        if observation.fingerprint == progress.last_action_fingerprint:
            progress.repeated_actions += 1
        else:
            progress.repeated_actions = 1
        progress.last_action_fingerprint = observation.fingerprint
        progress.consecutive_failures = 0 if observation.success else progress.consecutive_failures + 1
        # New errors are not progress. Repeating an identical source/result is not progress.
        if observation.success and observation.output_hash not in progress.observed_hashes:
            progress.observed_hashes.append(observation.output_hash)
            progress.progress_count += 1

    @staticmethod
    def trigger(state: RunState) -> str | None:
        p = state.progress_state
        if p.consecutive_failures >= 2:
            return "consecutive_failures"
        if p.repeated_actions >= 3:
            return "repeated_actions"
        if state.turn_count >= state.turn_budget:
            return "lease_exhausted"
        return None

    @staticmethod
    def can_renew(state: RunState) -> bool:
        p = state.progress_state
        return (p.progress_count > p.checkpoint_progress
                and p.consecutive_failures == 0 and p.repeated_actions < 3)


class CompletionGate:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def check(self, state: RunState, decision: FinalResponse, history: History) -> list[str]:
        problems: list[str] = []
        if state.requires_evidence and not history.observations:
            problems.append("An environment task cannot complete without a successful tool observation.")
        if history.observations:
            if not history.evidence_valid(decision.evidence_ids):
                problems.append("Tool tasks require successful observation IDs as evidence.")
            if not history.observations[-1].success:
                problems.append("Last action failed; recover or ask the user instead of claiming completion.")
        elif decision.evidence_ids:
            problems.append("Unknown evidence IDs.")
        criteria = list(state.success_criteria)
        if state.plan:
            if state.plan.approved_revision != state.plan.revision:
                problems.append("Plan revision is not approved.")
            if not all(step.done for step in state.plan.steps):
                problems.append("Not all plan steps are complete.")
            criteria += state.plan.success_criteria
        for criterion in criteria:
            if criterion.kind == "tool_result":
                if not result_evidence(criterion, history):
                    problems.append(f"Criterion {criterion.id} requires the latest matching {criterion.tool_name} operation to succeed with the required result.")
                continue
            if criterion.kind == "tool_success":
                last_write = max((i for i,o in enumerate(history.observations)
                                  if o.action.name in {"write_file", "patch_file"}), default=-1)
                candidates = [(i,o) for i,o in enumerate(history.observations)
                              if o.success and o.action.name == criterion.tool_name]
                if not any(not criterion.after_last_write or i > last_write for i,o in candidates):
                    problems.append(f"Criterion {criterion.id} requires successful {criterion.tool_name}"
                                    + (" after the latest file write/patch." if criterion.after_last_write else "."))
                continue
            if criterion.kind == "evidence":
                if not history.evidence_valid(decision.criteria_evidence.get(criterion.id, [])):
                    problems.append(f"Criterion {criterion.id} needs successful environment evidence.")
                continue
            try:
                path = self.workspace.resolve(criterion.path)
                valid = path.is_file()
                if valid and criterion.kind == "file_contains":
                    valid = (path.stat().st_size <= 1_000_000
                             and criterion.contains in path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                valid = False
            if not valid:
                problems.append(f"Deterministic verifier failed: {criterion.id} ({criterion.description}).")
        return problems

    def criteria_results(self, state, history, evidence):
        results = []
        criteria = [("goal", c) for c in state.success_criteria]
        if state.plan:
            criteria += [("plan", c) for c in state.plan.success_criteria]
        for scope, criterion in criteria:
            probe = RunState(goal=state.goal, success_criteria=[criterion])
            decision = FinalResponse(content="criterion check", evidence_ids=[o.action_id for o in history.observations if o.success],
                                     criteria_evidence=evidence)
            # Global completion checks do not describe this individual criterion.
            clean = History(observations=history.observations)
            problems = self.check(probe, decision, clean)
            verified = not any(p.startswith(("Criterion ", "Deterministic verifier")) for p in problems)
            results.append({"scope":scope, "id":criterion.id, "description":criterion.description,
                            "kind":criterion.kind, "verified":verified,
                            "evidence_ids":result_evidence(criterion, history) if criterion.kind == "tool_result" else []})
        return results
