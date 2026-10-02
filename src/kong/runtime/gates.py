import json

from kong.contracts import FinalResponse, History, RunState
from kong.workspace import Workspace
from kong.runtime.evidence import fresh


def subset(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(actual[k], v) for k,v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(subset(a,b) for a,b in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def exact_json(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(exact_json(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(exact_json(a, b) for a, b in zip(actual, expected))
    return actual == expected


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def pointer(value, path):
    try:
        for key in path[1:].split("/"):
            key = key.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def result_evidence(criterion, history, workspace=None, state=None):
    candidates = [(i,o) for i,o in enumerate(history.observations)
        if o.action.name == criterion.tool_name
        and (criterion.operation is None or o.action.arguments.get("operation") == criterion.operation)
        and subset(o.action.arguments, criterion.arguments_match)]
    if not candidates:
        return []
    # A newer failure or changed result supersedes earlier success for the same selector.
    index, observation = candidates[-1]
    needs_fresh = criterion.evidence_scope == "current"
    if not observation.success or needs_fresh and not fresh(criterion, history, index, workspace, state):
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
                if not result_evidence(criterion, history, self.workspace, state):
                    problems.append(f"Criterion {criterion.id} requires the latest matching {criterion.tool_name} operation to succeed with the required result.")
                continue
            if criterion.kind == "tool_success":
                candidates = [(i,o) for i,o in enumerate(history.observations)
                              if o.action.name == criterion.tool_name]
                # A newer failed verification supersedes the earlier passing one.
                valid = bool(candidates) and candidates[-1][1].success
                if valid and criterion.evidence_scope == "current":
                    valid = fresh(criterion, history, candidates[-1][0], self.workspace, state)
                if not valid:
                    problems.append(f"Criterion {criterion.id} requires successful {criterion.tool_name}"
                                    + (" after the latest possible mutation, bound to current artifact versions." if criterion.evidence_scope == "current" else "."))
                continue
            if criterion.kind == "evidence":
                ids = decision.criteria_evidence.get(criterion.id, [])
                valid = history.evidence_valid(ids)
                if valid and criterion.evidence_scope == "current":
                    valid = all(fresh(criterion, history, index, self.workspace, state)
                                for index, observation in enumerate(history.observations) if observation.action_id in ids)
                if not valid:
                    problems.append(f"Criterion {criterion.id} needs successful environment evidence.")
                continue
            try:
                path = self.workspace.resolve(criterion.path)
                valid = path.is_file()
                if valid and criterion.kind == "file_contains":
                    valid = (path.stat().st_size <= 1_000_000
                             and criterion.contains in path.read_text(encoding="utf-8"))
                if valid and criterion.kind in {"file_equals", "file_json"}:
                    valid = path.stat().st_size <= 1_000_000
                    if valid:
                        with path.open("rb") as stream:
                            data = stream.read(1_000_001)
                        valid = len(data) <= 1_000_000
                        if valid:
                            text = data.decode("utf-8-sig" if criterion.kind == "file_json" else "utf-8")
                            valid = (text == criterion.expected_text if criterion.kind == "file_equals" else
                                     exact_json(json.loads(text, object_pairs_hook=unique_object), criterion.json_match))
            except (OSError, ValueError, UnicodeError, RecursionError):
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
            clean = History(events=history.events, observations=history.observations)
            problems = self.check(probe, decision, clean)
            verified = not any(p.startswith(("Criterion ", "Deterministic verifier")) for p in problems)
            results.append({"scope":scope, "id":criterion.id, "description":criterion.description,
                            "kind":criterion.kind, "verified":verified,
                            "evidence_ids":result_evidence(criterion, history, self.workspace, state) if criterion.kind == "tool_result" else [],
                            "artifact_versions":{o.action_id: o.artifact_versions for o in history.observations
                                                 if o.action.name == criterion.tool_name and o.artifact_versions}})
        return results
