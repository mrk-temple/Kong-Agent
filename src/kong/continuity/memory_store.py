"""Transactional derived memory and FTS5 index; original History stays in RunStore."""
import json
from pathlib import Path
import re

from kong.continuity.contracts import ContextPacket, now
from kong.continuity.memory_contracts import Episode, MemoryRecord, MemoryStatus, ThreadItem, ThreadState
from kong.continuity.storage import ThreadStore


def lexical_terms(text: str) -> list[str]:
    # unicode61 does not segment Chinese words. Add overlapping CJK bigrams
    # explicitly, while retaining Latin words. No vector service required.
    words = re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+", text.lower())
    terms = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            terms.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        else:
            terms.append(word)
    return list(dict.fromkeys(terms))


class MemoryStore(ThreadStore):
    def __init__(self, path: Path):
        super().__init__(path)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS thread_revisions(thread_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS thread_items(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS items_thread ON thread_items(thread_id);
                CREATE TABLE IF NOT EXISTS applied_deltas(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS context_cursors(run_id TEXT PRIMARY KEY, event_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS episodes(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    last_event INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS long_term_memories(id TEXT PRIMARY KEY, scope TEXT NOT NULL, scope_id TEXT NOT NULL,
                    status TEXT NOT NULL, memory_key TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS memories_scope ON long_term_memories(scope, scope_id, status);
                CREATE TABLE IF NOT EXISTS memory_links(memory_id TEXT NOT NULL, event_id TEXT NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(memory_id, event_id));
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, terms);
                CREATE TABLE IF NOT EXISTS context_packets(run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS context_audit(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    def state(self, thread_id: str) -> ThreadState:
        with self._connection() as db:
            revision = db.execute("SELECT revision FROM thread_revisions WHERE thread_id=?", (thread_id,)).fetchone()
            items = db.execute("SELECT data FROM thread_items WHERE thread_id=? ORDER BY rowid", (thread_id,)).fetchall()
        return ThreadState(thread_id=thread_id, revision=revision[0] if revision else 0,
                           items=[ThreadItem.model_validate_json(row[0]) for row in items])

    def save_state(self, state: ThreadState, base_revision: int, delta_id: str) -> bool:
        delta_id = state.thread_id + ":" + delta_id
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM applied_deltas WHERE id=? AND thread_id=?", (delta_id, state.thread_id)).fetchone():
                return False
            row = db.execute("SELECT revision FROM thread_revisions WHERE thread_id=?", (state.thread_id,)).fetchone()
            if (row[0] if row else 0) != base_revision:
                raise ValueError("Stale ThreadState revision")
            db.execute("INSERT OR REPLACE INTO thread_revisions VALUES (?, ?)", (state.thread_id, state.revision))
            for item in state.items:
                db.execute("INSERT OR REPLACE INTO thread_items VALUES (?, ?, ?)",
                           (item.id, state.thread_id, item.model_dump_json()))
            db.execute("INSERT INTO applied_deltas VALUES (?, ?)", (delta_id, state.thread_id))
        return True

    def applied(self, delta_id: str, thread_id: str) -> bool:
        delta_id = thread_id + ":" + delta_id
        with self._connection() as db:
            return db.execute("SELECT 1 FROM applied_deltas WHERE id=? AND thread_id=?", (delta_id, thread_id)).fetchone() is not None

    def cursor(self, run_id: str) -> int:
        with self._connection() as db:
            row = db.execute("SELECT event_count FROM context_cursors WHERE run_id=?", (run_id,)).fetchone()
        return row[0] if row else 0

    def reset_thread_derivatives(self, thread_id: str) -> None:
        """Rebuild input only: retain membership, raw snapshots, memory tombstones and actual packets."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM thread_items WHERE thread_id=?", (thread_id,))
            db.execute("INSERT INTO thread_revisions VALUES (?, 1) ON CONFLICT(thread_id) DO UPDATE SET revision=revision+1", (thread_id,))
            db.execute("DELETE FROM applied_deltas WHERE thread_id=?", (thread_id,))
            db.execute("DELETE FROM episodes WHERE thread_id=?", (thread_id,))
            db.execute("DELETE FROM context_cursors WHERE run_id IN (SELECT run_id FROM thread_runs WHERE thread_id=?)", (thread_id,))

    def set_cursor(self, run_id: str, count: int) -> None:
        with self._connection() as db:
            db.execute("INSERT INTO context_cursors VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET event_count=max(event_count, excluded.event_count)", (run_id, count))

    def episodes(self, thread_id: str) -> list[Episode]:
        with self._connection() as db:
            rows = db.execute("SELECT data FROM episodes WHERE thread_id=? ORDER BY rowid", (thread_id,)).fetchall()
        return [Episode.model_validate_json(row[0]) for row in rows]

    def save_episode(self, episode: Episode) -> None:
        with self._connection() as db:
            db.execute("INSERT OR IGNORE INTO episodes VALUES (?, ?, ?, ?, ?)",
                       (episode.id, episode.thread_id, episode.run_id, episode.last_event, episode.model_dump_json()))

    @staticmethod
    def _write_memory(db, record: MemoryRecord) -> None:
        db.execute("INSERT OR REPLACE INTO long_term_memories VALUES (?, ?, ?, ?, ?, ?)",
                   (record.id, record.scope, record.scope_id, record.status, record.key, record.model_dump_json()))
        db.execute("DELETE FROM memory_fts WHERE memory_id=?", (record.id,))
        if record.status == MemoryStatus.ACTIVE:
            db.execute("INSERT INTO memory_fts VALUES (?, ?)", (record.id, " ".join(lexical_terms(record.content + " " + record.key))))
        for source in record.sources:
            db.execute("INSERT OR REPLACE INTO memory_links VALUES (?, ?, ?)",
                       (record.id, source.event_id, source.model_dump_json()))

    def save_memory(self, record: MemoryRecord) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if record.status == MemoryStatus.ACTIVE:
                rows = db.execute("SELECT data FROM long_term_memories WHERE scope=? AND scope_id=? AND memory_key=? AND status='active' AND id!=?",
                                  (record.scope, record.scope_id, record.key, record.id)).fetchall()
                for row in rows:
                    old = MemoryRecord.model_validate_json(row[0])
                    old.status = MemoryStatus.SUPERSEDED
                    old.updated_at = now()
                    record.supersedes = old.id
                    self._write_memory(db, old)
            self._write_memory(db, record)

    def memories(self) -> list[MemoryRecord]:
        with self._connection() as db:
            rows = db.execute("SELECT data FROM long_term_memories ORDER BY rowid").fetchall()
        return [MemoryRecord.model_validate_json(row[0]) for row in rows]

    def get_memory(self, memory_id: str) -> MemoryRecord:
        with self._connection() as db:
            row = db.execute("SELECT data FROM long_term_memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown memory")
        return MemoryRecord.model_validate_json(row[0])

    def lexical(self, query: str) -> dict[str, float]:
        terms = lexical_terms(query)[:64]
        if not terms:
            return {}
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        with self._connection() as db:
            rows = db.execute("SELECT memory_id, bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 200",
                              (expression,)).fetchall()
        return {row[0]: 1 / (index + 1) for index, row in enumerate(rows)}

    def touch(self, ids: list[str]) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for memory_id in ids:
                row = db.execute("SELECT data FROM long_term_memories WHERE id=?", (memory_id,)).fetchone()
                if row:
                    record = MemoryRecord.model_validate_json(row[0])
                    record.last_accessed_at = now()
                    db.execute("UPDATE long_term_memories SET data=? WHERE id=?", (record.model_dump_json(), memory_id))

    def save_packet(self, packet: ContextPacket) -> None:
        with self._connection() as db:
            db.execute("INSERT OR REPLACE INTO context_packets VALUES (?, ?)", (packet.run_id, packet.model_dump_json()))

    def packet(self, run_id: str) -> ContextPacket | None:
        with self._connection() as db:
            row = db.execute("SELECT data FROM context_packets WHERE run_id=?", (run_id,)).fetchone()
        return ContextPacket.model_validate_json(row[0]) if row else None

    def audit(self, run_id: str, kind: str, **data) -> None:
        with self._connection() as db:
            db.execute("INSERT INTO context_audit(run_id, kind, data, created_at) VALUES (?, ?, ?, ?)",
                       (run_id, kind, json.dumps(data, ensure_ascii=False), now()))

    def audits(self, run_id: str) -> list[dict]:
        with self._connection() as db:
            rows = db.execute("SELECT kind, data, created_at FROM context_audit WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [{"kind": row[0], "data": json.loads(row[1]), "created_at": row[2]} for row in rows]
