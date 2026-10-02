"""Offline, fixed-input before/after probes. Never calls a model service.

Run in separate processes against each checkout; output directories are unique.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repository.resolve() / "src"))
    from kong.contracts import Act, Action, Criterion, FinalResponse
    from kong.continuity.memory import source_for
    from kong.continuity.memory_contracts import MemoryDelta, MemoryMutation
    from kong.models.fake import ScriptedModel
    from kong.runtime.loop import Runtime
    from kong.storage import RunStore
    from kong.tools.defaults import default_registry
    from kong.workspace import Workspace
    from kong.config import ModelConfig
    from kong.models.compatible import CompatibleModel
    from kong.models.base import ModelProtocolError
    from kong.context import Context
    import httpx

    root = args.output.resolve() / uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    workspace_path = root / "workspace"
    workspace_path.mkdir()
    package = args.repository.resolve() / "src/kong"
    resources = {p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(package.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}

    async def scenario():
        workspace = Workspace(workspace_path)
        registry = default_registry(workspace, allow_process=True)
        runtime = Runtime.new("hello", ScriptedModel([FinalResponse(content="ok")]), workspace, registry,
                              store=RunStore(workspace_path / ".kong/runs"))
        await runtime.run()
        state = runtime.context_engine.store.state(runtime.thread.thread_id)
        memory = runtime.context_engine.manager.merge(MemoryDelta(thread_id=state.thread_id, base_revision=state.revision,
            mutations=[MemoryMutation(key="unsupported", content="The user owns a lunar base",
                sources=[source_for(state.thread_id, runtime.state.run_id, 0)])]))
        status = memory.items[-1].status.value
        (workspace_path / "artifact.txt").write_text("good", encoding="utf-8")
        runtime.state.success_criteria = [Criterion(id="check", description="check artifact", kind="tool_success",
                                                    tool_name="process_exec", after_last_write=True)]
        for code in ["from pathlib import Path; assert Path('artifact.txt').read_text() == 'good'",
                     "from pathlib import Path; Path('artifact.txt').write_text('bad')"]:
            await runtime._actions(Act(actions=[Action(name="process_exec", arguments={"argv": ["python", "-c", code]})]))
        gate_errors = runtime.controller.completion_gate.check(runtime.state,
            FinalResponse(content="done", evidence_ids=[runtime.history.observations[-1].action_id]), runtime.history)
        secret = "SYNTHETIC_SECRET_DO_NOT_LOG"
        bad = {"kind": "plan", "plan": {"goal": "g", "rationale": "r", "steps": [{"id": "s", "title": {secret: secret}}],
               "success_criteria": [{"id": "c", "description": "d"}]}, secret: secret}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
                json={"choices": [{"message": {"content": json.dumps(bad)}}]}))) as client:
            try:
                await CompatibleModel(ModelConfig(base_url="http://localhost/v1", model="mock"), client).generate(Context([]))
                diagnostic = "unexpected accepted response"
            except ModelProtocolError as error:
                diagnostic = str(error)
        runtime.save()
        await registry.aclose()
        return {
            "model_service_calls": 0,
            "cases": {
                "unrelated_source": {"pass": status == "candidate", "observed_status": status},
                "process_change_after_check": {"pass": bool(gate_errors), "completion_gate_errors": gate_errors,
                    "actions_succeeded": [o.success for o in runtime.history.observations],
                    "final_artifact": (workspace_path / "artifact.txt").read_text()},
                "diagnostic_field_path": {"pass": "$.plan.steps[].title" in diagnostic, "safe_diagnostic": diagnostic},
                "diagnostic_privacy": {"pass": secret not in diagnostic},
            },
            "history_path": str(runtime.store.path(runtime.state.run_id).relative_to(root)),
        }

    result = asyncio.run(scenario())
    result.update(schema="v04a-counterexamples-1", fixture_only=True, resources=resources,
                  human_intervention={"measured": False, "count": None},
                  probe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    with (root / "result.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"result": str(root / "result.json"), "cases": result["cases"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
