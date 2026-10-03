"""Offline serial/concurrent and input-boundary comparison with all run records.

The file-read delay is deliberate, fixed simulated I/O latency. It does not
measure disk throughput or make a general model performance claim.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kong.contracts import Act, Action, NeedUserInput, Status
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.builtin.read_file import ReadFileTool
from kong.tools.registry import ToolRegistry
from kong.workspace import Workspace


class DelayedRead(ReadFileTool):
    def _run(self, **kwargs):
        time.sleep(0.1)
        return super()._run(**kwargs)


async def compare(destination):
    destination.mkdir(parents=True, exist_ok=False)
    rows = []
    for concurrency in [1, 3]:
        root = destination / f"reads-{concurrency}"
        root.mkdir()
        names = [f"input-{i}.txt" for i in range(6)]
        for i, name in enumerate(names):
            (root / name).write_text(f"unseen value {i}", encoding="utf-8")
        workspace = Workspace(root)
        registry = ToolRegistry()
        registry.register(DelayedRead(workspace))
        model = ScriptedModel([Act(actions=[Action(name="read_file", arguments={"path": n}) for n in names]),
                               NeedUserInput(question="comparison complete")])
        runtime = Runtime.new("Read six independent files", model, workspace, registry,
            read_concurrency=concurrency, store=RunStore(root / ".kong" / "runs"))
        started = time.perf_counter()
        await runtime.run()
        rows.append({"concurrency": concurrency, "seconds": round(time.perf_counter() - started, 4),
            "metrics": runtime.metrics.model_dump(mode="json"),
            "results": [{"id": o.action_id, "path": o.action.arguments["path"], "output": o.output, "success": o.success}
                        for o in runtime.history.observations], "run_id": runtime.state.run_id,
            "status": runtime.state.status.value})
    root = destination / "interactive"
    root.mkdir()
    (root / "one.txt").write_text("one")
    (root / "tail.txt").write_text("tail")
    workspace = Workspace(root)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace))
    model = ScriptedModel([Act(actions=[Action(name="read_file", arguments={"path": p}) for p in ["one.txt", "tail.txt"]]),
                           NeedUserInput(question="new constraint observed")])
    runtime = Runtime.new("read", model, workspace, registry, read_concurrency=1,
                          store=RunStore(root / ".kong" / "runs"))
    def event(kind, data):
        if kind == "action_finished" and data["id"] == "a1":
            runtime.submit_input("constraint", "Stop the old batch and ask for review")
    runtime.on_event = event
    await runtime.run()
    same = rows[0]["results"] == rows[1]["results"]
    output = {"schema": 1, "fixture_io_delay_seconds": 0.1, "reads": rows,
              "identical_results": same, "speedup": round(rows[0]["seconds"] / rows[1]["seconds"], 3),
              "input_revoked_tail": len(runtime.history.observations) == 1,
              "input_in_next_context": "Stop the old batch" in json.dumps(model.contexts[-1].messages),
              "interactive_status": runtime.state.status.value,
              "scope": "Controlled fixture with fixed I/O delay; not live model throughput or general disk performance"}
    output["all_passed"] = same and output["speedup"] > 1.5 and output["input_revoked_tail"] and output["input_in_next_context"] and runtime.state.status == Status.WAITING_USER
    (destination / "comparison.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True, help="A new local directory; existing evidence is never overwritten")
    args = parser.parse_args()
    result = asyncio.run(compare(args.output))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
