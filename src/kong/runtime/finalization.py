"""Describe a rejected submission without executing or binding any action."""
from kong.runtime.evidence import fresh


def rejection_feedback(gate, state, decision, history, problems):
    from kong.runtime.gates import result_evidence, subset
    from kong.runtime.completion import stable_bytes
    results = gate.criteria_results(state, history, decision.criteria_evidence)
    criteria = list(state.success_criteria) + (state.plan.success_criteria if state.plan else [])
    valid, related, historical, by_criterion = set(), set(), set(), {}
    changed_goal = max((index for index, event in enumerate(history.events)
                       if event.type == "human" and event.payload.get("command") == "input_goal"), default=-1)
    epoch = ({event.payload.get("action_id") for event in history.events[changed_goal + 1:]
              if event.type == "action_intent"} if changed_goal >= 0 else None)
    for criterion in criteria:
        ids = []
        if criterion.kind == "tool_result":
            ids = result_evidence(criterion, history, gate.workspace, state)
        for index, obs in enumerate(history.observations):
            if not obs.success:
                continue
            matches = False
            if criterion.kind in {"tool_result", "tool_success"}:
                matches = (obs.action.name == criterion.tool_name and (criterion.kind != "tool_result" or
                    (criterion.operation is None or obs.action.arguments.get("operation") == criterion.operation)
                    and subset(obs.action.arguments, criterion.arguments_match)))
            elif criterion.kind == "evidence":
                matches = True
            elif obs.action.name == "read_file":
                try:
                    matches = gate.workspace.resolve(obs.action.arguments.get("path", "")) == gate.workspace.resolve(criterion.path)
                except (OSError, ValueError):
                    pass
            if not matches:
                continue
            related.add(obs.action_id)
            if criterion.kind in {"tool_success", "evidence"}:
                if criterion.evidence_scope == "event" or fresh(criterion, history, index, gate.workspace, state):
                    ids.append(obs.action_id)
            elif criterion.kind.startswith("file_"):
                try:
                    text = stable_bytes(gate.workspace, criterion.path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                    if obs.output == text and not any(o.effect != "read" for o in history.observations[index + 1:]):
                        ids.append(obs.action_id)
                except (OSError, ValueError, UnicodeError):
                    pass
        ids = sorted(set(ids).intersection(epoch) if epoch is not None else set(ids))
        by_criterion.setdefault(criterion.id, []).extend(ids)
        (historical if criterion.evidence_scope == "event" else valid).update(ids)
    final_commands = {"command:" + command.id for command in state.completion_contract.commands if command.verify_final} if state.completion_contract else set()
    for result in results:
        if result["scope"] != "contract":
            continue
        ids = result["evidence_ids"]
        if result["verified"]:
            by_criterion[result["id"]] = ids
            if result["kind"] == "command" and result["id"] not in final_commands:
                historical.update(ids)
            else:
                valid.update(ids)
        if state.completion_contract:
            paths = {gate.workspace.resolve(path) for path in state.completion_contract.version_paths()}
            for obs in history.observations:
                if not obs.success:
                    continue
                if obs.action.name == "process_exec" and any(obs.action.arguments.get("argv") == command.argv
                                                           for command in state.completion_contract.commands):
                    related.add(obs.action_id)
                elif obs.action.name == "read_file":
                    try:
                        if gate.workspace.resolve(obs.action.arguments.get("path", "")) in paths:
                            related.add(obs.action_id)
                    except (OSError, ValueError):
                        pass
    if epoch is not None:
        valid.intersection_update(epoch)
        historical.intersection_update(epoch)
    successful = [obs.action_id for obs in history.observations if obs.success and (epoch is None or obs.action_id in epoch)]
    binding_only = {result["id"] for result in results if not result["verified"]
                    and result["kind"] == "evidence" and by_criterion.get(result["id"])}
    failed = [result for result in results if not result["verified"] and result["id"] not in binding_only]
    output_failed = any(result["kind"] in {"artifact", "aggregate", "file_exists", "file_contains", "file_equals", "file_json"} for result in failed)
    uncertain_checks = any(result["kind"] in {"command", "tool_success", "tool_result", "protected", "binding"} for result in failed)
    additional_tool = bool(failed) or (state.requires_evidence and not successful) or bool(history.observations and not history.observations[-1].success)
    mutation = True if output_failed else None if uncertain_checks else False
    if additional_tool:
        next_action = "Inspect the failed requirements. Reverify current files where required; do not repeat successful work or mutate protected sources."
    elif state.plan and not all(step.done for step in state.plan.steps):
        next_action = "Complete the active approved step using existing evidence, then return a corrected respond."
    else:
        next_action = "Return a corrected respond with actual evidence_ids and criteria_evidence from the available observations. Do not run another tool unless task state changes."
    unsatisfied = [result["id"] for result in results if not result["verified"]]
    if any(problem.startswith("Tool tasks require") for problem in problems):
        unsatisfied.append("submission:evidence_ids")
    if any(problem.startswith("Contract final evidence") for problem in problems):
        unsatisfied.append("submission:final_evidence")
    return {"satisfied": [result["id"] for result in results if result["verified"]][:100],
            "unsatisfied": unsatisfied[:100], "reasons": problems[:12],
            "current_valid_observations": sorted(valid)[-12:],
            "historical_event_observations": sorted(historical)[-12:],
            "available_successful_observations": successful[-12:],
            "stale_observations": sorted(related - valid - historical)[-12:],
            "criterion_observations": {key: sorted(set(ids))[-12:] for key, ids in list(by_criterion.items())[:100]},
            "artifact_mutation_required": mutation, "additional_tool_required": additional_tool,
            "next_action": next_action, "evidence_rule": "IDs are candidates for your submission, never automatically selected or bound. Historical receipts do not prove current artifacts."}
