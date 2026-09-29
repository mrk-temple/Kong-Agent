"""SQLite Thread metadata only. Raw Run snapshots remain in .kong/runs/."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from kong.continuity.contracts import Thread


class ThreadStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                    title TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thread_runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES threads(thread_id)
                );
                CREATE INDEX IF NOT EXISTS thread_runs_thread ON thread_runs(thread_id);
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, workspace: Path, title: str = "", *, thread_id: str | None = None) -> Thread:
        fields = {"thread_id": thread_id} if thread_id is not None else {}
        thread = Thread(workspace=str(workspace.resolve()), title=title, **fields)
        with self._connection() as db:
            db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                       (thread.thread_id, thread.workspace, thread.title, thread.status, thread.created_at))
        return thread

    def get(self, thread_id: str) -> Thread:
        with self._connection() as db:
            row = db.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown thread")
        return Thread.model_validate(dict(row))

    def list_threads(self, workspace: Path) -> list[Thread]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM threads WHERE workspace=? ORDER BY created_at, thread_id",
                              (str(workspace.resolve()),)).fetchall()
        return [Thread.model_validate(dict(row)) for row in rows]

    def set_archived(self, thread_id: str, archived: bool) -> Thread:
        self.get(thread_id)
        with self._connection() as db:
            db.execute("UPDATE threads SET status=? WHERE thread_id=?",
                       ("archived" if archived else "active", thread_id))
        return self.get(thread_id)

    def thread_for_run(self, run_id: str) -> str | None:
        with self._connection() as db:
            row = db.execute("SELECT thread_id FROM thread_runs WHERE run_id=?", (run_id,)).fetchone()
        return row[0] if row else None

    def attach_run(self, thread_id: str, run_id: str, workspace: Path) -> None:
        with self._connection() as db:
            # Serialize archive/attach and competing membership changes.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None or Path(row["workspace"]).resolve() != workspace.resolve():
                raise ValueError("Thread must belong to the current workspace")
            existing = db.execute("SELECT thread_id FROM thread_runs WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                if existing[0] != thread_id:
                    raise ValueError("Run already belongs to another thread")
                return  # Restoring an existing Run is allowed even when archived.
            if row["status"] != "active":
                raise ValueError("Cannot add a new Run to an archived thread")
            db.execute("INSERT INTO thread_runs VALUES (?, ?)", (run_id, thread_id))

    def runs(self, thread_id: str) -> list[str]:
        self.get(thread_id)
        with self._connection() as db:
            return [row[0] for row in db.execute(
                "SELECT run_id FROM thread_runs WHERE thread_id=? ORDER BY rowid", (thread_id,))]
