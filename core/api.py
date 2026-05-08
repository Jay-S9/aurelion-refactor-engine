"""
Aurelion Refactor Engine v5 - Python API
Clean, importable interface for embedding Aurelion in other tools or scripts.

The API layer sits ABOVE the CLI and does NOT depend on argparse.
All arguments are plain Python types. All results are plain dicts or dataclasses.

Usage:
    from aurelion import api

    # Run a plan file
    result = api.run_plan("plan.toml", dry_run=True)
    print(result.summary)

    # Text replacement
    result = api.replace_text(
        old="OLD_API",
        new="NEW_API",
        target_dir="./src",
        extensions=[".py", ".yaml"],
        dry_run=True,
    )

    # Preview a plan
    preview = api.preview_plan("plan.toml", group="core")

    # Query history
    runs = api.history(limit=10)
    last = api.last_run()

    # State management
    api.invalidate_cache()
    stats = api.state_stats()

All API functions return ApiResult objects with:
  .success    bool
  .data       dict  — primary result payload
  .errors     list  — error strings (empty on success)
  .exit_code  int   — 0 = ok, 1 = failure
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class ApiResult:
    """
    Standardised return type for all API functions.
    All fields are JSON-serializable.
    """
    success:   bool
    exit_code: int
    data:      Dict[str, Any]          = field(default_factory=dict)
    errors:    List[str]               = field(default_factory=list)
    warnings:  List[str]               = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.success

    @property
    def summary(self) -> Dict[str, Any]:
        """Shorthand for data.get('summary', {})."""
        return self.data.get("summary", {})

    @property
    def modified_files(self) -> List[str]:
        """All files modified across all rules."""
        rules = self.data.get("rules", [])
        files: List[str] = []
        for r in rules:
            files.extend(r.get("files_modified", []))
        return files

    def raise_on_error(self) -> "ApiResult":
        """Raise AurelionError if not successful."""
        if not self.success:
            raise AurelionError(
                f"Aurelion operation failed: {'; '.join(self.errors)}"
            )
        return self


class AurelionError(Exception):
    """Base exception for programmatic API errors."""
    pass


# ── Internal bootstrap ─────────────────────────────────────────────────────────

def _bootstrap_logger(silent: bool = False) -> Any:
    """Create a logger instance for API calls."""
    sys.path.insert(0, str(Path(__file__).parent))
    from core.logger import AurelionLogger
    return AurelionLogger(log_to_file=not silent)


def _bootstrap_backup_manager(logger) -> Any:
    from utils.backup import BackupManager
    return BackupManager(logger)


def _load_plugins_safe(logger) -> None:
    """Load plugins without raising on failure."""
    try:
        from plugins.loader import load_plugins
        load_plugins(logger=logger)
    except Exception:
        pass


# ── Plan operations ────────────────────────────────────────────────────────────

def run_plan(
    plan_path:    Union[str, Path],
    *,
    dry_run:      bool = False,
    workers:      Optional[int] = None,
    strict:       bool = True,
    group:        Optional[str] = None,
    tag:          Optional[str] = None,
    yes:          bool = True,       # API callers almost always auto-confirm
    export:       bool = False,
    incremental:  bool = False,
    silent:       bool = False,
) -> ApiResult:
    """
    Execute a plan file programmatically.

    Args:
        plan_path:   Path to .toml or .json plan file.
        dry_run:     If True, no files are modified.
        workers:     Override worker count for all rules.
        strict:      Abort plan on first rule error.
        group:       Run only rules in this group.
        tag:         Run only rules with this tag.
        yes:         Auto-confirm (default True for API use).
        export:      Write JSON report to history/exports/.
        incremental: Skip files unchanged since last run.
        silent:      Suppress console output.

    Returns:
        ApiResult with .data containing the full report dict.
    """
    logger  = _bootstrap_logger(silent)
    backup  = _bootstrap_backup_manager(logger)
    _load_plugins_safe(logger)

    from utils.rule_parser import parse_plan, PlanValidationError
    from core.plan_runner import PlanRunner
    from core.history_manager import HistoryManager
    from core.report_exporter import ReportExporter

    history = HistoryManager(logger)

    try:
        plan = parse_plan(plan_path)
    except (FileNotFoundError, PlanValidationError) as e:
        return ApiResult(success=False, exit_code=1, errors=[str(e)])
    except Exception as e:
        return ApiResult(success=False, exit_code=1, errors=[f"Parse error: {e}"])

    run_record = history.start_run(
        command="run",
        plan_file=str(plan_path),
        plan_name=plan.name,
        dry_run=dry_run,
        group=group,
        tag=tag,
    )

    runner = PlanRunner(
        logger=logger,
        backup_manager=backup,
        dry_run=dry_run,
        workers=workers,
        strict=strict,
        yes=yes,
        group_filter=group,
        tag_filter=tag,
    )

    results  = runner.run(plan)
    aborted  = any(r.aborted for r in results)
    record   = history.finish_run(run_record, results, aborted=aborted)

    exporter = ReportExporter(record, results, logger, plan=plan)
    report   = exporter.to_dict()

    if export:
        exporter.export()

    if incremental:
        from utils.state_manager import StateManager
        sm = StateManager(logger)
        for r in results:
            for f in r.modified:
                sm.update_hash(Path(f))
        sm.flush()

    success = record["status"] in ("success", "empty")
    errors  = [
        f"{e[0]}: {e[1]}"
        for r in results for e in r.errors
    ]

    return ApiResult(
        success=success,
        exit_code=0 if success else 1,
        data=report,
        errors=errors,
    )


def preview_plan(
    plan_path: Union[str, Path],
    *,
    group:     Optional[str] = None,
    tag:       Optional[str] = None,
    silent:    bool = False,
) -> ApiResult:
    """
    Resolve a plan's execution order and file counts without modifying anything.
    Returns a dict with rule summaries and dependency graph info.
    """
    logger = _bootstrap_logger(silent)
    _load_plugins_safe(logger)

    from utils.rule_parser import parse_plan, PlanValidationError
    from core.dependency_resolver import DependencyResolver, DependencyError
    from engines.rule_engine import _resolve_glob

    try:
        plan = parse_plan(plan_path)
    except (FileNotFoundError, PlanValidationError) as e:
        return ApiResult(success=False, exit_code=1, errors=[str(e)])

    candidates = plan.enabled_rules
    if group:
        candidates = [r for r in candidates if r.group == group]
    if tag:
        candidates = [r for r in candidates if tag in (r.tags or [])]

    try:
        resolver = DependencyResolver()
        ordered  = resolver.resolve(candidates)
    except DependencyError as e:
        return ApiResult(success=False, exit_code=1, errors=[str(e)])

    rule_previews = []
    for rule in ordered:
        base  = Path(rule.base_dir) if rule.base_dir else None
        files = _resolve_glob(rule.target, rule.exclude_dirs, rule.exclude_paths, base_dir=base)
        rule_previews.append({
            "name":         rule.name,
            "type":         rule.rule_type,
            "group":        rule.group,
            "tags":         rule.tags or [],
            "depends_on":   rule.depends_on or [],
            "file_count":   len(files),
            "target_glob":  rule.target,
        })

    return ApiResult(
        success=True,
        exit_code=0,
        data={
            "plan_name":  plan.name,
            "plan_file":  str(plan_path),
            "rules":      rule_previews,
            "total_rules": len(ordered),
            "dep_graph":  resolver.visualize_graph(ordered),
        },
    )


# ── Text replacement ───────────────────────────────────────────────────────────

def replace_text(
    old:           str,
    new:           str,
    *,
    target_dir:    Optional[Union[str, Path]] = None,
    target_file:   Optional[Union[str, Path]] = None,
    target_all:    bool = False,
    extensions:    Optional[List[str]] = None,
    exclude_dirs:  Optional[List[str]] = None,
    case_sensitive: bool = True,
    dry_run:        bool = False,
    workers:        int = 1,
    silent:         bool = False,
) -> ApiResult:
    """
    Perform a text search-and-replace programmatically.
    Mirrors the CLI 'replace' command with Pythonic arguments.
    """
    logger  = _bootstrap_logger(silent)
    backup  = _bootstrap_backup_manager(logger)

    from utils.resolver import resolve_target_files
    from engines.text_engine import TextReplacementEngine
    from engines.rule_engine import _parallel_scan

    excl = exclude_dirs or [".git", "__pycache__", "node_modules", ".venv", "backups"]

    files = resolve_target_files(
        target_all=target_all,
        target_dir=str(target_dir) if target_dir else None,
        target_file=str(target_file) if target_file else None,
        extensions=extensions,
        exclude_dirs=excl,
        exclude_paths=[],
    )

    if not files:
        return ApiResult(success=True, exit_code=0, data={"modified": [], "skipped": [], "errors": []})

    engine = TextReplacementEngine(
        old_text=old,
        new_text=new,
        case_sensitive=case_sensitive,
        encoding="utf-8",
        logger=logger,
    )

    if workers > 1:
        matches = _parallel_scan(engine, files, workers)
    else:
        matches = engine.scan(files)

    if not matches:
        return ApiResult(success=True, exit_code=0, data={"modified": [], "skipped": [], "errors": []})

    if dry_run:
        return ApiResult(
            success=True, exit_code=0,
            data={"modified": [str(m["file"]) for m in matches], "skipped": [], "errors": [], "dry_run": True}
        )

    backup.backup_files([Path(m["file"]) for m in matches])
    results = engine.apply(matches)

    return ApiResult(
        success=not results["errors"],
        exit_code=0 if not results["errors"] else 1,
        data=results,
        errors=[f"{e[0]}: {e[1]}" for e in results.get("errors", [])],
    )


# ── History queries ────────────────────────────────────────────────────────────

def history(limit: int = 20) -> List[Dict[str, Any]]:
    """Return a list of recent run summaries (lightweight, from index)."""
    from core.history_manager import HistoryManager
    return HistoryManager().list_runs(limit=limit)


def last_run() -> Optional[Dict[str, Any]]:
    """Return the full record for the most recent run."""
    from core.history_manager import HistoryManager
    return HistoryManager().last()


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the full record for a specific run_id."""
    from core.history_manager import HistoryManager
    return HistoryManager().get_run(run_id)


# ── State management ───────────────────────────────────────────────────────────

def invalidate_cache(path: Optional[Union[str, Path]] = None) -> int:
    """
    Invalidate incremental state cache.
    If path is given, invalidates only that file. Otherwise clears all.
    Returns count of entries cleared.
    """
    from utils.state_manager import StateManager
    sm = StateManager()
    if path:
        sm.invalidate(Path(path))
        sm.flush()
        return 1
    count = sm.invalidate_all()
    sm.flush()
    return count


def state_stats() -> Dict[str, Any]:
    """Return state/cache statistics."""
    from utils.state_manager import StateManager
    sm = StateManager()
    return {**sm.stats(), "state_file": str(StateManager.STATE_FILE)}
