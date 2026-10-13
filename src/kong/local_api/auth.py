"""Exact loopback origin, ephemeral session and cookie CSRF boundary."""

import hmac
import secrets
import time
from urllib.parse import urlsplit
from kong.application.contracts import AppError


class LocalSession:
    def __init__(self, origin, token=None):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Local API requires an exact 127.0.0.1 HTTP origin")
        self.origin, self.host = origin, parsed.netloc
        self.token = token or secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.expires = time.monotonic() + 8 * 3600

    def check(self, request):
        if request.headers.get("host") != self.host:
            raise AppError("forbidden", "本地 Host 不匹配。", 403)
        origin = request.headers.get("origin")
        if (
            origin
            and origin != self.origin
            or request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}
        ):
            raise AppError("forbidden", "拒绝其他网站访问本地 Agent。", 403)
        if not request.url.path.startswith("/v1/") or request.url.path == "/v1/health":
            return
        if time.monotonic() > self.expires:
            raise AppError(
                "session_expired", "本地会话已过期，请从本地入口重新启动。", 401
            )
        bearer = request.headers.get("authorization", "")
        authorized_bearer = hmac.compare_digest(bearer, "Bearer " + self.token)
        cookie = request.cookies.get("kong_session", "")
        authorized_cookie = hmac.compare_digest(cookie, self.token)
        if not (authorized_bearer or authorized_cookie):
            raise AppError("session_required", "需要本地会话授权。", 401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if origin != self.origin:
                raise AppError("forbidden", "写入需要精确本地来源。", 403)
            if not authorized_bearer and not hmac.compare_digest(
                request.headers.get("x-kong-csrf", ""), self.csrf
            ):
                raise AppError("forbidden", "写入校验失败，请刷新本地会话。", 403)
