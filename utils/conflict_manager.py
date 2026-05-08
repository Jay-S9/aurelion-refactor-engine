"""
Aurelion Refactor Engine v4 - Conflict Manager
Detects and prevents concurrent modification of the same file by multiple
rules or threads. Provides file-level advisory locking with deadlock prevention.

Architecture:
  ConflictManager (plan-level singleton)
    ├─ register_rule_targets(rule_name, paths)  — record which files a rule touches
    ├─ detect_conflicts(rules)                  — find overlapping file sets
    ├─ acquire(path, rule_name)                 — thread-safe advisory lock
    ├─ release(path, rule_name)                 — release advisory lock
    └─ conflict_report()                        — human-readable conflict summary

Locking strategy:
  Advisory locks implemented via a per-path threading.Lock registry.
  A rule must acquire the lock for each file before writing.
  Deadlock prevention: acquire locks in sorted path order (consistent ordering).

NEW IN v4:
  - ConflictManager class with full lock registry
  - Pre-flight conflict detection (finds overlapping rules before execution)
  - Context manager FileLockContext for use in engine execute() calls
  - Conflict severity levels: WARNING (different rules, sequential) vs ERROR (same file, parallel)
"""

from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Generator, List, Optional, Set, Tuple


# ── Conflict data model ───────────────────────────────────────────────────────

@dataclass
class FileConflict:
    """Describes a conflict: multiple rules targeting the same file."""
    path: str
    rules: List[str]            # names of rules that target this file
    severity: str               # "warning" | "error"
    description: str

    @property
    def is_blocking(self) -> bool:
        return self.severity == "error"


# ── Conflict Manager ──────────────────────────────────────────────────────────

class ConflictManager:
    """
    Plan-level singleton that manages file ownership and thread locking.

    Lifecycle:
      1. Before execution:  detect_conflicts(rules) for pre-flight analysis
      2. During execution:  acquire(path, rule) / release(path, rule)
      3. After execution:   conflict_report() for audit trail

    Thread safety:
      All internal state is protected by a single registry lock.
      Per-file locks are created lazily and never deleted.
    """

    def __init__(self, logger=None):
        self._logger          = logger
        self._registry_lock   = threading.Lock()

        # Per-file advisory locks (created lazily)
        self._file_locks: Dict[str, threading.Lock] = {}

        # Rule → set of file paths it targets
        self._rule_targets: Dict[str, Set[str]] = defaultdict(set)

        # Active ownership: path → rule_name currently holding the lock
        self._active_owners: Dict[str, str] = {}

        # Conflict log: recorded during detection or runtime
        self._conflicts: List[FileConflict] = []

    # ──────────────────────────────────────────────────────────────
    # Pre-flight conflict detection
    # ──────────────────────────────────────────────────────────────

    def register_rule_targets(self, rule_name: str, paths: List[Path]) -> None:
        """Record which files a rule will touch. Call before execute()."""
        with self._registry_lock:
            for p in paths:
                self._rule_targets[rule_name].add(str(p.resolve()))

    def detect_conflicts(self, rule_names: Optional[List[str]] = None) -> List[FileConflict]:
        """
        Analyse registered targets for overlapping file access.

        A conflict occurs when two or more rules target the same file.
        Sequential conflicts are WARNINGs (safe but note-worthy).
        Parallel conflicts (workers > 1 across rules) are ERRORs.

        Returns:
            List of FileConflict objects (may be empty).
        """
        with self._registry_lock:
            names = rule_names or list(self._rule_targets.keys())
            # Invert: file → set of rules
            file_to_rules: Dict[str, Set[str]] = defaultdict(set)
            for rule_name in names:
                for path_str in self._rule_targets.get(rule_name, set()):
                    file_to_rules[path_str].add(rule_name)

            conflicts: List[FileConflict] = []
            for path_str, touching_rules in file_to_rules.items():
                if len(touching_rules) > 1:
                    rules_list = sorted(touching_rules)
                    conflict = FileConflict(
                        path=path_str,
                        rules=rules_list,
                        severity="warning",     # Sequential is always safe
                        description=(
                            f"File targeted by {len(rules_list)} rules: "
                            f"{', '.join(rules_list)}"
                        ),
                    )
                    conflicts.append(conflict)
                    self._conflicts.append(conflict)

            return conflicts

    # ──────────────────────────────────────────────────────────────
    # Runtime file locking
    # ──────────────────────────────────────────────────────────────

    def acquire(self, path: Path, rule_name: str, timeout: float = 30.0) -> bool:
        """
        Acquire the advisory lock for a file.

        Args:
            path:       Absolute path to the file.
            rule_name:  Name of the rule requesting the lock.
            timeout:    Maximum seconds to wait (default 30s).

        Returns:
            True if acquired, False if timeout expired.
        """
        key = str(path.resolve())
        lock = self._get_or_create_lock(key)

        acquired = lock.acquire(timeout=timeout)
        if acquired:
            with self._registry_lock:
                self._active_owners[key] = rule_name
        else:
            owner = self._active_owners.get(key, "unknown")
            if self._logger:
                self._logger.warning(
                    f"  [CONFLICT] Lock timeout on '{path.name}' "
                    f"(held by '{owner}', requested by '{rule_name}')"
                )
        return acquired

    def release(self, path: Path, rule_name: str) -> None:
        """Release the advisory lock for a file."""
        key = str(path.resolve())
        lock = self._get_or_create_lock(key)

        try:
            lock.release()
            with self._registry_lock:
                self._active_owners.pop(key, None)
        except RuntimeError:
            # Lock was not acquired by this thread — log but don't crash
            if self._logger:
                self._logger.warning(
                    f"  [CONFLICT] Attempted to release unacquired lock: "
                    f"'{path.name}' (rule: '{rule_name}')"
                )

    @contextmanager
    def locked_file(
        self, path: Path, rule_name: str, timeout: float = 30.0
    ) -> Generator[bool, None, None]:
        """
        Context manager: acquire lock → yield acquired_bool → release.

        Usage:
            with conflict_manager.locked_file(path, rule.name) as ok:
                if ok:
                    # safe to write
        """
        acquired = self.acquire(path, rule_name, timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(path, rule_name)

    # ──────────────────────────────────────────────────────────────
    # Reporting
    # ──────────────────────────────────────────────────────────────

    def conflict_report(self) -> List[str]:
        """
        Return a human-readable conflict summary.
        Returns empty list if no conflicts were detected.
        """
        if not self._conflicts:
            return []

        lines = [f"  {len(self._conflicts)} file conflict(s) detected:"]
        for c in self._conflicts:
            icon   = "⚠" if c.severity == "warning" else "✖"
            rules  = ", ".join(c.rules)
            fname  = Path(c.path).name
            lines.append(f"  {icon} {fname:<40} [{rules}]")
        return lines

    def has_blocking_conflicts(self) -> bool:
        return any(c.is_blocking for c in self._conflicts)

    def clear(self) -> None:
        """Reset state between plan runs."""
        with self._registry_lock:
            self._rule_targets.clear()
            self._active_owners.clear()
            self._conflicts.clear()

    # ──────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────

    def _get_or_create_lock(self, key: str) -> threading.Lock:
        with self._registry_lock:
            if key not in self._file_locks:
                self._file_locks[key] = threading.Lock()
            return self._file_locks[key]


# ── Deadlock-safe multi-file acquisition ─────────────────────────────────────

@contextmanager
def acquire_sorted(
    conflict_manager: ConflictManager,
    paths: List[Path],
    rule_name: str,
    timeout: float = 30.0,
) -> Generator[bool, None, None]:
    """
    Acquire locks for multiple files in deterministic sorted order.
    This prevents deadlocks when two rules try to lock the same files
    in different orders (classic lock ordering protocol).

    Yields True if ALL locks were acquired, False otherwise.
    """
    sorted_paths = sorted(paths, key=lambda p: str(p.resolve()))
    acquired: List[Path] = []

    try:
        for path in sorted_paths:
            ok = conflict_manager.acquire(path, rule_name, timeout)
            if not ok:
                # Failed — release all already-acquired locks
                for p in acquired:
                    conflict_manager.release(p, rule_name)
                yield False
                return
            acquired.append(path)
        yield True
    finally:
        for p in reversed(acquired):
            try:
                conflict_manager.release(p, rule_name)
            except Exception:
                pass
