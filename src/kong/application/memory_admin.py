"""A bounded owner worker. Core connections are created and closed on that worker."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from kong.memory import (
    MemoryService,
    Authority,
    Capability,
    Scope,
    Bottle,
    MemoryDraft,
    Source,
)
from kong.memory.errors import (
    MemoryError,
    RevisionConflict,
    IdempotencyConflict,
    NotFound,
    PermissionDenied,
)
from kong.application.contracts import AppError


class MemoryAdmin:
    def __init__(self, path, owner="local-owner", profile="local-profile"):
        self.path = path
        self.auth = Authority(
            owner_id=owner,
            profile_id=profile,
            capabilities=frozenset(Capability),
            scopes=(Scope(),),
        )
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kong-owner")
        self.core = None
        self.pending = 0

    def _start(self):
        self.core = MemoryService(self.path)
        auth = self.auth.model_copy(update={"input_id": "local-bootstrap"})
        self.core.create_profile(auth, command_id="local-profile-bootstrap-v1")
        if not any(
            b.bottle_id == "personal-memory" for b in self.core.list_bottles(auth)
        ):
            self.core.create_bottle(
                auth,
                Bottle(
                    bottle_id="personal-memory",
                    profile_id=auth.profile_id,
                    title="我的记忆",
                ),
                command_id="local-bottle-bootstrap-v1",
            )
            self.core.activate_bottle(
                auth,
                "personal-memory",
                expected_revision=1,
                command_id="local-bottle-activate-v1",
            )

    async def start(self):
        await asyncio.get_running_loop().run_in_executor(self.worker, self._start)

    async def invoke(self, function):
        if self.pending >= 16:
            raise AppError("local_resource_limit", "记忆操作队列已满。", 429)
        self.pending += 1
        try:
            return await asyncio.get_running_loop().run_in_executor(
                self.worker, function
            )
        except MemoryError as error:
            if isinstance(error, (RevisionConflict, IdempotencyConflict)):
                raise AppError(
                    "revision_conflict", "内容或操作版本已变化，请重新读取后确认。"
                ) from None
            if isinstance(error, NotFound):
                raise AppError("not_found", "记忆或来源已不可用。", 404) from None
            if isinstance(error, PermissionDenied):
                raise AppError("forbidden", "此操作超出当前授权。", 403) from None
            raise AppError(
                "memory_policy", "记忆操作不符合当前 Core 规则。", 422
            ) from None
        finally:
            self.pending -= 1

    async def call(self, method, *args, command_id=None, **kwargs):
        auth = (
            self.auth.model_copy(update={"input_id": command_id + ":input"})
            if command_id
            else self.auth
        )
        if command_id:
            kwargs["command_id"] = command_id
        return await self.invoke(
            lambda: getattr(self.core, method)(auth, *args, **kwargs)
        )

    async def remember(self, body):
        auth = self.auth.model_copy(update={"input_id": body.command_id + ":input"})

        def operation():
            bottle = next(
                (
                    b
                    for b in self.core.list_bottles(auth)
                    if b.bottle_id == body.bottle_id
                ),
                None,
            )
            if (
                bottle is None
                or bottle.kind != "memory"
                or bottle.policy.scope != Scope()
            ):
                raise AppError("invalid_bottle", "请选择当前用户的记忆瓶。", 422)
            receipt = self.core.remember(
                auth,
                MemoryDraft(
                    content=body.content,
                    kind=body.kind,
                    category=body.category,
                    sensitivity=body.sensitivity,
                ),
                Source(
                    source_id=auth.input_id,
                    origin="user_command",
                    speaker="user",
                    excerpt=body.content,
                ),
                command_id=body.command_id + ":remember",
            )
            result = {
                "command_id": body.command_id,
                "record_id": receipt.record_id,
                "revision": receipt.revision,
                "status": "committed",
            }
            try:
                self.core.add_memory_to_bottle(
                    auth,
                    body.bottle_id,
                    receipt.record_id,
                    command_id=body.command_id + ":membership",
                )
            except (MemoryError, ValueError):
                result["status"] = "partial"
            return result

        return await self.invoke(operation)

    async def close(self):
        if self.core:
            await asyncio.get_running_loop().run_in_executor(
                self.worker, self.core.close
            )
        self.worker.shutdown(wait=True, cancel_futures=True)
