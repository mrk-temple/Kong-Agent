"""Deterministic delta merge and promotion, with checked event provenance."""
import hashlib
import json
from pathlib import Path
import re
from typing import Protocol

from kong.continuity.contracts import ContextSource, Thread, now
from kong.continuity.memory_contracts import (
    MemoryCandidate, MemoryDelta, MemoryKind, MemoryMutation, MemoryRecord,
    MemoryScope, MemoryStatus, ThreadItem, ThreadState,
)
from kong.continuity.memory_store import MemoryStore
from kong.contracts import History
from kong.storage import RunStore


def stable_id(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


def source_for(thread_id: str, run_id: str, index: int) -> ContextSource:
    return ContextSource(kind="event", thread_id=thread_id, run_id=run_id, event_index=index,
                         event_id=f"{run_id}:event:{index}", path=f"history.events[{index}]")


class DeltaExtractor(Protocol):
    def extract(self, thread_id: str, run_id: str, history: History, start: int, revision: int) -> MemoryDelta: ...


class ExplicitExtractor:
    """Small offline fallback for explicit statements. Semantic deltas may also
    be proposed by the normal model response; no second model request exists.
    """
    def extract(self, thread_id: str, run_id: str, history: History, start: int, revision: int) -> MemoryDelta:
        mutations = []
        for index in range(start, len(history.events)):
            event = history.events[index]
            if event.type != "human" or event.payload.get("command") not in {"goal", "answer", "discuss", "memory_add"}:
                continue
            raw = event.payload.get("text", "")
            if not isinstance(raw, str):
                continue
            text = raw.strip()
            source = source_for(thread_id, run_id, index)
            key, kind, content = None, "fact", text
            if event.payload.get("command") == "memory_add":
                key, kind = event.payload["key"], event.payload["kind"]
            elif re.search(r"以后.{0,8}(叫我|称呼我)|(?:always )?call me\b", text, re.I):
                key, kind = "user.preferred_name", "preference"
            elif re.search(r"(以后|默认|始终|总是).{0,12}(中文|英文|英语|汉语).{0,12}(回复|回答)|(?:always|prefer).{0,20}(?:Chinese|English)", text, re.I):
                key, kind = "user.response_language", "preference"
            elif re.match(r"(?:你好[，,！!\s]*)?(?:我叫|我是|我的名字是)|my name is\b", text, re.I):
                key = "user.self_identification"
            elif re.match(r"(?:我喜欢|我偏好|I prefer\b)", text, re.I) and not re.search(r"这次|这封|这一个|this time|for this", text, re.I):
                key, kind = "preference." + stable_id(text), "preference"
            elif text.startswith(("决定：", "决定:", "Decision:")):
                key, kind = "decision." + stable_id(text), "decision"
            elif text.startswith(("约束：", "约束:", "Constraint:")):
                key, kind = "constraint." + stable_id(text), "constraint"
            elif text.startswith(("待办：", "待办:", "TODO:")):
                key, kind = "open." + stable_id(text), "open_loop"
            if key:
                mutations.append(MemoryMutation(key=key, kind=kind, content=content,
                                                importance=0.8, confidence=0.95, sources=[source]))
        return MemoryDelta(id=stable_id(thread_id, run_id, start, len(history.events), "explicit"),
                           thread_id=thread_id, base_revision=revision, mutations=mutations[:100])


class ThreadMemoryManager:
    def __init__(self, store: MemoryStore, runs: RunStore):
        self.store = store
        self.runs = runs

    def event(self, source: ContextSource, thread_id: str, current: tuple[str, History] | None = None):
        if (source.kind != "event" or source.thread_id != thread_id or source.event_index is None
                or source.event_id != f"{source.run_id}:event:{source.event_index}"
                or self.store.thread_for_run(source.run_id) != thread_id):
            raise ValueError("Invalid memory provenance or Thread scope")
        if current and current[0] == source.run_id:
            history = current[1]
        else:
            snapshot = self.runs.load(source.run_id)
            thread = self.store.get(thread_id)
            if Path(snapshot.workspace).resolve() != Path(thread.workspace).resolve() or snapshot.thread_id not in (None, thread_id):
                raise ValueError("Source workspace mismatch")
            history = snapshot.history
        if source.event_index >= len(history.events):
            raise ValueError("Unknown source event")
        source.snapshot_path = str(self.runs.path(source.run_id))
        return history.events[source.event_index]

    def merge(self, delta: MemoryDelta, current: tuple[str, History] | None = None) -> ThreadState:
        state = self.store.state(delta.thread_id)
        if self.store.applied(delta.id, delta.thread_id):
            return state
        if state.revision != delta.base_revision:
            raise ValueError("Stale MemoryDelta revision")
        # Validate the entire batch before changing any persisted derived state.
        for mutation in delta.mutations:
            events = [self.event(s, delta.thread_id, current) for s in mutation.sources]
            if mutation.kind != "artifact" and not any(e.type == "human" for e in events):
                raise ValueError("Facts/preferences/decisions require a user source")
            if mutation.kind == "artifact" and not any(e.type == "human" or (
                    e.type == "observation" and e.payload.get("observation", {}).get("success")) for e in events):
                raise ValueError("Artifact requires user or successful observation")
            if mutation.operation in {"update", "supersede", "resolve", "reject"} and not mutation.target_id:
                raise ValueError("Mutation requires target_id")
        for index, mutation in enumerate(delta.mutations):
            new_id = stable_id(delta.thread_id, mutation.kind, mutation.key, mutation.content,
                               [(s.run_id, s.event_index) for s in mutation.sources])
            if mutation.operation in {"add", "update", "supersede"}:
                if any(i.id == new_id for i in state.items):
                    continue
                for prior in state.items:
                    if (prior.key == mutation.key
                            and prior.status in {MemoryStatus.REJECTED, MemoryStatus.RESOLVED}
                            and {s.event_id for s in mutation.sources} <= {s.event_id for s in prior.sources}):
                        raise ValueError("Inactive memory needs a new user source")
            target = next((item for item in state.items if item.id == mutation.target_id), None)
            if mutation.target_id and (target is None or target.key != mutation.key or target.kind != mutation.kind):
                raise ValueError("Unknown or mismatched mutation target")
            if mutation.operation in {"resolve", "reject"}:
                if target.kind == "decision" and target.confirmed_by:
                    authorization = [self.event(s, delta.thread_id, current) for s in mutation.sources]
                    if not any(e.type == "human" and e.payload.get("command") in {"memory_reject", "memory_forget", "memory_resolve"}
                               and e.payload.get("item_id") == target.id
                               for e in authorization):
                        raise ValueError("Confirmed decisions require an explicit memory management command")
                target.status = MemoryStatus.RESOLVED if mutation.operation == "resolve" else MemoryStatus.REJECTED
                target.sources = list({s.event_id: s for s in target.sources + mutation.sources}.values())
                target.updated_at = now()
                continue
            existing = [i for i in state.items if i.key == mutation.key
                        and i.status in {MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE}]
            if mutation.kind != "decision" and any(i.kind == "decision" and i.confirmed_by and i.status == MemoryStatus.ACTIVE for i in existing):
                raise ValueError("A confirmed decision requires a new decision candidate to replace it")
            membership = {run_id: order for order, run_id in enumerate(self.store.runs(delta.thread_id))}
            def newest(sources):
                return max((membership.get(s.run_id, -1), s.event_index or 0) for s in sources)
            if existing and newest(mutation.sources) < max(newest(i.sources) for i in existing):
                raise ValueError("Older sources cannot overwrite a newer memory revision")
            if existing and existing[-1].content == mutation.content:
                continue
            status = MemoryStatus.CANDIDATE if mutation.kind == "decision" else MemoryStatus.ACTIVE
            # A pending replacement decision cannot silently revoke a previously
            # confirmed decision. Active conflict is removed only on confirmation.
            for old in existing:
                if status == MemoryStatus.ACTIVE or old.status == MemoryStatus.CANDIDATE:
                    old.status = MemoryStatus.SUPERSEDED
                    old.updated_at = now()
            state.items.append(ThreadItem(id=new_id, thread_id=delta.thread_id,
                key=mutation.key, kind=mutation.kind, content=mutation.content, status=status,
                sources=mutation.sources, importance=mutation.importance, confidence=mutation.confidence,
                supersedes=existing[-1].id if existing else None))
        state.revision += 1
        self.store.save_state(state, delta.base_revision, delta.id)
        return self.store.state(delta.thread_id)

    def confirm(self, item_id: str, source: ContextSource, current: tuple[str, History]) -> ThreadItem:
        state = self.store.state(source.thread_id)
        item = next((i for i in state.items if i.id == item_id), None)
        event = self.event(source, source.thread_id, current)
        if (item is None or item.status not in {MemoryStatus.CANDIDATE, MemoryStatus.ACTIVE}
                or event.type != "human" or event.payload.get("command") != "memory_confirm"
                or event.payload.get("item_id") != item_id
                or event.payload.get("content_hash") != stable_id(item.content)):
            raise ValueError("Confirmation must reference the exact current memory content")
        revision = state.revision
        for old in state.items:
            if old.id != item.id and old.key == item.key and old.status == MemoryStatus.ACTIVE:
                old.status = MemoryStatus.SUPERSEDED
                old.updated_at = now()
        item.status = MemoryStatus.ACTIVE
        item.confirmed_by = source
        item.sources.append(source)
        item.updated_at = now()
        state.revision += 1
        self.store.save_state(state, revision, stable_id("confirm", source.event_id, item_id))
        return item


class PromotionPolicy:
    """Only explicit durable preferences auto-promote; other types need confirmation."""
    def candidate(self, item: ThreadItem, thread: Thread, manager: ThreadMemoryManager,
                  current: tuple[str, History], project_id: str | None = None) -> MemoryCandidate | None:
        kinds = {"preference": MemoryKind.PREFERENCE, "fact": MemoryKind.STABLE_FACT,
                 "project": MemoryKind.PROJECT, "decision": MemoryKind.DECISION, "procedure": MemoryKind.PROCEDURE}
        if item.kind not in kinds or item.status not in {MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE}:
            return None
        events = [manager.event(source, item.thread_id, current) for source in item.sources]
        # Promotion scope belongs to the original source, not the Run that
        # happens to trigger consolidation later.
        project_id = manager.runs.load(item.sources[0].run_id).project_id
        text = "\n".join(e.payload.get("text", "") for e in events if e.type == "human")
        durable = bool(re.search(r"以后|默认|始终|总是|我喜欢|我偏好|记住|always|prefer|remember|call me", text, re.I))
        local = bool(re.search(r"这次|这封|仅本次|this time|for this", text, re.I))
        explicit_add = next((e for e in events if e.payload.get("command") == "memory_add"), None)
        if item.kind == "fact" and not item.confirmed_by and not re.search(r"记住|remember", text, re.I) and not explicit_add:
            return None  # A self-introduction alone is Thread-local.
        scope = MemoryScope.GLOBAL if item.kind in {"preference", "fact"} else MemoryScope.PROJECT if project_id else MemoryScope.WORKSPACE
        if item.kind in {"preference", "fact"} and (item.key.startswith("project.") or re.search(
                r"这个项目|本项目|当前项目|this project|for the project", text, re.I)):
            scope = MemoryScope.PROJECT if project_id else MemoryScope.WORKSPACE
        if explicit_add:
            scope = MemoryScope(explicit_add.payload.get("scope", scope))
        scope_id = {MemoryScope.GLOBAL: "*", MemoryScope.WORKSPACE: thread.workspace,
                    MemoryScope.PROJECT: project_id, MemoryScope.THREAD: thread.thread_id}[scope]
        if not scope_id:
            raise ValueError("Project memory requires project_id")
        verbatim = any(item.content in e.payload.get("text", "") for e in events if e.type == "human")
        auto = item.kind == "preference" and durable and not local and verbatim and item.confidence >= 0.85
        confirmed = item.confirmed_by is not None
        return MemoryCandidate(id=stable_id(item.id, scope, scope_id), thread_item_id=item.id, thread_id=item.thread_id,
                               key=item.key, content=item.content, kind=kinds[item.kind], scope=scope, scope_id=scope_id,
                               sources=item.sources, importance=item.importance, confidence=item.confidence,
                               confirmed_by=item.confirmed_by,
                               status=MemoryStatus.ACTIVE if auto or confirmed else MemoryStatus.CANDIDATE)

    def promote(self, candidate: MemoryCandidate, store: MemoryStore) -> MemoryRecord:
        if candidate.kind == MemoryKind.DECISION and candidate.status == MemoryStatus.ACTIVE and not candidate.confirmed_by:
            raise ValueError("Decision requires explicit human confirmation")
        try:
            existing = store.get_memory(candidate.id)
        except ValueError:
            existing = None
        if existing and (existing.status in {MemoryStatus.REJECTED, MemoryStatus.SUPERSEDED, MemoryStatus.RESOLVED}
                         or existing.status == candidate.status):
            return existing
        record = MemoryRecord(**candidate.model_dump())
        if existing:
            record.created_at = existing.created_at
        store.save_memory(record)
        return record
