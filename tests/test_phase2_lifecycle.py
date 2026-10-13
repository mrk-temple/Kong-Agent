import asyncio
import json
import httpx
import pytest
from pydantic import SecretStr
from kong.application.service import ApplicationService
from kong.application.contracts import (
    RunCreate,
    ThreadCreate,
    HumanCommand,
    InputCommand,
    MemoryCreate,
    FreshCommand,
    AppError,
)
from kong.application.tracing import message_hash
from kong.config import ModelConfig
from kong.contracts import (
    Act,
    Action,
    FinalResponse,
    PlanProposal,
    Plan,
    PlanStep,
    Criterion,
    CompletePlanStep,
)
from kong.models.fake import ScriptedModel
from kong.models.compatible import CompatibleModel
from kong.memory.errors import PolicyViolation


def execute(test):
    return asyncio.run(test())


def test_plan_exact_approval_and_real_file(tmp_path):
    async def check():
        criterion = Criterion(
            id="file",
            description="caller text",
            kind="file_equals",
            path="result.txt",
            expected_text="verified",
        )
        model = ScriptedModel(
            [
                PlanProposal(
                    plan=Plan(
                        goal="write result.txt",
                        rationale="bounded file task",
                        steps=[PlanStep(id="write", title="write file")],
                        success_criteria=[criterion],
                    )
                ),
                Act(
                    actions=[
                        Action(
                            name="write_file",
                            arguments={"path": "result.txt", "content": "verified"},
                        )
                    ]
                ),
                CompletePlanStep(step_id="write", evidence_ids=["a1"]),
                FinalResponse(content="任务完成", evidence_ids=["a1"]),
            ]
        )
        async with ApplicationService(tmp_path, model_factory=lambda: model) as app:
            thread = await app.create_thread(ThreadCreate(command_id="t"))
            result = await app.create_run(
                thread["thread_id"],
                RunCreate(
                    command_id="r",
                    text="write result.txt",
                    intent="task",
                    mode="plan",
                    acceptance=[criterion],
                ),
            )
            await app.wait(result["run_id"])
            detail = await app.run_detail(result["run_id"])
            assert detail["status"] == "waiting_user"
            assert not (app.factory.workspace.root / "result.txt").exists()
            with pytest.raises(AppError):
                await app.human(
                    result["run_id"],
                    HumanCommand(command_id="wrong", kind="approve", revision=999),
                )
            await app.human(
                result["run_id"],
                HumanCommand(
                    command_id="approve",
                    kind="approve",
                    revision=detail["pending_approval"]["revision"],
                ),
            )
            await app.wait(result["run_id"])
            assert (await app.run_detail(result["run_id"]))[
                "outcome_kind"
            ] == "task_accepted"
            assert (app.factory.workspace.root / "result.txt").read_text() == "verified"

    execute(check)


def test_cancel_single_driver_and_resume(tmp_path):
    async def check():
        class Blocking:
            def __init__(self):
                self.calls = 0
                self.started = asyncio.Event()

            async def generate(self, context):
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    await asyncio.Event().wait()
                return FinalResponse(content="恢复后答复")

        model = Blocking()
        async with ApplicationService(tmp_path, model_factory=lambda: model) as app:
            thread = await app.create_thread(ThreadCreate(command_id="t"))
            result = await app.create_run(
                thread["thread_id"],
                RunCreate(command_id="r", text="hello", intent="chat"),
            )
            await model.started.wait()
            with pytest.raises(AppError):
                await app.create_run(
                    thread["thread_id"],
                    RunCreate(command_id="r2", text="duplicate actor", intent="chat"),
                )
            await app.inputs(
                result["run_id"], InputCommand(command_id="cancel", kind="cancel")
            )
            await app.wait(result["run_id"])
            assert (await app.run_detail(result["run_id"]))["can_resume"]
            await app.human(
                result["run_id"], HumanCommand(command_id="resume", kind="resume")
            )
            await app.wait(result["run_id"])
            assert (await app.run_detail(result["run_id"]))[
                "outcome_kind"
            ] == "answer_finished"

    execute(check)


def test_partial_membership_recovery_never_remembers_twice(tmp_path):
    async def check():
        async with ApplicationService(
            tmp_path, model_factory=lambda: ScriptedModel([])
        ) as app:
            original = app.admin.core.add_memory_to_bottle
            app.admin.core.add_memory_to_bottle = lambda *a, **k: (_ for _ in ()).throw(
                PolicyViolation("fixture interruption")
            )
            body = MemoryCreate(command_id="remember", content="稳定偏好")
            receipt = await app.memory_create(body)
            assert receipt["status"] == "partial"
            app.admin.core.add_memory_to_bottle = original
            restored = await app.memory_create(body)
            assert (
                restored["status"] == "committed"
                and restored["record_id"] == receipt["record_id"]
            )
            assert len((await app.memory_records())["items"]) == 1

    execute(check)


@pytest.mark.parametrize("protocol_failure", [False, True])
def test_actual_payload_receipts_and_protocol_failure(tmp_path, protocol_failure):
    async def check():
        payloads = []

        def provider(request):
            payloads.append(json.loads(request.content))
            content = (
                "{bad}"
                if protocol_failure
                else json.dumps({"kind": "respond", "content": "中文接受回答"})
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": content}, "finish_reason": "stop"}
                    ],
                    "usage": {"total_tokens": 30},
                },
            )

        config = ModelConfig(
            base_url="http://fixture.invalid/v1",
            model="fixture",
            api_key=SecretStr("hidden-fixture-key"),
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)
        ) as transport:
            async with ApplicationService(
                tmp_path,
                model_config=config,
                model_factory=lambda: CompatibleModel(config, transport),
            ) as app:
                saved = await app.memory_create(
                    MemoryCreate(
                        command_id="save",
                        content="预览偏好：中文回答带 BLUE47。",
                        category="preference",
                    )
                )
                thread = await app.create_thread(ThreadCreate(command_id="t"))
                receipt = await app.create_run(
                    thread["thread_id"],
                    RunCreate(
                        command_id="r",
                        text="我的预览偏好是什么？",
                        intent="chat",
                        mode="fast",
                    ),
                )
                await app.wait(receipt["run_id"])
                trace = (await app.context_trace(receipt["run_id"]))["items"]
                delivered = next(
                    e for e in trace if e["kind"] == "memory.request_delivered"
                )
                assert delivered["data"]["message_sha256"] == message_hash(
                    payloads[0]["messages"]
                )
                assert any(
                    m["record_id"] == saved["record_id"]
                    for m in delivered["data"]["selected"]
                )
                assert "hidden-fixture-key" not in json.dumps(trace)
                if protocol_failure:
                    assert any(
                        e["kind"] == "memory.request_failed"
                        and e["data"]["delivery_known"] == "yes"
                        for e in trace
                    )
                    assert not app.runtimes[receipt["run_id"]].history.observations

    execute(check)


def test_memory_update_fresh_run_and_forget_blocks_old_content(tmp_path):
    async def check():
        async with ApplicationService(
            tmp_path,
            model_factory=lambda: ScriptedModel([FinalResponse(content="回答")]),
        ) as app:
            saved = await app.memory_create(
                MemoryCreate(command_id="save", content="验收标签为 BLUE47。")
            )
            thread = await app.create_thread(ThreadCreate(command_id="t"))
            receipt = await app.create_run(
                thread["thread_id"],
                RunCreate(
                    command_id="r", text="验收标签是什么？", intent="chat", mode="fast"
                ),
            )
            await app.wait(receipt["run_id"])
            record = await app.memory_record(saved["record_id"])
            await app.memory_revise(
                saved["record_id"], "验收标签为 GREEN82。", record["revision"], "revise"
            )
            assert (await app.run_detail(receipt["run_id"]))["fresh_required"]
            fresh = await app.fresh(
                receipt["run_id"],
                FreshCommand(
                    command_id="fresh",
                    text="验收标签是什么？",
                    intent="chat",
                    mode="fast",
                ),
            )
            await app.wait(fresh["run_id"])
            packet = app.runtimes[fresh["run_id"]].context_engine.last_packet
            assert "GREEN82" in json.dumps(
                packet.messages
            ) and "BLUE47" not in json.dumps(packet.messages)
            assert (
                app.runtimes[fresh["run_id"]].context_engine.memory_binding["capture"]
                == "off"
            )
            preview = await app.memory_preview(saved["record_id"])
            await app.memory_forget(
                saved["record_id"],
                preview["expected_revision"],
                preview["digest"],
                "forget",
            )
            after = await app.fresh(
                fresh["run_id"],
                FreshCommand(
                    command_id="after",
                    text="验收标签是什么？",
                    intent="chat",
                    mode="fast",
                ),
            )
            await app.wait(after["run_id"])
            assert "GREEN82" not in json.dumps(
                app.runtimes[after["run_id"]].context_engine.last_packet.messages
            )

    execute(check)
