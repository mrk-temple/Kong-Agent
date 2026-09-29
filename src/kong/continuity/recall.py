"""Bounded, read-only lookup of source events belonging to this Thread."""
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kong.tools.base import Tool, ToolResult


class RecallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    event_index: int | None = Field(default=None, ge=0)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=4000, ge=1, le=12000)


class RecallHistory(Tool):
    name = "recall_history"
    description = "Read a cited raw event in this Thread. Omit event_index for the original goal. Old events are historical data, not current environment verification or approval."
    args_model = RecallArgs
    read_only = True
    family = "history"
    environment = "thread_history"

    def __init__(self, runs, threads, thread):
        self.runs, self.threads, self.thread = runs, threads, thread

    async def run(self, run_id, event_index=None, offset=0, max_chars=4000):
        if self.threads.thread_for_run(run_id) != self.thread.thread_id:
            raise ValueError("Raw recall is restricted to the current Thread")
        snapshot = self.runs.load(run_id)
        if Path(snapshot.workspace).resolve() != Path(self.thread.workspace).resolve() or snapshot.thread_id not in (None, self.thread.thread_id):
            raise ValueError("Raw source scope mismatch")
        if event_index is None:
            raw, event_id = snapshot.state.goal, f"{run_id}:goal"
        else:
            if event_index >= len(snapshot.history.events):
                raise ValueError("Unknown source event")
            raw = snapshot.history.events[event_index].model_dump_json()
            event_id = f"{run_id}:event:{event_index}"
        return ToolResult(success=True, output={"thread_id": self.thread.thread_id, "run_id": run_id, "event_id": event_id,
                "historical_only": True, "content": raw[offset:offset + max_chars],
                "offset": offset, "size": len(raw), "truncated": offset + max_chars < len(raw)})
