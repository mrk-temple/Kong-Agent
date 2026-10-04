"""Caller-declared acceptance; an execution receipt is not business completion."""
import os
from pathlib import PurePosixPath

from kong.completion_contracts import has_business_conditions

ACCEPTANCE_REQUIRED = "Acceptance conditions required:"


def path_key(value):
    key = PurePosixPath(value.replace("\\", "/")).as_posix()
    return key.casefold() if os.name == "nt" else key


def has_declared_acceptance(state):
    if state.completion_contract:
        return has_business_conditions(state.completion_contract) and not state.contract_invalidated
    criteria = list(state.success_criteria)
    if state.plan and state.plan.approved_revision == state.plan.revision:
        criteria += state.plan.success_criteria
    strong = [c for c in criteria if c.kind in {"file_equals", "file_json", "tool_result"}]
    if not strong:
        return False
    covered = {path_key(c.path) for c in strong if c.path}
    covered.update(path_key(p) for c in strong if c.evidence_scope == "current" for p in c.artifact_paths)
    # An unrelated correct field/file cannot certify another weak deliverable.
    weak = {path_key(c.path) for c in criteria if c.kind in {"file_exists", "file_contains"} and c.path}
    return weak <= covered


def acceptance_problem(state, history):
    has_environment_conditions = bool(state.success_criteria or state.completion_contract or
        state.plan and state.plan.success_criteria)
    if state.completion_scope == "mechanical" or not (history.observations or state.requires_evidence or has_environment_conditions):
        return None
    if has_declared_acceptance(state):
        return None
    return (ACCEPTANCE_REQUIRED + " environment tasks need caller-declared value/result conditions covering "
            "their deliverables. Existence and successful operations alone do not establish business completion. "
            "Ask for the missing conditions; do not invent them or retry tools to gain authority.")
