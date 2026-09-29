import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time

from kong.web.transport import WebError


def stable_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


class WebStore:
    """Bounded workspace cache; source payloads are immutable, indexes replaceable."""
    def __init__(self, workspace: Path):
        self.path = workspace / ".kong" / "web.db"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS web_entries (key TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL)")
                yield connection
        finally:
            connection.close()

    def get(self, key, ttl=None):
        if not self.path.exists():
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT payload,created FROM web_entries WHERE key=?", (key,)).fetchone()
        if not row or ttl is not None and time.time() - row[1] >= ttl:
            return None
        return json.loads(row[0])

    def put(self, key, value, *, immutable=False):
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode()) > 1000000:
            raise WebError("Source snapshot exceeds cache entry limit")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT payload FROM web_entries WHERE key=?", (key,)).fetchone()
            if existing and immutable:
                return json.loads(existing[0])
            count, size = conn.execute("SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) FROM web_entries").fetchone()
            previous = len(existing[0].encode()) if existing else 0
            if count + (0 if existing else 1) > 512 or size - previous + len(encoded.encode()) > 32000000:
                raise WebError("Workspace web cache quota reached (512 records / 32 MB); manage it before further retrieval")
            conn.execute("INSERT OR REPLACE INTO web_entries VALUES (?,?,?)", (key, encoded, time.time()))
        return value

    def source(self, source_id):
        if not re.fullmatch(r"[a-f0-9]{32}", source_id):
            raise WebError("Invalid source ID")
        result = self.get("source:" + source_id)
        if result is None:
            raise WebError("Source snapshot unavailable in this workspace; fetch it again in live mode")
        return result
