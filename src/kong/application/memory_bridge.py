"""Bounded async adapter. The SQLite service lives only on its dedicated worker."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import BoundedSemaphore

from kong.memory import (
    AccessState,
    Authority,
    Capability,
    InteractionObservation,
    MemoryContext,
    MemoryService,
)
from kong.memory.errors import MemoryError, NotFound
from kong.memory.policy import require


class MemoryBridge:
    def __init__(self, path: Path | str, authority: Authority, *, timeout=0.5):
        if timeout <= 0:
            raise ValueError("A positive Memory I/O deadline is required")
        require(authority, Capability.READ)
        self.path = Path(path)
        self.authority = authority.model_copy(
            update={
                "actor": "observer",
                "input_id": None,
                "capabilities": authority.capabilities
                & frozenset({Capability.READ, Capability.OBSERVE}),
            }
        )
        self.timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kong-memory")
        self._slot = BoundedSemaphore(1)
        self._service = None
        self._closed = False
        self._delivered = set()

    @property
    def principal(self):
        return {
            "owner_id": self.authority.owner_id,
            "profile_id": self.authority.profile_id,
            "scopes": [s.model_dump(mode="json") for s in self.authority.scopes],
        }

    @property
    def can_observe(self):
        return Capability.OBSERVE in self.authority.capabilities

    @property
    def protected_paths(self):
        return tuple(
            Path(str(self.path.resolve()) + suffix)
            for suffix in ("", "-wal", "-shm", "-journal")
        )

    def _invoke(self, operation, args):
        if self._service is None:
            if not self.path.is_file():
                raise NotFound("Memory database is unavailable")
            self._service = MemoryService(self.path)
        return getattr(self._service, operation)(self.authority, *args)

    async def _read(self, operation, *args, timeout=None):
        if self._closed or not self._slot.acquire(blocking=False):
            return None  # Bounded queue, not an unbounded pile of timed-out reads.
        try:
            future = self._pool.submit(self._invoke, operation, args)
        except BaseException:
            self._slot.release()
            raise
        future.add_done_callback(lambda _: self._slot.release())
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future),
                self.timeout if timeout is None else min(timeout, self.timeout),
            )
        except (OSError, sqlite3.Error, MemoryError, ValueError, TimeoutError):
            return None  # No stale cache; a late result is never reused.

    async def retrieve_context(self, query) -> MemoryContext | None:
        return await self._read(
            "retrieve_context", query[:2000] if isinstance(query, str) else query
        )

    async def get_access_state(self) -> AccessState | None:
        return await self._read("get_access_state")

    @staticmethod
    def _observation_messages(snapshot):
        from kong.continuity.history import HistorySelector
        from kong.continuity.contracts import ConversationMessage, ContextSource

        messages = HistorySelector.conversation(snapshot, snapshot.thread_id)
        # state.goal can change when queued input_goal is applied. The initial
        # event must keep its original content and stable idempotency key.
        original = next(
            (
                e.payload.get("text")
                for e in snapshot.history.events
                if e.type == "human" and e.payload.get("command") == "goal"
            ),
            None,
        )
        if original is None:
            original = next(
                (
                    e.payload.get("previous_goal")
                    for e in snapshot.history.events
                    if e.type == "human" and e.payload.get("command") == "input_goal"
                ),
                snapshot.state.goal,
            )
        if isinstance(original, str):
            messages[0] = messages[0].model_copy(update={"content": original})
        for index, event in enumerate(snapshot.history.events):
            if event.type == "human" and event.payload.get("command") in {
                "input_message",
                "input_goal",
                "input_constraint",
            }:
                text = event.payload.get("text")
                if isinstance(text, str) and text:
                    messages.append(
                        ConversationMessage(
                            role="user",
                            content=text,
                            source=ContextSource(
                                kind="event",
                                thread_id=snapshot.thread_id,
                                run_id=snapshot.state.run_id,
                                event_index=index,
                                event_id=f"{snapshot.state.run_id}:event:{index}",
                                path=f"history.events[{index}]",
                            ),
                        )
                    )
        return sorted(
            messages,
            key=lambda m: -1 if m.source.event_index is None else m.source.event_index,
        )

    async def observe_snapshot(self, snapshot, *, max_seconds=0.1):
        """Only persisted accepted conversation, with the Run's ORIGINAL consent.

        Hosts can replay this after a crash without starting an Agent Loop.
        The inbox deduplicates even when this process-local optimization is lost.
        """
        if not self.can_observe:
            return False
        binding = snapshot.memory_binding
        if binding is None or any(
            binding.get(k) != v for k, v in self.principal.items()
        ):
            return False
        if max_seconds <= 0:
            raise ValueError("Observation replay requires a positive time budget")
        deadline = asyncio.get_running_loop().time() + max_seconds
        delivered = 0
        for message in self._observation_messages(snapshot):
            if message.role == "assistant" and message.source.event_index is not None:
                index = message.source.event_index
                decision = snapshot.history.events[index].payload.get("decision", {})
                if decision.get("kind") == "ask":
                    following = []
                    for later in snapshot.history.events[index + 1 :]:
                        if later.type in {"decision", "human"}:
                            break
                        following.append(later)
                    if not any(
                        e.type == "feedback"
                        and e.payload.get("transition", {}).get("reason")
                        == "user_input_required"
                        for e in following
                    ):
                        continue
            event_id = message.source.event_id
            key = (self.authority.profile_id, event_id)
            if key in self._delivered:
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if delivered >= 32 or remaining <= 0:
                return False  # Replay the persisted snapshot later; bounded work per commit.
            capture = binding.get("capture", "off")
            event = InteractionObservation(
                event_id=event_id,
                capture=capture,
                original_access=AccessState.model_validate(binding["access"])
                if binding.get("access")
                else None,
                role=message.role,
                text=message.content[:6000] if capture == "on" else None,
            )
            receipt = await self._read("observe_interaction", event, timeout=remaining)
            if receipt is None:
                return False
            if len(self._delivered) >= 4096:
                self._delivered.clear()  # Correctness belongs to the durable idempotency key.
            self._delivered.add(key)
            delivered += 1
        return True

    def _close_worker(self):
        if self._service is not None:
            self._service.close()
            self._service = None

    async def aclose(self):
        if not self._closed:
            self._closed = True
            try:
                await asyncio.wrap_future(self._pool.submit(self._close_worker))
            finally:
                self._pool.shutdown(wait=False, cancel_futures=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()
