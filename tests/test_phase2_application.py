import asyncio
import pytest
from kong.application.contracts import (
    RunCreate,
    ThreadCreate,
    MemoryCreate,
    HumanCommand,
    AppError,
)
from kong.application.service import ApplicationService
from kong.models.fake import ScriptedModel
from kong.contracts import FinalResponse as Respond, NeedUserInput as Ask, Act, Action


def run(test):
    return asyncio.run(test())


def test_missing_task_acceptance_never_calls_model(tmp_path):
    async def check():
        model = ScriptedModel([Respond(kind="respond", content="请提供验收条件")])
        async with ApplicationService(tmp_path, model_factory=lambda: model) as app:
            thread = await app.create_thread(ThreadCreate(command_id="thread-one"))
            for acceptance in (
                [],
                [
                    {
                        "id": "weak",
                        "description": "exists",
                        "kind": "file_exists",
                        "path": "x.txt",
                    }
                ],
            ):
                with pytest.raises(AppError) as error:
                    await app.create_run(
                        thread["thread_id"],
                        RunCreate(
                            command_id="missing",
                            text="交付环境",
                            intent="task",
                            acceptance=acceptance,
                        ),
                    )
                assert error.value.code == "acceptance_required"
            assert model.calls == 0
            assert not app.factory.store.list_runs()

    run(check)


def test_chat_idempotency_and_restart(tmp_path):
    async def check():
        model = ScriptedModel([Respond(kind="respond", content="你好")])
        async with ApplicationService(tmp_path, model_factory=lambda: model) as app:
            thread = await app.create_thread(ThreadCreate(command_id="t"))
            request = RunCreate(command_id="r", text="你好", intent="chat", mode="fast")
            first = await app.create_run(thread["thread_id"], request)
            assert await app.create_run(thread["thread_id"], request) == first
            with pytest.raises(AppError):
                await app.create_run(
                    thread["thread_id"], request.model_copy(update={"text": "other"})
                )
            await app.wait(first["run_id"])
            detail = await app.run_detail(first["run_id"])
            assert detail["outcome_kind"] == "answer_finished"
            assert detail["messages"][-1]["content"] == "你好"
            assert model.calls == 1
        async with ApplicationService(
            tmp_path, model_factory=lambda: ScriptedModel([])
        ) as restored:
            assert await restored.create_run(thread["thread_id"], request) == first
            assert (await restored.run_detail(first["run_id"]))["status"] == "completed"

    run(check)


def test_memory_cas_forget_and_replay_has_no_content(tmp_path):
    async def check():
        async with ApplicationService(
            tmp_path, model_factory=lambda: ScriptedModel([])
        ) as app:
            body = MemoryCreate(
                command_id="save-one", content="偏好简短中文回答", category="preference"
            )
            first = await app.memory_create(body)
            assert first["status"] == "committed"
            assert (await app.memory_create(body))["record_id"] == first["record_id"]
            record = await app.memory_record(first["record_id"])
            revision = record["revision"]
            await app.memory_revise(
                first["record_id"], "改为详细中文回答", revision, "edit-one"
            )
            with pytest.raises(AppError):
                await app.memory_revise(
                    first["record_id"], "stale", revision, "edit-two"
                )
            preview = await app.memory_preview(first["record_id"])
            await app.memory_forget(
                first["record_id"],
                preview["expected_revision"],
                preview["digest"],
                "forget-one",
            )
            assert not (await app.memory_records())["items"]
            replay = str(app.events.read())
            assert "简短" not in replay and "详细" not in replay

    run(check)


def test_failed_read_asks_then_owner_can_stop(tmp_path):
    async def check():
        model = ScriptedModel(
            [
                Act(
                    kind="act",
                    actions=[
                        Action(name="read_file", arguments={"path": "missing.txt"})
                    ],
                ),
                Respond(kind="respond", content="已停止", evidence_ids=[]),
                Ask(kind="ask", question="读取失败，请提供文件或结束任务。"),
            ]
        )
        async with ApplicationService(tmp_path, model_factory=lambda: model) as app:
            thread = await app.create_thread(ThreadCreate(command_id="t"))
            receipt = await app.create_run(
                thread["thread_id"],
                RunCreate(
                    command_id="r",
                    text="读missing.txt，失败则问我",
                    intent="task",
                    acceptance=[
                        {
                            "id": "read",
                            "description": "读取实际文本",
                            "kind": "tool_result",
                            "tool_name": "read_file",
                            "output_match": {"text": "expected"},
                        }
                    ],
                ),
            )
            await app.wait(receipt["run_id"])
            detail = await app.run_detail(receipt["run_id"])
            assert detail["status"] == "waiting_user"
            assert detail["outcome_kind"] != "task_accepted"
            assert len(app.runtimes[receipt["run_id"]].history.observations) == 1
            await app.human(
                receipt["run_id"], HumanCommand(command_id="stop", kind="reject")
            )
            assert (await app.run_detail(receipt["run_id"]))["status"] == "stopped"

    run(check)
