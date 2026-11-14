"""SQLite project database with forward-only migrations.

Tables hold provenance (sources), logical tracks, albums (assignment never moves files),
cached analyses, immutable processing versions, approvals, restartable jobs and an audit log.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: base schema
    """
    CREATE TABLE library_roots (
        id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, added_at REAL NOT NULL);
    CREATE TABLE sources (
        id INTEGER PRIMARY KEY, root_id INTEGER REFERENCES library_roots(id),
        path TEXT NOT NULL UNIQUE, rel_path TEXT NOT NULL, size INTEGER, mtime REAL,
        sha256 TEXT, probe_json TEXT, codec TEXT, family TEXT, is_lossy INTEGER,
        sample_rate INTEGER, channels INTEGER, bits INTEGER, duration_s REAL,
        decode_status TEXT DEFAULT 'UNVERIFIED', decode_error TEXT,
        first_seen REAL, last_seen REAL, missing INTEGER DEFAULT 0);
    CREATE INDEX idx_sources_sha ON sources(sha256);
    CREATE TABLE text_files (
        id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, sha256 TEXT, encoding TEXT,
        parsed_json TEXT, first_seen REAL);
    CREATE TABLE albums (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, folder_hint TEXT,
        metadata_json TEXT DEFAULT '{}', created_at REAL);
    CREATE TABLE tracks (
        id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL UNIQUE REFERENCES sources(id),
        title TEXT, album_id INTEGER REFERENCES albums(id), track_no INTEGER,
        metadata_json TEXT DEFAULT '{}', text_id INTEGER REFERENCES text_files(id),
        text_match_confidence REAL, text_match_status TEXT DEFAULT 'NONE',
        dup_group TEXT, style_override TEXT, created_at REAL);
    CREATE TABLE analyses (
        id INTEGER PRIMARY KEY, track_id INTEGER NOT NULL REFERENCES tracks(id),
        source_sha TEXT NOT NULL, analysis_version TEXT NOT NULL, kind TEXT NOT NULL,
        result_json TEXT NOT NULL, created_at REAL,
        UNIQUE(track_id, source_sha, analysis_version, kind));
    CREATE TABLE versions (
        id INTEGER PRIMARY KEY, track_id INTEGER NOT NULL REFERENCES tracks(id),
        version_no INTEGER NOT NULL, mode TEXT NOT NULL, config_json TEXT NOT NULL,
        config_hash TEXT NOT NULL, parent_version_id INTEGER, status TEXT NOT NULL,
        output_path TEXT, output_sha256 TEXT, report_json TEXT, created_at REAL,
        UNIQUE(track_id, version_no));
    CREATE TABLE approvals (
        id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES versions(id),
        decision TEXT NOT NULL, actor TEXT, note TEXT, created_at REAL);
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, params_json TEXT NOT NULL,
        status TEXT NOT NULL, created_at REAL, updated_at REAL, error TEXT,
        idempotency_key TEXT UNIQUE);
    CREATE TABLE job_items (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
        track_id INTEGER, stage TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER DEFAULT 0,
        error TEXT, result_json TEXT, updated_at REAL, UNIQUE(job_id, track_id, stage));
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT, action TEXT NOT NULL, details_json TEXT);
    CREATE TABLE exports (
        id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT, profile TEXT, status TEXT,
        manifest_json TEXT, created_at REAL);
    """,
    # 2: album gaps / ordering metadata
    """
    ALTER TABLE tracks ADD COLUMN gap_after_s REAL DEFAULT 2.0;
    """,
]


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (v INTEGER NOT NULL)")
            row = self.conn.execute("SELECT v FROM schema_version").fetchone()
            v = row[0] if row else 0
            if not row:
                self.conn.execute("INSERT INTO schema_version (v) VALUES (0)")
            for i, sql in enumerate(MIGRATIONS[v:], start=v + 1):
                self.conn.execute("BEGIN")
                try:
                    for stmt in [s for s in sql.split(";") if s.strip()]:
                        self.conn.execute(stmt)
                    self.conn.execute("UPDATE schema_version SET v=?", (i,))
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    @property
    def schema_version(self) -> int:
        return self.conn.execute("SELECT v FROM schema_version").fetchone()[0]

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def transaction(self):
        db = self

        class _Tx:
            def __enter__(self):
                db._lock.acquire()
                db.conn.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self, et, ev, tb):
                try:
                    db.conn.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    db._lock.release()
                return False

        return _Tx()

    def audit(self, action: str, details: dict | None = None, actor: str = "local") -> None:
        self.execute("INSERT INTO audit_log (ts, actor, action, details_json) VALUES (?,?,?,?)",
                     (time.time(), actor, action, json.dumps(details or {}, ensure_ascii=False)))

    def close(self) -> None:
        with self._lock:
            self.conn.close()
