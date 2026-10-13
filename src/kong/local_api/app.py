"""Optional FastAPI host, with a single actor and deterministic teardown."""

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from pydantic import ValidationError
from kong.application.contracts import AppError
from kong.application.service import ApplicationService
from kong.local_api.auth import LocalSession
from kong.local_api.routes import router


def create_app(
    data_root,
    *,
    origin,
    token=None,
    model_factory=None,
    model_config=None,
    static_root=None,
):
    session = LocalSession(origin, token)

    @asynccontextmanager
    async def lifespan(app):
        async with ApplicationService(
            data_root, model_factory=model_factory, model_config=model_config
        ) as service:
            app.state.service = service
            yield

    app = FastAPI(
        title="Kong Local Preview 0.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.session = session

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        try:
            session.check(request)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                length = request.headers.get("content-length", "0")
                if not length.isdecimal():
                    raise AppError("invalid_input", "请求长度无效。", 400)
                if int(length) > 256000:
                    raise AppError("request_too_large", "请求超过本地限制。", 413)
                size = 0
                chunks = []
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 256000:
                        raise AppError("request_too_large", "请求超过本地限制。", 413)
                    chunks.append(chunk)
                request._body = b"".join(chunks)
                if not request.headers.get("content-type", "").startswith(
                    "application/json"
                ):
                    raise AppError("invalid_input", "写入需要 JSON 请求。", 415)
            response = await call_next(request)
        except AppError as error:
            response = JSONResponse(error.dto(), status_code=error.status)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(request, error):
        return JSONResponse(error.dto(), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def dto_error(request, error):
        return JSONResponse(
            {
                "code": "invalid_input",
                "message": "输入格式不符合本地接口要求。",
                "retryable": False,
                "fields": [
                    {"path": list(item["loc"]), "type": item["type"]}
                    for item in error.errors()
                ],
            },
            status_code=422,
        )

    @app.exception_handler(ValueError)
    @app.exception_handler(ValidationError)
    async def invalid_value(request, error):
        return JSONResponse(
            {
                "code": "invalid_input",
                "message": "状态、路径或配置不符合当前规则。",
                "retryable": False,
            },
            status_code=422,
        )

    app.include_router(router())
    static = Path(static_root) if static_root else Path(__file__).parent / "static"

    @app.get("/{path:path}")
    async def frontend(path: str):
        target = (static / path).resolve()
        if target.is_relative_to(static.resolve()) and target.is_file():
            return FileResponse(target)
        if (static / "index.html").is_file() and (
            not path or path.startswith(("chat", "memory", "workspace", "activity"))
        ):
            return FileResponse(static / "index.html")
        return JSONResponse(
            {"code": "frontend_missing", "message": "先构建 Web 界面。"},
            status_code=404,
        )

    return app
