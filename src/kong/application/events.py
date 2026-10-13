"""Durable bounded metadata and command receipts; never another Memory database."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from kong.application.contracts import AppError


class EventStore:
    def __init__(self, path, profile):
        self.profile = profile
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              origin TEXT UNIQUE NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS offsets(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS run_intent(run_id TEXT PRIMARY KEY, intent TEXT NOT NULL);
        """)

    @property
    def cursor(self):
        return f"{self.profile}:{self.watermark}"

    @property
    def watermark(self):
        row = self.db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='events'"
        ).fetchone()
        return row[0] if row else 0

    def offset(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute(
                    "INSERT INTO offsets VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)",
                    (key, value),
                )
        row = self.db.execute(
            "SELECT value FROM offsets WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else 0

    def recent(self, limit=256):
        rows = self.db.execute(
            "SELECT sequence,body FROM events ORDER BY sequence DESC LIMIT ?",
            (min(limit, 256),),
        ).fetchall()
        return [
            {
                **json.loads(row["body"]),
                "sequence": row["sequence"],
                "event_id": f"{self.profile}:{row['sequence']}",
            }
            for row in reversed(rows)
        ]

    def append(
        self, kind, data, *, origin, run_id=None, thread_id=None, request_id=None
    ):
        prior = self.db.execute(
            "SELECT sequence FROM events WHERE origin=?", (origin,)
        ).fetchone()
        if prior:
            return f"{self.profile}:{prior[0]}"
        now = datetime.now(timezone.utc).isoformat()
        body = {
            "version": 1,
            "profile_id": self.profile,
            "kind": kind,
            "timestamp": now,
            "run_id": run_id,
            "thread_id": thread_id,
            "request_id": request_id,
            "data": data,
        }
        with self.db:
            sequence = self.db.execute(
                "INSERT INTO events(origin,body,created_at) VALUES(?,?,?)",
                (origin, json.dumps(body, ensure_ascii=False), now),
            ).lastrowid
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            self.db.execute(
                "DELETE FROM events WHERE sequence<=? OR created_at<?",
                (sequence - 100000, cutoff),
            )
        return f"{self.profile}:{sequence}"

    def read(self, after=None, limit=256):
        if not 1 <= limit <= 256:
            raise AppError("invalid_cursor", "事件批次最多 256 条。", 400)
        value = 0
        if after:
            prefix, sep, number = after.rpartition(":")
            if prefix != self.profile or not sep or not number.isdecimal():
                raise AppError("invalid_cursor", "事件游标不属于当前用户。", 403)
            value = int(number)
            minimum, maximum = self.db.execute(
                "SELECT min(sequence),max(sequence) FROM events"
            ).fetchone()
            if minimum is not None and value < minimum - 1:
                raise AppError(
                    "event_cursor_expired", "历史事件已裁剪，请刷新当前状态。", 410
                )
            if minimum is None and value < self.watermark:
                raise AppError(
                    "event_cursor_expired", "历史事件已裁剪，请刷新当前状态。", 410
                )
            if value > self.watermark:
                raise AppError("invalid_cursor", "事件游标超出当前记录。", 400)
        result = []
        for row in self.db.execute(
            "SELECT sequence,body FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
            (value, limit),
        ):
            result.append(
                {
                    **json.loads(row["body"]),
                    "sequence": row["sequence"],
                    "event_id": f"{self.profile}:{row['sequence']}",
                }
            )
        return result

    def claim(self, command, digest):
        row = self.db.execute(
            "SELECT digest,result FROM commands WHERE id=?", (command,)
        ).fetchone()
        if row:
            if row["digest"] != digest:
                raise AppError("command_conflict", "同一操作编号对应了不同内容。")
            return (
                json.loads(row["result"])
                if row["result"]
                else {"status": "unknown", "command_id": command}
            )
        with self.db:
            self.db.execute("INSERT INTO commands VALUES(?,?,NULL)", (command, digest))
        return None

    def finish(self, command, result):
        with self.db:
            self.db.execute(
                "UPDATE commands SET result=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), command),
            )
        return result

    def receipt(self, command):
        row = self.db.execute(
            "SELECT result FROM commands WHERE id=?", (command,)
        ).fetchone()
        if not row:
            raise AppError("not_found", "没有此操作记录。", 404)
        return (
            json.loads(row[0])
            if row[0]
            else {"command_id": command, "status": "unknown"}
        )

    def intent(self, run_id, value=None):
        if value:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO run_intent VALUES(?,?)", (run_id, value)
                )
        row = self.db.execute(
            "SELECT intent FROM run_intent WHERE run_id=?", (run_id,)
        ).fetchone()
        return row[0] if row else "legacy"

    def close(self):
        self.db.close()
