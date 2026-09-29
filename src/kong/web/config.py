import os
from pathlib import Path
import tomllib
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from urllib.parse import urlsplit

from kong.contracts import Contract


class WebConfig(Contract):
    mode: Literal["disabled", "cached", "live"] = "disabled"
    provider: Literal["none", "brave", "tavily"] = "none"
    api_key_env: str = ""
    api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    timeout: float = Field(default=20, gt=0, le=45)
    cache_ttl_seconds: int = Field(default=300, ge=0, le=86400)
    max_response_bytes: int = Field(default=2000000, ge=10000, le=4000000)
    proxy_url: str | None = None

    @field_validator("proxy_url")
    @classmethod
    def valid_proxy(cls, value):
        if value is None:
            return value
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("proxy_url must be an explicit credential-free HTTP(S) proxy")
        return value

    def status(self):
        return {"mode": self.mode, "provider": self.provider, "api_key_env": self.api_key_env,
                "key_present": bool(self.api_key.get_secret_value()),
                "search_available": self.mode != "disabled" and self.provider != "none" and
                    (self.mode == "cached" or bool(self.api_key.get_secret_value())),
                "fetch_available": self.mode != "disabled",
                "proxy_configured": self.proxy_url is not None,
                "cache": "workspace snapshots, not an online search index"}


def load_web_config(path: Path, mode=None):
    data = tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    section = data.get("web", {})
    if not isinstance(section, dict):
        raise ValueError("[web] must be a TOML table")
    raw = dict(section)
    if "api_key" in raw:
        raise ValueError("Web API keys must use api_key_env, not plaintext TOML")
    if mode is not None:
        raw["mode"] = mode
    raw.setdefault("api_key_env", {"brave": "BRAVE_SEARCH_API_KEY", "tavily": "TAVILY_API_KEY"}.get(raw.get("provider"), ""))
    config = WebConfig(**raw)
    config.api_key = SecretStr(os.environ.get(config.api_key_env, ""))
    return config
