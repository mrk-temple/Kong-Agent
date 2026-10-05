"""Verify actual owned resource receipts; never dispatch or synthesize an ID."""


def closure_result(requirement, history):
    tool = requirement.tool
    key, opens = {"terminal": ("session", {"start"}), "browser": ("page", {"open"}),
                  "mcp": ("server", {"list", "call"})}[tool]
    opened, unknown, closed = {}, False, []
    for obs in history.observations:
        if obs.action.name != tool or not obs.dispatched:
            continue
        op = obs.action.arguments.get("operation")
        if op not in opens | {"close"}:
            continue
        if obs.outcome_unknown:
            unknown = True
            continue
        if op in opens:
            # MCP list/call lazily opens a connection named by the caller's
            # configured server argument. Even a failed request may own it;
            # server enumeration alone opens nothing. Never infer a close.
            rid = (obs.action.arguments.get(key) if tool == "mcp" else
                   obs.output.get(key) if isinstance(obs.output, dict) else None)
            if isinstance(rid, str) and rid:
                opened[rid] = None  # Navigating/reopening cannot reuse an earlier close.
            elif obs.success:
                unknown = True
        else:
            if not obs.success or not isinstance(obs.output, dict):
                continue
            rid = obs.action.arguments.get(key)
            if isinstance(rid, str) and rid in opened and obs.output.get("closed") == rid:
                opened[rid] = obs.action_id
    missing = [rid for rid, receipt in opened.items() if receipt is None]
    closed = list(dict.fromkeys(receipt for receipt in opened.values() if receipt is not None))
    issues = []
    if not opened:
        issues.append({"code": "cleanup.open_not_observed", "tool": tool})
    if unknown:
        issues.append({"code": "cleanup.effect_unknown", "tool": tool})
    if missing:
        issues.append({"code": "cleanup.resources_open", "tool": tool, "resources": missing})
    actions = [] if unknown else [{"name": tool, "arguments": {"operation": "close", key: rid}} for rid in missing]
    return not issues, closed, issues, actions
