import pytest

from kong.evaluation.live_manifest import LiveManifest
from kong.evaluation.scenarios import DEVELOPMENT_LIBRARY


def manifest():
    return LiveManifest(candidate_commit="a" * 40, resources={"src/kong/evaluation/long_driver.py": "b" * 64},
        image="sha256:" + "c" * 64, model={"model": "explicit provider"},
        scenarios=[s.manifest() for s in DEVELOPMENT_LIBRARY], max_requests=480,
        max_total_tokens=4_000_000, max_active_seconds=7200)


def test_eight_task_scope_budget_and_resource_drift_are_checked_before_requests():
    plan = manifest()
    plan = LiveManifest.model_validate_json(plan.model_dump_json())
    plan.verify(DEVELOPMENT_LIBRARY, plan.resources, plan.image, plan.model)
    for tasks, resources, image, model in ((DEVELOPMENT_LIBRARY[:2], plan.resources, plan.image, plan.model),
        (DEVELOPMENT_LIBRARY, {}, plan.image, plan.model),
        (DEVELOPMENT_LIBRARY, plan.resources, "sha256:" + "d" * 64, plan.model),
        (DEVELOPMENT_LIBRARY, plan.resources, plan.image, {"model": "changed"})):
        with pytest.raises(ValueError):
            plan.verify(tasks, resources, image, model)
    for changes in ({"max_requests": 479}, {"max_total_tokens": 120}, {"max_requests": True},
                    {"model": {"api_key": "private"}}, {"scenarios": [DEVELOPMENT_LIBRARY[0].manifest()] * 8}):
        with pytest.raises(ValueError):
            LiveManifest.model_validate({**plan.model_dump(), **changes})
