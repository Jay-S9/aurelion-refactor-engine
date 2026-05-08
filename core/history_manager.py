"""
Aurelion Refactor Engine v5 - History Manager
Tracks every plan/command execution with persistent metadata.

Storage layout:
  history/
    index.json                  ← master run index (lightweight, fast)
    runs/
      {run_id}.json             ← full run record per execution
    exports/                    ← user-exported reports land here

Run record schema:
  {
    "run_id":       "20260414-152301-abc123",
    "version":      "5.0.0",
    "timestamp":    "2026-04-14T15:23:01.234567",
    "command":      "run",
    "plan_file":    "/path/to/plan.toml",
    "plan_name":    "My Migration",
    "dry_run":      false,
    "status":       "success" | "partial" | "aborted" | "failed",
    "duration":     4.231,
    "rules": [
      {
        "name":     "rule-name",
        "type":     "replace",
        "status":   "success" | "failed" | "aborted",
        "modified": 12,
        "skipped":  3,
        "errors":   0,
        "duration": 1.05
      }
    ],
    "totals": {
      "rules_run":     6,
      "rules_ok":      5,
      "rules_failed":  1,
      "files_modified": 42,
      "files_skipped":  8,
      "errors":         2
    },
    "environment": {
      "cwd":           "/home/user/project",
      "python":        "3.12.3",
      "platform":      "linux"
    }
  }

NEW IN v5:
  - HistoryManager.start_run()  — open a new run context
  - HistoryManager.finish_run() — persist full run record
  - HistoryManager.list_runs()  — return reverse-chronological run list
  - HistoryManager.get_run()    — fetch a specific run by id
  - HistoryManager.last()       — return the most recent run
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from engines.rule_engine import RuleResult

VERSION = "5.0.0"


# ── Run record dataclass (plain dict for JSON-serializability) ─────────────────

def _empty_run_record() -> Dict[str, Any]:
    return {
        "run_id":      "",
        "version":     VERSION,
        "timestamp":   "",
        "command":     "",
        "plan_file":   None,
        "plan_name":   None,
        "dry_run":     False,
        "group":       None,
        "tag":         None,
        "status":      "running",   # updated on finish
        "duration":    0.0,
        "rules":       [],
        "totals": {
            "rules_run":      0,
            "rules_ok":       0,
            "rules_failed":   0,
            "files_modified": 0,
            "files_skipped":  0,
            "errors":         0,
        },
        "environment": {
            "cwd":      "",
            "python":   "",
            "platform": "",
        },
    }


class HistoryManager:
    """
    Persistent execution history for all Aurelion operations.

    Lifecycle:
      ctx = manager.start_run(command="run", plan_file=..., ...)
      # ... execution happens ...
      manager.finish_run(ctx, results)

    The context is a plain dict mutated in-place, then written to disk.
    """

    HISTORY_DIR  = Path("history")
    RUNS_DIR     = HISTORY_DIR / "runs"
    EXPORTS_DIR  = HISTORY_DIR / "exports"
    INDEX_FILE   = HISTORY_DIR / "index.json"

    def __init__(self, logger=None):
        self._logger = logger
        self._ensure_dirs()

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    def start_run(
        self,
        command:   str,
        plan_file: Optional[str] = None,
        plan_name: Optional[str] = None,
        dry_run:   bool = False,
        group:     Optional[str] = None,
        tag:       Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Open a new run context. Returns the mutable run record dict.
        Does NOT write to disk yet — call finish_run() when done.
        """
        run_id    = self._generate_run_id()
        now       = datetime.now(tz=timezone.utc)
        record    = _empty_run_record()

        record.update({
            "run_id":    run_id,
            "timestamp": now.isoformat(),
            "command":   command,
            "plan_file": str(plan_file) if plan_file else None,
            "plan_name": plan_name,
            "dry_run":   dry_run,
            "group":     group,
            "tag":       tag,
            "_start_mono": time.monotonic(),   # internal — stripped before save
        })
        record["environment"] = {
            "cwd":      str(Path.cwd()),
            "python":   f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": platform.system().lower(),
        }
        return record

    def finish_run(
        self,
        record:  Dict[str, Any],
        results: List[RuleResult],
        aborted: bool = False,
    ) -> Dict[str, Any]:
        """
        Populate timing, rule summaries, and totals. Persist to disk.
        Returns the final record (with internal keys stripped).
        """
        start_mono = record.pop("_start_mono", time.monotonic())
        record["duration"] = round(time.monotonic() - start_mono, 3)

        # Build per-rule entries
        rule_entries = []
        for r in results:
            status = (
                "aborted" if r.aborted
                else ("failed" if r.errors else "success")
            )
            rule_entries.append({
                "name":     r.rule_name,
                "type":     r.rule_type,
                "status":   status,
                "modified": len(r.modified),
                "skipped":  len(r.skipped),
                "errors":   len(r.errors),
                "duration": r.duration_seconds,
            })
        record["rules"] = rule_entries

        # Totals
        rules_ok     = sum(1 for r in results if r.success)
        rules_failed = sum(1 for r in results if not r.success)
        record["totals"] = {
            "rules_run":      len(results),
            "rules_ok":       rules_ok,
            "rules_failed":   rules_failed,
            "files_modified": sum(len(r.modified) for r in results),
            "files_skipped":  sum(len(r.skipped)  for r in results),
            "errors":         sum(len(r.errors)    for r in results),
        }

        # Overall status
        if aborted:
            record["status"] = "aborted"
        elif rules_failed > 0:
            record["status"] = "partial"
        elif not results:
            record["status"] = "empty"
        else:
            record["status"] = "success"

        # Persist to JSON (always — backward compat)
        self._save_run(record)
        self._update_index(record)

        # Also persist to SQLite if available
        try:
            from core.db import get_db
            db = get_db(self._logger)
            if db.available:
                db.insert_run(record)
        except Exception:
            pass

        return record

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Return the most recent `limit` run summaries.
        Tries SQLite first (faster), falls back to JSON index.
        """
        try:
            from core.db import get_db
            db = get_db(self._logger)
            if db.available:
                rows = db.list_runs(limit=limit)
                if rows:
                    return rows
        except Exception:
            pass
        # JSON fallback
        index = self._load_index()
        runs  = index.get("runs", [])
        return list(reversed(runs))[:limit]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the full run record. Tries SQLite first, then JSON file."""
        try:
            from core.db import get_db
            db = get_db(self._logger)
            if db.available:
                record = db.get_run(run_id)
                if record:
                    return record
        except Exception:
            pass
        # JSON fallback
        run_file = self.RUNS_DIR / f"{run_id}.json"
        if not run_file.exists():
            return None
        try:
            return json.loads(run_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def last(self) -> Optional[Dict[str, Any]]:
        """Return the most recent full run record."""
        runs = self.list_runs(limit=1)
        if not runs:
            return None
        return self.get_run(runs[0]["run_id"])

    def delete_run(self, run_id: str) -> bool:
        """Delete a specific run record. Returns True if deleted."""
        run_file = self.RUNS_DIR / f"{run_id}.json"
        if run_file.exists():
            run_file.unlink()
            self._rebuild_index()
            return True
        return False

    def clear_history(self) -> int:
        """Delete all run records. Returns count deleted."""
        count = 0
        for f in self.RUNS_DIR.glob("*.json"):
            f.unlink()
            count += 1
        self._save_index({"runs": []})
        return count

    # ──────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        self.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    def _generate_run_id(self) -> str:
        """Generate a sortable, unique run ID: YYYYMMDD-HHMMSS-<6hex>"""
        now    = datetime.now()
        suffix = uuid.uuid4().hex[:6]
        return now.strftime("%Y%m%d-%H%M%S") + f"-{suffix}"

    def _save_run(self, record: Dict[str, Any]) -> None:
        run_file = self.RUNS_DIR / f"{record['run_id']}.json"
        try:
            run_file.write_text(
                json.dumps(record, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            if self._logger:
                self._logger.warning(f"  [HISTORY] Failed to save run: {e}")

    def _update_index(self, record: Dict[str, Any]) -> None:
        """Append a lightweight summary entry to the index."""
        index = self._load_index()
        runs  = index.get("runs", [])

        # Lightweight index entry — no per-file detail
        entry = {
            "run_id":    record["run_id"],
            "timestamp": record["timestamp"],
            "command":   record["command"],
            "plan_name": record.get("plan_name"),
            "status":    record["status"],
            "duration":  record["duration"],
            "dry_run":   record.get("dry_run", False),
            "totals":    record["totals"],
        }

        # Dedup by run_id (shouldn't happen, but be safe)
        runs = [r for r in runs if r["run_id"] != record["run_id"]]
        runs.append(entry)

        index["runs"] = runs
        self._save_index(index)

    def _load_index(self) -> Dict[str, Any]:
        if not self.INDEX_FILE.exists():
            return {"runs": []}
        try:
            return json.loads(self.INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"runs": []}

    def _save_index(self, index: Dict[str, Any]) -> None:
        try:
            self.INDEX_FILE.write_text(
                json.dumps(index, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            if self._logger:
                self._logger.warning(f"  [HISTORY] Failed to save index: {e}")

    def _rebuild_index(self) -> None:
        """Rebuild index from all run files on disk (used after deletion)."""
        runs = []
        for f in sorted(self.RUNS_DIR.glob("*.json")):
            try:
                record = json.loads(f.read_text(encoding="utf-8"))
                runs.append({
                    "run_id":    record["run_id"],
                    "timestamp": record["timestamp"],
                    "command":   record["command"],
                    "plan_name": record.get("plan_name"),
                    "status":    record["status"],
                    "duration":  record["duration"],
                    "dry_run":   record.get("dry_run", False),
                    "totals":    record.get("totals", {}),
                })
            except Exception:
                continue
        self._save_index({"runs": runs})
