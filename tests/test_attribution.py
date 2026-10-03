import asyncio

from kong.contracts import Act, Action, Criterion, NeedUserInput, Status
from kong.models.fake import ScriptedModel
from kong.reporting import run_report
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def test_driver_attributes_real_context_hash_persistence_and_waits(tmp_path):
    class DelayedModel(ScriptedModel):
        async def generate(self, context):
            await asyncio.sleep(0.03)
            return await super().generate(context)
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    workspace = Workspace(tmp_path)
    model = DelayedModel([Act(actions=[Action(name="read_file", arguments={"path": "source.txt"})]),
                          NeedUserInput(question="review")])
    runtime = Runtime.new("read", model, workspace, default_registry(workspace),
                          store=RunStore(tmp_path / ".kong/runs"))
    runtime.state.success_criteria = [Criterion(id="read", description="read source", kind="tool_success",
                                               tool_name="read_file", artifact_paths=["source.txt"])]
    assert asyncio.run(runtime.run()).status == Status.WAITING_USER
    metrics = run_report(runtime)["metrics"]
    assert metrics["attribution_coverage"] == "current_driver"
    assert metrics["model_seconds"] >= 0.06
    assert metrics["context_build_seconds"] > 0 and metrics["validation_seconds"] > 0
    assert metrics["evidence_hash_seconds"] > 0 and metrics["hash_calls"] >= 2
    assert metrics["hashed_file_count"] >= 2 and metrics["hashed_bytes"] >= 12
    assert metrics["artifact_count"] == 1
    assert metrics["snapshot_seconds"] > 0 and metrics["snapshot_calls"] > 0
    assert metrics["snapshot_bytes"] == runtime.store.path(runtime.state.run_id).stat().st_size
    assert metrics["tool_wait_seconds"] > 0 and metrics["retry_wait_seconds"] == 0
    assert metrics["executor_seconds"] == metrics["tool_seconds"]
    assert metrics["history_bytes"] > 0 and metrics["history_event_count"] == len(runtime.history.events)
    assert runtime.store.load(runtime.state.run_id).metrics.hash_calls == runtime.metrics.hash_calls


def test_legacy_attribution_stays_unknown_when_restored(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("old", ScriptedModel([]), workspace, default_registry(workspace))
    snapshot = runtime.snapshot().model_dump(mode="json")
    snapshot.pop("metrics")
    from kong.storage import Snapshot
    restored = Runtime.restore(Snapshot.model_validate(snapshot), ScriptedModel([]), workspace, runtime.registry)
    metrics = run_report(restored)["metrics"]
    assert metrics["attribution_coverage"] == "unmeasured"
    assert metrics["context_build_seconds"] is None and metrics["hashed_bytes"] is None
    assert metrics["snapshot_bytes"] is None


def test_parallel_metric_scopes_never_credit_other_runs():
    from kong.contracts import RunMetrics
    from kong.runtime.metrics import count, metrics_scope
    async def task(metrics, amount):
        with metrics_scope(metrics):
            count("hashed_bytes", amount)
            await asyncio.sleep(0)
            count("hashed_bytes", amount)
    async def scenario():
        one, two = RunMetrics(), RunMetrics()
        await asyncio.gather(task(one, 3), task(two, 7))
        count("hashed_bytes", 100)
        assert (one.hashed_bytes, two.hashed_bytes) == (6, 14)
    asyncio.run(scenario())


def test_activity_does_not_claim_verified_goal_progress(tmp_path):
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("produce target", ScriptedModel([]), workspace, default_registry(workspace))
    runtime.state.success_criteria = [Criterion(id="target", description="deliver target", kind="file_exists",
                                               path="target.txt")]
    for index in range(6):
        (tmp_path / f"unrelated{index}.txt").write_text(str(index))
    asyncio.run(runtime._actions(Act(actions=[Action(name="read_file", arguments={"path": f"unrelated{i}.txt"})
                                                for i in range(6)])))
    progress = run_report(runtime)["progress"]
    assert progress["activity_count"] == 6 and progress["verified_criteria_count"] == 0
    assert progress["goal_progress_measured"] is False
