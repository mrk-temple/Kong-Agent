"""Passive verification/closure measurements; no approval or lease authority."""


def verification_epoch(history):
    return max((i for i, event in enumerate(history.events) if event.type == "human" and
                event.payload.get("command") in {"input_goal", "input_constraint"} or
                event.type == "feedback" and "completion_contract_bound" in event.payload), default=-1)


def checkpoint_values(criteria):
    outputs = [r for r in criteria if r["kind"] in {"artifact", "aggregate", "file_exists", "file_contains", "file_equals", "file_json"}]
    authority = [r for r in criteria if r["kind"] in {"protected", "binding", "version"}]
    return {"artifact_checks_passed": bool(outputs) and all(r["verified"] for r in outputs + authority) if outputs else None,
            "verified_requirement_ids": sorted(r["id"] for r in criteria if r["verified"]),
            "declared_requirement_count": len(criteria)}


def verification_summary(state, criteria):
    from kong.completion_contracts import has_business_conditions
    if state.completion_contract:
        coverage = "host_contract"
    elif any(r["kind"] in {"file_equals", "file_json"} for r in criteria):
        coverage = "explicit_value_conditions"
    else:
        coverage = "mechanical_only" if criteria else "undeclared"
    business = (has_business_conditions(state.completion_contract) if state.completion_contract else coverage == "explicit_value_conditions")
    return {"coverage": coverage, "business_acceptance_declared": bool(business),
            "declared_checks_passed": all(r["verified"] for r in criteria) if criteria else None,
            "note": "Only the declared checks are verified. Existence, parsing, types and command success have different coverage; none proves undeclared business requirements."}


def closure_summary(runtime):
    history, metrics = runtime.history, runtime.metrics
    epoch = verification_epoch(history)
    checkpoints = [e.payload["verification_checkpoint"] for e in history.events
                   if e.payload.get("verification_checkpoint", {}).get("scope_event_index") == epoch]
    first = next((p for p in checkpoints if p["artifact_checks_passed"] is True), None)
    coverage = metrics.attribution_coverage if checkpoints else "unmeasured"
    rejections = []
    for index, event in enumerate(history.events):
        if index <= epoch or "completion_rejected" not in event.payload:
            continue
        next_decision = next((e for e in history.events[index + 1:] if e.type == "decision"), None)
        respond_index = next((i for i in range(index + 1, len(history.events)) if history.events[i].type == "decision" and
                              history.events[i].payload.get("decision", {}).get("kind") == "respond"), len(history.events))
        completed = bool(next_decision and any(e.turn == next_decision.turn and
            e.payload.get("transition", {}).get("reason") == "completion_gate_passed" for e in history.events[index + 1:]))
        rejections.append({"turn": event.turn, "additional_tool_required": event.payload.get("completion_feedback", {}).get("additional_tool_required"),
            "next_decision_kind": next_decision.payload.get("decision", {}).get("kind") if next_decision else None,
            "completed_on_next_decision": completed,
            "tool_observations_until_next_respond": sum(e.type == "observation" for e in history.events[index + 1:respond_index])})
    return {"checkpoint_coverage": coverage, "checkpoints": checkpoints, "first_artifact_checks_passed": first,
        "model_calls_since_first_artifact_checks_passed": metrics.model_calls - first["model_calls"] if first else None,
        "tool_calls_since_first_artifact_checks_passed": metrics.tool_calls - first["tool_calls"] if first else None,
        "active_seconds_since_first_artifact_checks_passed": max(0, metrics.active_seconds - first["active_seconds"]) if first else None,
        "artifact_checks_invalidated_after_first": bool(first and any(p["artifact_checks_passed"] is False for p in checkpoints[checkpoints.index(first) + 1:])),
        "rejections": rejections, "completed": runtime.state.status.value == "completed",
        "note": "Checkpoints observe declared conditions after budgeted decisions, not the exact instant bytes were written. Active time excludes human pauses; missing historical measurements stay unknown. Activity credit and lease policy are unchanged."}
