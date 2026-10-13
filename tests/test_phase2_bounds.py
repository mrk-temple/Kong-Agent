import asyncio
import json
import httpx
import pytest
from kong.application.events import EventStore
from kong.application.service import ApplicationService
from kong.application.contracts import AppError, MemoryCreate, ThreadCreate, RunCreate
from kong.local_api.app import create_app
from kong.models.compatible import CompatibleModel
from kong.config import ModelConfig
from kong.models.fake import ScriptedModel
from kong.contracts import FinalResponse
from fastapi.testclient import TestClient


def test_event_retention_watermark_and_bounded_pages(tmp_path):
    store = EventStore(tmp_path / "events.db", "one")
    for i in range(600):
        store.append("fixture", {}, origin=str(i))
    assert len(store.read()) == 256 and len(store.recent()) == 256
    assert store.recent()[0]["sequence"] == 345
    with store.db:
        store.db.execute("DELETE FROM events WHERE sequence<300")
    with pytest.raises(AppError) as e:
        store.read("one:2")
    assert e.value.status == 410
    with store.db:
        store.db.execute("DELETE FROM events")
    assert store.cursor == "one:600" and store.read("one:600") == []
    with pytest.raises(AppError) as e:
        store.read("one:599")
    assert e.value.status == 410
    with pytest.raises(AppError):
        store.read("two:600")
    store.close()


def test_restart_does_not_replay_trimmed_events(tmp_path):
    async def check():
        async with ApplicationService(
            tmp_path, model_factory=lambda: ScriptedModel([FinalResponse(content="hi")])
        ) as app:
            t = await app.create_thread(ThreadCreate(command_id="t"))
            r = await app.create_run(
                t["thread_id"], RunCreate(command_id="r", text="hi", intent="chat")
            )
            await app.wait(r["run_id"])
            last = app.events.cursor
            with app.events.db:
                app.events.db.execute("DELETE FROM events")
        async with ApplicationService(
            tmp_path, model_factory=lambda: ScriptedModel([])
        ) as app:
            assert app.events.cursor == last and app.events.recent() == []
            assert (await app.run_detail(r["run_id"]))[
                "outcome_kind"
            ] == "answer_finished"

    asyncio.run(check())


def test_memory_keyset_bound_to_query_and_inactive_state(tmp_path):
    async def check():
        async with ApplicationService(
            tmp_path, model_factory=lambda: ScriptedModel([])
        ) as app:
            for i in range(12):
                await app.memory_create(
                    MemoryCreate(command_id="m" + str(i), content="搜索项目 " + str(i))
                )
            page = await app.memory_records(limit=5, query="搜索")
            second = await app.memory_records(
                limit=5, query="搜索", after=page["next_cursor"]
            )
            assert not {r["record_id"] for r in page["items"]} & {
                r["record_id"] for r in second["items"]
            }
            with pytest.raises(ValueError):
                await app.memory_records(
                    limit=5, query="其它", after=page["next_cursor"]
                )
            app.admin.pending = 16
            with pytest.raises(AppError) as e:
                await app.memory_records()
            assert e.value.status == 429
            app.admin.pending = 0

    asyncio.run(check())


def test_chunked_request_size_is_bounded(tmp_path):
    with TestClient(
        create_app(tmp_path, origin="http://127.0.0.1:8765", token="fixture"),
        base_url="http://127.0.0.1:8765",
    ) as api:
        response = api.post(
            "/v1/threads",
            content=iter([b"x" * 128000, b"x" * 128001]),
            headers={
                "Origin": "http://127.0.0.1:8765",
                "Authorization": "Bearer fixture",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 413


def test_stream_200_receipt_is_real_and_precedes_protocol_parse():
    async def check():
        stages = []
        content = json.dumps({"kind": "respond", "content": "流式中文"})
        chunk = {
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        stream = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, text=stream
                )
            )
        ) as client:
            model = CompatibleModel(
                ModelConfig(
                    base_url="http://fixture.invalid/v1", model="fixture", stream=True
                ),
                client,
            )
            model.on_request = lambda stage, data: stages.append(stage)
            from kong.context import Context

            response = await model.generate(
                Context(messages=[{"role": "user", "content": "你好"}])
            )
            assert response.content == "流式中文" and stages == [
                "dispatched",
                "delivered",
            ]
            assert model.last_usage["total_tokens"] == 15

    asyncio.run(check())


def test_inspector_reads_do_not_compete_with_runtime_bridge(tmp_path):
    async def check():
        async with ApplicationService(
            tmp_path, model_factory=lambda: ScriptedModel([FinalResponse(content="hi")])
        ) as app:
            t = await app.create_thread(ThreadCreate(command_id="t"))
            r = await app.create_run(
                t["thread_id"], RunCreate(command_id="r", text="hi", intent="chat")
            )
            await app.wait(r["run_id"])
            bridge = app.runtimes[r["run_id"]].context_engine.memory_bridge
            assert bridge._slot.acquire(blocking=False)
            try:
                await app.memory_create(
                    MemoryCreate(command_id="new-memory", content="明确的新偏好")
                )
                assert (await app.run_detail(r["run_id"]))["fresh_required"]
            finally:
                bridge._slot.release()

    asyncio.run(check())


def test_known_running_snapshot_restores_paused_without_model_replay(tmp_path):
    async def check():
        from kong.contracts import Status

        async with ApplicationService(
            tmp_path,
            model_factory=lambda: ScriptedModel([FinalResponse(content="first")]),
        ) as app:
            thread = await app.create_thread(ThreadCreate(command_id="t"))
            receipt = await app.create_run(
                thread["thread_id"],
                RunCreate(command_id="r", text="hello", intent="chat"),
            )
            await app.wait(receipt["run_id"])
            runtime = app.runtimes[receipt["run_id"]]
            runtime.state.status = Status.RUNNING
            runtime.save()
        model = ScriptedModel([FinalResponse(content="explicit resume")])
        async with ApplicationService(
            tmp_path, model_factory=lambda: model
        ) as restored:
            detail = await restored.run_detail(receipt["run_id"])
            assert detail["status"] == "stopped" and detail["can_resume"]
            assert not restored.tasks
            from kong.application.contracts import HumanCommand

            await restored.human(
                receipt["run_id"], HumanCommand(command_id="resume", kind="resume")
            )
            await restored.wait(receipt["run_id"])
            assert (await restored.run_detail(receipt["run_id"]))[
                "outcome_kind"
            ] == "answer_finished"

    asyncio.run(check())
