from typing import Protocol

from kong.context import Context
from kong.continuity.contracts import ContextPacket
from kong.contracts import Decision


class ModelError(RuntimeError):
    """Safe provider error. Never contains credentials or response bodies."""


class ModelProtocolError(ModelError):
    """A completed response violated the decision schema; no action was run."""


class Model(Protocol):
    async def generate(self, context: Context | ContextPacket) -> Decision: ...
