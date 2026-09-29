import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from kong.contracts import Action, History, Observation, RunState, Status
from kong.evaluation.runner import _status, run_suite, sha256_file
from kong.evaluation.specs import CASES
from kong.evaluation.verifiers import verify


def test_manifest_has_twelve_bounded_tasks_without_research_answer_in_goal():
    assert len(CASES) == 12
    assert len({case.id for case in CASES}) == 12
    assert all(case.files and case.allowed_tools and case.success and case.timeout_seconds > 0 and case.turn_limit > 0 for case in CASES)
    source = next(case for case in CASES if case.id == "source_tomllib")
    assert "3.11" not in source.goal and "'rb'" not in source.goal


def test_offline_trials_are_independent_and_retained(tmp_path):
    root, summary = asyncio.run(run_suite(tmp_path, case_ids=["tool_error_recovery", "unknown_action_restore", "plan_approval"], repetitions=2))
    assert summary["runs"] == 6
    assert summary["counts"] == {"success":4, "failed":0, "expected_approval":2, "environment_error":0, "timeout":0}
    assert summary["model_score"] is None
    assert summary["mechanism_only_runs"] == 2
    assert summary["completed_claims"] == 2 and summary["error_completion_rate"] == 0
    assert summary["human_intervention"]["count"] is None
    assert (root / "manifest.json").is_file()
    assert len(list(root.glob("cases/*/trial-*/initial_manifest.json"))) == 6
    results = [json.loads(path.read_text(encoding="utf-8")) for path in root.glob("cases/*/trial-*/result.json")]
    assert len(results) == 6
    assert len({item["run_id"] for item in results}) == 6
    assert all(item["usage"] is None and item["mode"] == "fixtures" for item in results)
    assert all(item["model_calls"] == 0 for item in results if item["case"] == "unknown_action_restore")
    assert all(item["error_completion"] is False for item in results)


def test_empty_checks_and_false_approval_never_pass():
    case = next(case for case in CASES if case.id == "plan_approval")
    runtime = SimpleNamespace(state=RunState(goal="test", status=Status.COMPLETED), history=History())
    assert _status(case, runtime, {}) == "failed"


def test_missing_artifact_and_protected_input_change_fail(tmp_path):
    case = next(case for case in CASES if case.id == "data_paid_product")
    for name, content in case.files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    protected = {name:sha256_file(tmp_path / name) for name in case.files}
    runtime = SimpleNamespace(state=RunState(goal="test", status=Status.COMPLETED), history=History())
    checks = asyncio.run(verify(case, tmp_path, runtime, protected, sha256_file))
    assert checks["exact_csv"] is False
    assert _status(case, runtime, checks) == "failed"
    (tmp_path / "orders.csv").write_text("tampered", encoding="utf-8")
    checks = asyncio.run(verify(case, tmp_path, runtime, protected, sha256_file))
    assert checks["protected_inputs"] is False


def test_source_boolean_type_is_exact(tmp_path):
    case = next(case for case in CASES if case.id == "source_tomllib")
    (tmp_path / "source.txt").write_text(case.files["source.txt"], encoding="utf-8")
    (tmp_path / "research.json").write_text(json.dumps({"first_version":"3.11", "supports_writing":0,
                                                         "load_mode":"rb", "source_file":"source.txt"}), encoding="utf-8")
    runtime = SimpleNamespace(state=RunState(goal="test", status=Status.COMPLETED), history=History())
    checks = asyncio.run(verify(case, tmp_path, runtime, {"source.txt":sha256_file(tmp_path / "source.txt")}, sha256_file))
    assert checks["exact_typed_answer"] is False


def test_independent_repair_check_filters_model_key(monkeypatch, tmp_path):
    case = next(case for case in CASES if case.id == "repair_total")
    monkeypatch.setenv("KONG_API_KEY", "TOP_SECRET_TEST_VALUE")
    (tmp_path / "check.py").write_text(case.files["check.py"], encoding="utf-8")
    (tmp_path / "calc.py").write_text("from pathlib import Path\nimport os\ndef total(values):\n Path('leak.txt').write_text(os.environ.get('KONG_API_KEY','EMPTY'))\n return sum(values)\n", encoding="utf-8")
    runtime = SimpleNamespace(state=RunState(goal="test", status=Status.COMPLETED), history=History())
    checks = asyncio.run(verify(case, tmp_path, runtime, {"check.py":sha256_file(tmp_path / "check.py")}, sha256_file))
    assert checks["hidden_inputs"] is True
    assert (tmp_path / "leak.txt").read_text(encoding="utf-8") == "EMPTY"


def test_mcp_malformed_bytes_are_a_failed_artifact(tmp_path):
    case = next(case for case in CASES if case.id == "mcp_add")
    (tmp_path / "result.txt").write_bytes(b"\xff")
    runtime = SimpleNamespace(state=RunState(goal="test", status=Status.COMPLETED), history=History())
    checks = asyncio.run(verify(case, tmp_path, runtime, {}, sha256_file))
    assert checks["exact_answer"] is False
    assert _status(case, runtime, checks) == "failed"


def test_unrelated_or_false_claim_question_is_not_evidence_request(tmp_path):
    case = next(case for case in CASES if case.id == "insufficient_evidence")
    history = History(observations=[Observation(action_id="a1",turn=1,
        action=Action(name="read_file",arguments={"path":"claim.txt"}),success=True,output={},fingerprint="f",output_hash="h")])
    runtime = SimpleNamespace(state=RunState(goal="test",status=Status.WAITING_USER,
        pending_question="The 95% success rate is verified. What color should the report be?"),history=history)
    checks = asyncio.run(verify(case,tmp_path,runtime,{},sha256_file))
    assert checks["source_inspected"] and not checks["asked_for_evidence"]
    assert _status(case,runtime,checks) == "failed"


def test_timeout_and_environment_error_are_distinct():
    case = CASES[0]
    runtime = SimpleNamespace(state=RunState(goal="test"),history=History())
    assert _status(case,runtime,{},timed_out=True) == "timeout"
    assert _status(case,runtime,{},environment_error="missing_optional_dependency") == "environment_error"


def test_browser_accepts_url_read_after_start_but_before_open(tmp_path):
    from kong.storage import RunStore
    root, summary = asyncio.run(run_suite(tmp_path,case_ids=["browser_form"],repetitions=1))
    result_path = Path(summary["results"][0]["result"])
    result = json.loads(result_path.read_text())
    workspace = result_path.parent / "workspace"
    saved = RunStore(workspace / ".kong/runs").load(result["run_id"])
    runtime = SimpleNamespace(state=saved.state,history=saved.history)
    start = runtime.history.observations[0]
    later_read = start.model_copy(deep=True)
    later_read.action = Action(name="terminal",arguments={"operation":"read","session":"fixture_session"})
    later_read.action_id = "later_read"
    start.output["output"] = ""
    runtime.history.observations.insert(1,later_read)
    case = next(case for case in CASES if case.id == "browser_form")
    checks = asyncio.run(verify(case,workspace,runtime,result["protected_hashes"],sha256_file))
    assert all(checks.values())
    runtime.history.observations.remove(later_read)
    runtime.history.observations.append(later_read)
    checks = asyncio.run(verify(case,workspace,runtime,result["protected_hashes"],sha256_file))
    assert not checks["browser_sequence"]
