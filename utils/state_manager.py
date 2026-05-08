"""
Aurelion Refactor Engine v5 - State Manager
Maintains persistent state between runs to enable incremental processing.

Two concerns handled here:

1. FILE HASH TRACKING (incremental engine):
   Each file gets a SHA-256 hash stored in state.json.
   On the next run with --incremental, unchanged files are skipped.
   Hash entries are updated after each successful modification.

2. GENERAL KEY-VALUE STATE:
   Arbitrary run-state that other subsystems can read/write,
   such as last-run metadata, feature flags, or counters.

Storage:
  history/state.json    ← single JSON file, all state in one place

State schema:
  {
    "version":       "5.0.0",
    "last_updated":  "2026-04-14T15:23:01",
    "file_hashes": {
      "/abs/path/to/file.py": {
        "sha256":    "abc123...",
        "mtime":     1713104580.0,
        "last_seen": "2026-04-14T15:23:01"
      }
    },
    "kv": {
      "last_run_id": "20260414-152301-abc123",
      "run_count":   42
    }
  }

NEW IN v5:
  - StateManager.is_changed(path)     — True if file hash differs from stored
  - StateManager.update_hash(path)    — store current hash for a file
  - StateManager.mark_unchanged(path) — record as-seen without processing
  - StateManager.filter_changed(paths) — returns only changed files from a list
  - StateManager.set(key, value)       — store arbitrary state
  - StateManager.get(key, default)     — retrieve state value
  - StateManager.flush()               — write state to disk
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "5.0.0"

# How many bytes to read per chunk when hashing large files
_HASH_CHUNK_SIZE = 65_536   # 64 KB


class StateManager:
    """
    Thread-safe persistent state store.
    Loads state lazily on first access, writes atomically on flush().
    """

    STATE_FILE = Path("history") / "state.json"
    HASH_CHUNK = _HASH_CHUNK_SIZE

    def __init__(self, logger=None):
        self._logger  = logger
        self._lock    = threading.RLock()
        self._dirty   = False
        self._state: Optional[Dict[str, Any]] = None   # loaded lazily

    # ──────────────────────────────────────────────────────────────
    # Incremental engine — file hash API
    # ──────────────────────────────────────────────────────────────

    def is_changed(self, path: Path) -> bool:
        """
        Return True if the file's current hash differs from the stored hash.
        Tries SQLite first for speed, falls back to JSON state.
        """
        abs_key = str(path.resolve())

        # Try DB first
        try:
            from core.db import get_db
            db = get_db()
            if db.available:
                stored = db.get_hash(abs_key)
                if stored is None:
                    return True
                try:
                    current_mtime = path.stat().st_mtime
                except OSError:
                    return True
                if stored.get("mtime") == current_mtime:
                    return False
                return self._hash_file(path) != stored.get("sha256", "")
        except Exception:
            pass

        # JSON fallback
        state    = self._load()
        abs_key  = str(path.resolve())
        stored   = state["file_hashes"].get(abs_key)

        if stored is None:
            return True   # Never seen

        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            return True   # Can't stat → assume changed

        # Fast path: mtime unchanged → file almost certainly unchanged
        if stored.get("mtime") == current_mtime:
            return False

        # Slow path: full hash comparison
        current_hash = self._hash_file(path)
        return current_hash != stored.get("sha256")

    def filter_changed(self, paths: List[Path]) -> List[Path]:
        """
        Filter a list of Paths to only those that have changed since
        the last time update_hash() was called for them.
        Returns the subset of paths that should be processed.
        """
        return [p for p in paths if self.is_changed(p)]

    def update_hash(self, path: Path) -> None:
        """
        Compute and store the current hash + mtime for a file.
        Writes to SQLite (if available) AND JSON state (for fallback).
        """
        with self._lock:
            abs_key = str(path.resolve())
            try:
                mtime = path.stat().st_mtime
                sha   = self._hash_file(path)
            except OSError:
                return

            # Write to DB
            try:
                from core.db import get_db
                db = get_db()
                if db.available:
                    db.upsert_hash(abs_key, sha, mtime)
            except Exception:
                pass

            # Also update JSON state
            state = self._load()
            state["file_hashes"][abs_key] = {
                "sha256":    sha,
                "mtime":     mtime,
                "last_seen": datetime.now(tz=timezone.utc).isoformat(),
            }
            self._dirty = True

    def update_hashes_bulk(self, paths: List[Path]) -> None:
        """Update hashes for multiple files efficiently."""
        for path in paths:
            self.update_hash(path)

    def invalidate(self, path: Path) -> None:
        """Remove a file's hash entry, forcing it to be processed next run."""
        with self._lock:
            state   = self._load()
            abs_key = str(path.resolve())
            if abs_key in state["file_hashes"]:
                del state["file_hashes"][abs_key]
                self._dirty = True

    def invalidate_all(self) -> int:
        """Clear all stored hashes. Returns count cleared."""
        with self._lock:
            state = self._load()
            count = len(state["file_hashes"])
            state["file_hashes"].clear()
            self._dirty = True
            return count

    def stats(self) -> Dict[str, int]:
        """Return hash storage statistics."""
        state = self._load()
        return {
            "tracked_files": len(state["file_hashes"]),
        }

    # ──────────────────────────────────────────────────────────────
    # General key-value store
    # ──────────────────────────────────────────────────────────────

    def set(self, key: str, value: Any) -> None:
        """Store a named value in persistent state."""
        with self._lock:
            state = self._load()
            state["kv"][key] = value
            self._dirty = True

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a named value from persistent state."""
        state = self._load()
        return state["kv"].get(key, default)

    def delete(self, key: str) -> bool:
        """Delete a key from the KV store. Returns True if it existed."""
        with self._lock:
            state = self._load()
            if key in state["kv"]:
                del state["kv"][key]
                self._dirty = True
                return True
            return False

    # ──────────────────────────────────────────────────────────────
    # Persistence
    # ──────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """
        Persist state to disk if dirty.
        Uses atomic write (temp → rename) to prevent corruption.
        """
        with self._lock:
            if not self._dirty or self._state is None:
                return
            self._state["last_updated"] = datetime.now(tz=timezone.utc).isoformat()
            self._atomic_write(self.STATE_FILE, self._state)
            self._dirty = False

    def reload(self) -> None:
        """Force reload of state from disk."""
        with self._lock:
            self._state = None

    # ──────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────

    def _load(self) -> Dict[str, Any]:
        """Lazy-load state from disk. Thread-safe via _lock."""
        if self._state is not None:
            return self._state

        with self._lock:
            if self._state is not None:   # double-checked
                return self._state

            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

            if not self.STATE_FILE.exists():
                self._state = self._blank_state()
                return self._state

            try:
                raw = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
                # Migrate older states that lack new fields
                raw.setdefault("file_hashes", {})
                raw.setdefault("kv", {})
                raw.setdefault("version", VERSION)
                self._state = raw
            except Exception as e:
                if self._logger:
                    self._logger.warning(
                        f"  [STATE] Failed to load state file ({e}); starting fresh."
                    )
                self._state = self._blank_state()

            return self._state

    def _blank_state(self) -> Dict[str, Any]:
        return {
            "version":      VERSION,
            "last_updated": datetime.now(tz=timezone.utc).isoformat(),
            "file_hashes":  {},
            "kv":           {},
        }

    def _hash_file(self, path: Path) -> str:
        """Compute SHA-256 of a file in streaming chunks."""
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(self.HASH_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            return ""
        return h.hexdigest()

    def _atomic_write(self, path: Path, data: Dict[str, Any]) -> None:
        """Write JSON atomically to path using sibling temp file."""
        import tempfile
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(dir=parent, suffix=".tmp")
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(path)
        except Exception as e:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if self._logger:
                self._logger.warning(f"  [STATE] Atomic write failed: {e}")
