from typing import Protocol

from kong.context import Context
from kong.continuity.contracts import ContextPacket
from kong.contracts import Decision


class ModelError(RuntimeError):
    """Safe provider error. Never contains credentials or response bodies."""

    def __init__(self, message, *, category="other", retryable=False, status_code=None):
        super().__init__(message)
        self.category = category if category in {"other", "protocol", "timeout", "connection", "configuration",
            "authentication", "access", "billing", "rate_limit", "service_http"} else "other"
        self.retryable = retryable is True
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        self.fatal_configuration = self.category in {"configuration", "authentication", "access", "billing"}


class ModelProtocolError(ModelError):
    """A completed response violated the decision schema; no action was run."""

    def __init__(self, message: str, diagnostics=None):
        super().__init__(message, category="protocol", retryable=True)
        self.diagnostics = diagnostics or []


class Model(Protocol):
    async def generate(self, context: Context | ContextPacket) -> Decision: ...
