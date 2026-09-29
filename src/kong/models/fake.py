from collections import deque

from kong.context import Context
from kong.continuity.contracts import ContextPacket
from kong.contracts import Decision
from kong.models.base import ModelError


class ScriptedModel:
    """Offline deterministic driver for contract tests and demos, never a live fallback."""
    def __init__(self, decisions: list[Decision]):
        self.decisions = deque(decisions)
        self.calls = 0
        self.contexts: list[Context | ContextPacket] = []

    async def generate(self, context: Context | ContextPacket) -> Decision:
        self.calls += 1
        self.contexts.append(context)
        if not self.decisions:
            raise ModelError("Scripted model has no remaining decisions")
        return self.decisions.popleft()
