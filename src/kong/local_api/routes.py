"""Strict routes forward commands to the existing application actor."""

import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, Request, Response, Query
from fastapi.responses import StreamingResponse
from kong.application.contracts import (
    ThreadCreate,
    RunCreate,
    InputCommand,
    HumanCommand,
    FreshCommand,
    MemoryCreate,
    MemoryRevise,
    ForgetCommand,
    ActivationCommand,
    PinCommand,
    CandidateCommand,
    ModelSetup,
    Command,
)


def router():
    api = APIRouter(prefix="/v1")

    def service(request):
        return request.app.state.service

    @api.get("/health")
    async def health(request: Request):
        app = service(request)
        identity = Path(__file__).parent / "source_identity.json"
        return {
            "product": "Kong Local Preview 0.0.0",
            "runtime_version": "0.5.0rc1",
            "schema": 1,
            "source_identity": json.loads(identity.read_text(encoding="utf-8"))[
                "fingerprint_sha256"
            ]
            if identity.exists()
            else "phase2-development",
            "configured": bool(app.config),
            "cursor": app.events.cursor,
        }

    @api.post("/session")
    async def pair(request: Request, response: Response):
        session = request.app.state.session
        response.set_cookie(
            "kong_session",
            session.token,
            httponly=True,
            samesite="strict",
            max_age=8 * 3600,
            path="/v1",
        )
        return {"csrf_token": session.csrf}

    @api.get("/session")
    async def current_session(request: Request):
        return {"csrf_token": request.app.state.session.csrf}

    @api.get("/profiles")
    async def profiles(request: Request):
        return {
            "items": [
                (await service(request).admin.call("get_profile")).model_dump(
                    mode="json"
                )
            ]
        }

    @api.get("/model")
    async def model(request: Request):
        return service(request).model_status()

    @api.post("/model")
    async def setup(body: ModelSetup, request: Request):
        return await service(request).configure_model(body)

    @api.post("/model/test")
    async def test_model(body: Command, request: Request):
        return await service(request).connection_test(body.command_id)

    @api.get("/threads")
    async def threads(request: Request):
        return service(request).list_threads()

    @api.post("/threads", status_code=201)
    async def thread_create(body: ThreadCreate, request: Request):
        return await service(request).create_thread(body)

    @api.get("/threads/{thread_id}")
    async def thread_detail(thread_id: str, request: Request):
        return await service(request).thread_detail(thread_id)

    @api.post("/threads/{thread_id}/runs", status_code=202)
    async def run_create(thread_id: str, body: RunCreate, request: Request):
        return await service(request).create_run(thread_id, body)

    @api.get("/runs/{run_id}")
    async def run_detail(run_id: str, request: Request):
        return await service(request).run_detail(run_id)

    @api.post("/runs/{run_id}/inputs", status_code=202)
    async def inputs(run_id: str, body: InputCommand, request: Request):
        return await service(request).inputs(run_id, body)

    @api.post("/runs/{run_id}/human", status_code=202)
    async def human(run_id: str, body: HumanCommand, request: Request):
        return await service(request).human(run_id, body)

    @api.post("/runs/{run_id}/fresh", status_code=201)
    async def fresh(run_id: str, body: FreshCommand, request: Request):
        return await service(request).fresh(run_id, body)

    @api.get("/runs/{run_id}/context-trace")
    async def trace(run_id: str, request: Request):
        return await service(request).context_trace(run_id)

    @api.get("/commands/{command_id}")
    async def receipt(command_id: str, request: Request):
        return service(request).events.receipt(command_id)

    @api.get("/memory/bottles")
    async def bottles(request: Request):
        return await service(request).memory_bottles()

    @api.get("/memory/records")
    async def memories(
        request: Request,
        after: str | None = None,
        query: str = Query(default="", max_length=200),
        bottle_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=100),
    ):
        return service(request).clean(
            await service(request).memory_records(
                after=after, query=query, bottle_id=bottle_id, limit=limit
            )
        )

    @api.post("/memory/records", status_code=201)
    async def remember(body: MemoryCreate, request: Request):
        return await service(request).memory_create(body)

    @api.get("/memory/records/{record_id}")
    async def memory_detail(record_id: str, request: Request):
        return await service(request).memory_record(record_id)

    @api.patch("/memory/records/{record_id}")
    async def revise(record_id: str, body: MemoryRevise, request: Request):
        return await service(request).memory_revise(
            record_id, body.content, body.expected_revision, body.command_id
        )

    @api.post("/memory/records/{record_id}/forget-preview")
    async def preview(record_id: str, request: Request):
        return await service(request).memory_preview(record_id)

    @api.post("/memory/records/{record_id}/forget")
    async def forget(record_id: str, body: ForgetCommand, request: Request):
        return await service(request).memory_forget(
            record_id, body.expected_revision, body.approved_digest, body.command_id
        )

    @api.post("/memory/bottles/{bottle_id}/activation")
    async def activate(bottle_id: str, body: ActivationCommand, request: Request):
        return await service(request).activation(bottle_id, body)

    @api.post("/memory/records/{record_id}/pin")
    async def pin(record_id: str, body: PinCommand, request: Request):
        return await service(request).pin(record_id, body)

    @api.get("/memory/candidates")
    async def candidates(request: Request):
        return service(request).clean(await service(request).candidates())

    @api.post("/memory/candidates/{candidate_id}/preview")
    async def candidate_preview(candidate_id: str, request: Request):
        return await service(request).candidate_preview(candidate_id)

    @api.post("/memory/candidates/{candidate_id}/confirm")
    async def confirm(candidate_id: str, body: CandidateCommand, request: Request):
        return await service(request).candidate_confirm(candidate_id, body)

    @api.post("/memory/candidates/{candidate_id}/reject")
    async def reject(candidate_id: str, body: CandidateCommand, request: Request):
        return await service(request).candidate_reject(candidate_id, body)

    @api.get("/workspace/files")
    async def files(request: Request):
        return service(request).workspace_files()

    @api.get("/workspace/preview")
    async def file_preview(request: Request, path: str):
        return service(request).workspace_preview(path)

    @api.get("/events/history")
    async def history(request: Request, after: str | None = None):
        app = service(request)
        await app.sync_memory_events()
        items = app.events.read(after) if after else app.events.recent()
        return {
            "items": items,
            "cursor": items[-1]["event_id"] if items else app.events.cursor,
            "limit": 256,
        }

    @api.get("/events")
    async def events(request: Request, after: str | None = None):
        app = service(request)
        cursor = after or request.headers.get("last-event-id") or app.events.cursor
        app.events.read(cursor)

        async def stream():
            nonlocal cursor
            yield "event: snapshot\ndata: " + json.dumps({"cursor": cursor}) + "\n\n"
            while not app.closing and not await request.is_disconnected():
                entries = app.events.read(cursor, 256)
                for entry in entries:
                    cursor = entry["event_id"]
                    yield (
                        f"id: {cursor}\nevent: kong\ndata: "
                        + json.dumps(entry, ensure_ascii=False)
                        + "\n\n"
                    )
                if not entries:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return api
