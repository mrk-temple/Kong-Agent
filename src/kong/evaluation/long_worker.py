"""Fresh-process continuation at an acknowledged safe handoff; no write replay."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time

import httpx
from pydantic import SecretStr

from kong.config import ModelConfig
from kong.contracts import NeedUserInput
from kong.environments.docker import DockerBackend
from kong.models.compatible import CompatibleModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.tools.permissions import PermissionPolicy
from kong.workspace import Workspace

from .long_driver import HANDOFF_QUESTION, PROTOCOL, ObservedEngine, ObservedModel, ScenarioDriver, TaskLedger, _write_json, drive_stage, handoff_problems, restored_read_evidence
from .long_fixtures import Fixture, act
from .scenarios import Scenario


async def resume(request_path):
    data = json.loads(request_path.read_text(encoding="utf-8"))
    if data["protocol"] != PROTOCOL:
        raise ValueError("Worker protocol differs from the frozen caller")
    scenario = Scenario.from_manifest(data["scenario_manifest"])
    if scenario.id != data["scenario"]:
        raise ValueError("Worker scenario identity mismatch")
    workspace = Workspace(Path(data["workspace"]))
    store = RunStore(workspace.root / ".kong" / "runs")
    start = time.perf_counter()
    snapshot = store.load(data["run_id"])
    load_seconds = time.perf_counter() - start
    if snapshot.pending_action_ids or snapshot.pending_action_id or snapshot.state.status.value != "waiting_user":
        raise ValueError("Worker requires a paused Run with no pending effect")
    ledger = TaskLedger(scenario.budget, live=data["live"])
    for key in ("runs", "requests", "decisions", "known_tokens", "estimated_tokens", "unknown_token_upper", "usage_complete", "active_seconds", "calls"):
        setattr(ledger, key, data["ledger"][key])
    driver = ScenarioDriver(scenario, request_path.parent)
    driver.run_number = 1
    driver.interventions = data["interventions"]
    driver.applied = set(data["applied"])
    driver.revision = len(driver.applied)
    driver.compactions = data["compactions"]
    driver.contexts = data["contexts"]
    driver.bound_revision = data["bound_revision"]
    driver.pauses = data["pauses"]
    from kong.continuity.contracts import ContextBudget
    engine = ObservedEngine(budget=ContextBudget.model_validate(data["budget"]))
    backend = DockerBackend(workspace, "task", data["image"])
    registry = default_registry(workspace, allow_process=True, process_backend=backend)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        config = ModelConfig(**data["model"], api_key=SecretStr(os.environ.get("KONG_LONG_WORKER_API_KEY", ""))) if data["live"] else None
        inner = CompatibleModel(config, client) if config else Fixture([
            act("read_file", path=scenario.artifacts[0]),
            NeedUserInput(question=HANDOFF_QUESTION)])
        model = ObservedModel(inner, ledger, driver, config.max_tokens if config else 4096)
        runtime = Runtime.restore(snapshot, model, workspace, registry, store=store,
            permissions=PermissionPolicy.model_validate(data["permissions"]), context_engine=engine, on_event=driver.event)
        driver.runtime = runtime
        if handoff_problems(runtime):
            raise ValueError("Worker requires a verified stage handoff; cannot resume an arbitrary pause")
        before_observations = len(runtime.history.observations)
        engine.restore_observation_floor = before_observations
        engine.restore_artifact_paths = scenario.artifacts
        runtime.human("answer", text="Resume from the saved handoff in this new process. Check the current files and effects. Then ask for handoff to a new Run; do not replay prior writes.")
        ledger._active_started = time.perf_counter()
        try:
            await drive_stage(runtime, driver, ledger, runtime.state.completion_contract)
        finally:
            ledger.active_seconds = ledger.elapsed
            ledger._active_started = None
            runtime.save()
            _write_json(request_path.with_name("restart-result.json"), {
                "pid": os.getpid(), "run_id": runtime.state.run_id, "load_seconds": load_seconds,
                "ledger": {key: getattr(ledger, key) for key in data["ledger"]},
                "interventions": driver.interventions, "applied": sorted(driver.applied),
                "compactions": driver.compactions, "contexts": driver.contexts, "limit_reason": driver.limit_reason,
                "bound_revision": driver.bound_revision, "pauses": driver.pauses,
                "restored_artifact_read": restored_read_evidence(runtime.state, runtime.history,
                    workspace, scenario.artifacts, before_observations)})


if __name__ == "__main__":
    asyncio.run(resume(Path(sys.argv[1])))
