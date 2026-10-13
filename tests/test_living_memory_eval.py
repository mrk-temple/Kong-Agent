import asyncio

import httpx
import pytest

from kong.config import ModelConfig
from kong.evaluation.living_memory import LivingBudget, LivingCase, TracedCompatibleModel, run_living_memory, score_living_answer, usage_totals
from kong.models.compatible import CompatibleModel


def test_preparation_measures_engineering_and_never_fake_answer_performance(tmp_path):
    report, path = asyncio.run(run_living_memory(tmp_path, prepare_only=True))
    assert path.is_file() and report["behavior_source"] == "engineering"
    assert report["attempted_requests"] == report["answered"] == 0
    assert not report["live_evaluation_executed"]
    assert report["identity_state_unchanged"]
    assert report["metrics"]["answer_correctness"]["measured"] == 0
    formed = next(r for r in report["cases"] if r["case"] == "formation_session_2")
    assert formed["approved_record"]["current"]["version"] == 2
    assert formed["approved_record"]["current"]["episode"]["outcomes"] == ["结果采用 STAGED_RECOVERY_52", "结果验收 BADGE_CONTINUITY_62"]
    reference = next(r for r in report["cases"] if r["case"] == "implicit_reference")
    assert reference["retrieval_accuracy"]["recall"] == 1 and reference["context_injection"]
    assert "Cedar" in reference["planned_query"]["focus"]
    wide = next(r for r in report["cases"] if r["case"] == "long_context_wide")
    limited = next(r for r in report["cases"] if r["case"] == "long_context_limited")
    assert wide["context"]["recent_conversation"]
    assert wide["context_injection"] and limited["context_injection"]
    assert limited["context"]["dropped"]
    assert "EPISODE_PASS_23" not in str(limited["context"]["recent_conversation"])
    assert "EPISODE_PASS_23" in str(limited["context"]["messages"])
    assert limited["estimated_input_tokens"] <= limited["context"]["budget"]["window_tokens"] - limited["context"]["budget"]["output_reserved_tokens"]


def test_live_config_and_remote_authorization_are_required_before_transport(tmp_path):
    with pytest.raises(ValueError, match="explicit real-model"):
        asyncio.run(run_living_memory(tmp_path))
    remote = CompatibleModel(ModelConfig(base_url="https://example.invalid/v1", model="real"))
    with pytest.raises(ValueError, match="authorization"):
        asyncio.run(run_living_memory(tmp_path, remote))
    with pytest.raises(ValueError, match="Prepare-only"):
        asyncio.run(run_living_memory(tmp_path, remote, prepare_only=True))


def test_score_unknown_variants_and_nonapplicable_metrics_are_not_vacuous_passes():
    case = LivingCase("unknown", "What happened?", unknown=True)
    for answer in ("No records of that exist in this thread.", "I don't have any record of that.", "没有可用记录。"):
        assert score_living_answer(case, answer)["answer_correctness"]
    scored = score_living_answer(LivingCase("unscored", "Say hello"), "Hello.")
    assert scored["answer_correctness"] is None and scored["episodic_continuity"] is None
    assert scored["relationship_compliance"] is None
    assert not score_living_answer(case, "We won the award together.")["hallucinated_memories_absent"]


def test_real_transport_test_is_labeled_and_interruption_never_retries(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(402, json={"error": {"message": "synthetic billing interruption"}})
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = CompatibleModel(ModelConfig(base_url="http://localhost:1234/v1", model="transport-test"), client)
            return await run_living_memory(tmp_path, model, transport_test=True)
    report, _ = asyncio.run(execute())
    assert len(requests) == report["attempted_requests"] == 1
    assert report["stop_reason"] == "billing"
    assert report["behavior_source"] == "transport-test" and not report["live_evaluation_executed"]
    assert report["metrics"]["answer_correctness"]["measured"] == 0
    assert all(r["answer"] is None for r in report["cases"])


def test_budget_ends_attempts_and_comparison_uses_identical_packet(tmp_path):
    async def execute():
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"kind":"respond","content":"Not available."}'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            a = CompatibleModel(ModelConfig(base_url="http://localhost:1234/v1", model="a"), client)
            b = CompatibleModel(ModelConfig(base_url="http://localhost:1234/v1", model="b"), client)
            return await run_living_memory(tmp_path, a, comparison_model=b, transport_test=True, budget=LivingBudget(max_requests=4))
    report, _ = asyncio.run(execute())
    assert report["attempted_requests"] == 4 and report["stop_reason"] == "budget_exhausted"
    assert report["model_comparison"]["available"]
    pairs = [r for r in report["cases"] if r["case"] == "cross_session_fact"]
    assert len(pairs) == 2 and pairs[0]["context_hash"] == pairs[1]["context_hash"]
    assert report["cost"]["provider_usage"] is None  # Missing usage is unknown.
    assert report["identity_state_unchanged"]


def test_cost_requires_complete_nonnegative_usage_and_preserves_unknowns():
    rows = [{"request_attempted": True, "usage": None}, {"request_attempted": True,
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}]
    result = usage_totals(rows)
    assert result["usage_measured_requests"] == result["usage_unmeasured_requests"] == 1
    assert result["provider_usage"]["total_tokens"] == 12 and result["currency_cost"] is None


def test_trace_preserves_protocol_failure_content_without_headers(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"kind":"ask","content":"No records."}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = TracedCompatibleModel(ModelConfig(base_url="http://localhost:1234/v1", model="trace"), client)
            return await run_living_memory(tmp_path, model, transport_test=True, budget=LivingBudget(max_requests=1))
    report, _ = asyncio.run(execute())
    row = report["cases"][0]
    assert row["status"] == "protocol_error" and row["error_type"] == "ModelProtocolError"
    assert row["raw_decision"] == '{"kind":"ask","content":"No records."}'
    assert row["protocol_diagnostics"]
    assert report["cost"]["provider_usage"]["total_tokens"] == 12
    assert any(f["raw_decision"] == row["raw_decision"] for f in report["failures"])


def test_valid_clarification_is_scored_without_controller_or_memory_write(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"kind":"ask","question":"No record is available; could you clarify?"}'}}]})
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = TracedCompatibleModel(ModelConfig(base_url="http://localhost:1234/v1", model="clarification"), client)
            return await run_living_memory(tmp_path, model, transport_test=True, budget=LivingBudget(max_requests=1))
    report, _ = asyncio.run(execute())
    row = report["cases"][0]
    assert row["status"] == "answered" and row["interaction_kind"] == "ask"
    assert row["decision"]["question"] == row["answer"]
    assert "formation_success" not in row  # Question did not execute Runtime or grant Memory authority.
    missing = next(r for r in report["cases"] if r["case"] == "formed_episode")
    assert missing["unresolved_fixture_refs"] == ["formed_episode"]
    assert missing["retrieval_accuracy"]["recall"] == 0 and not missing["context_injection"]
