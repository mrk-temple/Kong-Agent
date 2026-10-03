"""Bounded lease credit for declared resources and verified requirements.

Activity remains an audit measurement. Credit is a scheduling signal within a
declared scope, not a proof of natural-language goal correctness.
"""
import hashlib
import json


def declared_criteria(state):
    approved = state.plan and state.plan.approved_revision == state.plan.revision
    return list(state.success_criteria) + (state.plan.success_criteria if approved else [])


class ProgressGate:
    @staticmethod
    def reset_scope(state):
        p = state.progress_state
        p.scope_key = None
        p.read_credit_keys = []
        p.verified_credit_keys = []
        p.checkpoint_progress = p.progress_count

    @staticmethod
    def sync_scope(state):
        spec = {"goal": state.goal, "paths": state.progress_paths,
                "criteria": [c.model_dump(mode="json") for c in declared_criteria(state)],
                "contract": state.completion_contract.model_dump(mode="json") if state.completion_contract else None,
                "invalidated": state.contract_invalidated}
        key = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if key != state.progress_state.scope_key:
            ProgressGate.reset_scope(state)
            state.progress_state.scope_key = key

    @staticmethod
    def observe(state, history, workspace):
        ProgressGate.sync_scope(state)
        observation = history.observations[-1]
        p = state.progress_state
        p.repeated_actions = p.repeated_actions + 1 if observation.fingerprint == p.last_action_fingerprint else 1
        p.last_action_fingerprint = observation.fingerprint
        p.consecutive_failures = 0 if observation.success else p.consecutive_failures + 1
        if not observation.success or observation.outcome_unknown or not observation.dispatched:
            return
        if observation.output_hash not in p.observed_hashes:
            p.observed_hashes.append(observation.output_hash)
            p.activity_count += 1
        if observation.effect != "read":
            return
        output = observation.output
        if not output or isinstance(output, dict) and output.get("content") == "":
            return  # An out-of-range excerpt is not new inspection progress.
        paths = list(state.progress_paths)
        if state.completion_contract:
            paths += state.completion_contract.version_paths()
        for c in declared_criteria(state):
            paths += ([c.path] if c.path else []) + c.artifact_paths
        try:
            path_value = observation.action.arguments.get("path")
            if path_value is None and observation.action.name == "list_dir":
                path_value = "."
            resolved = workspace.resolve(path_value) if isinstance(path_value, str) else None
            relevant = False
            if resolved is not None:
                for value in paths:
                    root = workspace.resolve(value)
                    if resolved == root or root.is_dir() and resolved.is_relative_to(root):
                        relevant = True
                        break
        except (ValueError, OSError):
            relevant = False
        if relevant:
            key = str(resolved)
        elif not paths:
            key = "unscoped_initial_exploration"  # One credit per unchanged scope.
        else:
            return
        if key not in p.read_credit_keys and len(p.read_credit_keys) < 10000:
            p.read_credit_keys.append(key)
            p.progress_count += 1

    @staticmethod
    def observe_verified(state, results):
        ProgressGate.sync_scope(state)
        if state.contract_invalidated or state.requires_replan or state.pending_approval:
            return
        p = state.progress_state
        for row in results:
            if not row["verified"] or row["kind"] in {"binding", "completion", "protected", "version"}:
                continue
            key = row["scope"] + ":" + row["id"]
            if key not in p.verified_credit_keys and len(p.verified_credit_keys) < 10000:
                p.verified_credit_keys.append(key)
                p.progress_count += 1

    @staticmethod
    def trigger(state):
        p = state.progress_state
        if p.consecutive_failures >= 2:
            return "consecutive_failures"
        if p.repeated_actions >= 3:
            return "repeated_actions"
        if state.turn_count >= state.turn_budget:
            return "lease_exhausted"
        return None

    @staticmethod
    def can_renew(state):
        ProgressGate.sync_scope(state)
        p = state.progress_state
        return p.progress_count > p.checkpoint_progress and p.consecutive_failures == 0 and p.repeated_actions < 3
