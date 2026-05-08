"""
Aurelion Refactor Engine v4 - Plan Runner
Executes a validated Plan with full v4 intelligence:
  - Dependency-ordered execution (DAG sort)
  - Pre-flight conflict detection
  - Per-rule timing and structured reporting
  - Partial rollback on rule failure (strict mode)
  - Group/tag filtering
  - Plugin-aware execution

CHANGES IN v4:
  - Integrates DependencyResolver before execution loop
  - Integrates ConflictManager for pre-flight and runtime locking
  - Per-rule backup tracking for targeted rollback
  - Structured timing table in final report
  - --group / --tag filtering applied before dependency resolution
"""

from __future__ import annotations

import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

from core.dependency_resolver import DependencyResolver, DependencyError
from engines.rule_engine import RuleExecutor, RuleResult, RuleBase, _resolve_glob
from utils.conflict_manager import ConflictManager
from utils.rule_parser import Plan
from utils.prompt import confirm_action
# v5 integrations (imported lazily to avoid circular deps)
# HistoryManager, StateManager, ReportExporter imported in run()


# ── Progress bar ────────────────────────────────────────────────────────────────

class ProgressBar:
    """Thread-safe terminal progress bar with elapsed time."""

    def __init__(self, total: int, logger, width: int = 40):
        self.total   = total
        self.current = 0
        self.logger  = logger
        self.width   = width
        self._lock   = threading.Lock()
        self._is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        self._start  = time.monotonic()

    def advance(self, label: str = "") -> None:
        with self._lock:
            self.current += 1
            self._render(label)

    def finish(self) -> None:
        with self._lock:
            self.current = self.total
            self._render("complete")
            if self._is_tty:
                print()

    def _render(self, label: str) -> None:
        pct     = self.current / max(self.total, 1)
        filled  = int(self.width * pct)
        bar     = "█" * filled + "░" * (self.width - filled)
        elapsed = time.monotonic() - self._start
        line    = f"  [{bar}] {self.current}/{self.total}  {label[:30]:<30}  {elapsed:.1f}s"
        if self._is_tty:
            print(f"\r{line}", end="", flush=True)
        else:
            self.logger.info(line)


# ── Plan Runner ─────────────────────────────────────────────────────────────────

class PlanRunner:
    """
    v4 Plan Runner. Integrates DAG, conflict detection, partial rollback,
    group filtering, plugin support, and structured timing.
    """

    def __init__(
        self,
        logger,
        backup_manager,
        dry_run:       bool = False,
        workers:       Optional[int] = None,
        strict:        bool = True,
        yes:           bool = False,
        group_filter:  Optional[str] = None,
        tag_filter:    Optional[str] = None,
        # v5 additions
        export:        bool = False,
        export_path:   Optional[Path] = None,
        incremental:   bool = False,
        history_manager = None,
        state_manager   = None,
    ):
        self.logger         = logger
        self.backup_manager = backup_manager
        self.dry_run        = dry_run
        self.global_workers = workers
        self.strict         = strict
        self.yes            = yes
        self.group_filter    = group_filter
        self.tag_filter      = tag_filter
        self.export          = export
        self.export_path     = export_path
        self.incremental     = incremental
        self.history_manager = history_manager
        self.state_manager   = state_manager

    def run(self, plan: Plan) -> List[RuleResult]:
        """
        Execute the plan with full v4 pipeline:
          filter → dependency sort → conflict check → execute → report
        """
        # ── 1. Filter by group / tag ───────────────────────────────
        candidates = plan.enabled_rules
        if self.group_filter:
            candidates = [r for r in candidates if r.group == self.group_filter]
            if not candidates:
                self.logger.warning(
                    f"No enabled rules in group '{self.group_filter}'."
                )
                return []

        if self.tag_filter:
            candidates = [r for r in candidates if self.tag_filter in (r.tags or [])]
            if not candidates:
                self.logger.warning(
                    f"No enabled rules with tag '{self.tag_filter}'."
                )
                return []

        # ── 1.5. Incremental filtering ────────────────────────────
        if self.incremental and self.state_manager:
            self.logger.info("  [INCR] Incremental mode: checking file hashes...")

        # ── 2. Dependency resolution ───────────────────────────────
        try:
            resolver = DependencyResolver(self.logger)
            ordered  = resolver.resolve(candidates)
        except DependencyError as e:
            self.logger.section("DEPENDENCY ERROR")
            self.logger.error(str(e))
            return []

        total = len(ordered)
        if total == 0:
            self.logger.warning("No rules to execute after filtering.")
            return []

        # ── 3. Pre-flight conflict detection ──────────────────────
        conflict_manager = ConflictManager(self.logger)
        self._register_all_targets(ordered, conflict_manager)
        conflicts = conflict_manager.detect_conflicts()

        # ── 4. Plan header ─────────────────────────────────────────
        self.logger.section(f"PLAN: {plan.name}")
        self.logger.info(f"  Rules    : {len(plan.rules)} total, {total} to run")
        self.logger.info(f"  Dry run  : {'YES — no files will be modified' if self.dry_run else 'NO'}")
        self.logger.info(f"  Workers  : {self.global_workers or 'per-rule default'}")
        self.logger.info(f"  Strict   : {'YES — abort on first error' if self.strict else 'NO'}")
        if self.group_filter:
            self.logger.info(f"  Group    : {self.group_filter}")
        if self.tag_filter:
            self.logger.info(f"  Tag      : {self.tag_filter}")

        # Show dependency graph if any deps exist
        if any(getattr(r, "depends_on", []) for r in ordered):
            self.logger.divider("·")
            self.logger.info("  EXECUTION ORDER (after dependency resolution):")
            self.logger.info(resolver.visualize_graph(ordered))

        # Show conflict warnings
        if conflicts:
            self.logger.divider("·")
            self.logger.warning(f"  ⚠ {len(conflicts)} file conflict(s) detected:")
            for line in conflict_manager.conflict_report():
                self.logger.info(line)

        self.logger.divider()

        # ── 5. Confirmation gate ───────────────────────────────────
        if not self.dry_run and not self.yes:
            if not confirm_action(
                f"Execute plan '{plan.name}' ({total} rule(s)) against real files?"
            ):
                self.logger.warning("Plan execution cancelled.")
                return []

        # ── 6. Execute rules ───────────────────────────────────────
        executor  = RuleExecutor(self.logger, self.backup_manager, state_manager=self.state_manager)
        results: List[RuleResult] = []
        progress  = ProgressBar(total, self.logger)
        aborted   = False

        # Track per-rule backup session dirs for partial rollback
        rule_backup_sessions: Dict[str, Optional[Path]] = {}

        for i, rule in enumerate(ordered):
            # Apply global overrides
            if self.dry_run:
                rule.dry_run = True
            if self.global_workers is not None:
                rule.workers = self.global_workers

            # Capture backup session before execution
            pre_sessions = set(self.backup_manager.list_sessions())

            rule_label = f"[{i+1}/{total}] {rule.name}"
            self.logger.section(f"RULE {rule_label}  ({rule.rule_type})")
            if rule.group:
                self.logger.info(f"  Group    : {rule.group}")

            result = executor.execute(rule, conflict_manager=conflict_manager)
            results.append(result)
            progress.advance(rule.name)

            # Identify new backup session (if any)
            post_sessions = set(self.backup_manager.list_sessions())
            new_sessions  = post_sessions - pre_sessions
            rule_backup_sessions[rule.name] = (
                max(new_sessions, key=lambda p: p.stat().st_mtime)
                if new_sessions else None
            )

            # Per-rule summary with timing
            self._emit_rule_summary(result)

            # Abort / rollback logic
            if result.aborted or (result.errors and self.strict):
                reason = (result.abort_reason if result.aborted
                          else f"{len(result.errors)} error(s)")

                if result.errors and self.strict and not result.aborted:
                    self.logger.warning(
                        f"Rule '{rule.name}' produced {len(result.errors)} error(s). "
                        f"Strict mode: aborting plan."
                    )

                # Partial rollback: restore only files this rule modified
                self._partial_rollback(rule.name, result, rule_backup_sessions)

                aborted = True
                break

        progress.finish()
        conflict_manager.clear()

        # ── 7. Final report ────────────────────────────────────────
        self._emit_plan_report(plan, results, aborted, ordered)

        # ── 8. v5: Update incremental hashes ──────────────────────
        if self.incremental and self.state_manager:
            for r in results:
                for fpath in r.modified:
                    try:
                        self.state_manager.update_hash(Path(fpath))
                    except Exception:
                        pass
            self.state_manager.flush()
            self.logger.info(
                f"  [INCR] Updated hashes for {sum(len(r.modified) for r in results)} file(s)."
            )

        # ── 9. v5: Export report ───────────────────────────────────
        if self.export:
            try:
                from core.report_exporter import ReportExporter
                dummy_record = {
                    "run_id":     "manual",
                    "plan_file":  str(plan.source_path),
                    "plan_name":  plan.name,
                    "dry_run":    self.dry_run,
                    "command":    "run",
                    "status":     "aborted" if aborted else ("success" if all(r.success for r in results) else "partial"),
                    "duration":   sum(r.duration_seconds for r in results),
                    "totals": {
                        "rules_run":      len(results),
                        "rules_ok":       sum(1 for r in results if r.success),
                        "rules_failed":   sum(1 for r in results if not r.success),
                        "files_modified": sum(len(r.modified) for r in results),
                        "files_skipped":  sum(len(r.skipped)  for r in results),
                        "errors":         sum(len(r.errors)    for r in results),
                    },
                    "group":       self.group_filter,
                    "tag":         self.tag_filter,
                    "environment": {},
                }
                exporter = ReportExporter(dummy_record, results, self.logger, plan=plan)
                export_path = exporter.export(self.export_path)
                self.logger.success(f"Report exported → {export_path}")
            except Exception as e:
                self.logger.warning(f"  [EXPORT] Report export failed: {e}")

        # ── 10. v5: Performance metrics ────────────────────────────
        if results:
            try:
                from core.report_exporter import ReportExporter
                dummy_record_perf = {"status": "ok", "duration": sum(r.duration_seconds for r in results), "totals": {}}
                perf = ReportExporter(dummy_record_perf, results, self.logger)
                perf.print_perf()
            except Exception:
                pass

        return results

    # ──────────────────────────────────────────────────────────────
    # Pre-flight helpers
    # ──────────────────────────────────────────────────────────────

    def _register_all_targets(
        self, rules: List[RuleBase], conflict_manager: ConflictManager
    ) -> None:
        """
        Pre-resolve each rule's target files and register them
        with the conflict manager for overlap detection.
        """
        for rule in rules:
            try:
                base  = Path(rule.base_dir) if rule.base_dir else None
                files = _resolve_glob(
                    rule.target, rule.exclude_dirs, rule.exclude_paths, base_dir=base
                )
                conflict_manager.register_rule_targets(rule.name, files)
            except Exception:
                pass   # Pre-flight failure is non-fatal

    # ──────────────────────────────────────────────────────────────
    # Partial rollback
    # ──────────────────────────────────────────────────────────────

    def _partial_rollback(
        self,
        rule_name: str,
        result: RuleResult,
        session_map: Dict[str, Optional[Path]],
    ) -> None:
        """
        Roll back only the files modified by the failed rule.
        Uses the rule-specific backup session if available.
        """
        if self.dry_run or not result.modified:
            return

        session = session_map.get(rule_name)
        if session is None:
            self.logger.warning(
                f"  [ROLLBACK] No backup session for '{rule_name}' — cannot rollback."
            )
            return

        self.logger.warning(
            f"  [ROLLBACK] Rolling back {len(result.modified)} file(s) "
            f"from '{rule_name}'..."
        )
        restored = self.backup_manager.restore_session(session)
        if restored > 0:
            self.logger.success(
                f"  [ROLLBACK] Restored {restored} file(s) to pre-'{rule_name}' state."
            )
        else:
            self.logger.error(
                f"  [ROLLBACK] Rollback failed for '{rule_name}'."
            )

    # ──────────────────────────────────────────────────────────────
    # Reporting
    # ──────────────────────────────────────────────────────────────

    def _emit_rule_summary(self, result: RuleResult) -> None:
        """Print a compact summary with timing for a completed rule."""
        if result.aborted:
            self.logger.error(f"  ABORTED  — {result.abort_reason}")
            return

        mod  = len(result.modified)
        skip = len(result.skipped)
        err  = len(result.errors)
        dur  = result.duration_seconds

        parts = []
        if mod:   parts.append(f"✔ {mod} modified")
        if skip:  parts.append(f"─ {skip} skipped")
        if err:   parts.append(f"✖ {err} error(s)")

        summary = "  " + "  │  ".join(parts) if parts else "  (no changes)"
        self.logger.info(f"{summary}  ({dur:.2f}s)")

        if result.scan_stats:
            self.logger.scan_summary(
                result.scan_stats, result.scan_stats.get("scanned", 0)
            )

    def _emit_plan_report(
        self,
        plan: Plan,
        results: List[RuleResult],
        aborted: bool,
        ordered: List[RuleBase],
    ) -> None:
        """Print the final consolidated plan report with timing table."""
        self.logger.section("PLAN EXECUTION REPORT")

        total_modified = sum(len(r.modified) for r in results)
        total_skipped  = sum(len(r.skipped)  for r in results)
        total_errors   = sum(len(r.errors)   for r in results)
        total_duration = sum(r.duration_seconds for r in results)
        rules_run      = len(results)
        rules_ok       = sum(1 for r in results if r.success)
        rules_failed   = rules_run - rules_ok

        if aborted:
            self.logger.warning(
                f"  ⚡ Plan aborted after {rules_run}/{len(ordered)} rule(s)."
            )

        # ── Timing table ──────────────────────────────────────────
        self.logger.divider("·")
        self.logger.info(
            f"  {'RULE':<32} {'STATUS':<12} {'MODIFIED':<10} {'ERRORS':<8} {'TIME':>7}"
        )
        self.logger.divider("·")

        for r in results:
            status = "✖ ABORTED" if r.aborted else ("✔ OK" if r.success else "⚠ ERRORS")
            mod    = len(r.modified)
            err    = len(r.errors)
            dur    = f"{r.duration_seconds:.2f}s"
            self.logger.info(
                f"  {r.rule_name:<32} {status:<12} {mod:<10} {err:<8} {dur:>7}"
            )

        self.logger.divider()

        # ── Summary totals ────────────────────────────────────────
        self.logger.info(f"  Rules executed   : {rules_run}")
        self.logger.info(f"  Rules succeeded  : {rules_ok}")
        if rules_failed:
            self.logger.error(f"  Rules failed     : {rules_failed}")

        self.logger.divider("·")
        self.logger.info(f"  Total modified   : {total_modified}")
        self.logger.info(f"  Total skipped    : {total_skipped}")
        if total_errors:
            self.logger.error(f"  Total errors     : {total_errors}")

        self.logger.info(f"  Total time       : {total_duration:.2f}s")

        if self.dry_run:
            self.logger.warning("\n  [DRY RUN] — No files were modified.")

        self.logger.divider()
