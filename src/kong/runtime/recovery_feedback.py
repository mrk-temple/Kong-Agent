"""Version-specific recovery advice; never dispatch or replay a failed write."""
import hashlib

from kong.runtime.completion import stable_bytes


def patch_failure_guidance(state, history, workspace):
    if state.pending_approval or state.requires_replan or state.mode.value == "plan" and (
            not state.plan or state.plan.approved_revision != state.plan.revision):
        return None
    patches = [o for o in history.observations if o.action.name == "patch_file"]
    if not patches or patches[-1].success or patches[-1].outcome_unknown:
        return None
    latest = patches[-1]
    diagnostic = latest.output if isinstance(latest.output, dict) else {}
    if diagnostic.get("code") not in {"patch.match_count", "patch.no_change"}:
        return None
    path = latest.action.arguments.get("path")
    try:
        data = stable_bytes(workspace, path)
        current = hashlib.sha256(data).hexdigest()
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, ValueError, UnicodeError):
        return None
    if current != diagnostic.get("source_sha256"):
        return None
    count = sum(not o.success and isinstance(o.output, dict) and o.output.get("source_sha256") == current
                and o.action.arguments.get("path") == path
                and o.action.arguments.get("old_text") == latest.action.arguments.get("old_text") for o in patches)
    reads = []
    for observation in history.observations:
        if not observation.success or observation.action.name != "read_file":
            continue
        try:
            matches = workspace.resolve(observation.action.arguments.get("path", "")) == workspace.resolve(path)
        except (OSError, ValueError):
            matches = False
        if matches and observation.output == text:
            reads.append(observation.action_id)
    return {"path": path, "source_sha256": current, "failed_observation": latest.action_id,
        "same_selector_failures": count, "current_read_candidates": reads[-3:], "code": diagnostic["code"],
        "next_action": "If modification is still required, use a unique literal from the current read. Change the failed selector; another unchanged read does not repair it. Preserve unrelated edits. Never replay a write or overwrite a whole file merely to bypass a mismatch."}
