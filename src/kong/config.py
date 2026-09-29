"""Explicit provider profiles; API secrets live in environment variables only."""
import os
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from kong.contracts import Contract


class ModelConfig(Contract):
    base_url: str
    model: str = Field(min_length=1)
    api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    json_mode: bool = True
    enable_thinking: bool | None = None
    timeout: float = Field(default=90, gt=0, le=600)
    max_tokens: int = Field(default=4096, ge=128, le=65536)
    context_window_tokens: int = Field(default=65536, gt=0)

    @model_validator(mode="after")
    def valid_context_window(self):
        if self.max_tokens >= self.context_window_tokens:
            raise ValueError("context_window_tokens must exceed max_tokens")
        return self

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        return value.rstrip("/")


def load_config(path: Path, profile: str | None = None) -> ModelConfig:
    if not path.exists():
        raise ValueError(f"Config not found: {path}. Copy kong.example.toml to kong.local.toml and select a profile.")
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    selected = profile or data.get("default_profile", "deepseek")
    profiles = data.get("profiles", {})
    if selected not in profiles:
        raise ValueError(f"Unknown model profile: {selected}")
    raw = dict(profiles[selected])
    key_env = raw.pop("api_key_env", "KONG_API_KEY")
    if "api_key" in raw:
        raise ValueError("Use api_key_env; do not put plaintext API keys in TOML")
    return ModelConfig(**raw, api_key=SecretStr(os.environ.get(key_env, "")))
