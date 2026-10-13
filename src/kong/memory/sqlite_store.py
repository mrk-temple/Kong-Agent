"""Memory-owned SQLite transactions and ordered, checksummed migrations."""
from contextlib import contextmanager
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3

from kong.lexical import lexical_terms
from kong.memory.domain import canonical, utc_now
from kong.memory.errors import SchemaError, ServiceClosed


APPLICATION_ID = 0x4B474D31  # KGM1; refuse to adopt a Runtime or arbitrary DB.
MIGRATIONS = (
    (
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)",
        "CREATE TABLE metadata(key TEXT PRIMARY KEY, value BLOB NOT NULL)",
        """CREATE TABLE profiles(profile_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, identity_version INTEGER NOT NULL DEFAULT 0,
            memory_revision INTEGER NOT NULL DEFAULT 0, deletion_epoch INTEGER NOT NULL DEFAULT 0, UNIQUE(profile_id, owner_id))""",
        """CREATE TABLE identity_versions(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), version INTEGER NOT NULL,
            content TEXT NOT NULL, approved_by TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(profile_id, version))""",
        """CREATE TABLE memory_records(profile_id TEXT NOT NULL, record_id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision>=1), state TEXT NOT NULL CHECK(state IN ('active','archived','quarantined','deleted')),
            kind TEXT NOT NULL CHECK(kind IN ('semantic','episodic')), category TEXT NOT NULL, subject_id TEXT NOT NULL,
            subject_kind TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, assertion_mode TEXT NOT NULL,
            sensitivity TEXT NOT NULL, occurred_at TEXT, expires_at TEXT, head_version INTEGER,
            PRIMARY KEY(profile_id, record_id), FOREIGN KEY(profile_id, owner_id) REFERENCES profiles(profile_id, owner_id),
            FOREIGN KEY(profile_id, record_id, head_version) REFERENCES memory_versions(profile_id, record_id, version) DEFERRABLE INITIALLY DEFERRED,
            CHECK((state='deleted')=(head_version IS NULL)))""",
        """CREATE TABLE memory_versions(profile_id TEXT NOT NULL, record_id TEXT NOT NULL, version INTEGER NOT NULL,
            content TEXT, evidence TEXT NOT NULL, created_at TEXT NOT NULL, supersedes INTEGER, purged_at TEXT,
            PRIMARY KEY(profile_id, record_id, version), FOREIGN KEY(profile_id, record_id) REFERENCES memory_records(profile_id, record_id),
            CHECK((content IS NULL)=(purged_at IS NOT NULL)))""",
        """CREATE TABLE sources(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), source_id TEXT NOT NULL,
            origin TEXT NOT NULL, speaker TEXT NOT NULL, excerpt TEXT, locator TEXT, digest TEXT NOT NULL, purged_at TEXT,
            PRIMARY KEY(profile_id, source_id))""",
        """CREATE TABLE memory_sources(profile_id TEXT NOT NULL, record_id TEXT NOT NULL, version INTEGER NOT NULL, source_id TEXT NOT NULL,
            PRIMARY KEY(profile_id, record_id, version, source_id),
            FOREIGN KEY(profile_id, record_id, version) REFERENCES memory_versions(profile_id, record_id, version),
            FOREIGN KEY(profile_id, source_id) REFERENCES sources(profile_id, source_id))""",
        """CREATE TABLE candidates(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), candidate_id TEXT NOT NULL,
            revision INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','validated','consolidated','rejected','cancelled')),
            draft TEXT, source_id TEXT NOT NULL, record_id TEXT, PRIMARY KEY(profile_id, candidate_id),
            FOREIGN KEY(profile_id, source_id) REFERENCES sources(profile_id, source_id),
            FOREIGN KEY(profile_id, record_id) REFERENCES memory_records(profile_id, record_id))""",
        """CREATE TABLE command_receipts(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), command_id TEXT NOT NULL,
            digest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(profile_id, command_id))""",
        """CREATE TABLE audit_entries(sequence INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
            command_id TEXT NOT NULL, actor TEXT NOT NULL, input_id TEXT, operation TEXT NOT NULL, object_id TEXT,
            from_state TEXT, to_state TEXT, created_at TEXT NOT NULL)""",
        "CREATE INDEX records_filter ON memory_records(profile_id,state,scope_kind,scope_id)",
        "CREATE INDEX source_records ON memory_sources(profile_id,source_id)",
        "CREATE VIRTUAL TABLE memory_fts USING fts5(profile_id UNINDEXED, record_id UNINDEXED, terms)",
    ),
    (
        """CREATE TABLE derivations(profile_id TEXT NOT NULL, parent_id TEXT NOT NULL, parent_version INTEGER NOT NULL,
            child_id TEXT NOT NULL, PRIMARY KEY(profile_id,parent_id,parent_version,child_id),
            FOREIGN KEY(profile_id,parent_id,parent_version) REFERENCES memory_versions(profile_id,record_id,version),
            FOREIGN KEY(profile_id,child_id) REFERENCES memory_records(profile_id,record_id))""",
        """CREATE TABLE source_suppressions(profile_id TEXT NOT NULL, source_id TEXT NOT NULL, deletion_epoch INTEGER NOT NULL,
            command_id TEXT NOT NULL, PRIMARY KEY(profile_id,source_id),
            FOREIGN KEY(profile_id,source_id) REFERENCES sources(profile_id,source_id))""",
        """CREATE TABLE deletion_receipts(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), command_id TEXT NOT NULL,
            record_id TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(profile_id,command_id))""",
        """CREATE TABLE memory_outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
            operation TEXT NOT NULL, object_id TEXT, memory_revision INTEGER NOT NULL, deletion_epoch INTEGER NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE record_collections(profile_id TEXT NOT NULL, record_id TEXT NOT NULL, collection_id TEXT NOT NULL,
            PRIMARY KEY(profile_id,record_id,collection_id), FOREIGN KEY(profile_id,record_id) REFERENCES memory_records(profile_id,record_id))""",
    ),
    (
        "ALTER TABLE profiles ADD COLUMN activation_revision INTEGER NOT NULL DEFAULT 0",
        "INSERT INTO metadata VALUES('store_id',lower(hex(randomblob(16))))",
        """CREATE TABLE bottles(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), bottle_id TEXT NOT NULL,
            title TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('persona','memory','relationship')),
            scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
            revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(profile_id,bottle_id))""",
        "CREATE INDEX collection_records ON record_collections(profile_id,collection_id,record_id)",
        """CREATE TRIGGER collection_owner_insert BEFORE INSERT ON record_collections
            WHEN NOT EXISTS(SELECT 1 FROM bottles WHERE profile_id=NEW.profile_id AND bottle_id=NEW.collection_id)
            BEGIN SELECT RAISE(ABORT,'Unknown Bottle'); END""",
        """CREATE TRIGGER collection_owner_update BEFORE UPDATE ON record_collections
            WHEN NOT EXISTS(SELECT 1 FROM bottles WHERE profile_id=NEW.profile_id AND bottle_id=NEW.collection_id)
            BEGIN SELECT RAISE(ABORT,'Unknown Bottle'); END""",
    ),
    (
        """CREATE TABLE interaction_inbox(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), event_id TEXT NOT NULL,
            digest TEXT NOT NULL, capture TEXT NOT NULL, status TEXT NOT NULL, payload TEXT,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, PRIMARY KEY(profile_id,event_id))""",
        """CREATE TABLE import_origins(profile_id TEXT NOT NULL, origin_profile TEXT NOT NULL, origin_record TEXT NOT NULL,
            digest TEXT NOT NULL, local_record TEXT NOT NULL, PRIMARY KEY(profile_id,origin_profile,origin_record),
            FOREIGN KEY(profile_id,local_record) REFERENCES memory_records(profile_id,record_id))""",
    ),
)
MIGRATIONS += ((
    """CREATE TABLE formation_jobs(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), job_id TEXT NOT NULL,
        event_id TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, bottle_id TEXT,
        deletion_epoch INTEGER NOT NULL, activation_revision INTEGER NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, lease_until TEXT, lease_token TEXT,
        error_type TEXT, PRIMARY KEY(profile_id,job_id), UNIQUE(profile_id,event_id,scope_kind,scope_id),
        FOREIGN KEY(profile_id,event_id) REFERENCES interaction_inbox(profile_id,event_id))""",
    "CREATE INDEX formation_due ON formation_jobs(profile_id,status,available_at)",
    """CREATE TABLE formation_candidates(profile_id TEXT NOT NULL, candidate_id TEXT NOT NULL, job_id TEXT NOT NULL,
        candidate_type TEXT NOT NULL, logical_key TEXT NOT NULL, importance REAL NOT NULL, episode TEXT,
        expires_at TEXT NOT NULL, PRIMARY KEY(profile_id,candidate_id),
        FOREIGN KEY(profile_id,candidate_id) REFERENCES candidates(profile_id,candidate_id),
        FOREIGN KEY(profile_id,job_id) REFERENCES formation_jobs(profile_id,job_id))""",
    """CREATE TABLE formation_traits(profile_id TEXT NOT NULL, record_id TEXT NOT NULL, logical_key TEXT NOT NULL,
        PRIMARY KEY(profile_id,record_id), FOREIGN KEY(profile_id,record_id) REFERENCES memory_records(profile_id,record_id))""",
    "CREATE INDEX formation_keys ON formation_traits(profile_id,logical_key)",
    """CREATE TABLE episodic_details(profile_id TEXT NOT NULL, record_id TEXT NOT NULL, version INTEGER NOT NULL,
        payload TEXT, PRIMARY KEY(profile_id,record_id,version),
        FOREIGN KEY(profile_id,record_id,version) REFERENCES memory_versions(profile_id,record_id,version))""",
    """CREATE TABLE memory_pins(profile_id TEXT NOT NULL, record_id TEXT NOT NULL, bottle_id TEXT NOT NULL,
        approved_by TEXT NOT NULL, PRIMARY KEY(profile_id,record_id,bottle_id),
        FOREIGN KEY(profile_id,record_id,bottle_id) REFERENCES record_collections(profile_id,record_id,collection_id)
        ON DELETE CASCADE)""",
),)
MIGRATIONS += ((
    "ALTER TABLE profiles ADD COLUMN relationship_version INTEGER NOT NULL DEFAULT 0",
    """CREATE TABLE relationship_versions(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), version INTEGER NOT NULL,
        content TEXT NOT NULL, approved_by TEXT NOT NULL, created_at TEXT NOT NULL, proposal_id TEXT,
        PRIMARY KEY(profile_id,version))""",
    """CREATE TABLE evolution_proposals(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), proposal_id TEXT NOT NULL,
        target TEXT NOT NULL CHECK(target IN ('identity','relationship')), state TEXT NOT NULL,
        revision INTEGER NOT NULL, base_version INTEGER NOT NULL, fingerprint TEXT NOT NULL, content TEXT,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, PRIMARY KEY(profile_id,proposal_id), UNIQUE(profile_id,fingerprint))""",
    """CREATE TABLE proposal_inputs(profile_id TEXT NOT NULL, proposal_id TEXT NOT NULL, record_id TEXT NOT NULL,
        version INTEGER NOT NULL, PRIMARY KEY(profile_id,proposal_id,record_id,version),
        FOREIGN KEY(profile_id,proposal_id) REFERENCES evolution_proposals(profile_id,proposal_id),
        FOREIGN KEY(profile_id,record_id,version) REFERENCES memory_versions(profile_id,record_id,version))""",
    """CREATE TABLE proposal_candidates(profile_id TEXT NOT NULL, proposal_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
        PRIMARY KEY(profile_id,proposal_id), FOREIGN KEY(profile_id,proposal_id) REFERENCES evolution_proposals(profile_id,proposal_id),
        FOREIGN KEY(profile_id,candidate_id) REFERENCES candidates(profile_id,candidate_id))""",
    """CREATE TABLE proposal_sources(profile_id TEXT NOT NULL, proposal_id TEXT NOT NULL, source_id TEXT NOT NULL,
        PRIMARY KEY(profile_id,proposal_id,source_id), FOREIGN KEY(profile_id,proposal_id) REFERENCES evolution_proposals(profile_id,proposal_id),
        FOREIGN KEY(profile_id,source_id) REFERENCES sources(profile_id,source_id))""",
    """CREATE TABLE reflection_runs(profile_id TEXT NOT NULL REFERENCES profiles(profile_id), run_id TEXT NOT NULL,
        payload TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, PRIMARY KEY(profile_id,run_id))""",
    """CREATE TABLE reflection_inputs(profile_id TEXT NOT NULL, run_id TEXT NOT NULL, record_id TEXT NOT NULL,
        version INTEGER NOT NULL, PRIMARY KEY(profile_id,run_id,record_id,version),
        FOREIGN KEY(profile_id,run_id) REFERENCES reflection_runs(profile_id,run_id),
        FOREIGN KEY(profile_id,record_id,version) REFERENCES memory_versions(profile_id,record_id,version))""",
    """CREATE TABLE episode_links(profile_id TEXT NOT NULL, left_id TEXT NOT NULL, left_version INTEGER NOT NULL,
        right_id TEXT NOT NULL, right_version INTEGER NOT NULL, kind TEXT NOT NULL, run_id TEXT NOT NULL,
        PRIMARY KEY(profile_id,left_id,left_version,right_id,right_version,kind),
        FOREIGN KEY(profile_id,left_id,left_version) REFERENCES memory_versions(profile_id,record_id,version),
        FOREIGN KEY(profile_id,right_id,right_version) REFERENCES memory_versions(profile_id,record_id,version),
        FOREIGN KEY(profile_id,run_id) REFERENCES reflection_runs(profile_id,run_id))""",
),)
SCHEMA_VERSION = len(MIGRATIONS)


def migration_checksum(statements: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(statements).encode()).hexdigest()


class SQLiteStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._validate_database()
            self._migrate()
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA secure_delete=ON")
            if sqlite3.sqlite_version_info >= (3, 42, 0):
                self._db.execute("INSERT INTO memory_fts(memory_fts, rank) VALUES ('secure-delete', 1)")
            self._key = bytes(self._db.execute("SELECT value FROM metadata WHERE key='digest_key'").fetchone()[0])
        except BaseException:
            self.close()
            raise

    @property
    def db(self):
        if self._db is None:
            raise ServiceClosed("Memory service is closed")
        return self._db

    def _validate_database(self):
        db = self.db
        app = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if app not in (0, APPLICATION_ID) or (app == 0 and (tables or version)):
            raise SchemaError("Not a Kong Memory Core database; legacy import must be explicit")
        if version < 0 or version > SCHEMA_VERSION or (app == APPLICATION_ID and version == 0):
            raise SchemaError("Unsupported Memory schema version")
        if version:
            rows = db.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall()
            expected = [(i, migration_checksum(MIGRATIONS[i - 1])) for i in range(1, version + 1)]
            if [tuple(row) for row in rows] != expected:
                raise SchemaError("Migration manifest does not match this schema")

    def _migrate(self):
        # Do not use executescript: it can implicitly commit an active transaction.
        with self.transaction() as db:
            self._validate_database()  # Another opener may have migrated while we waited.
            version = db.execute("PRAGMA user_version").fetchone()[0]
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            for index in range(version, SCHEMA_VERSION):
                for statement in MIGRATIONS[index]:
                    db.execute(statement)
                db.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (index + 1, migration_checksum(MIGRATIONS[index]), utc_now()))
                db.execute(f"PRAGMA user_version={index + 1}")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('digest_key',?)", (secrets.token_bytes(32),))
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise SchemaError("Foreign key check failed")

    @contextmanager
    def transaction(self, *, write=True):
        db = self.db
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def digest(self, value) -> str:
        return hmac.new(self._key, canonical(value).encode(), hashlib.sha256).hexdigest()

    def index(self, db, profile_id: str, record_id: str, content: str | None):
        db.execute("DELETE FROM memory_fts WHERE profile_id=? AND record_id=?", (profile_id, record_id))
        if content is not None:
            db.execute("INSERT INTO memory_fts VALUES(?,?,?)", (profile_id, record_id, " ".join(lexical_terms(content))))

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
