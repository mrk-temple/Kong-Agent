import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from kong.evaluation.long_driver import ScenarioDriver, TaskLedger, TaskLimit, completion_contract, run_trial
from kong.evaluation.long_graders import check_data, data_oracle, digest_file, grade
from kong.evaluation.scenarios import CODE_SCENARIO, DATA_SCENARIO, TaskBudget
from kong.models.fake import ScriptedModel
from kong.models.base import ModelError
from kong.tools.base import ToolResult


def seed(folder, scenario):
    for name, content in scenario.files:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return {p: digest_file(folder / p) for p in scenario.protected}


def put_data(folder, source, *, rows=None, report_rows=None):
    rows = data_oracle(source) if rows is None else rows
    report_rows = rows if report_rows is None else report_rows
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.csv").write_text("region,net_amount\n" + "".join(f"{r['region']},{r['net_amount']}\n" for r in rows), encoding="utf-8")
    total = sum((Decimal(r["net_amount"]) for r in report_rows), Decimal(0))
    (folder / "report.md").write_text("# Data report\n## Rules\nSource orders.csv, last physical row wins.\n## Results\n" +
        "".join(f"{r['region']}: {r['net_amount']}\n" for r in report_rows) + f"TOTAL: {total:.2f}\n## Unverified\nExternal behavior unverified.\n", encoding="utf-8")
    (folder / "process.py").write_text("pass\n", encoding="utf-8")


def source():
    return dict(DATA_SCENARIO.files)["task/orders.csv"]


def test_independent_oracle_handles_last_void_signed_refund_and_cents():
    text = source() + DATA_SCENARIO.appended_input[1]
    assert data_oracle(text) == [{"region": "East", "net_amount": "1.50"},
        {"region": "North", "net_amount": "12.05"}, {"region": "West", "net_amount": "0.20"}]


def test_correct_data_and_report_are_accepted(tmp_path):
    put_data(tmp_path, source())
    assert all(check_data(tmp_path, source()).values())


@pytest.mark.parametrize("kind", ["wrong_groups_same_total", "wrong_report", "duplicate", "missing", "wrong_precision"])
def test_data_grader_rejects_plausible_wrong_outputs(tmp_path, kind):
    rows = data_oracle(source())
    report = None
    if kind == "wrong_groups_same_total":
        rows = [{"region": "East", "net_amount": "3.10"}, {"region": "West", "net_amount": "1.50"}]
    elif kind == "wrong_report":
        report = [{"region": "East", "net_amount": "0.00"}]
    elif kind == "duplicate":
        rows = rows + rows
    elif kind == "missing":
        rows = rows[:1]
    elif kind == "wrong_precision":
        rows[0]["net_amount"] = "1.5"
    put_data(tmp_path, source(), rows=rows, report_rows=report)
    assert not all(check_data(tmp_path, source()).values())


def test_existing_grade_does_not_accept_later_artifact_edit(tmp_path):
    put_data(tmp_path, source())
    assert all(check_data(tmp_path, source()).values())
    old = digest_file(tmp_path / "summary.csv")
    (tmp_path / "summary.csv").write_text("region,net_amount\nEast,0.00\n", encoding="utf-8")
    assert old != digest_file(tmp_path / "summary.csv")
    assert not all(check_data(tmp_path, source()).values())


def test_task_budget_does_not_reset_across_runs_or_model_retries():
    ledger = TaskLedger(TaskBudget(requests=2, total_tokens=1000))
    packet = SimpleNamespace(estimated_input_tokens=20)
    ledger.begin_run()
    ledger.before_request(packet, 10)
    ledger.record_usage({"total_tokens": 7})
    ledger.begin_run()
    ledger.before_request(packet, 10)
    ledger.record_usage(None)  # a failed/retried provider request still consumed a slot
    with pytest.raises(TaskLimit, match="request_budget"):
        ledger.before_request(packet, 10)
    assert ledger.requests == 2 and ledger.runs == 2
    assert ledger.report()["actual_total_tokens"] is None
    assert ledger.report()["unknown_request_token_upper"] == 30


def test_known_usage_replaces_reservation_without_becoming_estimate():
    ledger = TaskLedger(TaskBudget(total_tokens=100))
    packet = SimpleNamespace(estimated_input_tokens=60)
    ledger.before_request(packet, 10)
    ledger.record_usage({"total_tokens": 20})
    ledger.before_request(packet, 10)
    ledger.record_usage({"total_tokens": 20})
    assert ledger.report()["actual_total_tokens"] == 40
    assert ledger.report()["estimated_request_token_upper"] == 140


def test_unknown_tokens_enforce_conservative_cap_before_next_request():
    ledger = TaskLedger(TaskBudget(total_tokens=100))
    packet = SimpleNamespace(estimated_input_tokens=60)
    ledger.before_request(packet, 10)
    ledger.record_usage({})
    with pytest.raises(TaskLimit, match="token_budget"):
        ledger.before_request(packet, 10)


def test_actual_usage_overrun_blocks_the_returned_decision():
    ledger = TaskLedger(TaskBudget(total_tokens=100))
    ledger.before_request(SimpleNamespace(estimated_input_tokens=10), 10)
    with pytest.raises(TaskLimit, match="actual_token_budget"):
        ledger.record_usage({"total_tokens": 101})
    assert ledger.known_tokens == 101


def test_bad_scenario_paths_and_budgets_fail_closed():
    with pytest.raises(ValueError):
        replace(CODE_SCENARIO, files=(("../hidden.py", "pass"),))
    with pytest.raises(ValueError):
        TaskBudget(requests=0)


def test_public_completion_contract_contains_no_hidden_inputs():
    public = completion_contract(CODE_SCENARIO)
    assert len(public.commands) == 1
    assert public.commands[0].fresh_python
    assert "check.py" in public.commands[0].argv[-1] and "test_added.py" in public.commands[0].argv[-1]
    assert "3.335" not in public.model_dump_json()


def test_uncaught_evaluator_preflight_error_still_records_attempt(tmp_path, monkeypatch):
    async def fail(self):
        raise ValueError("image unavailable")
    monkeypatch.setattr("kong.evaluation.long_driver.DockerBackend.prepare", fail)
    result = asyncio.run(run_trial(CODE_SCENARIO, tmp_path / "trial", "missing-image",
        lambda s, n: ScriptedModel([])))
    assert result["status"] == "environment_error"
    assert result["budget_usage"]["requests"] == 0
    assert (tmp_path / "trial" / "result.json").exists()


def test_protected_test_tampering_prevents_any_grader_execution(tmp_path, monkeypatch):
    folder = tmp_path / "agent"
    protected = seed(folder, CODE_SCENARIO)
    for path in CODE_SCENARIO.artifacts:
        (folder / path).write_text("pass\n", encoding="utf-8")
    (folder / "task/check.py").write_text("pass\n", encoding="utf-8")
    async def forbidden(*args, **kwargs):
        raise AssertionError("No execution allowed after protected hash mismatch")
    monkeypatch.setattr("kong.evaluation.long_graders.execute_isolated", forbidden)
    checks = asyncio.run(grade(CODE_SCENARIO, folder, "local", protected, grader_root=tmp_path / "external"))
    assert not checks["protected:task/check.py"]


def test_code_grader_rejects_public_sample_hardcoding(tmp_path, monkeypatch):
    folder = tmp_path / "agent"
    protected = seed(folder, CODE_SCENARIO)
    (folder / "task/test_added.py").write_text("pass", encoding="utf-8")
    async def execute(folder, image, argv, **kwargs):
        # The public sample succeeds while unseen answers are deliberately wrong.
        return ToolResult(success=True, output={"stdout": json.dumps({"note": "keep my existing integration note", "values": []})})
    monkeypatch.setattr("kong.evaluation.long_graders.execute_isolated", execute)
    checks = asyncio.run(grade(CODE_SCENARIO, folder, "local", protected, grader_root=tmp_path / "external"))
    assert checks["public_behavior"] and not checks["hidden_behavior"]


def test_data_grader_requires_rerun_and_unseen_inputs(tmp_path, monkeypatch):
    folder = tmp_path / "agent"
    protected = seed(folder, DATA_SCENARIO)
    put_data(folder / "task", source())
    async def do_nothing(*args, **kwargs):
        return ToolResult(success=True, output={"stdout": ""})
    monkeypatch.setattr("kong.evaluation.long_graders.execute_isolated", do_nothing)
    checks = asyncio.run(grade(DATA_SCENARIO, folder, "local", protected, grader_root=tmp_path / "external"))
    assert checks["all_rows"] and checks["rerun_same_bytes"]
    assert not checks["unseen_input"]  # hardcoded public outputs do not generalize


def test_coverage_does_not_turn_object_reconstruction_into_process_restart(tmp_path):
    driver = ScenarioDriver(CODE_SCENARIO, tmp_path)
    driver.restart = {"kind": "same_process_object_reconstruction"}
    assert driver.coverage()["same_run_reconstruction"]
    assert not driver.coverage()["process_restart"]


def test_missing_challenge_is_never_reported_as_covered(tmp_path):
    driver = ScenarioDriver(DATA_SCENARIO, tmp_path)
    driver.run_number = 3
    assert driver.coverage()["three_runs"]
    assert not driver.coverage()["two_compaction_checkpoints"]
    assert not driver.coverage()["three_effective_revisions"]


def test_model_observer_preserves_usage_stream_and_memory_proposal(tmp_path):
    from kong.evaluation.long_driver import ObservedModel
    proposal = {"thread_id": "0" * 32}
    inner = SimpleNamespace(last_usage={"total_tokens": 7}, on_stream=None, take_memory_delta=lambda: proposal)
    wrapper = ObservedModel(inner, TaskLedger(CODE_SCENARIO.budget), ScenarioDriver(CODE_SCENARIO, tmp_path))
    def callback(data):
        pass
    wrapper.on_stream = callback
    assert inner.on_stream is callback
    assert wrapper.last_usage == {"total_tokens": 7}
    assert wrapper.take_memory_delta() is proposal


def test_fatal_billing_stops_before_restart_or_next_scenario(tmp_path, monkeypatch):
    from kong.evaluation.long_driver import run_long_suite
    async def prepare(self):
        self.image_id = "sha256:" + "a" * 64
    monkeypatch.setattr("kong.evaluation.long_driver.DockerBackend.prepare", prepare)
    class Billing:
        async def generate(self, context):
            raise ModelError("Model service HTTP 402", category="billing", status_code=402)
    root, summary = asyncio.run(run_long_suite(tmp_path, "local", model_factory=lambda s, n: Billing()))
    assert summary["recorded_runs"] == 1
    assert summary["not_attempted"] == [DATA_SCENARIO.id]
    assert summary["stopped_reason"] == "fatal_model_configuration"
    result = json.loads((root / CODE_SCENARIO.id / "result.json").read_text(encoding="utf-8"))
    assert result["budget_usage"]["requests"] == 1
    assert result["restart"] is None


@pytest.mark.parametrize("category,status_code", [("rate_limit", 429), ("service_http", 503), ("timeout", None), ("connection", None)])
def test_exhausted_service_retries_stop_suite_as_environment_interruption(tmp_path, monkeypatch, category, status_code):
    from kong.evaluation.long_driver import run_long_suite
    async def prepare(self):
        self.image_id = "sha256:" + "a" * 64
    monkeypatch.setattr("kong.evaluation.long_driver.DockerBackend.prepare", prepare)
    class Unavailable:
        async def generate(self, context):
            raise ModelError("Service unavailable", category=category, status_code=status_code, retryable=True)
    root, summary = asyncio.run(run_long_suite(tmp_path, "local", model_factory=lambda s, n: Unavailable()))
    assert not summary["complete"] and summary["recorded_runs"] == 1
    assert summary["not_attempted"] == [DATA_SCENARIO.id]
    assert summary["results"][0]["status"] == "environment_error"
    assert summary["stopped_reason"] == "terminal_model_service:" + category
    result = json.loads((root / CODE_SCENARIO.id / "result.json").read_text(encoding="utf-8"))
    assert 1 < result["budget_usage"]["requests"] <= 4 and result["restart"] is None
    assert result["budget_usage"]["actual_total_tokens"] is None


def test_protocol_failure_or_recovered_service_error_is_not_terminal_service_interruption(tmp_path, monkeypatch):
    from kong.contracts import History, Status
    from kong.evaluation.long_driver import run_long_suite, terminal_service_error
    from kong.models.base import ModelProtocolError
    history = History()
    runtime = SimpleNamespace(state=SimpleNamespace(status=Status.WAITING_USER), history=history)
    history.append("model_error", 1, category="rate_limit", status_code=429)
    assert terminal_service_error(runtime)["status_code"] == 429
    history.append("decision", 2, decision={"kind": "ask", "question": "Unrelated question"})
    assert terminal_service_error(runtime) is None
    history.append("model_error", 3, category="protocol")
    assert terminal_service_error(runtime) is None
    async def prepare(self):
        self.image_id = "sha256:" + "a" * 64
    async def grade_stub(*args, **kwargs):
        return {"artifact_stub": False}
    monkeypatch.setattr("kong.evaluation.long_driver.DockerBackend.prepare", prepare)
    monkeypatch.setattr("kong.evaluation.long_driver.grade", grade_stub)
    class Invalid:
        async def generate(self, context):
            raise ModelProtocolError("Invalid decision")
    root, summary = asyncio.run(run_long_suite(tmp_path, "local", scenarios=[CODE_SCENARIO], model_factory=lambda s, n: Invalid()))
    assert summary["complete"] and summary["recorded_runs"] == 1 and summary["stopped_reason"] is None
    assert summary["results"][0]["status"] == "failed"


def test_failed_worker_cannot_overwrite_newer_pending_effect_snapshot(tmp_path, monkeypatch):
    from kong.contracts import NeedUserInput
    from kong.storage import RunStore
    trial = tmp_path / "trial"
    async def prepare(self):
        self.image_id = "sha256:" + "a" * 64
    monkeypatch.setattr("kong.evaluation.long_driver.DockerBackend.prepare", prepare)
    class Process:
        returncode = None
        pid = 123456
        async def communicate(self):
            store = RunStore(trial / "agent/.kong/runs")
            run_id = store.list_runs()[0]["run_id"]
            snapshot = store.load(run_id)
            snapshot.pending_action_id = "child-unknown"
            snapshot.pending_action_ids = ["child-unknown"]
            snapshot.history.append("action_intent", snapshot.state.turn_count, action_id="child-unknown")
            store.save(snapshot)
            self.returncode = 1
            return b"", b"worker failed after persisting an unknown effect"
    async def start(*args, **kwargs):
        return Process()
    monkeypatch.setattr("kong.evaluation.long_driver.asyncio.create_subprocess_exec", start)
    from kong.evaluation.long_driver import HANDOFF_QUESTION
    from kong.evaluation.long_fixtures import Fixture, act
    decisions = [act("read_file", path=p) for p in CODE_SCENARIO.protected]
    decisions += [act("write_file", path="task/test_added.py", content="pass\n")]
    decisions += [act("read_file", path=p) for p in CODE_SCENARIO.artifacts]
    decisions += [NeedUserInput(question=HANDOFF_QUESTION)]
    result = asyncio.run(run_trial(CODE_SCENARIO, trial, "local", lambda s, n: Fixture(decisions)))
    assert result["status"] == "environment_error"
    store = RunStore(trial / "agent/.kong/runs")
    snapshot = store.load(store.list_runs()[0]["run_id"])
    assert snapshot.pending_action_ids == ["child-unknown"]
    assert any(e.payload.get("action_id") == "child-unknown" for e in snapshot.history.events)


def test_hidden_resources_denied_to_all_agent_entrypoints(tmp_path):
    from kong.environments.docker import DockerBackend
    from kong.tools.defaults import default_registry
    from kong.tools.executor import ToolExecutor
    from kong.tools.permissions import PermissionPolicy
    from kong.workspace import Workspace
    seed(tmp_path, CODE_SCENARIO)
    (tmp_path / "grader-secret.txt").write_text("expected answers", encoding="utf-8")
    workspace = Workspace(tmp_path)
    backend = DockerBackend(workspace, "task", "local-image")
    registry = default_registry(workspace, allow_process=True, process_backend=backend)
    policy = PermissionPolicy(allowed_tools=["read_file", "search_files", "list_dir", "process_exec"], read_roots=["task"], write_roots=["task"])
    from kong.tools.base import ToolCall
    executor = ToolExecutor(registry)
    executor.permissions = policy
    for name, arguments in [("read_file", {"path": "grader-secret.txt"}), ("list_dir", {"path": "."}),
                            ("search_files", {"path": ".", "pattern": "expected"})]:
        result = asyncio.run(executor.execute(ToolCall(name=name, arguments=arguments)))
        assert not result.success
    tool = registry.get("process_exec")
    result = asyncio.run(tool.run(argv=["python", "-c", "pass"], cwd=".", timeout_seconds=1, max_output_bytes=1000, encoding="utf-8", fresh_python=False))
    assert not result.success and "outside" in result.error


@pytest.mark.parametrize("scenario", [CODE_SCENARIO, DATA_SCENARIO])
def test_stage_contract_forbids_respond_and_requires_current_reads(scenario):
    from kong.evaluation.long_driver import HANDOFF_QUESTION
    for number in (1, 2):
        contract = completion_contract(scenario, number)
        assert not contract.allow_completion
        assert contract.required_question == HANDOFF_QUESTION
        assert set(contract.read_before_ask) == set(scenario.protected + scenario.artifacts)
    assert completion_contract(scenario, 3).allow_completion


@pytest.mark.parametrize("decision_kind", ["respond", "arbitrary_ask"])
def test_unplanned_pause_or_early_respond_never_starts_a_new_run(tmp_path, monkeypatch, decision_kind):
    from kong.contracts import FinalResponse, NeedUserInput
    from kong.evaluation.long_fixtures import Fixture
    async def prepare(self):
        self.image_id = "sha256:" + "a" * 64
    async def forbidden(*args, **kwargs):
        raise AssertionError("An unverified pause must never start a worker")
    async def grade_stub(*args, **kwargs):
        return {"artifact_stub": True}
    monkeypatch.setattr("kong.evaluation.long_driver.DockerBackend.prepare", prepare)
    monkeypatch.setattr("kong.evaluation.long_driver.asyncio.create_subprocess_exec", forbidden)
    monkeypatch.setattr("kong.evaluation.long_driver.grade", grade_stub)
    decision = (FinalResponse(content="Requesting handoff in a completion response.") if decision_kind == "respond"
                else NeedUserInput(question="Unrelated question."))
    result = asyncio.run(run_trial(CODE_SCENARIO, tmp_path / "trial", "local", lambda s, n: Fixture([decision])))
    assert result["status"] == "failed"
    assert result["stopped_reason"] == "unplanned_stage_end"
    assert len(result["runs"]) == 1 and result["restart"] is None
    assert result["runtime_status"] != "completed"


def test_synthetic_revision_decision_cannot_duplicate_previous_usage(tmp_path):
    from kong.evaluation.long_driver import ObservedModel
    inner = SimpleNamespace(last_usage={"total_tokens": 999})
    driver = ScenarioDriver(CODE_SCENARIO, tmp_path)
    driver.runtime = SimpleNamespace(queued_inputs=[object()])
    driver.observe_compaction = lambda: None
    ledger = TaskLedger(CODE_SCENARIO.budget)
    observer = ObservedModel(inner, ledger, driver)
    asyncio.run(observer.generate(SimpleNamespace()))
    assert observer.last_usage is None
    assert ledger.requests == 0


@pytest.mark.parametrize("verified_handoff", [True, False])
def test_compaction_created_by_pausing_decision_is_delivered_only_after_verified_handoff(tmp_path, verified_handoff):
    from kong.completion_contracts import CompletionContract
    from kong.continuity.memory_contracts import ContinuityPolicy
    from kong.contracts import NeedUserInput, Status
    from kong.evaluation.long_driver import HANDOFF_QUESTION, ObservedEngine, ObservedModel, drive_stage
    from kong.runtime.loop import Runtime
    from kong.storage import RunStore
    from kong.tools.defaults import default_registry
    from kong.workspace import Workspace
    protected = seed(tmp_path, CODE_SCENARIO)
    (tmp_path / "task/test_added.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "initial.json").write_text(json.dumps({"protected_hashes": protected}), encoding="utf-8")
    revision = replace(CODE_SCENARIO.interventions[1], supersedes=None)
    scenario = replace(CODE_SCENARIO, interventions=(revision,))
    driver = ScenarioDriver(scenario, tmp_path)
    driver.run_number = 1
    ledger = TaskLedger(scenario.budget, live=False)
    inner = ScriptedModel([NeedUserInput(question=HANDOFF_QUESTION if verified_handoff else "Unrelated question."),
                           NeedUserInput(question=HANDOFF_QUESTION)])
    contract = (completion_contract(scenario, 1) if verified_handoff else
                CompletionContract(version=2, family="code", allow_completion=False))
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("read and hand off", ObservedModel(inner, ledger, driver), workspace, default_registry(workspace),
        store=RunStore(tmp_path / ".kong/runs"), context_engine=ObservedEngine(policy=ContinuityPolicy(compact_turns=1)),
        completion_contract=contract, on_event=driver.event)
    driver.runtime = runtime
    for path in scenario.protected + scenario.artifacts:
        asyncio.run(runtime._actions(act_for_read(path)))
    asyncio.run(drive_stage(runtime, driver, ledger, contract))
    assert runtime.state.status == Status.WAITING_USER
    assert driver.compactions  # Produced by the first ask's real commit(), not a pre-request hook.
    if verified_handoff:
        assert driver.revision == 1 and ledger.requests == 2
        assert runtime.constraints == [revision.text]
        assert [p["classification"] for p in driver.pauses] == ["post_handoff_compaction_revision", "scenario_constraint_rebind"]
        assert not runtime.queued_inputs and not runtime.state.contract_invalidated
        assert runtime.state.completion_contract == contract
        assert revision.text in json.dumps(inner.contexts[-1].messages)
    else:
        assert driver.revision == 0 and ledger.requests == 1
        assert not driver.interventions and not driver.pauses and not runtime.queued_inputs
        assert driver.pending_compaction_interventions() == [revision]


def test_development_library_covers_four_families_with_distinct_inputs():
    from collections import Counter
    from kong.evaluation.scenarios import DEVELOPMENT_LIBRARY, Scenario
    assert len(DEVELOPMENT_LIBRARY) == len({s.id for s in DEVELOPMENT_LIBRARY}) == 8
    assert Counter(s.task_group for s in DEVELOPMENT_LIBRARY) == {
        "code_multi_file": 2, "code_continuation": 2, "data_report": 2, "data_continuation": 2}
    for scenario in DEVELOPMENT_LIBRARY:
        assert Scenario.from_manifest(json.loads(json.dumps(scenario.manifest()))) == scenario
    customized = replace(CODE_SCENARIO, budget=TaskBudget(requests=7, total_tokens=123456))
    assert Scenario.from_manifest(json.loads(json.dumps(customized.manifest()))).budget == customized.budget


@pytest.mark.parametrize("budget", [{"requests": True}, {"decisions": 1.5}, {"active_seconds": float("nan")}, {"active_seconds": float("inf")}])
def test_invalid_task_budget_cannot_bypass_worker_limits(budget):
    with pytest.raises(ValueError):
        TaskBudget(**budget)


def test_declared_external_edit_cannot_rebaseline_protected_resources():
    with pytest.raises(ValueError, match="unprotected"):
        replace(CODE_SCENARIO, external_edit=("task/check.py", "pass"))


def test_public_invoice_check_rejects_boolean_cancellation_interpretation(tmp_path):
    import subprocess
    import sys
    seed(tmp_path, CODE_SCENARIO)
    from kong.evaluation.long_fixtures import INVOICE
    (tmp_path / "task/invoice.py").write_text(INVOICE, encoding="utf-8")
    (tmp_path / "task/money.py").write_text("from decimal import Decimal, ROUND_HALF_UP\ndef money(line):\n    if line is None or line.get('cancelled'): return Decimal('0.00')\n    return (Decimal(str(line['price'])) * line['quantity']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)\n", encoding="utf-8")
    check = subprocess.run([sys.executable, "-B", "check.py"], cwd=tmp_path / "task", capture_output=True)
    assert check.returncode != 0  # The exact live failure must now fail the public receipt.
    assert "status == 'cancelled'" in dict(CODE_SCENARIO.files)["task/requirements.md"]


def test_empty_result_public_check_matches_external_oracle(tmp_path):
    import subprocess
    import sys
    from kong.evaluation.scenarios import DEVELOPMENT_LIBRARY
    scenario = next(s for s in DEVELOPMENT_LIBRARY if s.id == "dev_empty_accounting_report_v1")
    seed(tmp_path, scenario)
    text = dict(scenario.files)["task/orders.csv"]
    put_data(tmp_path / "task", text)
    assert all(check_data(tmp_path / "task", text).values())
    assert subprocess.run([sys.executable, "-B", "check.py"], cwd=tmp_path / "task", capture_output=True).returncode == 0


def test_large_cents_are_not_rounded_through_float():
    from kong.evaluation.scenarios import DEVELOPMENT_LIBRARY
    scenario = next(s for s in DEVELOPMENT_LIBRARY if s.id == "dev_large_cents_revisions_v1")
    text = dict(scenario.files)["task/orders.csv"] + scenario.appended_input[1]
    assert data_oracle(text) == [{"region": "East", "net_amount": "90071992547410.26"},
        {"region": "North", "net_amount": "0.00"}, {"region": "West", "net_amount": "1.01"}]


def test_stage_guidance_names_missing_read_and_checkpoint_without_dispatch(tmp_path):
    from kong.evaluation.long_driver import HANDOFF_QUESTION, stage_guidance
    from kong.contracts import Phase, History
    from kong.runtime.state import create_run_state
    from kong.workspace import Workspace
    state = create_run_state("continue")
    state.completion_contract = completion_contract(DATA_SCENARIO, 2)
    state.phase = Phase.ASSESS
    guidance = stage_guidance(state, History(), Workspace(tmp_path))
    assert "task/check.py" in guidance["missing_current_reads"]
    assert guidance["required_question"] == HANDOFF_QUESTION and guidance["phase"] == "assess"
    assert "assess" in guidance["instruction"]
    assert "task/report.md" in guidance["absent_output_paths"]
    assert "task/check.py" in guidance["unavailable_input_paths"]
    assert "produce absent outputs before reading" in guidance["instruction"]
    assert not list(tmp_path.iterdir())
    assert guidance["assessment_options"]["bounded_recovery_available"]
    assert guidance["assessment_options"]["continue_without_verified_progress_pauses"]
    state.progress_state.recovery_attempts = 1
    unchanged = state.model_dump(mode="json")
    assert not stage_guidance(state, History(), Workspace(tmp_path))["assessment_options"]["bounded_recovery_available"]
    assert state.model_dump(mode="json") == unchanged  # Read-only guidance cannot grant credits or a lease.


def test_installed_package_freeze_never_reports_an_empty_stable_identity(tmp_path, monkeypatch):
    import kong.evaluation.long_driver as driver
    installed = tmp_path / "Lib/site-packages/kong"
    (installed / "evaluation").mkdir(parents=True)
    entry = installed / "evaluation/long_driver.py"
    entry.write_text("installed source", encoding="utf-8")
    resource = installed / "resource.txt"
    resource.write_text("original", encoding="utf-8")
    monkeypatch.setattr(driver, "__file__", str(entry))
    before = driver.freeze_resources()
    assert "src/kong/evaluation/long_driver.py" in before
    resource.write_text("changed", encoding="utf-8")
    assert driver.freeze_resources() != before
    assert "uv.lock" not in before  # No fabricated checkout identity for a wheel.


def test_restore_read_requires_new_successful_current_version_evidence(tmp_path):
    from kong.evaluation.long_driver import restored_read_evidence
    from kong.runtime.loop import Runtime
    from kong.tools.defaults import default_registry
    from kong.workspace import Workspace
    (tmp_path / "out.txt").write_text("current", encoding="utf-8")
    workspace = Workspace(tmp_path)
    runtime = Runtime.new("read", ScriptedModel([]), workspace, default_registry(workspace),
        completion_contract={"version": 2, "family": "data", "allow_completion": False,
            "read_before_ask": ["out.txt"]})
    asyncio.run(runtime._actions(act_for_read("out.txt")))
    floor = len(runtime.history.observations)
    assert not restored_read_evidence(runtime.state, runtime.history, workspace, ("out.txt",), floor)
    asyncio.run(runtime._actions(act_for_read("out.txt")))
    assert restored_read_evidence(runtime.state, runtime.history, workspace, ("out.txt",), floor)
    unknown = runtime.history.model_copy(deep=True)
    unknown.observations[-1].outcome_unknown = True
    assert not restored_read_evidence(runtime.state, unknown, workspace, ("out.txt",), floor)
    (tmp_path / "out.txt").write_text("external edit", encoding="utf-8")
    assert not restored_read_evidence(runtime.state, runtime.history, workspace, ("out.txt",), floor)


def act_for_read(path):
    from kong.contracts import Act, Action
    return Act(actions=[Action(name="read_file", arguments={"path": path})])


def test_restored_context_explains_additional_read_without_invalidating_old_evidence(tmp_path):
    from kong.continuity.contracts import ContextItem, ContextSource
    from kong.evaluation.long_driver import ObservedEngine
    from kong.runtime.finalization import completion_guidance
    from kong.runtime.loop import Runtime
    from kong.tools.defaults import default_registry
    from kong.workspace import Workspace
    (tmp_path / "out.txt").write_text("current", encoding="utf-8")
    workspace = Workspace(tmp_path)
    engine = ObservedEngine()
    runtime = Runtime.new("read and hand off", ScriptedModel([]), workspace, default_registry(workspace),
        context_engine=engine, completion_contract={"version": 2, "family": "data", "allow_completion": False,
            "read_before_ask": ["out.txt"], "required_question": "handoff"})
    asyncio.run(runtime._actions(act_for_read("out.txt")))
    engine.restore_observation_floor = len(runtime.history.observations)
    engine.restore_artifact_paths = ("out.txt",)
    for pending in (True, False):
        status = completion_guidance(runtime.controller.completion_gate, runtime.state, runtime.history)
        assert not status["additional_tool_required"]  # Same-Run evidence remains valid in Runtime.
        engine.supplemental_items = [ContextItem(category="runtime_feedback", priority=0,
            content="RUNTIME VERIFICATION STATUS:\n" + json.dumps(status),
            sources=[ContextSource(kind="state", run_id=runtime.state.run_id, path="completion_contract")])]
        packet = asyncio.run(engine.prepare(thread=runtime.thread, state=runtime.state,
            history=runtime.history, tools=runtime.registry.definitions()))
        public = next(i for i in packet.items if i.content.startswith("PUBLIC STAGE HANDOFF STATUS:"))
        assert json.loads(public.content.split("\n", 1)[1])["post_restore_read_required"] is pending
        verification = next(i for i in packet.items if i.content.startswith("RUNTIME VERIFICATION STATUS:"))
        caller = json.loads(verification.content.split("\n", 1)[1])
        assert bool(caller.get("caller_post_restore_read_required")) is pending
        assert len(runtime.history.observations) == (1 if pending else 2)  # Context performs no read.
        if pending:
            assert "post_restore_read" in [s.path for s in verification.sources]
            asyncio.run(runtime._actions(act_for_read("out.txt")))


def test_worker_nonzero_exit_preserves_diagnostics_and_redacts_credentials(tmp_path):
    import sys
    from kong.evaluation.long_driver import wait_restore_process
    secret = "sk-" + "synthetic-worker-credential-123456"
    async def probe():
        process = await asyncio.create_subprocess_exec(sys.executable, "-c",
            f"import sys;print('worker output');sys.stderr.write('failure {secret}\\n');sys.exit(3)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await wait_restore_process(process, tmp_path, 10, secret)
        return process.returncode
    assert asyncio.run(probe()) == 3
    diagnostic = json.loads((tmp_path / "restart-diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostic["returncode"] == 3 and diagnostic["capture_complete"]
    assert diagnostic["streams"]["stderr"]["redacted"]
    assert secret.encode() not in (tmp_path / "restart-stderr.log").read_bytes()
    assert b"failure [redacted]" in (tmp_path / "restart-stderr.log").read_bytes()
    assert b"worker output" in (tmp_path / "restart-stdout.log").read_bytes()


def test_worker_timeout_is_killed_recorded_and_not_retried(tmp_path):
    import sys
    from kong.evaluation.long_driver import wait_restore_process
    async def probe():
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(5)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        with pytest.raises(TimeoutError):
            await wait_restore_process(process, tmp_path, 0.05)
        assert process.returncode is not None
    asyncio.run(probe())
    diagnostic = json.loads((tmp_path / "restart-diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostic["interruption"] == "TimeoutError" and not diagnostic["capture_complete"]
