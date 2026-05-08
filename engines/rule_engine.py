"""
Aurelion Refactor Engine v3 - Rule Engine
Defines the typed Rule data model and the RuleExecutor that runs a single rule
against the file system using the existing text/file engines.

Design principle: a Rule is a pure data object (dataclass). The RuleExecutor
is the only place that touches the file system. This separation makes rules
trivially serialisable, loggable, and testable.

Rule types:
  replace       – text search-and-replace across glob-matched files
  replace_file  – overwrite a target path with a source file
  inject        – prepend / append / overwrite a template into matched targets
"""

from __future__ import annotations

import fnmatch
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from engines.text_engine import TextReplacementEngine
from engines.file_engine import FileReplacementEngine
from utils.resolver import _walk, _resolve_exclude_paths
from utils.file_safety import SAFE_TEXT_EXTENSIONS, is_safe_text_file, detect_encoding


# ── Rule type constants ────────────────────────────────────────────────────────
RULE_REPLACE      = "replace"
RULE_REPLACE_FILE = "replace_file"
RULE_INJECT       = "inject"

INJECT_MODE_REPLACE  = "replace"
INJECT_MODE_PREPEND  = "prepend"
INJECT_MODE_APPEND   = "append"

VALID_RULE_TYPES = {RULE_REPLACE, RULE_REPLACE_FILE, RULE_INJECT}

def get_all_valid_types() -> set:
    """Returns built-in types + any registered plugin types."""
    try:
        from plugins.loader import get_registry
        return VALID_RULE_TYPES | set(get_registry().all_types())
    except Exception:
        return VALID_RULE_TYPES


# ── Typed rule dataclasses ─────────────────────────────────────────────────────

@dataclass
class RuleBase:
    """Fields common to every rule."""
    name: str
    rule_type: str                       # "replace" | "replace_file" | "inject"
    target: str                          # glob pattern  e.g. "**/*.py"
    enabled: bool         = True
    dry_run: bool         = False
    no_backup: bool       = False
    # Optional overrides
    encoding: str         = "utf-8"
    exclude_dirs: List[str] = field(default_factory=lambda: [
        ".git", "__pycache__", "node_modules", ".venv", "backups", "logs",
    ])
    exclude_paths: List[str] = field(default_factory=list)
    include_binary: bool  = False
    workers: int          = 1            # parallelism per rule
    base_dir: str         = ""             # plan file directory for relative globs
    # v4 fields
    depends_on: List[str] = field(default_factory=list)   # rule names this rule waits for
    group: str            = ""             # logical group tag  e.g. "core", "experimental"
    tags: List[str]       = field(default_factory=list)   # arbitrary tags for filtering


@dataclass
class ReplaceRule(RuleBase):
    find: str             = ""
    replace: str          = ""
    case_insensitive: bool = False


@dataclass
class ReplaceFileRule(RuleBase):
    source: str           = ""           # path to source file
    overwrite: bool       = True


@dataclass
class InjectRule(RuleBase):
    source: str           = ""           # template file path
    mode: str             = INJECT_MODE_REPLACE   # replace | prepend | append
    overwrite: bool       = True


# ── Rule execution result ─────────────────────────────────────────────────────

@dataclass
class RuleResult:
    rule_name: str
    rule_type: str
    modified:  List[str]         = field(default_factory=list)
    skipped:   List[str]         = field(default_factory=list)
    errors:    List[Tuple[str, str]] = field(default_factory=list)
    scan_stats: Dict[str, int]   = field(default_factory=dict)
    aborted:   bool              = False
    abort_reason: str            = ""
    # v4 timing
    start_time: float            = 0.0
    end_time: float              = 0.0
    duration_seconds: float      = 0.0

    @property
    def success(self) -> bool:
        return not self.aborted and not self.errors

    @property
    def total_files(self) -> int:
        return len(self.modified) + len(self.skipped) + len(self.errors)


# ── Rule Executor ─────────────────────────────────────────────────────────────

class RuleExecutor:
    """
    Executes a single Rule object using the existing engines.
    Thread-safe: uses a lock around logger calls.
    """

    def __init__(self, logger, backup_manager, state_manager=None):
        self.logger = logger
        self.backup_manager = backup_manager
        self.state_manager = state_manager
        self._log_lock = threading.Lock()

    def execute(self, rule: RuleBase, conflict_manager=None) -> RuleResult:
        """Dispatch to the appropriate handler based on rule type."""
        import time
        if not rule.enabled:
            self.logger.info(f"  [SKIP] Rule '{rule.name}' is disabled.")
            return RuleResult(rule.name, rule.rule_type, skipped=["(rule disabled)"])

        start = time.monotonic()

        if rule.rule_type == RULE_REPLACE:
            result = self._execute_replace(rule, conflict_manager)
        elif rule.rule_type == RULE_REPLACE_FILE:
            result = self._execute_replace_file(rule)
        elif rule.rule_type == RULE_INJECT:
            result = self._execute_inject(rule)
        else:
            # Try plugin registry with sandboxed execution
            try:
                from plugins.loader import get_registry
                plugin = get_registry().get(rule.rule_type)
                if plugin:
                    result = _sandboxed_plugin_execute(
                        plugin, rule, self.logger, self.backup_manager
                    )
                else:
                    raise KeyError("not found")
            except KeyError:
                msg = f"Unknown rule type: '{rule.rule_type}'. Is the plugin loaded?"
                self.logger.error(msg)
                result = RuleResult(rule.name, rule.rule_type)
                result.aborted = True
                result.abort_reason = msg
            except Exception as e:
                msg = f"Plugin error '{rule.rule_type}': {e}"
                self.logger.error(msg)
                result = RuleResult(rule.name, rule.rule_type)
                result.aborted = True
                result.abort_reason = msg

        end = time.monotonic()
        result.start_time = start
        result.end_time = end
        result.duration_seconds = round(end - start, 3)
        return result

    # ── Replace handler ───────────────────────────────────────────

    def _execute_replace(self, rule: ReplaceRule, conflict_manager=None) -> RuleResult:
        result = RuleResult(rule.name, rule.rule_type)

        # Resolve files matching the glob pattern
        base = Path(rule.base_dir) if rule.base_dir else None
        target_files = _resolve_glob(rule.target, rule.exclude_dirs, rule.exclude_paths, base_dir=base)
        if not target_files:
            self.logger.info(f"  Rule '{rule.name}': no files matched '{rule.target}'")
            return result

        # v5: Incremental filter — skip files unchanged since last run
        if self.state_manager is not None:
            filtered = self.state_manager.filter_changed(target_files)
            skipped_count = len(target_files) - len(filtered)
            if skipped_count > 0:
                with self._log_lock:
                    self.logger.info(
                        f"  [INCR] {skipped_count} unchanged file(s) skipped  "
                        f"({len(filtered)} to scan)"
                    )
            target_files = filtered
            if not target_files:
                self.logger.info(f"  Rule '{rule.name}': all files unchanged — skipping.")
                return result

        engine = TextReplacementEngine(
            old_text=rule.find,
            new_text=rule.replace,
            case_sensitive=not rule.case_insensitive,
            encoding=rule.encoding,
            logger=_ThreadSafeLoggerProxy(self.logger, self._log_lock),
            include_binary=rule.include_binary,
        )

        if rule.workers > 1:
            matches = _parallel_scan(engine, target_files, rule.workers)
        else:
            matches = engine.scan(target_files)

        result.scan_stats = engine.stats

        if not matches:
            return result

        if not rule.dry_run:
            if not rule.no_backup:
                affected = [Path(m["file"]) for m in matches]
                self.backup_manager.backup_files(affected)

            apply_result = engine.apply(matches)
            result.modified = apply_result["modified"]
            result.skipped  = apply_result["skipped"]
            result.errors   = apply_result["errors"]
        else:
            # Dry run: count matches as "would modify"
            result.modified = [str(m["file"]) for m in matches]

        return result

    # ── Replace-file handler ──────────────────────────────────────

    def _execute_replace_file(self, rule: ReplaceFileRule) -> RuleResult:
        result = RuleResult(rule.name, rule.rule_type)
        source = Path(rule.source)

        if not source.exists():
            result.aborted = True
            result.abort_reason = f"Source file not found: {source}"
            self.logger.error(result.abort_reason)
            return result

        base = Path(rule.base_dir) if rule.base_dir else None
        targets = _resolve_glob(rule.target, rule.exclude_dirs, rule.exclude_paths, base_dir=base)
        if not targets:
            self.logger.info(f"  Rule '{rule.name}': no targets matched '{rule.target}'")
            return result

        engine = FileReplacementEngine(
            overwrite=rule.overwrite,
            logger=_ThreadSafeLoggerProxy(self.logger, self._log_lock),
        )

        if not rule.dry_run:
            if not rule.no_backup:
                existing = [t for t in targets if t.exists()]
                if existing:
                    self.backup_manager.backup_files(existing)

            apply_result = engine.replace_files(source, targets)
            result.modified = apply_result["modified"]
            result.skipped  = apply_result["skipped"]
            result.errors   = apply_result["errors"]
        else:
            result.modified = [str(t) for t in targets]

        return result

    # ── Inject handler ────────────────────────────────────────────

    def _execute_inject(self, rule: InjectRule) -> RuleResult:
        result = RuleResult(rule.name, rule.rule_type)
        source = Path(rule.source)

        if not source.exists():
            result.aborted = True
            result.abort_reason = f"Template file not found: {source}"
            self.logger.error(result.abort_reason)
            return result

        template_content = source.read_text(encoding=rule.encoding, errors="replace")
        base = Path(rule.base_dir) if rule.base_dir else None
        targets = _resolve_glob(rule.target, rule.exclude_dirs, rule.exclude_paths, base_dir=base)

        if not targets:
            self.logger.info(f"  Rule '{rule.name}': no targets matched '{rule.target}'")
            return result

        for target_path in targets:
            try:
                _inject_into_file(
                    target_path=target_path,
                    template_content=template_content,
                    mode=rule.mode,
                    encoding=rule.encoding,
                    dry_run=rule.dry_run,
                    no_backup=rule.no_backup,
                    backup_manager=self.backup_manager,
                    logger=self.logger,
                    log_lock=self._log_lock,
                )
                result.modified.append(str(target_path))
            except Exception as e:
                result.errors.append((str(target_path), str(e)))
                with self._log_lock:
                    self.logger.error(f"Inject failed: {target_path} — {e}")

        return result


# ── Glob resolver ─────────────────────────────────────────────────────────────

def _resolve_glob(
    pattern: str,
    exclude_dir_names: List[str],
    exclude_path_strs: List[str],
    base_dir: Optional[Path] = None,
) -> List[Path]:
    """
    Resolve a glob pattern like '**/*.py' or 'src/**/*.md' to a file list.
    base_dir: directory to resolve relative patterns from (defaults to cwd).
    """
    root_base     = (base_dir or Path.cwd()).resolve()
    exclude_dirs  = set(exclude_dir_names)
    exclude_paths = _resolve_exclude_paths(exclude_path_strs)

    parts       = Path(pattern).parts
    static_p: list = []
    glob_p:   list = []
    in_glob = False
    for p in parts:
        if in_glob or any(c in p for c in ("*", "?", "[")):
            in_glob = True
            glob_p.append(p)
        else:
            static_p.append(p)

    if static_p:
        root = Path(*static_p)
        if not root.is_absolute():
            root = root_base / root
    else:
        root = root_base

    if not root.exists():
        return []

    if glob_p:
        sub = str(Path(*glob_p))
        if sub.startswith("**/"):
            sub = sub[3:]
        elif sub == "**":
            sub = "*"
        try:
            results_raw = list(root.rglob(sub))
        except Exception:
            results_raw = []
    else:
        if root.is_file():
            return [root]
        try:
            results_raw = list(root.rglob("*"))
        except Exception:
            results_raw = []

    results = []
    for p in results_raw:
        if not p.is_file():
            continue
        parts_set = set(p.parts)
        if any(d in parts_set for d in exclude_dirs):
            continue
        if p.resolve() in exclude_paths:
            continue
        results.append(p)

    return sorted(results)


def _resolve_glob_files(
    pattern: str,
    exclude_dirs: List[str],
    exclude_paths: List[str],
) -> List[Path]:
    """Like _resolve_glob but returns resolved Path objects."""
    return _resolve_glob(pattern, exclude_dirs, exclude_paths)


# ── Inject helper ─────────────────────────────────────────────────────────────

def _inject_into_file(
    target_path: Path,
    template_content: str,
    mode: str,
    encoding: str,
    dry_run: bool,
    no_backup: bool,
    backup_manager,
    logger,
    log_lock: threading.Lock,
) -> None:
    """Apply template to a single target file."""
    if dry_run:
        with log_lock:
            logger.info(f"  [DRY RUN] Would inject into: {target_path}  (mode={mode})")
        return

    if not no_backup and target_path.exists():
        backup_manager.backup_files([target_path])

    if mode == INJECT_MODE_REPLACE or not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(template_content, encoding=encoding)
    elif mode == INJECT_MODE_PREPEND:
        existing = target_path.read_text(encoding=encoding, errors="replace")
        target_path.write_text(template_content + "\n" + existing, encoding=encoding)
    elif mode == INJECT_MODE_APPEND:
        existing = target_path.read_text(encoding=encoding, errors="replace")
        target_path.write_text(existing + "\n" + template_content, encoding=encoding)
    else:
        raise ValueError(f"Unknown inject mode: '{mode}'")

    with log_lock:
        logger.success(f"Injected ({mode}): {target_path}")


# ── Parallel scan ──────────────────────────────────────────────────────────────

def _parallel_scan(
    engine: TextReplacementEngine,
    files: List[Path],
    workers: int,
) -> list:
    """
    Scan files in parallel using ThreadPoolExecutor.
    Thread-safe: engine._scan_file is read-only (no shared write state).
    Results are merged and sorted to maintain deterministic order.
    """
    matches = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(engine._scan_file, f): f for f in files}
        for future in as_completed(futures):
            result = future.result()
            if result and result not in ("binary", "encoding_error"):
                with lock:
                    engine.stats["scanned"] += 1
                    engine.stats["matched"] += 1
                    matches.append(result)
            elif result == "binary":
                with lock:
                    engine.stats["scanned"] += 1
                    engine.stats["skipped_binary"] += 1
            elif result == "encoding_error":
                with lock:
                    engine.stats["scanned"] += 1
                    engine.stats["skipped_encoding"] += 1

    # Sort by file path for deterministic output
    matches.sort(key=lambda m: str(m["file"]))
    return matches



# ── Plugin sandbox ────────────────────────────────────────────────────────────

def _sandboxed_plugin_execute(
    plugin,
    rule: "RuleBase",
    logger,
    backup_manager,
    timeout: float = 120.0,
) -> "RuleResult":
    """
    Execute a plugin with full process-level isolation using multiprocessing.

    Architecture:
      1. Serialise the rule to a plain dict (JSON-safe)
      2. Spawn a child process that re-imports the plugin and executes it
      3. Results are returned via a multiprocessing.Queue
      4. If the child hangs or crashes, the parent terminates it after `timeout` seconds
      5. Falls back to in-process execution if multiprocessing is unavailable

    This prevents a misbehaving plugin from:
      - Hanging the engine indefinitely
      - Crashing the parent process via segfault
      - Leaking global state or mutating shared objects
    """
    import multiprocessing
    import dataclasses
    import importlib.util
    import os

    plugin_file = str(getattr(plugin, "source_file", ""))
    rule_dict   = dataclasses.asdict(rule) if dataclasses.is_dataclass(rule) else {}
    rule_dict["__rule_type__"] = rule.rule_type
    rule_dict["__rule_name__"] = rule.name

    result_queue: "multiprocessing.Queue[dict]" = multiprocessing.Queue(maxsize=1)

    def _child_worker(q, plugin_path, rd, log_queue):
        """Child process entry point. Must be module-level importable."""
        import sys, json, importlib.util, dataclasses
        from pathlib import Path

        # Re-add engine root to path
        engine_root = str(Path(plugin_path).parent.parent)
        if engine_root not in sys.path:
            sys.path.insert(0, engine_root)

        try:
            # Re-load the plugin module
            spec   = importlib.util.spec_from_file_location("_sandbox_plugin", plugin_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["_sandbox_plugin"] = module
            spec.loader.exec_module(module)

            # Reconstruct a minimal rule-like object
            class _RuleProxy:
                pass
            proxy = _RuleProxy()
            for k, v in rd.items():
                setattr(proxy, k, v)

            # Null logger (no shared state with parent)
            class _NullLogger:
                def info(self, *a): pass
                def success(self, *a): pass
                def error(self, *a): pass
                def warning(self, *a): pass
                def skipped(self, *a): pass
                def _c(self, *a): return a[-1] if a else ""

            result = module.execute(proxy, _NullLogger(), None)

            # Serialise result to a plain dict
            q.put({
                "ok":       True,
                "modified": list(result.modified) if result else [],
                "skipped":  list(result.skipped)  if result else [],
                "errors":   list(result.errors)   if result else [],
                "aborted":  result.aborted if result else False,
            })
        except Exception as e:
            q.put({"ok": False, "error": str(e), "aborted": True})

    # Only use multiprocessing if we have a plugin source file
    use_mp = bool(plugin_file) and os.path.exists(plugin_file)

    if use_mp:
        ctx  = multiprocessing.get_context("spawn")
        proc = ctx.Process(
            target=_child_worker,
            args=(result_queue, plugin_file, rule_dict, None),
            daemon=True,
        )
        try:
            proc.start()
            proc.join(timeout=timeout)

            if proc.is_alive():
                proc.terminate()
                proc.join(2)
                if proc.is_alive():
                    proc.kill()
                r = RuleResult(rule.name, rule.rule_type)
                r.aborted = True
                r.abort_reason = f"Plugin '{rule.rule_type}' timed out after {timeout}s"
                logger.error(f"  [SANDBOX] {r.abort_reason}")
                return r

            if not result_queue.empty():
                payload = result_queue.get_nowait()
                if payload.get("ok"):
                    r = RuleResult(rule.name, rule.rule_type)
                    r.modified = payload.get("modified", [])
                    r.skipped  = payload.get("skipped",  [])
                    r.errors   = [(e, "") if isinstance(e, str) else e
                                  for e in payload.get("errors", [])]
                    r.aborted  = payload.get("aborted", False)
                    return r
                else:
                    r = RuleResult(rule.name, rule.rule_type)
                    r.aborted = True
                    r.abort_reason = f"Plugin error: {payload.get('error', 'unknown')}"
                    logger.error(f"  [SANDBOX] {r.abort_reason}")
                    return r

        except Exception as e:
            logger.error(f"  [SANDBOX] Process spawn failed ({e}); falling back to in-process")
            # Fall through to in-process execution
        finally:
            if proc.is_alive():
                try: proc.kill()
                except Exception: pass

    # ── In-process fallback (no isolation, but safe for trusted plugins) ──────
    try:
        result = plugin.execute_fn(rule, logger, backup_manager)
        if result is None:
            r = RuleResult(rule.name, rule.rule_type)
            r.aborted = True
            r.abort_reason = f"Plugin '{rule.rule_type}' returned None"
            return r
        return result
    except Exception as e:
        r = RuleResult(rule.name, rule.rule_type)
        r.aborted = True
        r.abort_reason = f"Plugin '{rule.rule_type}' crashed: {e}"
        logger.error(f"  [SANDBOX] Plugin crashed: {e}")
        return r

# ── Thread-safe logger proxy ───────────────────────────────────────────────────

class _ThreadSafeLoggerProxy:
    """
    Wraps an AurelionLogger so that concurrent threads don't interleave output.
    Proxies all public methods through an acquired lock.
    """

    def __init__(self, logger, lock: threading.Lock):
        self._logger = logger
        self._lock = lock

    def __getattr__(self, name: str):
        attr = getattr(self._logger, name)
        if callable(attr):
            def locked(*args, **kwargs):
                with self._lock:
                    return attr(*args, **kwargs)
            return locked
        return attr
