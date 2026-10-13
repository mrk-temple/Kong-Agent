"""Frozen V0.5 gates. Development success never substitutes for a held-out study."""
from collections import Counter
from statistics import median


def paired_long_task_gate(study):
    if study is None:
        return {"status": "not_measured", "reason": "No frozen RC5/candidate held-out comparison"}
    contract, trials = study.get("contract", {}), study.get("trials", [])
    families = contract.get("tasks", {})
    if (contract.get("kind") != "held_out" or contract.get("frozen_before_results") is not True
            or contract.get("primary_goal") != "correctness" or contract.get("repetitions") != 3
            or Counter(families.values()) != {"code": 6, "data": 6}
            or not contract.get("source_identity") or not contract.get("harness_identity")
            or not contract.get("model_identity") or not contract.get("budget_identity")):
        return {"status": "incomplete", "reason": "Formal identity, scope or preselected metric is missing"}
    expected = {(arm, task, repeat) for arm in ("RC5", "candidate") for task in families for repeat in (1, 2, 3)}
    statuses = {"success", "failed", "environment_error", "budget_stop", "invalid_coverage", "cancelled", "timeout"}
    if any(type(r.get("repeat")) is not int or r.get("status") not in statuses for r in trials):
        return {"status": "incomplete", "reason": "Each started trial requires a typed repetition and recorded outcome"}
    keys = [(r.get("arm"), r.get("task"), r.get("repeat")) for r in trials]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        return {"status": "incomplete", "reason": "Expected all 72 distinct paired attempts; no pooling or missing trials"}
    if any(r.get("mode") != "live" or r.get("resource_identity_stable") is not True
           or r.get("image_identity_stable") is not True or r.get("grade_evidence_verified") is not True for r in trials):
        return {"status": "incomplete", "reason": "Each trial needs verified real-model grading and frozen identities"}
    if any(r.get("error_completion") is not False or r.get("severe_boundary_error") is not False
           for r in trials if r["arm"] == "candidate"):
        return {"status": "failed", "reason": "Wrong completion or severe boundary error blocks acceptance"}
    scores = {arm: {family: sum(r["status"] == "success" for r in trials
                              if r["arm"] == arm and families[r["task"]] == family)
                    for family in ("code", "data")} for arm in ("RC5", "candidate")}
    baseline = sum(scores["RC5"].values())
    candidate = sum(scores["candidate"].values())
    task_scores = {key: r for key, r in zip(keys, trials)}
    costs = {}
    for metric in ("total_tokens", "active_seconds"):
        ratios = []
        for task in families:
            for repeat in (1, 2, 3):
                left, right = task_scores[("RC5", task, repeat)], task_scores[("candidate", task, repeat)]
                if left["status"] == right["status"] == "success":
                    before, after = left.get(metric), right.get(metric)
                    if (type(before) not in {int, float} or type(after) not in {int, float}
                            or before <= 0 or after < 0):
                        return {"status": "not_measured", "reason": "Paired successful-task cost is unknown"}
                    import math
                    if not math.isfinite(before) or not math.isfinite(after):
                        return {"status": "not_measured", "reason": "Invalid paired cost"}
                    ratios.append(after / before)
        # Ratios are descriptive; the contract checks ratios of matched medians.
        left_costs, right_costs = [], []
        for task in families:
            for repeat in (1, 2, 3):
                left, right = task_scores[("RC5", task, repeat)], task_scores[("candidate", task, repeat)]
                if left["status"] == right["status"] == "success":
                    left_costs.append(left[metric])
                    right_costs.append(right[metric])
        costs[metric] = {"paired_successes": len(ratios), "median_pair_ratio": median(ratios) if ratios else None,
                         "matched_median_ratio": median(right_costs) / median(left_costs) if ratios else None}
    correct = candidate >= 30 and all(scores["candidate"][f] >= 14 and scores["candidate"][f] >= scores["RC5"][f]
                                      for f in ("code", "data")) and candidate - baseline >= 3
    if not correct:
        status, reason = "failed", "Correctness target or scenario regression gate did not pass"
    elif any(c["matched_median_ratio"] is None for c in costs.values()):
        status, reason = "not_measured", "No matched success cost denominator"
    elif any(c["matched_median_ratio"] > 1.2 for c in costs.values()):
        status, reason = "failed", "Matched successful-task resource medians exceed the +20% cap"
    else:
        status, reason = "passed", "Preselected correctness gain and resource gates passed"
    return {"status": status, "reason": reason, "denominator_per_arm": 36, "scores": scores,
            "baseline_successes": baseline, "candidate_successes": candidate, "gain": candidate - baseline, "costs": costs}


def human_burden_gate(study):
    if study is None:
        return {"status": "not_measured", "reason": "Synthetic interventions are not human supervision"}
    trials = study.get("trials", [])
    if study.get("kind") != "human_paired" or study.get("human_measured") is not True or len(trials) != 16:
        return {"status": "incomplete", "reason": "Expected 16 genuine supervised paired trials"}
    keys = [(r.get("arm"), r.get("family"), r.get("variation")) for r in trials]
    if any(type(r.get("variation")) is not int for r in trials):
        return {"status": "incomplete", "reason": "Human variations must use explicit integer identities"}
    expected = {(arm, family, v) for arm in ("RC5", "candidate")
                for family in ("code_modify", "code_restore", "data_report", "data_continue") for v in (1, 2)}
    if set(keys) != expected or len(set(keys)) != 16:
        return {"status": "incomplete", "reason": "Human trial pairing is incomplete"}
    import math
    for r in trials:
        if (not r.get("operator") or not r.get("event_log_sha256") or not r.get("order")
                or type(r.get("success")) is not bool):
            return {"status": "incomplete", "reason": "Operator, order, outcome and event evidence are required"}
        for name in ("corrections", "reminders", "rescues", "necessary_approvals", "unnecessary_confirmations", "active_supervision_minutes"):
            value = r.get(name)
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                return {"status": "not_measured", "reason": "Missing or invalid measured human burden"}
        if r["unnecessary_confirmations"] > r["corrections"] + r["reminders"]:
            return {"status": "incomplete", "reason": "Unnecessary confirmations must be classified within corrections/reminders"}
    groups = {arm: [r for r in trials if r["arm"] == arm] for arm in ("RC5", "candidate")}
    burden = {arm: sum(r["corrections"] + r["reminders"] for r in rs) / 8
              for arm, rs in groups.items()}
    rescues = {arm: sum(r["rescues"] for r in rs) for arm, rs in groups.items()}
    minutes = {arm: median(r["active_supervision_minutes"] for r in rs) for arm, rs in groups.items()}
    successes = {arm: sum(r["success"] for r in rs) for arm, rs in groups.items()}
    passed = (burden["candidate"] <= burden["RC5"] + 0.5 and rescues["candidate"] <= rescues["RC5"]
              and minutes["candidate"] <= minutes["RC5"] + max(minutes["RC5"] * 0.1, 1)
              and successes["candidate"] >= successes["RC5"])
    return {"status": "passed" if passed else "failed", "average_burden": burden, "rescues": rescues,
            "median_supervision_minutes": minutes, "successes": successes, "denominator_per_arm": 8}


def release_assessment(*, engineering=None, development=None, paired=None, human=None,
                       memory_study=None, skill_study=None, v04_regression=None):
    gates = {"formal_long_tasks": paired_long_task_gate(paired), "human_supervision": human_burden_gate(human)}
    engineering_ok = (engineering is not None and engineering.get("source_identity_verified") is True
        and engineering.get("windows", {}).get("passed", 0) > 0 and engineering.get("windows", {}).get("failed") == 0
        and engineering.get("linux", {}).get("passed", 0) > 0 and engineering.get("linux", {}).get("failed") == 0
        and engineering.get("ruff") == "passed" and engineering.get("wheel_install") == "passed")
    gates["engineering"] = {"status": "passed" if engineering_ok else "incomplete"}
    # Auxiliary studies are supplied only after their evidence/identity audit.
    for name, study in (("memory_and_retrieval", memory_study), ("skill_lifecycle_and_ablation", skill_study), ("v04_model_regression", v04_regression)):
        gates[name] = {"status": study["status"] if study and study.get("evidence_verified") is True
                      and study.get("status") in {"passed", "failed"} else "not_measured"}
    accepted = all(g["status"] == "passed" for g in gates.values())
    return {"schema": "kong-v05-acceptance-1", "accepted": accepted,
            "release_status": "accepted" if accepted else "engineering_candidate",
            "gates": gates, "development_calibration": development,
            "development_in_formal_denominator": False, "self_review": True}
