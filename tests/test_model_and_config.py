import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from kong.config import ModelConfig, load_config
from kong.context import Context
from kong.contracts import FinalResponse, Status
from kong.models.base import ModelError
from kong.models.compatible import CompatibleModel
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def config(**kwargs):
    return ModelConfig(base_url="http://localhost:11434/v1", model="local-model", **kwargs)


def response(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def test_compatible_transport_exact_endpoint_headers_and_single_request():
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "http://localhost:11434/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key-not-real"
        body = json.loads(request.content)
        assert body["model"] == "local-model"
        assert body["response_format"] == {"type": "json_object"}
        assert body["stream"] is False
        return httpx.Response(200, json=response('{"kind":"respond","content":"你好"}'))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = CompatibleModel(config(api_key=SecretStr("test-key-not-real")), client)
            result = await model.generate(Context(messages=[{"role": "user", "content": "hi"}]))
            assert isinstance(result, FinalResponse)
    asyncio.run(scenario())
    assert len(requests) == 1


def test_local_no_key_and_optional_json_mode():
    def handler(request):
        assert "authorization" not in request.headers
        assert "response_format" not in json.loads(request.content)
        return httpx.Response(200, json=response('```json\n{"kind":"respond","content":"OK"}\n```'))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert (await CompatibleModel(config(json_mode=False), client).generate(Context([]))).content == "OK"
    asyncio.run(scenario())


def test_explicit_thinking_setting_and_safe_usage_metadata():
    def handler(request):
        assert json.loads(request.content)["enable_thinking"] is False
        body = response('{"kind":"respond","content":"ok"}')
        body["usage"] = {"prompt_tokens":12, "completion_tokens":4, "total_tokens":16, "private":"not copied"}
        return httpx.Response(200,json=body)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = CompatibleModel(config(enable_thinking=False),client)
            await model.generate(Context([]))
            assert model.last_usage == {"prompt_tokens":12,"completion_tokens":4,"total_tokens":16}
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [response(""), response("not-json"), response('{"kind":"unknown"}'),
    response('{"kind":"respond","content":"done","approved":true}'), response(None), {"choices": []},
    response('{}', "length")])
def test_invalid_responses_never_become_completion(body):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as client:
            with pytest.raises(ModelError):
                await CompatibleModel(config(), client).generate(Context([]))
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500])
def test_provider_errors_do_not_leak_response_or_key(status):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="secret-in-body"))) as client:
            with pytest.raises(ModelError) as exc:
                await CompatibleModel(config(api_key=SecretStr("secret-key")), client).generate(Context([]))
            assert str(status) in str(exc.value)
            assert "secret" not in str(exc.value)
    asyncio.run(scenario())


def test_timeout_is_safe_and_not_retried():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private details")
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ModelError, match="timed out"):
                await CompatibleModel(config(), client).generate(Context([]))
    asyncio.run(scenario())
    assert len(calls) == 1


def test_profile_loading_without_serializing_secret(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('default_profile="local"\n[profiles.local]\nbase_url="http://localhost:11434/v1"\nmodel="small"\napi_key_env="TEST_KONG_KEY"', encoding="utf-8")
    monkeypatch.setenv("TEST_KONG_KEY", "private-key")
    loaded = load_config(path)
    assert loaded.api_key.get_secret_value() == "private-key"
    assert "private-key" not in repr(loaded)
    assert "api_key" not in loaded.model_dump()


@pytest.mark.parametrize("url", ["file:///private", "https://key@example.com/v1", "https://example.com/v1?key=secret"])
def test_reject_credential_urls(url):
    with pytest.raises(ValidationError):
        ModelConfig(base_url=url, model="x")


def test_http_model_runtime_roundtrip(tmp_path):
    (tmp_path / "info.txt").write_text("real environment evidence", encoding="utf-8")
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            content = '{"kind":"act","actions":[{"name":"read_file","arguments":{"path":"info.txt"}}]}'
        else:
            audit = body["messages"][-1]["content"]
            assert "real environment evidence" in audit
            assert '"action_id":"a1"' in audit
            content = '{"kind":"respond","content":"读取成功","evidence_ids":["a1"]}'
        return httpx.Response(200, json=response(content))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            workspace = Workspace(tmp_path)
            runtime = Runtime.new("读取 info.txt", CompatibleModel(config(), client), workspace, default_registry(workspace))
            assert (await runtime.run()).status == Status.COMPLETED
    asyncio.run(scenario())
    assert len(requests) == 2
