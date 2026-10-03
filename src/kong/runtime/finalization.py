"""Describe a rejected submission without executing or binding any action."""
from kong.runtime.evidence import fresh


def criterion_details(criterion, history, workspace, state, verified):
    """Describe existing legacy requirements; never manufacture a command."""
    details = {"path": criterion.path, "tool": criterion.tool_name,
               "artifact_paths": list(criterion.artifact_paths), "evidence_scope": criterion.evidence_scope,
               "issues": []}
    if verified:
        return details
    if criterion.kind in {"tool_success", "tool_result"}:
        from kong.runtime.gates import subset
        from kong.runtime.evidence import targets
        candidates = [(i,o) for i,o in enumerate(history.observations) if o.action.name == criterion.tool_name
            and (criterion.kind != "tool_result" or (criterion.operation is None or
                o.action.arguments.get("operation") == criterion.operation) and
                subset(o.action.arguments, criterion.arguments_match))]
        if not candidates:
            code = "criterion.not_observed"
        else:
            index, obs = candidates[-1]
            if not obs.success or obs.outcome_unknown:
                code = "criterion.latest_failed_or_unknown"
            elif criterion.evidence_scope == "current" and any(
                    obs.artifact_versions_before.get(path) != obs.artifact_versions.get(path)
                    for path in targets(criterion, state)):
                code = "criterion.generation_not_stable_verification"
            elif criterion.evidence_scope == "current" and not fresh(criterion, history, index, workspace, state):
                code = "criterion.current_verification_required"
            else:
                code = "criterion.result_mismatch"
        details["issues"] = [{"code": code, "tool": criterion.tool_name}]
    elif criterion.kind == "evidence":
        details["issues"] = [{"code": "criterion.evidence_required"}]
    else:
        details["issues"] = [{"code": "criterion.file_condition_failed", "path": criterion.path}]
    return details


def remaining_steps(state, results, failed, output_failed, *, acceptance_required=False, last_failed=False):
    """Advice only; no dispatch, implicit parameter changes or evidence binding."""
    contract = state.completion_contract
    if acceptance_required:
        return [{"kind": "ask", "reason": "Caller-declared acceptance conditions are missing; do not invent them or replay tools."}]
    if any(result["kind"] in {"protected", "binding", "version", "completion"} for result in failed):
        return [{"kind": "ask", "reason": "Resolve the failed host requirement; do not modify protected sources or replace the contract."}]
    steps = []
    if output_failed:
        steps.append({"kind": "repair_artifact", "paths": [r.get("path") for r in failed if r["kind"] in
                      {"artifact", "file_exists", "file_contains", "file_equals", "file_json"}],
                      "instruction": "Inspect and fix only the failing artifact/business requirement within authorized scope."})
    commands = [r for r in results if r["kind"] == "command" and (not r["verified"] or output_failed and
                any("command:" + c.id == r["id"] and c.verify_final for c in (contract.commands if contract else [])))]
    for result in commands:
        steps.append({"kind": "act", "requirement": result["id"], "action": result["required_action"],
                      "instruction": "Execute the declared command with these exact required arguments; verify its exit/output."})
    legacy = [r for r in failed if r["kind"] in {"tool_success", "tool_result", "evidence"}]
    for result in legacy:
        steps.append({"kind": "verify_criterion", "requirement": result["id"], "tool": result.get("tool"),
                      "artifact_paths": result.get("artifact_paths", []), "issues": result.get("issues", []),
                      "instruction": "Obtain actual evidence satisfying this declared criterion on the final versions. "
                          "A generating mutation is not stable verification. Use an authorized final check; "
                          "do not automatically replay a generator or invent argv, results or IDs."})
    if last_failed:
        steps.append({"kind": "recover_last_action", "instruction": "Inspect the latest failure and recover or ask; "
                      "successful older receipts do not override it. Do not automatically replay a side effect."})
    reads = [r["path"] for r in failed if r["kind"] == "read"]
    if output_failed and contract:
        reads += contract.required_reads
    if contract:
        reads += [a.path for a in contract.artifacts if a.require_readback and (commands or legacy or output_failed or
                  any(r["id"] == "readback:" + a.id and not r["verified"] for r in results))]
    for path in dict.fromkeys(reads):
        steps.append({"kind": "act", "action": {"name": "read_file", "arguments": {"path": path}},
                      "instruction": "Read the current complete file after the commands; later mutations invalidate final readback."})
    if state.plan and not all(step.done for step in state.plan.steps):
        steps.append({"kind": "complete_step", "instruction": "Submit the active approved step with actual successful evidence."})
    steps.append({"kind": "respond", "instruction": "Submit actual evidence_ids and required criteria_evidence; never invent or automatically bind IDs."})
    return steps[:40]


def completion_guidance(gate, state, history):
    # Do not encourage environment work before approval, after changed input, or
    # when an interrupted intent still needs explicit resolution.
    if (not (state.completion_contract or state.success_criteria or state.plan) or not history.observations or state.contract_invalidated
            or state.requires_replan or state.pending_approval or state.mode.value == "plan" and
            (not state.plan or state.plan.approved_revision != state.plan.revision)):
        return None
    from kong.contracts import FinalResponse
    probe = FinalResponse(content="verification status only")
    problems = gate.check(state, probe, history)
    feedback = rejection_feedback(gate, state, probe, history, problems)
    feedback["instruction"] = ("This is current read-only verification status, not a completion decision. "
        "Follow runtime phase/approval first. When no further tool is required, submit a corrected respond using actual IDs. "
        "Do not repeat already verified work. No action or evidence is selected on your behalf.")
    return feedback


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
        next_action = "Follow the failed requirement issues and remaining_steps. Run required commands before final readback; use the declared fresh_python flag. Do not repeat verified work or mutate protected sources."
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
            "requirement_issues": [{"id": r["id"], "issues": r.get("issues", [])} for r in failed][:30],
            "remaining_steps": remaining_steps(state, results, failed, output_failed,
                acceptance_required=any(p.startswith("Acceptance conditions required:") for p in problems),
                last_failed=bool(history.observations and not history.observations[-1].success)),
            "current_valid_observations": sorted(valid)[-12:],
            "historical_event_observations": sorted(historical)[-12:],
            "available_successful_observations": successful[-12:],
            "stale_observations": sorted(related - valid - historical)[-12:],
            "criterion_observations": {key: sorted(set(ids))[-12:] for key, ids in list(by_criterion.items())[:100]},
            "artifact_mutation_required": mutation, "additional_tool_required": additional_tool,
            "next_action": next_action, "evidence_rule": "IDs are candidates for your submission, never automatically selected or bound. Historical receipts do not prove current artifacts."}
