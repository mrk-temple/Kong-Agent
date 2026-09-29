"""Offline demo: an interrupted action with unknown outcome, human /resolve, no replay.

Mirrors the unknown_action_restore evaluation case, then drives the same run to
completion with a scripted model. No model API, no network. Prints JSON evidence
and exits 0 only when every boundary holds.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from kong.contracts import Act, Action, Criterion, FinalResponse, Status
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = "input.txt"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def demo(workspace_root: Path) -> dict:
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(workspace_root)
    store = RunStore(workspace_root / ".kong" / "runs")
    registry = default_registry(workspace)
    (workspace_root / PROTECTED).write_text("protected\n", encoding="utf-8")
    protected_before = sha256_file(workspace_root / PROTECTED)

    model = ScriptedModel([
        Act(actions=[Action(name="write_file", arguments={
            "path": "output.txt", "content": "RESOLVED AND WRITTEN\n"})]),
        FinalResponse(content="中断已由人工解决，重新提案的动作已执行并核验。", evidence_ids=["a2"]),
    ])
    runtime = Runtime.new("interrupted action demo", model, workspace, registry, store=store)
    runtime.state.success_criteria = [Criterion(
        id="c1", description="output.txt contains the resolved marker",
        kind="file_contains", path="output.txt", contains="RESOLVED AND WRITTEN")]

    # Inject an action whose outcome is unknown: intent saved, execution never observed.
    runtime.history.append("action_intent", 0, action_id="a1",
                           action=Action(name="write_file", arguments={
                               "path": "canary.txt", "content": "DUPLICATE"}).model_dump())
    runtime.pending_action_id = "a1"
    runtime.save()
    runtime = Runtime.restore(store.load(runtime.state.run_id), model, workspace, registry, store=store)

    await runtime.run()
    paused = {
        "status_after_restore_run": runtime.state.status.value,
        "pending_action_id": runtime.pending_action_id,
        "observations": len(runtime.history.observations),
        "model_calls": runtime.metrics.model_calls,
        "canary_exists": (workspace_root / "canary.txt").exists(),
        "output_exists_before_resolve": (workspace_root / "output.txt").exists(),
    }

    # Any other human command is refused while the interrupted action is unresolved.
    refused = False
    try:
        runtime.human("answer", text="try to continue anyway")
    except ValueError:
        refused = True

    runtime.resolve_interrupted_action(
        "Inspected the workspace: canary.txt was never created; the write did not execute.")
    await runtime.run()

    protected_after = sha256_file(workspace_root / PROTECTED)
    evidence = {
        "workspace": str(workspace_root),
        "run_id": runtime.state.run_id,
        "paused": paused,
        "other_command_refused_while_unresolved": refused,
        "final_status": runtime.state.status.value,
        "stop_reason": runtime.state.stop_reason,
        "pending_action_cleared": runtime.pending_action_id is None,
        "model_calls": runtime.metrics.model_calls,
        "action_intents": [e.payload.get("action_id") for e in runtime.history.events
                           if e.type == "action_intent"],
        "observations": len(runtime.history.observations),
        "canary_never_created": not (workspace_root / "canary.txt").exists(),
        "output_exists": (workspace_root / "output.txt").exists(),
        "output_content_ok": (workspace_root / "output.txt").exists()
        and "RESOLVED AND WRITTEN" in (workspace_root / "output.txt").read_text(encoding="utf-8"),
        "protected_file_unchanged": protected_before == protected_after,
        "gate_passed": runtime.state.stop_reason == "goal_completed",
    }
    checks = {
        "paused_waiting_user": paused["status_after_restore_run"] == Status.WAITING_USER.value,
        "paused_with_pending_action": paused["pending_action_id"] == "a1",
        "zero_replay": paused["observations"] == 0 and not paused["canary_exists"],
        "zero_model_calls_before_resolve": paused["model_calls"] == 0,
        "refused_other_commands": refused,
        "completed_after_resolve": runtime.state.status.value == Status.COMPLETED.value,
        "completion_gate_passed": evidence["gate_passed"],
        "action_ids_never_reused": evidence["action_intents"] == ["a1", "a2"],
        "protected_unchanged": evidence["protected_file_unchanged"],
    }
    evidence["checks"] = checks
    evidence["all_passed"] = all(checks.values())
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path,
                        help="Workspace directory (default: a fresh dir under .kong/demos/)")
    args = parser.parse_args()
    root = (args.workspace or ROOT / ".kong" / "demos" / f"interrupted-{uuid4().hex[:8]}").resolve()
    evidence = asyncio.run(demo(root))
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
