"""
Aurelion Refactor Engine v7 - Database Layer
SQLite-backed persistence with full JSON fallback for compatibility.

All v1–v6 JSON files remain readable. The DB layer is purely additive:
  - New writes go to SQLite (faster queries, no file-per-run overhead)
  - If SQLite fails at any point, falls back to JSON transparently
  - Migration tool converts existing JSON history to SQLite on first run

Schema:
  runs       — execution history (mirrors history_manager run records)
  file_hashes — incremental engine state (mirrors state_manager)
  profiles    — named profiles (mirrors profiles/*.toml)
  ai_prompts  — AI planner prompt + response history
  kv          — general key-value store

NEW IN v7:
  - Database class: unified interface for all persistence
  - Database.execute() / fetchall() / fetchone() helpers
  - Database.migrate_from_json() — one-time import of existing data
  - Integrated into HistoryManager, StateManager on first use
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

DB_VERSION = 7
DB_PATH    = Path("history") / "aurelion.db"

# ── Schema DDL ─────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT    PRIMARY KEY,
    version      TEXT    NOT NULL DEFAULT '7.0.1',
    timestamp    TEXT    NOT NULL,
    command      TEXT    NOT NULL,
    plan_file    TEXT,
    plan_name    TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'running',
    duration     REAL    NOT NULL DEFAULT 0,
    group_filter TEXT,
    tag_filter   TEXT,
    payload      TEXT    NOT NULL DEFAULT '{}'  -- full JSON blob
);
CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status    ON runs(status);

CREATE TABLE IF NOT EXISTS file_hashes (
    path         TEXT PRIMARY KEY,
    sha256       TEXT NOT NULL,
    mtime        REAL NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    name         TEXT PRIMARY KEY,
    description  TEXT,
    payload      TEXT NOT NULL DEFAULT '{}'  -- full TOML/JSON content
);

CREATE TABLE IF NOT EXISTS ai_prompts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,
    prompt       TEXT    NOT NULL,
    response     TEXT,
    plan_name    TEXT,
    rules_count  INTEGER DEFAULT 0,
    confidence   REAL    DEFAULT 0.0,
    validation_score REAL DEFAULT 0.0,
    status       TEXT    DEFAULT 'success'
);

CREATE TABLE IF NOT EXISTS kv (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""


class Database:
    """
    Thread-safe SQLite wrapper with automatic JSON fallback.

    Usage:
        db = Database()
        db.open()
        with db.connection() as conn:
            conn.execute("INSERT INTO kv VALUES (?,?,?)", ...)
        db.close()

    Or use the context manager:
        with Database() as db:
            rows = db.fetchall("SELECT * FROM runs ORDER BY timestamp DESC LIMIT ?", (10,))
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        logger=None,
    ):
        self._path    = path or DB_PATH
        self._logger  = logger
        self._lock    = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._available = False

    # ──────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────

    def open(self) -> bool:
        """
        Open (or create) the SQLite database. Returns True on success.
        Silently falls back to unavailable mode on failure.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                timeout=10.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._apply_schema()
            self._available = True
            return True
        except Exception as e:
            if self._logger:
                self._logger.warning(f"  [DB] SQLite unavailable ({e}); using JSON fallback")
            self._available = False
            return False

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> "Database":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    @property
    def available(self) -> bool:
        return self._available and self._conn is not None

    # ──────────────────────────────────────────────────────────────
    # Query helpers
    # ──────────────────────────────────────────────────────────────

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield the connection with implicit commit/rollback."""
        if not self.available:
            raise RuntimeError("Database not available")
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: Tuple = ()) -> sqlite3.Cursor:
        """Execute a write statement."""
        with self.connection() as conn:
            return conn.execute(sql, params)

    def executemany(self, sql: str, seq: List[Tuple]) -> None:
        """Execute a statement for multiple rows."""
        with self.connection() as conn:
            conn.executemany(sql, seq)

    def fetchall(self, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        """Execute a SELECT and return all rows as dicts."""
        if not self.available:
            return []
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def fetchone(self, sql: str, params: Tuple = ()) -> Optional[Dict[str, Any]]:
        """Execute a SELECT and return the first row as a dict."""
        if not self.available:
            return None
        with self._lock:
            cursor = self._conn.execute(sql, params)
            row = cursor.fetchone()
            return dict(row) if row else None

    # ──────────────────────────────────────────────────────────────
    # Runs table
    # ──────────────────────────────────────────────────────────────

    def insert_run(self, record: Dict[str, Any]) -> bool:
        """Insert a run record. Returns True on success."""
        if not self.available:
            return False
        try:
            self.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, version, timestamp, command, plan_file, plan_name,
                    dry_run, status, duration, group_filter, tag_filter, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["run_id"],
                    record.get("version", "7.0.1"),
                    record.get("timestamp", ""),
                    record.get("command", ""),
                    record.get("plan_file"),
                    record.get("plan_name"),
                    1 if record.get("dry_run") else 0,
                    record.get("status", "unknown"),
                    record.get("duration", 0.0),
                    record.get("group"),
                    record.get("tag"),
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            return True
        except Exception as e:
            if self._logger:
                self._logger.warning(f"  [DB] insert_run failed: {e}")
            return False

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return run index entries (lightweight) sorted newest-first."""
        rows = self.fetchall(
            "SELECT run_id, timestamp, command, plan_name, status, duration, dry_run "
            "FROM runs ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        # Normalise: add totals placeholder for compatibility
        for r in rows:
            r["dry_run"] = bool(r.get("dry_run"))
            r.setdefault("totals", {})
        return rows

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return the full run payload for a given run_id."""
        row = self.fetchone("SELECT payload FROM runs WHERE run_id = ?", (run_id,))
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except Exception:
            return row

    def delete_run(self, run_id: str) -> bool:
        if not self.available:
            return False
        try:
            self.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            return True
        except Exception:
            return False

    def clear_runs(self) -> int:
        if not self.available:
            return 0
        rows = self.fetchall("SELECT COUNT(*) as c FROM runs")
        count = rows[0]["c"] if rows else 0
        self.execute("DELETE FROM runs")
        return count

    # ──────────────────────────────────────────────────────────────
    # File hashes table
    # ──────────────────────────────────────────────────────────────

    def upsert_hash(self, path: str, sha256: str, mtime: float) -> None:
        self.execute(
            "INSERT OR REPLACE INTO file_hashes (path, sha256, mtime, last_seen) VALUES (?,?,?,?)",
            (path, sha256, mtime, datetime.now(tz=timezone.utc).isoformat()),
        )

    def get_hash(self, path: str) -> Optional[Dict[str, Any]]:
        return self.fetchone("SELECT * FROM file_hashes WHERE path = ?", (path,))

    def delete_hash(self, path: str) -> None:
        self.execute("DELETE FROM file_hashes WHERE path = ?", (path,))

    def clear_hashes(self) -> int:
        rows = self.fetchall("SELECT COUNT(*) as c FROM file_hashes")
        count = rows[0]["c"] if rows else 0
        self.execute("DELETE FROM file_hashes")
        return count

    def hash_count(self) -> int:
        rows = self.fetchall("SELECT COUNT(*) as c FROM file_hashes")
        return rows[0]["c"] if rows else 0

    # ──────────────────────────────────────────────────────────────
    # AI prompts table
    # ──────────────────────────────────────────────────────────────

    def insert_ai_prompt(
        self,
        prompt: str,
        response: str = "",
        plan_name: str = "",
        rules_count: int = 0,
        confidence: float = 0.0,
        validation_score: float = 0.0,
        status: str = "success",
    ) -> int:
        """Insert AI prompt record. Returns the new row id."""
        if not self.available:
            return -1
        with self.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO ai_prompts
                   (timestamp, prompt, response, plan_name, rules_count,
                    confidence, validation_score, status)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(tz=timezone.utc).isoformat(),
                    prompt, response, plan_name, rules_count,
                    confidence, validation_score, status,
                ),
            )
            return cursor.lastrowid

    def list_ai_prompts(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.fetchall(
            "SELECT id, timestamp, plan_name, rules_count, confidence, "
            "validation_score, status, prompt FROM ai_prompts "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )

    # ──────────────────────────────────────────────────────────────
    # KV store
    # ──────────────────────────────────────────────────────────────

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?,?,?)",
            (key, json.dumps(value), datetime.now(tz=timezone.utc).isoformat()),
        )

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    def kv_delete(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key = ?", (key,))

    # ──────────────────────────────────────────────────────────────
    # Migration
    # ──────────────────────────────────────────────────────────────

    def migrate_from_json(self, history_dir: Path = Path("history")) -> Dict[str, int]:
        """
        One-time migration: import existing JSON run files into SQLite.
        Safe to call repeatedly — uses INSERT OR REPLACE.
        Returns counts of migrated items.
        """
        if not self.available:
            return {}

        counts: Dict[str, int] = {"runs": 0, "hashes": 0}
        runs_dir = history_dir / "runs"

        # Migrate run JSON files
        if runs_dir.exists():
            for json_file in sorted(runs_dir.glob("*.json")):
                try:
                    record = json.loads(json_file.read_text(encoding="utf-8"))
                    if self.insert_run(record):
                        counts["runs"] += 1
                except Exception:
                    pass

        # Migrate state.json hashes
        state_file = history_dir / "state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                for path, entry in state.get("file_hashes", {}).items():
                    self.upsert_hash(
                        path,
                        entry.get("sha256", ""),
                        entry.get("mtime", 0.0),
                    )
                    counts["hashes"] += 1
            except Exception:
                pass

        if self._logger and (counts["runs"] or counts["hashes"]):
            self._logger.info(
                f"  [DB] Migration complete: {counts['runs']} runs, "
                f"{counts['hashes']} file hashes imported."
            )

        return counts

    # ──────────────────────────────────────────────────────────────
    # CSV export
    # ──────────────────────────────────────────────────────────────

    def export_runs_csv(self, output_path: Path) -> int:
        """Export runs table to CSV. Returns row count written."""
        import csv
        rows = self.fetchall(
            "SELECT run_id, timestamp, command, plan_name, status, duration, dry_run "
            "FROM runs ORDER BY timestamp DESC"
        )
        if not rows:
            return 0
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    # ──────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────

    def _apply_schema(self) -> None:
        """Apply DDL schema and mark version."""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            # Record schema version
            existing = self._conn.execute(
                "SELECT version FROM schema_version WHERE version = ?", (DB_VERSION,)
            ).fetchone()
            if not existing:
                self._conn.execute(
                    "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?,?)",
                    (DB_VERSION, datetime.now(tz=timezone.utc).isoformat()),
                )
                self._conn.commit()


# ── Module-level convenience ───────────────────────────────────────────────────

_shared_db: Optional[Database] = None
_shared_lock = threading.Lock()


def get_db(logger=None) -> Database:
    """
    Return the process-level shared Database instance.
    Opens connection lazily on first call.
    """
    global _shared_db
    with _shared_lock:
        if _shared_db is None or not _shared_db.available:
            _shared_db = Database(logger=logger)
            _shared_db.open()
        return _shared_db
