import asyncio
import json

import httpx
import pytest

from kong.config import ModelConfig
from kong.evaluation.memory_behavior import EvalBudget, OfflineBehaviorModel, check_live_config, compare_behavior_reports, run_memory_behavior
from kong.models.compatible import CompatibleModel


def test_offline_stages_and_sessions_are_independent(tmp_path):
    report, path = asyncio.run(run_memory_behavior(tmp_path))
    assert path.is_file()
    assert report["behavior_source"] == "simulated"
    assert not report["live_evaluation_executed"]
    assert report["answered"] == 10
    for name, metric in report["metrics"].items():
        if name == "context_exclusions_compliance":
            assert metric == {"passed": 4, "measured": 4, "unmeasured": 6}
            continue
        if name == "natural_reference":
            assert metric == {"passed": 1, "measured": 1, "unmeasured": 9}
            continue
        assert metric == {"passed": 10, "measured": 10, "unmeasured": 0}


def test_bad_answers_do_not_get_credit_from_retrieval(tmp_path):
    report, _ = asyncio.run(run_memory_behavior(tmp_path, OfflineBehaviorModel(wrong_answers=True), model_name="deliberately-wrong"))
    assert report["metrics"]["retrieval_success"]["passed"] == 10
    assert report["metrics"]["context_injection_success"]["passed"] == 10
    assert report["metrics"]["answer_correctness"]["passed"] < 10
    assert report["metrics"]["answer_privacy_compliance"]["passed"] < 10
    assert report["metrics"]["persona_consistency"]["passed"] == 0


def test_budget_stops_without_inventing_unattempted_answers(tmp_path):
    report, _ = asyncio.run(run_memory_behavior(tmp_path, budget=EvalBudget(max_requests=1)))
    assert report["attempted_requests"] == 1
    assert report["stop_reason"] == "budget_exhausted"
    assert report["metrics"]["answer_correctness"]["unmeasured"] == 9
    assert all(row["answer"] is None and row["usage"] is None for row in report["cases"][1:])


def test_remote_and_placeholder_profiles_are_gated_before_network():
    with pytest.raises(ValueError, match="authorization"):
        check_live_config(ModelConfig(base_url="https://example.invalid/v1", model="real-model"))
    with pytest.raises(ValueError, match="placeholder"):
        check_live_config(ModelConfig(base_url="http://127.0.0.1:1234/v1", model="your-loaded-model-id"))
    check_live_config(ModelConfig(base_url="http://localhost:1234/v1", model="local-loaded"))


def test_provider_interruption_is_not_answer_failure_or_retry(tmp_path):
    class Broken:
        async def generate(self, packet):
            raise TimeoutError()
    report, _ = asyncio.run(run_memory_behavior(tmp_path, Broken()))
    assert report["attempted_requests"] == 1
    assert report["answered"] == 0
    assert report["metrics"]["answer_correctness"]["unmeasured"] == 10


def test_real_transport_contract_with_mock_http_and_model_comparison(tmp_path):
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"kind":"respond","content":"Not available."}'}}]})
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            model = CompatibleModel(ModelConfig(base_url="http://127.0.0.1:1234/v1", model="test-transport"), client)
            return await run_memory_behavior(tmp_path, model, live=True, transport_test=True, model_name="HTTP-mock-test-only", budget=EvalBudget(max_requests=2))
    report, _ = asyncio.run(execute())
    assert len(requests) == 2 and report["answered"] == 2
    assert report["behavior_source"] == "transport-test" and not report["live_evaluation_executed"]
    assert all(p["model"] == "test-transport" for p in requests)
    first, _ = asyncio.run(run_memory_behavior(tmp_path))
    second, _ = asyncio.run(run_memory_behavior(tmp_path, OfflineBehaviorModel(wrong_answers=True), model_name="wrong-test-double"))
    assert compare_behavior_reports([first, second])["models"][0]["metrics"]["answer_correctness"]["passed"] > second["metrics"]["answer_correctness"]["passed"]
    with pytest.raises(ValueError):
        compare_behavior_reports([first, report])


def test_missing_identity_injection_cannot_receive_empty_expected_ids_credit(tmp_path, monkeypatch):
    from kong.continuity.engine import ContextEngine
    original = ContextEngine.prepare
    async def missing_identity(self, **kwargs):
        packet = await original(self, **kwargs)
        packet.items = [i for i in packet.items if i.category != "core_identity"]
        packet.messages = [m for m in packet.messages if "APPROVED KONG IDENTITY" not in m["content"]]
        return packet
    monkeypatch.setattr(ContextEngine, "prepare", missing_identity)
    report, _ = asyncio.run(run_memory_behavior(tmp_path))
    assert report["metrics"]["retrieval_success"]["passed"] == 10
    assert report["metrics"]["context_injection_success"]["passed"] == 0
    assert report["metrics"]["answer_correctness"]["passed"] == 10  # Deliberately separate, even when a fake guesses.


def test_private_leak_in_other_answer_is_caught_and_rubric_versions_do_not_mix(tmp_path):
    from kong.evaluation.memory_behavior import BehaviorCase, score_answer
    scored = score_answer(BehaviorCase("public", "venue", answer_terms=("VENUE_AMBER_17",)), "VENUE_AMBER_17 and INACTIVE_PRIVATE_31")
    assert scored["answer_correctness"] and not scored["answer_privacy_compliance"]
    assert scored["natural_reference"] is None
    report, _ = asyncio.run(run_memory_behavior(tmp_path))
    old = {**report, "rubric_version": 1}
    with pytest.raises(ValueError, match="rubric"):
        compare_behavior_reports([report, old])
