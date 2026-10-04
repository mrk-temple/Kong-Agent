import asyncio
import json
from types import SimpleNamespace

from kong.evaluation import runner
from kong.evaluation.specs import CASES
from kong.evaluation.variation_verifiers import verify_variation
from kong.evaluation.variations import CONTRACTS, VARIATION_CASES
from kong.storage import RunStore
from test_evaluation_profiles import mechanical_fixture_suite


def test_legacy_contracts_remain_frozen_and_variations_are_separate():
    assert len(CASES) == 12
    assert runner._digest([runner.manifest(case) for case in CASES]) == "d4b0edcd9ce9be21478c795b95ed1e503619781a08b3d87749e626a7dbd4179a"
    assert len(VARIATION_CASES) == len(CONTRACTS) == 16
    assert not ({case.id for case in CASES} & {case.id for case in VARIATION_CASES})
    assert {case.category for case in VARIATION_CASES} == {"repair_v04", "data_v04", "recovery_v04", "revision_v04", "continuation_v04"}
    assert all(case.files and case.success and CONTRACTS[case.id].artifact for case in VARIATION_CASES)


def test_behavior_resource_fingerprint_covers_skill_references_and_verifier(monkeypatch):
    resources = runner._behavior_resources()
    for name in ("src/kong/skills/builtin/coding/SKILL.md",
                 "src/kong/skills/builtin/coding/references/verification.md",
                 "src/kong/evaluation/variation_verifiers.py", "src/kong/models/compatible.py",
                 "examples/kong_eval.py", "pyproject.toml"):
        assert name in resources
    original = runner.sha256_file
    monkeypatch.setattr(runner, "sha256_file", lambda path: "0" * 64 if path.name == "verification.md" else original(path))
    changed = runner._behavior_resources()
    assert runner._digest(changed) != runner._digest(resources)


def test_harness_exception_diagnostic_omits_paths_and_error_text():
    try:
        raise PermissionError(13, "SECRET_CANARY", "C:/private/api.txt")
    except PermissionError as exc:
        diagnostic = runner._safe_exception_location(exc, "runtime_run")
    encoded = json.dumps(diagnostic)
    assert diagnostic["stage"] == "runtime_run" and diagnostic["exception_type"] == "PermissionError"
    assert diagnostic["errno"] == 13
    assert "SECRET_CANARY" not in encoded and "api.txt" not in encoded and "C:/private" not in encoded


def test_revision_task_retains_trials_and_old_correction_fails(tmp_path):
    root, summary = asyncio.run(mechanical_fixture_suite(tmp_path, task_set="variation16", case_ids=["v04_revision_status"], repetitions=2))
    assert summary["runs"] == 2 and summary["counts"]["success"] == 2
    assert summary["task_set"] == "variation16" and summary["model_score"] is None
    assert summary["human_intervention"]["corrections"] is None
    assert summary["human_intervention"]["rescues"] is None
    assert len(list(root.glob("cases/*/trial-*/result.json"))) == 2
    result_path = root / "cases/v04_revision_status/trial-01/result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    workspace = result_path.parent / "workspace"
    assert {item["path"] for item in result["artifacts"]} == {"report.json"}
    assert result["resource_fingerprint"] == summary["resource_fingerprint"]
    (workspace / "report.json").write_text('{"included_statuses":["active","archived"],"total":57}', encoding="utf-8")
    snapshot = RunStore(workspace / ".kong" / "runs").load(result["run_id"])
    runtime = SimpleNamespace(state=snapshot.state, history=snapshot.history)
    case = next(case for case in VARIATION_CASES if case.id == "v04_revision_status")
    checks = asyncio.run(verify_variation(case, workspace, runtime, result["protected_hashes"], runner.sha256_file))
    assert checks["exact_revised_report"] is False


def test_v2_accepts_only_protected_successful_check_as_equivalent_readback(tmp_path):
    root, summary = asyncio.run(mechanical_fixture_suite(tmp_path, task_set="variation16_v2",
        case_ids=["v04_data_inventory"], repetitions=1))
    assert summary["counts"]["success"] == 1
    path = root / "cases/v04_data_inventory/trial-01/result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    workspace = path.parent / "workspace"
    snapshot = RunStore(workspace / ".kong/runs").load(result["run_id"])
    snapshot.history.observations = [o for o in snapshot.history.observations
        if not (o.action.name == "read_file" and o.action.arguments.get("path") == "summary.csv")]
    runtime = SimpleNamespace(state=snapshot.state, history=snapshot.history)
    case = next(case for case in VARIATION_CASES if case.id == "v04_data_inventory")
    def verify(policy):
        return asyncio.run(verify_variation(case, workspace, runtime, result["protected_hashes"], runner.sha256_file,
                                           readback_policy=policy))
    assert not verify("direct")["artifact_read_after_write"]
    assert verify("protected_check")["artifact_read_after_write"]
    checker = next(o for o in snapshot.history.observations if o.action.name == "process_exec"
                   and "check.py" in o.action.arguments.get("argv", []))
    checker.success = False
    assert not verify("protected_check")["artifact_read_after_write"]
    checker.success = True
    (workspace / "check.py").write_text("print('fake success')", encoding="utf-8")
    assert not verify("protected_check")["artifact_read_after_write"]


def test_plan_variations_have_no_environment_actions(tmp_path):
    root, summary = asyncio.run(mechanical_fixture_suite(tmp_path, task_set="plan3", repetitions=1))
    assert summary["runs"] == summary["counts"]["expected_approval"] == 3
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["runtime_criteria"]) == 3


def test_variation_export_retains_artifacts_and_resource_fingerprint(tmp_path):
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "examples/export_eval_report.py"
    spec = importlib.util.spec_from_file_location("eval_export", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, summary = asyncio.run(mechanical_fixture_suite(tmp_path / "runs", task_set="variation16_v2",
        case_ids=["v04_repair_mean"], repetitions=1))
    output = tmp_path / "public.json"
    assert module.export(root, output) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    trial = report["trials"][0]
    assert trial["resource_fingerprint"] == summary["resource_fingerprint"]
    assert "stats.py" in {a["path"] for a in trial["artifacts"]}
    assert str(tmp_path) not in output.read_text(encoding="utf-8")
