"""Replaceable FTS5 + metadata retrieval. Scope is a filter, never a ranking hint."""
from datetime import datetime, timezone
from typing import Protocol

from kong.continuity.contracts import Thread
from kong.continuity.memory_contracts import MemoryKind, MemoryRecord, MemoryScope, MemoryStatus, RankingConfig
from kong.continuity.memory_store import MemoryStore, lexical_terms


class MemoryRetriever(Protocol):
    def retrieve(self, query: str, thread: Thread, project_id: str | None = None, limit: int = 12) -> list[MemoryRecord]: ...


def scope_matches(record: MemoryRecord, thread: Thread, project_id: str | None = None) -> bool:
    return record.scope_id == {MemoryScope.GLOBAL: "*", MemoryScope.WORKSPACE: thread.workspace,
                               MemoryScope.PROJECT: project_id, MemoryScope.THREAD: thread.thread_id}[record.scope]


class SQLiteMemoryRetriever:
    def __init__(self, stores: list[MemoryStore], ranking: RankingConfig | None = None):
        self.stores = stores
        self.ranking = ranking or RankingConfig()

    def retrieve(self, query: str, thread: Thread, project_id: str | None = None, limit: int = 12) -> list[MemoryRecord]:
        results = {}
        terms = set(lexical_terms(query))
        timestamp = datetime.now(timezone.utc)
        w = self.ranking
        for store in self.stores:
            lexical = store.lexical(query)
            for record in store.memories():
                if record.status != MemoryStatus.ACTIVE or not scope_matches(record, thread, project_id):
                    continue
                if record.kind == MemoryKind.DECISION and not record.confirmed_by:
                    continue
                if record.expires_at and datetime.fromisoformat(record.expires_at) <= timestamp:
                    continue
                # Durable interaction preferences apply without lexical overlap.
                if record.id not in lexical and record.kind != MemoryKind.PREFERENCE:
                    continue
                age = max(0, (timestamp - datetime.fromisoformat(record.updated_at)).total_seconds() / 86400)
                overlap = len(terms & set(lexical_terms(record.content))) / max(1, len(terms))
                score = (w.lexical * lexical.get(record.id, 0) + w.importance * record.importance
                         + w.recency / (1 + age) + w.scope_match * (record.scope != MemoryScope.GLOBAL)
                         + w.entity_overlap * overlap)
                results[record.id] = (score, record)
        return [pair[1] for pair in sorted(results.values(), key=lambda pair: (-pair[0], pair[1].id))[:limit]]
