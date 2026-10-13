"""A surface facade can embed the actor or connect to its existing local host."""

import inspect
import os
from urllib.parse import urlsplit
import httpx
from kong.application.service import ApplicationService
from kong.application.contracts import AppError


class LocalClient:
    def __init__(self, root, *, connect=None, model_config=None, model_factory=None):
        self.root, self.connect, self.config, self.model_factory = (
            root,
            connect,
            model_config,
            model_factory,
        )
        self.service, self.http = None, None

    async def __aenter__(self):
        if self.connect:
            parsed = urlsplit(self.connect)
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise AppError(
                    "invalid_origin", "只能连接明确的 127.0.0.1 本地服务。", 422
                )
            token = os.environ.get("KONG_LOCAL_TOKEN", "")
            if not token:
                raise AppError(
                    "session_required", "连接已有服务需要设置 KONG_LOCAL_TOKEN。", 401
                )
            self.http = httpx.AsyncClient(
                base_url=self.connect,
                headers={
                    "Origin": self.connect.rstrip("/"),
                    "Authorization": "Bearer " + token,
                },
                timeout=50,
                follow_redirects=False,
            )
            await self.request("GET", "/v1/profiles")
        else:
            self.service = await ApplicationService(
                self.root, model_config=self.config, model_factory=self.model_factory
            ).__aenter__()
        return self

    async def __aexit__(self, *_):
        if self.http:
            await self.http.aclose()
        if self.service:
            await self.service.aclose()

    async def request(self, method, path, body=None):
        try:
            response = await self.http.request(method, path, json=body)
        except httpx.HTTPError:
            raise AppError(
                "connection_lost", "连接中断，请先读取状态；写入不会自动重发。", 503
            ) from None
        result = response.json()
        if response.is_error:
            raise AppError(
                result.get("code", "request_failed"),
                result.get("message", "操作失败"),
                response.status_code,
            )
        return result

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            if self.service:
                if name == "event_history":
                    return {
                        "items": self.service.events.read(),
                        "cursor": self.service.events.cursor,
                    }
                result = getattr(self.service, name)(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
            body = (
                args[-1].model_dump(mode="json")
                if args and hasattr(args[-1], "model_dump")
                else None
            )
            if name == "model_status":
                return await self.request("GET", "/v1/model")
            if name == "configure_model":
                if args[0].api_key:
                    body["api_key"] = args[0].api_key.get_secret_value()
                return await self.request("POST", "/v1/model", body)
            if name == "list_threads":
                return await self.request("GET", "/v1/threads")
            if name == "create_thread":
                return await self.request("POST", "/v1/threads", body)
            if name == "create_run":
                return await self.request("POST", f"/v1/threads/{args[0]}/runs", body)
            if name == "thread_detail":
                return await self.request("GET", f"/v1/threads/{args[0]}")
            if name == "run_detail":
                return await self.request("GET", f"/v1/runs/{args[0]}")
            if name in {"human", "inputs", "fresh"}:
                return await self.request("POST", f"/v1/runs/{args[0]}/{name}", body)
            if name == "context_trace":
                return await self.request("GET", f"/v1/runs/{args[0]}/context-trace")
            if name == "memory_records":
                return await self.request("GET", "/v1/memory/records")
            if name == "memory_bottles":
                return await self.request("GET", "/v1/memory/bottles")
            if name == "memory_record":
                return await self.request("GET", f"/v1/memory/records/{args[0]}")
            if name == "memory_create":
                return await self.request("POST", "/v1/memory/records", body)
            if name == "memory_preview":
                return await self.request(
                    "POST", f"/v1/memory/records/{args[0]}/forget-preview", {}
                )
            if name == "memory_revise":
                return await self.request(
                    "PATCH",
                    f"/v1/memory/records/{args[0]}",
                    {
                        "content": args[1],
                        "expected_revision": args[2],
                        "command_id": args[3],
                    },
                )
            if name == "memory_forget":
                return await self.request(
                    "POST",
                    f"/v1/memory/records/{args[0]}/forget",
                    {
                        "expected_revision": args[1],
                        "approved_digest": args[2],
                        "command_id": args[3],
                    },
                )
            if name == "event_history":
                return await self.request("GET", "/v1/events/history")
            if name == "wait":
                import asyncio

                while True:
                    detail = await self.run_detail(args[0])
                    if detail["status"] != "running" and not detail["driver_active"]:
                        return
                    await asyncio.sleep(0.1)
            raise AppError("unsupported_operation", "此代理不支持该操作。", 422)

        return call
