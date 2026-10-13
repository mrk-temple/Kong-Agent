"""One application actor, shared by HTTP, terminal and Textual surfaces."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4, uuid5, NAMESPACE_URL
from pydantic import SecretStr
from kong.application.contracts import AppError
from kong.application.events import EventStore
from kong.application.memory_admin import MemoryAdmin
from kong.application.runtime_factory import RuntimeFactory
from kong.application.tracing import TracedModel
from kong.config import ModelConfig
from kong.contracts import Status
from kong.continuity.history import HistorySelector
from kong.runtime.acceptance import has_declared_acceptance
from kong.runtime.state import create_run_state
from kong.memory import Source
from kong.models.compatible import CompatibleModel


def payload_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


class ApplicationService:
    def __init__(self, data_root, *, model_factory=None, model_config=None):
        self.root = Path(data_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.model_factory, self.config = model_factory, model_config
        self.provider = "compatible"
        self.runtimes, self.tasks, self.locks = {}, {}, {}
        self.command_lock = asyncio.Lock()
        self.closing = False
        self.secrets = set()
        self.admin = MemoryAdmin(self.root / "core.db")
        self.factory = RuntimeFactory(self.root, self.admin.auth)
        self.instance_lock = self.factory.store.lock("0" * 32)
        self.events = None
        self.core_cursor = 0

    async def __aenter__(self):
        self.instance_lock.__enter__()
        try:
            self.events = EventStore(
                self.root / "application.db", self.admin.auth.profile_id
            )
            self.core_cursor = self.events.offset("core")
            await self.admin.start()
            config_file = self.root / "model-config.json"
            if self.config is None and config_file.exists():
                saved = json.loads(config_file.read_text(encoding="utf-8"))
                self.provider = saved.pop("provider", "compatible")
                self.config = ModelConfig(
                    **saved, api_key=SecretStr(os.environ.get("KONG_API_KEY", ""))
                )
            for entry in self.factory.store.list_runs():
                if entry["run_id"] == "0" * 32:
                    continue
                runtime = self.factory.restore(
                    entry["run_id"],
                    self._model(allow_unconfigured=True),
                    self._runtime_event,
                )
                if (
                    runtime.state.status == Status.RUNNING
                    and not runtime.pending_action_ids
                ):
                    runtime.human(
                        "interrupt",
                        text="Host paused this known-action snapshot after service restart; explicit resume is required.",
                    )
                self._attach(runtime)
                for event in runtime.run_events:
                    self._runtime_event(
                        event.kind, {"event": event.model_dump(mode="json")}
                    )
            await self.sync_memory_events()
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, *_):
        await self.aclose()

    def _model(self, allow_unconfigured=False):
        if self.model_factory:
            raw = self.model_factory()
        elif self.config:
            raw = CompatibleModel(self.config.model_copy(deep=True))
        elif allow_unconfigured:
            raw = UnconfiguredModel()
        else:
            raise AppError("model_unconfigured", "先配置模型，再开始对话。", 422)
        return TracedModel(raw, self._trace)

    def _attach(self, runtime):
        runtime.model.engine = runtime.context_engine
        self.runtimes[runtime.state.run_id] = runtime
        self.locks[runtime.state.run_id] = asyncio.Lock()

    def clean(self, value):
        text = json.dumps(value, ensure_ascii=False, default=str)
        if self.config and self.config.api_key.get_secret_value():
            self.secrets.add(self.config.api_key.get_secret_value())
        for secret in self.secrets:
            text = text.replace(secret, "[已隐藏密钥]")
        return json.loads(text)

    def _runtime_event(self, kind, data):
        event = data.get("event", {})
        if not event or not self.events:
            return
        key = "runtime:" + event["run_id"]
        if event["sequence"] <= self.events.offset(key):
            return
        allowed = {
            "action_id",
            "tool",
            "reason",
            "category",
            "status_code",
            "received_chars",
            "decision_chars",
            "revision",
            "kind",
            "pending_action_ids",
        }
        safe = {
            key: value for key, value in event.get("data", {}).items() if key in allowed
        }
        safe_event = {key: value for key, value in event.items() if key != "data"}
        safe_event["data"] = self.clean(safe)
        self.events.append(
            "runtime." + kind,
            {"runtime_event": safe_event},
            origin=f"runtime:{event['run_id']}:{event['sequence']}",
            run_id=event["run_id"],
            thread_id=event.get("thread_id"),
        )
        self.events.offset(key, event["sequence"])

    def _trace(self, kind, data, request_id, run_id, thread_id):
        self.events.append(
            kind,
            self.clean(data),
            origin=f"trace:{request_id}:{kind}",
            run_id=run_id,
            thread_id=thread_id,
            request_id=request_id,
        )

    async def sync_memory_events(self):
        page = await self.admin.call("read_events", after=self.core_cursor, limit=256)
        for entry in page["items"]:
            self.events.append(
                "memory.mutation", entry, origin=f"core:{entry['sequence']}"
            )
        self.core_cursor = page["next_cursor"]
        self.events.offset("core", self.core_cursor)

    async def _command(self, command, payload, operation, *, recoverable=False):
        if self.closing:
            raise AppError("service_closing", "服务正在安全退出。", 503)
        async with self.command_lock:
            previous = self.events.claim(command, payload_hash(payload))
            if previous and (previous.get("status") != "partial" or not recoverable):
                if previous.get("status") == "unknown":
                    raise AppError(
                        "action_unknown",
                        "上次操作结果未知，请先读取状态；不会自动重发。",
                    )
                return previous
            try:
                result = await operation()
            except AppError:
                raise  # Preserve uncertain results until explicitly inspected.
            except ValueError:
                raise AppError(
                    "invalid_state", "当前状态或修订不允许此操作，请刷新后重试。"
                ) from None
            return self.events.finish(command, self.clean(result))

    async def create_thread(self, body):
        async def operation():
            thread_id = uuid5(NAMESPACE_URL, str(self.root) + ":" + body.command_id).hex
            try:
                thread = self.factory.threads.get(thread_id)
            except ValueError:
                thread = self.factory.threads.create(
                    self.factory.workspace.root, body.title, thread_id=thread_id
                )
            return thread.model_dump(mode="json")

        return await self._command(
            body.command_id, {"route": "thread", **body.model_dump()}, operation
        )

    def list_threads(self):
        return self.clean(
            {
                "items": [
                    thread.model_dump(mode="json")
                    for thread in self.factory.threads.list_threads(
                        self.factory.workspace.root
                    )
                ],
                "next_cursor": None,
            }
        )

    def _busy(self, thread_id, excluding=None):
        for runtime in self.runtimes.values():
            if (
                runtime.thread.thread_id == thread_id
                and runtime.state.run_id != excluding
            ):
                if (
                    runtime._running
                    or runtime.pending_action_ids
                    or runtime.state.status in {Status.RUNNING, Status.WAITING_USER}
                ):
                    raise AppError(
                        "run_busy",
                        "当前对话有未结束任务，请先回应、结束或处理未知动作。",
                    )
        if sum(not task.done() for task in self.tasks.values()) >= 4:
            raise AppError("local_resource_limit", "最多同时执行四个对话。", 429)

    async def create_run(self, thread_id, body):
        if not body.text.strip():
            raise AppError("invalid_input", "输入不能为空。", 422)
        self.factory.threads.get(thread_id)
        state = create_run_state(body.text)
        state.success_criteria, state.completion_contract = (
            body.acceptance,
            body.completion_contract,
        )
        if body.intent == "task" and not has_declared_acceptance(state):
            raise AppError(
                "acceptance_required",
                "任务需要你确认的强验收条件，例如目标文件与准确内容。输入已保留。",
                422,
            )

        async def operation():
            self._busy(thread_id)
            runtime = self.factory.new(
                body, thread_id, self._model(), self._runtime_event
            )
            self._attach(runtime)
            self.events.intent(runtime.state.run_id, body.intent)
            receipt = {
                "command_id": body.command_id,
                "status": "accepted",
                "run_id": runtime.state.run_id,
                "event_cursor": self.events.cursor,
            }
            self._schedule(runtime)
            return receipt

        # Configuration validation before a durable command claim.
        if not self.model_factory and not self.config:
            raise AppError("model_unconfigured", "先配置模型，再开始对话。", 422)
        return await self._command(
            body.command_id,
            {"route": "run", "thread_id": thread_id, **body.model_dump(mode="json")},
            operation,
        )

    def _schedule(self, runtime):
        prior = self.tasks.get(runtime.state.run_id)
        if prior and not prior.done():
            raise AppError("run_busy", "此任务已有运行入口。")

        async def drive():
            try:
                await runtime.run()
            except Exception as error:
                self.events.append(
                    "application.driver_failed",
                    {"category": type(error).__name__},
                    origin="failure:" + uuid4().hex,
                    run_id=runtime.state.run_id,
                )

        self.tasks[runtime.state.run_id] = asyncio.create_task(
            drive(), name="kong-run-" + runtime.state.run_id
        )

    async def wait(self, run_id):
        task = self.tasks.get(run_id)
        if task:
            await task

    def _runtime(self, run_id):
        if run_id not in self.runtimes:
            raise AppError("not_found", "没有此任务。", 404)
        return self.runtimes[run_id]

    async def run_detail(self, run_id):
        runtime = self._runtime(run_id)
        state = runtime.state
        access = await self.admin.call("get_access_state")
        engine = runtime.context_engine
        changed = bool(
            not engine.allow_initial_binding
            and engine.memory_binding
            and engine.memory_binding.get("access") != access.model_dump(mode="json")
        )
        intent = self.events.intent(run_id)
        outcome = (
            "task_accepted"
            if state.status == Status.COMPLETED and intent == "task"
            else "answer_finished"
            if state.status == Status.COMPLETED and intent == "chat"
            else "pending"
        )
        messages = (
            [
                entry.model_dump(mode="json")
                for entry in HistorySelector.conversation(
                    runtime.snapshot(), runtime.thread.thread_id
                )
            ]
            if not changed
            else []
        )
        observations = [
            {
                "action_id": entry.action_id,
                "tool": entry.action.name,
                "success": entry.success,
                "error": entry.error,
                "effect": entry.effect,
            }
            for entry in runtime.history.observations
        ]
        from kong.reporting import run_report

        return self.clean(
            {
                "run_id": run_id,
                "thread_id": runtime.thread.thread_id,
                "intent": intent,
                "mode": state.mode.value,
                "status": state.status.value,
                "driver_active": runtime._running,
                "phase": state.phase.value,
                "turn_count": state.turn_count,
                "outcome_kind": outcome,
                "pending_question": state.pending_question
                if not changed
                else "记忆已变化，请提供完整新目标。",
                "pending_approval": state.pending_approval.model_dump(mode="json")
                if state.pending_approval and not changed
                else None,
                "plan": state.plan.model_dump(mode="json")
                if state.plan and not changed
                else None,
                "unknown_actions": runtime.pending_action_ids,
                "fresh_required": changed,
                "messages": messages,
                "observations": observations,
                "stop_reason": state.stop_reason,
                "metrics": runtime.metrics.model_dump(mode="json"),
                "report": run_report(runtime)
                if not changed
                else {"history": "原报告保留在本地审计，记忆内容不再作为当前上下文。"},
                "can_resume": state.status == Status.STOPPED
                and state.stop_reason == "user_interrupted"
                and not changed,
            }
        )

    async def thread_detail(self, thread_id):
        thread = self.factory.threads.get(thread_id)
        runs = [
            await self.run_detail(run_id)
            for run_id in self.factory.threads.runs(thread_id)
            if run_id in self.runtimes
        ]
        return {
            **thread.model_dump(mode="json"),
            "runs": runs,
            "accepted_messages": [
                {**message, "run_id": entry["run_id"]}
                for entry in runs
                for message in entry["messages"]
            ],
        }

    async def inputs(self, run_id, body):
        runtime = self._runtime(run_id)

        async def operation():
            async with self.locks[run_id]:
                return {
                    "command_id": body.command_id,
                    "status": "accepted",
                    "queued_input_id": runtime.submit_input(body.kind, body.text),
                }

        return await self._command(
            body.command_id,
            {"route": "input", "run_id": run_id, **body.model_dump()},
            operation,
        )

    async def human(self, run_id, body):
        runtime = self._runtime(run_id)
        if body.kind == "approve" and (
            not runtime.state.pending_approval
            or body.revision != runtime.state.pending_approval.revision
        ):
            raise AppError("approval_changed", "审批修订已变化，请重新审阅当前计划。")

        async def operation():
            async with self.locks[run_id]:
                if runtime._running:
                    raise AppError("run_busy", "请等待当前执行到暂停边界。")
                if body.kind == "resolve":
                    runtime.resolve_interrupted_action(body.text)
                else:
                    if await runtime.memory_access_changed() and body.kind != "reject":
                        raise AppError(
                            "fresh_required", "记忆已变化，需提供完整新目标。"
                        )
                    self._busy(runtime.thread.thread_id, excluding=run_id)
                    runtime.human(body.kind, text=body.text, revision=body.revision)
                if runtime.state.status == Status.RUNNING:
                    self._schedule(runtime)
                return {
                    "command_id": body.command_id,
                    "status": "accepted",
                    "run_id": run_id,
                }

        return await self._command(
            body.command_id,
            {"route": "human", "run_id": run_id, **body.model_dump()},
            operation,
        )

    async def fresh(self, run_id, body):
        runtime = self._runtime(run_id)
        state = create_run_state(body.text)
        state.success_criteria, state.completion_contract = (
            body.acceptance,
            body.completion_contract,
        )
        if body.intent == "task" and not has_declared_acceptance(state):
            raise AppError(
                "acceptance_required", "新任务仍需要你确认的强验收条件。", 422
            )

        async def operation():
            async with self.locks[run_id]:
                self._busy(runtime.thread.thread_id, excluding=run_id)
                fresh = await runtime.start_fresh_after_memory_change(
                    body.text, model=self._model()
                )
                self._attach(fresh)
                fresh.state.success_criteria = body.acceptance
                if body.completion_contract:
                    fresh.set_completion_contract(body.completion_contract)
                fresh.save()
                self.events.intent(fresh.state.run_id, body.intent)
                self._schedule(fresh)
                return {
                    "command_id": body.command_id,
                    "status": "accepted",
                    "run_id": fresh.state.run_id,
                }

        return await self._command(
            body.command_id,
            {"route": "fresh", "run_id": run_id, **body.model_dump()},
            operation,
        )

    async def memory_records(self, *, after=None, query="", bottle_id=None, limit=100):
        return await self.admin.call(
            "page_records", after=after, query=query, bottle_id=bottle_id, limit=limit
        )

    async def memory_bottles(self):
        return {
            "items": [
                b.model_dump(mode="json") for b in await self.admin.call("list_bottles")
            ]
        }

    async def memory_record(self, record_id):
        record = await self.admin.call("get_record", record_id, include_inactive=True)
        memberships = await self.admin.call("record_memberships", record_id)
        return self.clean(
            {
                **record.model_dump(mode="json"),
                "memberships": [b.model_dump(mode="json") for b in memberships],
                "visible": record.state.value == "active"
                and any(b.active for b in memberships),
                "visible_reason": "任一启用的记忆瓶允许使用"
                if any(b.active for b in memberships)
                else "没有启用的记忆瓶允许使用",
            }
        )

    async def memory_create(self, body):
        async def operation():
            receipt = await self.admin.remember(body)
            await self.sync_memory_events()
            return receipt

        return await self._command(
            body.command_id,
            {"route": "remember", **body.model_dump()},
            operation,
            recoverable=True,
        )

    async def _memory_mutation(self, command, payload, method, *args, **kwargs):
        async def operation():
            result = await self.admin.call(method, *args, command_id=command, **kwargs)
            await self.sync_memory_events()
            return result.model_dump(mode="json")

        return await self._command(command, {"route": method, **payload}, operation)

    async def memory_revise(self, record_id, content, revision, command):
        return await self._memory_mutation(
            command,
            {"record": record_id, "content": content, "revision": revision},
            "revise_memory",
            record_id,
            content,
            Source(
                source_id=command + ":input",
                origin="user_command",
                speaker="user",
                excerpt=content,
            ),
            expected_revision=revision,
        )

    async def memory_preview(self, record_id):
        return {
            **(await self.admin.call("preview_forget", record_id)).model_dump(
                mode="json"
            ),
            "retained": [
                "原 Runtime 历史与报告",
                "外部导出和备份",
                "已发送给模型服务的请求",
            ],
        }

    async def memory_forget(self, record_id, revision, approved_digest, command):
        return await self._memory_mutation(
            command,
            {"record": record_id, "revision": revision, "digest": approved_digest},
            "forget_memory",
            record_id,
            expected_revision=revision,
            approved_plan_digest=approved_digest,
        )

    async def activation(self, bottle_id, body):
        return await self._memory_mutation(
            body.command_id,
            {"bottle": bottle_id, **body.model_dump()},
            "activate_bottle" if body.active else "deactivate_bottle",
            bottle_id,
            expected_revision=body.expected_revision,
        )

    async def pin(self, record_id, body):
        return await self._memory_mutation(
            body.command_id,
            {"record": record_id, **body.model_dump()},
            "pin_memory",
            record_id,
            body.bottle_id,
            pinned=body.pinned,
        )

    async def candidates(self):
        return {
            "items": [
                c.model_dump(mode="json")
                for c in await self.admin.call("list_formation_candidates")
            ]
        }

    async def candidate_preview(self, candidate_id):
        return (
            await self.admin.call("preview_consolidation", candidate_id)
        ).model_dump(mode="json")

    async def candidate_confirm(self, candidate_id, body):
        return await self._memory_mutation(
            body.command_id,
            {"candidate": candidate_id, **body.model_dump()},
            "consolidate_candidate",
            candidate_id,
            approved_digest=body.approved_digest,
        )

    async def candidate_reject(self, candidate_id, body):
        return await self._memory_mutation(
            body.command_id,
            {"candidate": candidate_id, **body.model_dump()},
            "reject_candidate",
            candidate_id,
            expected_revision=body.expected_revision,
        )

    def model_status(self):
        return {
            "provider": self.provider,
            "model": self.config.model if self.config else None,
            "base_url": self.config.base_url if self.config else None,
            "configured": bool(self.config),
            "api_key_configured": bool(
                self.config and self.config.api_key.get_secret_value()
            ),
            "stream": self.config.stream if self.config else False,
        }

    async def configure_model(self, body):
        key = (
            body.api_key
            if body.api_key and body.api_key.get_secret_value()
            else self.config.api_key
            if self.config
            else SecretStr(os.environ.get("KONG_API_KEY", ""))
        )
        config = ModelConfig(
            base_url=body.base_url,
            model=body.model,
            api_key=key,
            stream=body.stream,
            json_mode=body.json_mode,
            timeout=45,
            max_tokens=1400,
        )

        async def operation():
            if self.config and self.config.api_key.get_secret_value():
                self.secrets.add(self.config.api_key.get_secret_value())
            if key.get_secret_value():
                self.secrets.add(key.get_secret_value())
            self.config, self.provider = config, body.provider
            saved = {**config.model_dump(mode="json"), "provider": body.provider}
            (self.root / "model-config.json").write_text(
                json.dumps(saved, ensure_ascii=False), encoding="utf-8"
            )
            return self.model_status()

        payload = {
            "route": "model",
            **body.model_dump(),
            "key_digest": payload_hash(key.get_secret_value()),
        }
        return await self._command(body.command_id, payload, operation)

    async def connection_test(self, command):
        from kong.context import Context

        if not self.config:
            raise AppError("model_unconfigured", "先配置模型。", 422)

        async def operation():
            from kong.models.base import ModelError

            try:
                model = CompatibleModel(
                    self.config.model_copy(update={"max_tokens": 128, "stream": False})
                )
                await model.generate(
                    Context(
                        messages=[
                            {
                                "role": "system",
                                "content": 'Return only JSON: {"kind":"respond","content":"连接正常"}',
                            }
                        ]
                    )
                )
                return {
                    "command_id": command,
                    "status": "connected",
                    "usage": model.last_usage,
                }
            except ModelError as error:
                return {
                    "command_id": command,
                    "status": "failed",
                    "category": error.category,
                    "http_status": error.status_code,
                }

        return await self._command(command, {"route": "connection"}, operation)

    async def context_trace(self, run_id):
        self._runtime(run_id)
        result, after = [], None
        while True:
            batch = self.events.read(after, 256)
            if not batch:
                break
            result.extend(
                event
                for event in batch
                if event["run_id"] == run_id and event["kind"].startswith("memory.")
            )
            after = batch[-1]["event_id"]
        return {
            "items": result,
            "content": "轨迹仅包含版本和收据；正文按当前授权读取。",
        }

    def workspace_files(self):
        items = []
        for path in self.factory.workspace.root.rglob("*"):
            if len(items) >= 500:
                break
            if path.is_file():
                try:
                    self.factory.workspace.resolve(
                        str(path.relative_to(self.factory.workspace.root))
                    )
                except ValueError:
                    continue
                items.append(
                    {
                        "path": path.relative_to(
                            self.factory.workspace.root
                        ).as_posix(),
                        "bytes": path.stat().st_size,
                    }
                )
        return {
            "items": items,
            "workspace": str(self.factory.workspace.root),
            "limit": 500,
        }

    def workspace_preview(self, path):
        target = self.factory.workspace.resolve(path)
        if not target.is_file() or target.stat().st_size > 256000:
            raise AppError(
                "preview_unavailable", "仅支持 256KB 以内的实际文本文件。", 422
            )
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeError:
            raise AppError(
                "preview_unavailable", "此文件不能作为 UTF-8 文本预览。", 422
            ) from None
        return self.clean(
            {
                "path": path,
                "content": content,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )

    async def aclose(self):
        if self.closing:
            return
        self.closing = True
        for runtime in self.runtimes.values():
            if runtime._running and runtime.state.status == Status.RUNNING:
                runtime.submit_input("cancel")
        tasks = [task for task in self.tasks.values() if not task.done()]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self.factory.close()
        await self.admin.close()
        if self.events:
            self.events.close()
        self.instance_lock.__exit__(None, None, None)


class UnconfiguredModel:
    async def generate(self, context):
        from kong.models.base import ModelError

        raise ModelError("Model configuration required", category="configuration")
