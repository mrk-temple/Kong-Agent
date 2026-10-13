from copy import deepcopy

from kong.evaluation.acceptance import human_burden_gate, paired_long_task_gate, release_assessment


def paired_fixture():
    tasks = {f"code{i}": "code" for i in range(6)} | {f"data{i}": "data" for i in range(6)}
    rows = []
    for arm in ("RC5", "candidate"):
        limits = {"code": 14, "data": 14} if arm == "RC5" else {"code": 16, "data": 15}
        for family in ("code", "data"):
            for ordinal in range(18):
                rows.append({"arm": arm, "task": f"{family}{ordinal // 3}", "repeat": ordinal % 3 + 1,
                    "status": "success" if ordinal < limits[family] else "failed", "mode": "live",
                    "resource_identity_stable": True, "image_identity_stable": True,
                    "grade_evidence_verified": True, "error_completion": False, "severe_boundary_error": False,
                    "total_tokens": 100 if arm == "RC5" else 110, "active_seconds": 10 if arm == "RC5" else 11})
    return {"contract": {"kind": "held_out", "frozen_before_results": True, "primary_goal": "correctness",
        "repetitions": 3, "tasks": tasks, "source_identity": "fixture", "harness_identity": "fixture",
        "model_identity": "fixture", "budget_identity": "fixture"}, "trials": rows}


def human_fixture():
    return {"kind": "human_paired", "human_measured": True,
        "trials": [{"arm": arm, "family": family, "variation": v, "operator": "fixture",
            "order": order + 1, "event_log_sha256": "fixture", "success": True,
            "corrections": 1, "reminders": 1, "rescues": 0, "necessary_approvals": 1,
            "unnecessary_confirmations": 1, "active_supervision_minutes": 3}
            for order, (arm, family, v) in enumerate((a, f, n) for a in ("RC5", "candidate")
                for f in ("code_modify", "code_restore", "data_report", "data_continue") for n in (1, 2))]}


def test_development_passes_and_high_test_counts_never_grant_release_acceptance():
    assessment = release_assessment(development={"model_successes": 8, "planned_runs": 8, "complete": True},
        engineering={"source_identity_verified": True, "windows": {"passed": 999, "failed": 0},
                     "linux": {"passed": 999, "failed": 0}, "ruff": "passed", "wheel_install": "passed"})
    assert not assessment["accepted"] and assessment["gates"]["engineering"]["status"] == "passed"
    assert assessment["gates"]["formal_long_tasks"]["status"] == "not_measured"
    assert assessment["gates"]["human_supervision"]["status"] == "not_measured"
    assert not assessment["development_in_formal_denominator"]


def test_paired_correctness_resource_limits_and_complete_denominators():
    study = paired_fixture()
    passed = paired_long_task_gate(study)
    assert passed["status"] == "passed" and passed["gain"] == 3 and passed["denominator_per_arm"] == 36
    for edit in ("duplicate", "missing", "development", "unverified", "cost", "wrong_completion"):
        bad = deepcopy(study)
        if edit == "duplicate":
            bad["trials"].append(bad["trials"][0])
        elif edit == "missing":
            bad["trials"].pop()
        elif edit == "development":
            bad["contract"]["kind"] = "development"
        elif edit == "unverified":
            bad["trials"][0]["grade_evidence_verified"] = "true"
        elif edit == "cost":
            for trial in bad["trials"]:
                if trial["arm"] == "candidate":
                    trial["total_tokens"] = 121
        else:
            bad["trials"][-1]["error_completion"] = True
        assert paired_long_task_gate(bad)["status"] != "passed"


def test_zero_unknown_or_nonfinite_cost_cannot_claim_efficiency():
    for value in (None, 0, float("nan"), True):
        study = paired_fixture()
        study["trials"][0]["total_tokens"] = value
        assert paired_long_task_gate(study)["status"] == "not_measured"


def test_synthetic_human_events_missing_fields_and_regressions_block_acceptance():
    study = human_fixture()
    assert human_burden_gate(study)["status"] == "passed"
    assert human_burden_gate(study)["average_burden"]["candidate"] == 2
    for edit in ("synthetic", "missing", "rescue", "burden", "minutes", "success", "unknown"):
        bad = deepcopy(study)
        if edit == "synthetic":
            bad["human_measured"] = False
        elif edit == "missing":
            bad["trials"].pop()
        elif edit == "unknown":
            bad["trials"][-1]["active_supervision_minutes"] = None
        else:
            for r in bad["trials"]:
                if r["arm"] == "candidate":
                    if edit == "rescue":
                        r["rescues"] = 1
                    elif edit == "burden":
                        r["corrections"] = 2
                    elif edit == "minutes":
                        r["active_supervision_minutes"] = 5
                    else:
                        r["success"] = False
        assert human_burden_gate(bad)["status"] != "passed"
