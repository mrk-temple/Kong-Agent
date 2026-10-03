"""Host input and event contracts. Only the loop applies queued input."""
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import Field
from kong.contracts import Contract


class RunInput(Contract):
    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: Literal["constraint", "goal", "cancel"]
    text: str = Field(default="", max_length=16000)


class RunEvent(Contract):
    version: Literal[1] = 1
    sequence: int = Field(ge=1)
    run_id: str
    thread_id: str
    turn: int
    status: Literal["running", "waiting_user", "completed", "stopped", "failed"]
    kind: Literal["permissions", "isolation", "input_queued", "input_applied", "model_waiting", "model_output",
                  "model_finished", "model_error", "model_retry", "decision_discarded", "batch_revoked",
                  "action_started", "action_finished", "observation", "transition", "action_unknown",
                  "completed", "paused", "cancelled", "stopped", "failed"]
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict = Field(default_factory=dict)
