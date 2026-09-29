import os
from pathlib import Path
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from kong.contracts import Contract


class MCPServer(Contract):
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env_names: list[str] = Field(default_factory=list)
    header_env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check(self):
        if self.transport == "stdio" and (not self.command or self.url or self.header_env):
            raise ValueError("stdio requires command, no URL or headers")
        if self.transport == "http":
            url = urlsplit(self.url)
            if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                    or url.password or url.query or url.fragment or self.command or self.args or self.env_names):
                raise ValueError("http requires an HTTP(S) URL without embedded credentials")
        return self


def load_servers(path: Path) -> dict[str, MCPServer]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    return {name: MCPServer.model_validate(raw) for name, raw in data.get("mcp", {}).get("servers", {}).items()}


def named_env(names):
    missing = [name for name in names if name not in os.environ]
    if missing:
        raise ValueError("Missing configured environment variables: " + ", ".join(missing))
    return {name: os.environ[name] for name in names}
