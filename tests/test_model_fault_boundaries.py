import asyncio

import httpx
import pytest

from kong.config import ModelConfig
from kong.context import Context
from kong.evaluation import runner
from kong.models.base import ModelError
from kong.models.compatible import CompatibleModel


@pytest.mark.parametrize("status,category,retryable,fatal", [(401,"authentication",False,True), (402,"billing",False,True),
    (404,"configuration",False,True), (429,"rate_limit",True,False), (503,"service_http",True,False)])
def test_provider_fault_is_classified_without_exposing_response_or_retrying(status, category, retryable, fatal):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error":{"message":"PRIVATE_CONTENT_CANARY"}})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = CompatibleModel(ModelConfig(base_url="http://localhost:11434/v1", model="test"), client)
            with pytest.raises(ModelError) as error:
                await model.generate(Context([]))
            assert error.value.category == category
            assert error.value.retryable is retryable and error.value.fatal_configuration is fatal
            assert error.value.status_code == status
            assert "PRIVATE_CONTENT_CANARY" not in str(error.value)
    asyncio.run(scenario())
    assert len(calls) == 1


def test_known_quota_error_does_not_become_a_rate_limit_retry():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(429, json={"error":{"code":"insufficient_quota"}}))) as client:
            with pytest.raises(ModelError) as error:
                await CompatibleModel(ModelConfig(base_url="http://localhost:11434/v1", model="test"), client).generate(Context([]))
            assert error.value.category == "billing" and not error.value.retryable
    asyncio.run(scenario())


def test_suite_stops_on_fatal_configuration_and_reports_unattempted_trials(tmp_path, monkeypatch):
    calls = []
    async def unavailable(self, context):
        calls.append(context)
        raise ModelError("Model service HTTP 402. Check account status.", category="billing", status_code=402)
    monkeypatch.setattr(runner.MeteredModel, "generate", unavailable)
    root, summary = asyncio.run(runner.run_suite(tmp_path, repetitions=2, live=True,
        config=ModelConfig(base_url="http://localhost:11434/v1", model="test")))
    assert len(calls) == summary["runs"] == 1
    assert summary["planned_runs"] == 24 and summary["not_attempted"] == 23
    assert not summary["complete"] and summary["stopped_reason"] == "fatal_model_configuration"
    assert summary["model_pass_rate_all_attempts"] == 0 and summary["usage_complete"] is False
    assert len(list(root.glob("cases/*/trial-*/result.json"))) == 1
