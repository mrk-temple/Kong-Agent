import asyncio
import json

import httpx
import pytest

from kong.config import ModelConfig
from kong.context import Context
from kong.contracts import Status
from kong.models.base import ModelProtocolError
from kong.models.compatible import CompatibleModel
from kong.runtime.loop import Runtime
from kong.tools.defaults import default_registry
from kong.workspace import Workspace


def chunk(content="", finish=None, usage=None):
    payload = {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]}
    if usage is not None:
        payload["usage"] = usage
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts = parts
    async def __aiter__(self):
        for part in self.parts:
            yield part
    async def aclose(self):
        pass


def config():
    return ModelConfig(base_url="http://localhost/v1", model="fixture", stream=True)


def test_stream_chunks_only_become_a_decision_after_full_validation():
    async def scenario():
        wire = b"".join([chunk('{"kind":"respond",'), chunk('"content":"hello"}', "stop", {"total_tokens": 17}),
                         b"data: [DONE]\n\n"])
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=ByteStream([wire[:13], wire[13:47], wire[47:]]))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = CompatibleModel(config(), client)
            progress = []
            model.on_stream = progress.append
            result = await model.generate(Context([]))
            assert result.content == "hello" and model.last_usage == {"total_tokens": 17}
            assert progress and all(set(p) == {"received_chars", "decision_chars"} for p in progress)
            assert json.loads(requests[0].content)["stream"] is True
            assert len(requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("parts", [
    [chunk('{"kind":"act","actions":[{"name":"write_file","arguments":{"path":"forbidden.txt","content":"x"}}]}')],
    [chunk('{"kind":"respond","content":"partial"}', "length"), b"data: [DONE]\n\n"],
    [b'data: {"choices": "invalid"}\n\n', b"data: [DONE]\n\n"],
    [chunk('{"kind":"unknown","private":"SECRET_CANARY"}', "stop"), b"data: [DONE]\n\n"],
    [chunk('{"kind":"respond",', "stop"), b"data: [DONE]\n\n"],
])
def test_incomplete_or_invalid_stream_cannot_execute_actions(tmp_path, parts):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ByteStream(parts)))) as client:
            model = CompatibleModel(config(), client)
            workspace = Workspace(tmp_path)
            runtime = Runtime.new("write", model, workspace, default_registry(workspace))
            assert (await runtime.run()).status == Status.WAITING_USER
            assert not runtime.history.observations and not (tmp_path / "forbidden.txt").exists()
            assert runtime.metrics.model_calls == 2  # One visible protocol repair; no tool recovery.
            assert "SECRET_CANARY" not in runtime.history.model_dump_json()
    asyncio.run(scenario())


def test_valid_json_fragment_does_not_start_write_until_stream_finish(tmp_path):
    async def scenario():
        partial, release = asyncio.Event(), asyncio.Event()
        class Delayed(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield chunk('{"kind":"act","actions":[{"name":"write_file","arguments":{"path":"result.txt","content":"ok"}}]}')
                partial.set()
                await release.wait()
                yield chunk("", "stop") + b"data: [DONE]\n\n"
            async def aclose(self):
                pass
        requests = []
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                stream = Delayed()
            else:
                stream = ByteStream([chunk('{"kind":"respond","content":"done","evidence_ids":["a1"]}', "stop"),
                                     b"data: [DONE]\n\n"])
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            workspace = Workspace(tmp_path)
            runtime = Runtime.new("write", CompatibleModel(config(), client), workspace, default_registry(workspace))
            from kong.contracts import Criterion
            runtime.state.success_criteria = [Criterion(id="out", description="written value", kind="file_equals",
                                                       path="result.txt", expected_text="ok")]
            task = asyncio.create_task(runtime.run())
            await partial.wait()
            assert not (tmp_path / "result.txt").exists() and not runtime.pending_action_id
            release.set()
            assert (await task).status == Status.COMPLETED
            assert (tmp_path / "result.txt").read_text() == "ok"
    asyncio.run(scenario())


def test_stream_total_deadline_bounds_endless_heartbeats():
    async def scenario():
        class Heartbeats(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b": heartbeat\n\n"
                    await asyncio.sleep(0.005)
            async def aclose(self):
                pass
        cfg = config()
        cfg.timeout = 0.03
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Heartbeats()))) as client:
            from kong.models.base import ModelError
            with pytest.raises(ModelError) as exc:
                await CompatibleModel(cfg, client).generate(Context([]))
            assert exc.value.category == "timeout" and exc.value.retryable
    asyncio.run(scenario())


def test_stream_parser_error_does_not_include_response_body():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={"content-type": "text/event-stream"},
                stream=ByteStream([b'data: PRIVATE_SECRET_BAD_JSON\n\n'])))) as client:
            with pytest.raises(ModelProtocolError) as exc:
                await CompatibleModel(config(), client).generate(Context([]))
            assert "PRIVATE_SECRET" not in str(exc.value)
    asyncio.run(scenario())


def test_provider_thinking_control_is_explicit_and_fingerprinted(tmp_path):
    from kong.evaluation.runner import run_suite
    from pydantic import ValidationError
    cfg = config()
    cfg.thinking_mode = "disabled"
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"kind":"respond","content":"ok"}'},
                                                      "finish_reason": "stop"}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await CompatibleModel(cfg, client).generate(Context([]))
        assert requests[0]["thinking"] == {"type": "disabled"} and "enable_thinking" not in requests[0]
        root, _ = await run_suite(tmp_path, case_ids=["unknown_action_restore"], repetitions=1,
                                  live=True, config=cfg)
        manifest = json.loads((root / "manifest.json").read_text())
        assert manifest["configuration"]["stream"] and manifest["configuration"]["thinking_mode"] == "disabled"
    asyncio.run(scenario())
    with pytest.raises(ValidationError):
        ModelConfig(base_url="http://localhost/v1", model="x", enable_thinking=False, thinking_mode="disabled")


def test_oversized_sse_line_is_bounded_without_waiting_for_newline():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={"content-type": "text/event-stream"},
                stream=ByteStream([b"data: " + b"x" * 128001])))) as client:
            with pytest.raises(ModelProtocolError, match="bounded event limit"):
                await CompatibleModel(config(), client).generate(Context([]))
    asyncio.run(scenario())
