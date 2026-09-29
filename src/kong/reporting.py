"""Deterministic run summaries, kept separate from the model's completion claim."""
from datetime import datetime, timezone
from kong.runtime.gates import CompletionGate


def resource_handles(history):
    handles = {"terminal":set(), "browser":set(), "mcp":set()}
    for obs in history.observations:
        if not obs.success or obs.action.name not in handles:
            continue
        name, args = obs.action.name, obs.action.arguments
        output = obs.output if isinstance(obs.output, dict) else {}
        key = output.get("session") if name == "terminal" else output.get("page") if name == "browser" else args.get("server")
        if args.get("operation") == "close":
            handles[name].discard(args.get({"terminal":"session", "browser":"page", "mcp":"server"}[name]))
        elif key:
            if name == "terminal" and not output.get("running", False):
                handles[name].discard(key)
            else:
                handles[name].add(key)
    return handles


def run_report(runtime):
    state, history = runtime.state, runtime.history
    last = next((e.payload["decision"] for e in reversed(history.events)
                 if e.type == "decision" and e.payload.get("decision", {}).get("kind") == "respond"), {})
    criteria = CompletionGate(runtime.workspace).criteria_results(state, history, last.get("criteria_evidence", {}))
    artifacts = {}
    for obs in history.observations:
        if not obs.success:
            continue
        if obs.action.name in {"write_file", "patch_file"} or (obs.action.name == "browser" and obs.action.arguments.get("operation") == "screenshot"):
            path = obs.action.arguments.get("path")
            if path:
                artifacts[path] = obs.action_id
    for c in state.success_criteria + (state.plan.success_criteria if state.plan else []):
        if c.kind in {"file_exists", "file_contains"} and c.path:
            artifacts.setdefault(c.path, None)
    files = []
    for path, evidence in artifacts.items():
        try:
            resolved = runtime.workspace.resolve(path)
            files.append({"path":str(resolved), "exists":resolved.is_file(), "evidence_id":evidence})
        except (ValueError, OSError):
            continue
    metrics = runtime.metrics.model_dump()
    metrics["active_seconds"] = round(metrics["active_seconds"], 3)
    metrics["model_seconds"] = round(metrics["model_seconds"], 3)
    metrics["usage_complete"] = (not metrics["legacy_unknown"] and metrics["usage_calls"] == metrics["model_calls"] == state.turn_count)
    return {"schema_version":1, "generated_at":datetime.now(timezone.utc).isoformat(),
        "run_id":state.run_id, "status":state.status.value, "goal":state.goal,
        "model_claim":state.final_output, "stop_reason":state.stop_reason,
        "pending_question":state.pending_question, "pending_action_id":runtime.pending_action_id,
        "artifacts":files, "criteria":criteria,
        "unverified":[c["description"] for c in criteria if not c["verified"]] +
            (["未配置确定性验收条件；成功工具证据不等于目标语义正确。"] if not criteria else []),
        "scope_note":"仅检查显式条件与已记录证据；未声明的任务要求、视觉质量、工具外写入未自动验证。文件状态以摘要生成时间为准。",
        "metrics":metrics, "failed_tools":[{"id":o.action_id,"tool":o.action.name,"error":o.error} for o in history.observations if not o.success],
        "model_errors":[e.payload.get("error") for e in history.events if e.type == "model_error"],
        "resource_handles_in_history":{k:sorted(v) for k,v in resource_handles(history).items()},
        "resource_note":"历史句柄不保证仍存活；CLI 退出时清理，重启后需重新检查。",
        "recovery_warnings":[e.payload["unavailable_resources"] for e in history.events if "unavailable_resources" in e.payload]}
