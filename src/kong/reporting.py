"""Deterministic run summaries, kept separate from the model's completion claim."""
from datetime import datetime, timezone
import json
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
        if c.kind in {"file_exists", "file_contains", "file_equals", "file_json"} and c.path:
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
    metrics["runtime_overhead_seconds"] = (round(max(0, runtime.metrics.active_seconds -
        runtime.metrics.model_seconds - runtime.metrics.tool_wall_seconds), 6)
        if runtime.metrics.tool_timing_complete else None)
    metrics["timing_scope"] = ("Active driver time excludes time paused for humans and final report I/O. Model time includes request/stream and its event I/O. "
        "Tool elapsed is executor validation/authorization/execution/cleanup, excluding timing checkpoint I/O; cumulative time sums overlapping calls. "
        "Tool wall time measures batch dispatch/checkpoint/execution/draining. Runtime overhead is the remaining active driver time, "
        "including context, evidence hashes, persistence and retry waits. Historical/inflight missing durations remain unknown.")
    metrics["usage_complete"] = (not metrics["legacy_unknown"] and metrics["usage_calls"] == metrics["model_calls"] == state.turn_count)
    metrics["executor_seconds"] = metrics["tool_seconds"]
    for key in ("context_build_seconds", "evidence_hash_seconds", "snapshot_seconds", "retry_wait_seconds",
                "validation_seconds", "tool_wait_seconds", "hash_calls", "hashed_file_count", "hashed_bytes",
                "snapshot_calls", "artifact_count"):
        if metrics["attribution_coverage"] == "unmeasured":
            metrics[key] = None
        elif key.endswith("_seconds"):
            metrics[key] = round(metrics[key], 6)
    metrics["history_event_count"] = len(history.events)
    metrics["run_event_count"] = len(runtime.run_events)
    metrics["observation_count"] = len(history.observations)
    metrics["history_bytes"] = len(json.dumps(history.model_dump(mode="json"), ensure_ascii=False,
                                               separators=(",", ":")).encode("utf-8"))
    metrics["attribution_note"] = ("Measured spans overlap: validation can include hashing; model/tool waits can include validation and event persistence. "
        "Do not sum these spans. Hash counters count visits and bytes read, including repeated files; artifact_count is the last capture scope size. "
        "Measurements cover this driver, direct action batches and runtime saves; restored history is partial. "
        "The saved snapshot lags its own final serialization duration/size by one save; this live report records the last completed save. "
        "Report generation is excluded. history_bytes is one compact UTF-8 serialization at report time.")
    progress = {"activity_count":state.progress_state.progress_count,
                "verified_criteria_count":sum(c["verified"] for c in criteria), "declared_criteria_count":len(criteria),
                "goal_progress_measured":False,
                "note":"Unique successful outputs currently earn activity credit. This is not verified goal progress; lease policy is unchanged."}
    return {"schema_version":1, "generated_at":datetime.now(timezone.utc).isoformat(),
        "run_id":state.run_id, "status":state.status.value, "goal":state.goal,
        "model_claim":state.final_output, "stop_reason":state.stop_reason,
        "pending_question":state.pending_question, "pending_action_id":runtime.pending_action_id,
        "pending_action_ids":runtime.pending_action_ids,
        "last_event_sequence":runtime.run_events[-1].sequence if runtime.run_events else None,
        "artifacts":files, "criteria":criteria,
        "unverified":[c["description"] for c in criteria if not c["verified"]] +
            (["未配置确定性验收条件；成功工具证据不等于目标语义正确。"] if not criteria else []),
        "scope_note":"仅检查显式条件与已记录证据；未声明的任务要求、视觉质量、工具外写入未自动验证。文件状态以摘要生成时间为准。",
        "metrics":metrics, "progress":progress,
        "failed_tools":[{"id":o.action_id,"tool":o.action.name,"error":o.error} for o in history.observations if not o.success],
        "model_errors":[e.payload.get("error") for e in history.events if e.type == "model_error"],
        "resource_handles_in_history":{k:sorted(v) for k,v in resource_handles(history).items()},
        "resource_note":"历史句柄不保证仍存活；CLI 退出时清理，重启后需重新检查。",
        "recovery_warnings":[e.payload["unavailable_resources"] for e in history.events if "unavailable_resources" in e.payload]}
