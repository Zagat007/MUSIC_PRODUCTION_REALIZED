"""Resumable state of blind tests: one SQLite file, the whole test state saved after every step."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from pathlib import Path

from srm.versus.core import DEFAULT_SETTINGS, NOT_STARTED


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, group_key TEXT NOT NULL, song TEXT, "
                "folder TEXT, status TEXT, winner TEXT, confidence INTEGER, state_json TEXT NOT NULL, "
                "archived INTEGER DEFAULT 0, created_at REAL, updated_at REAL)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(group_key, archived)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")

    # ---- settings
    def settings(self) -> dict:
        out = copy.deepcopy(DEFAULT_SETTINGS)
        with self._lock:
            row = self.conn.execute("SELECT value_json FROM settings WHERE key='versus'").fetchone()
        if row:
            saved = json.loads(row["value_json"])
            out["weights"].update(saved.get("weights", {}))
            out.update({k: v for k, v in saved.items() if k != "weights" and k in out})
        return out

    def save_settings(self, settings: dict) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO settings (key, value_json) VALUES ('versus', ?) "
                              "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                              (json.dumps(settings, ensure_ascii=False),))

    def get_value(self, key: str, default=None):
        with self._lock:
            row = self.conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_value(self, key: str, value) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO settings (key, value_json) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                              (key, json.dumps(value, ensure_ascii=False)))

    # ---- sessions
    def create(self, state: dict) -> int:
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute("UPDATE sessions SET archived=1 WHERE group_key=? AND archived=0",
                              (state["group"]["key"],))
            cur = self.conn.execute(
                "INSERT INTO sessions (group_key, song, folder, status, state_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (state["group"]["key"], state["group"]["name"], state["group"]["folder"],
                                           NOT_STARTED, json.dumps(state, ensure_ascii=False), now, now))
            return cur.lastrowid

    def save(self, session_id: int, state: dict, *, status: str, winner: str | None = None,
             confidence: int | None = None) -> None:
        """Called after every comparison, rating and decision: closing the program loses nothing."""
        with self._lock, self.conn:
            self.conn.execute("UPDATE sessions SET state_json=?, status=?, winner=?, confidence=?, updated_at=? "
                              "WHERE id=?", (json.dumps(state, ensure_ascii=False), status, winner, confidence,
                                             time.time(), session_id))

    def load(self, session_id: int) -> dict:
        with self._lock:
            row = self.conn.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise KeyError(session_id)
        return json.loads(row["state_json"])

    def active(self, group_key: str) -> dict | None:
        """The current (not restarted) test of a group, as a summary row."""
        with self._lock:
            row = self.conn.execute(
                "SELECT id, song, folder, status, winner, confidence, created_at, updated_at FROM sessions "
                "WHERE group_key=? AND archived=0 ORDER BY id DESC LIMIT 1", (group_key,)).fetchone()
        return dict(row) if row else None

    def history(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, group_key, song, folder, status, winner, confidence, archived, created_at, updated_at "
                "FROM sessions ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
