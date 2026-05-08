"""
Aurelion Refactor Engine v7 - Executor
Orchestrates operation pipelines: validation → backup → execute → report.

CHANGES IN v5:
  - _run_history() added: execution history viewer
  - _run_plan() wires HistoryManager, StateManager, export flags
  - Performance metrics output via ReportExporter

CHANGES IN v4:
  - _run_preview() added: plan visualization with DAG + file counts
  - _run_plan() passes group_filter and tag_filter to PlanRunner
  - _list_plugins() handler added

CHANGES IN v3:
  - _run_plan()   added: dispatches to PlanRunner for multi-rule plan files
  - _run_inject() added: template injection via RuleExecutor
  - _run_text_replace() gains --workers support via parallel scan
  - All handlers now thread-safe via engine-level locking
"""

import json
import sys
from pathlib import Path
from typing import Optional

from core.logger import AurelionLogger
from core.plan_runner import PlanRunner
from engines.text_engine import TextReplacementEngine
from engines.file_engine import FileReplacementEngine
from engines.rule_engine import RuleExecutor, InjectRule, _parallel_scan
from utils.backup import BackupManager
from utils.config import load_config
from utils.prompt import confirm_action
from utils.resolver import resolve_target_files, resolve_file_copy_targets
from utils.diff_renderer import render_diff
from utils.rule_parser import parse_plan, generate_example_plan, PlanValidationError


class Executor:
    """
    Central orchestrator. Receives parsed args, delegates to engines/runners,
    manages safety gates (dry-run, backup, confirm), and reports results.
    """

    def __init__(self, args, config_path: Optional[str] = None):
        self.args = args
        # Load profile first so config can be merged
        self._profile = None
        profile_name = getattr(args, "profile", None)
        if profile_name:
            try:
                from core.profile_manager import ProfileManager
                pm = ProfileManager()
                self._profile = pm.load(profile_name)
            except Exception as e:
                import sys as _sys
                print(f"[WARN] Profile '{profile_name}': {e}", file=_sys.stderr)

        base_config = load_config(config_path) if config_path else {}
        if self._profile:
            from core.profile_manager import ProfileManager
            pm = ProfileManager()
            base_config = pm.apply_to_config(self._profile, base_config)

        self.config = base_config
        self.logger = AurelionLogger()
        self.backup_manager = BackupManager(self.logger)

    # ─────────────────────────────────────────────────────────────
    # Public dispatch
    # ─────────────────────────────────────────────────────────────

    def run(self) -> int:
        command = self.args.command
        try:
            dispatch = {
                "replace":      self._run_text_replace,
                "replace-file": self._run_replace_file,
                "copy-file":    self._run_copy_file,
                "inject":       self._run_inject,
                "run":          self._run_plan,
                "preview":      self._run_preview,
                "history":      self._run_history,
                "ai":           self._run_ai,
                "dashboard":    self._run_dashboard,
                "server":       self._run_server,
                "plugins":      self._run_plugins,
                "profile":      self._run_profile,
                "auth":         self._run_auth,
                "db":           self._run_db,
                "restore":      self._run_restore,
            }
            handler = dispatch.get(command)
            if handler is None:
                self.logger.error(f"Unknown command: {command}")
                return 1
            return handler()
        except KeyboardInterrupt:
            self.logger.warning("\nOperation cancelled by user (Ctrl+C).")
            return 1
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}", exc_info=True)
            return 1

    # ─────────────────────────────────────────────────────────────
    # Text replacement pipeline
    # ─────────────────────────────────────────────────────────────

    def _run_text_replace(self) -> int:
        args = self.args

        target_files = resolve_target_files(
            target_all=getattr(args, "target_all", False),
            target_dir=getattr(args, "target_dir", None),
            target_file=getattr(args, "target_file", None),
            extensions=getattr(args, "extensions", None),
            exclude_dirs=getattr(args, "exclude_dirs", []),
            exclude_paths=getattr(args, "exclude_paths", []),
        )

        if not target_files:
            self.logger.warning("No files matched the specified target/filters.")
            return 0

        workers = getattr(args, "workers", 1)
        self.logger.info(
            f"  Resolved : {len(target_files)} file(s) to scan"
            + (f"  (workers={workers})" if workers > 1 else "")
        )

        engine = TextReplacementEngine(
            old_text=args.old_text,
            new_text=args.new_text,
            case_sensitive=args.case_sensitive,
            encoding=args.encoding,
            logger=self.logger,
            include_binary=getattr(args, "include_binary", False),
        )

        # Parallel scan if --workers > 1
        if workers > 1:
            matches = _parallel_scan(engine, target_files, workers)
        else:
            matches = engine.scan(target_files)

        scan_stats = engine.stats
        self.logger.scan_summary(scan_stats, len(target_files))

        if not matches:
            self.logger.info(f"  No matches found for '{args.old_text}'.")
            return 0

        self._display_diff_preview(matches, args, engine)

        if args.dry_run:
            self.logger.info("\n[DRY RUN] No files were modified.")
            return 0

        total_occurrences = sum(m["count"] for m in matches)
        if not args.yes:
            if not confirm_action(
                f"Apply {total_occurrences} replacement(s) across {len(matches)} file(s)?"
            ):
                self.logger.warning("Operation cancelled.")
                return 1

        if not getattr(args, "no_backup", False):
            self.backup_manager.backup_files([Path(m["file"]) for m in matches])

        results = engine.apply(matches)
        self.logger.report(results, scan_stats=scan_stats)
        return 0

    # ─────────────────────────────────────────────────────────────
    # File replacement pipeline
    # ─────────────────────────────────────────────────────────────

    def _run_replace_file(self) -> int:
        args = self.args
        source = Path(args.source)
        target = Path(args.target)

        engine  = FileReplacementEngine(overwrite=args.overwrite, logger=self.logger)
        targets = resolve_file_copy_targets(source, [target])

        if not targets:
            self.logger.warning("No valid target files resolved.")
            return 0

        self._display_file_op_preview("REPLACE-FILE", source, targets, args)

        if args.dry_run:
            self.logger.info("[DRY RUN] No files were modified.")
            return 0

        if not args.yes:
            if not confirm_action(f"Replace {len(targets)} file(s) with '{source.name}'?"):
                self.logger.warning("Operation cancelled.")
                return 1

        if not getattr(args, "no_backup", False):
            existing = [p for p in targets if p.exists()]
            if existing:
                self.backup_manager.backup_files(existing)

        results = engine.replace_files(source, targets)
        self.logger.report(results)
        return 0

    # ─────────────────────────────────────────────────────────────
    # Multi-target copy pipeline
    # ─────────────────────────────────────────────────────────────

    def _run_copy_file(self) -> int:
        args    = self.args
        source  = Path(args.source)
        targets = resolve_file_copy_targets(source, [Path(t) for t in args.targets])
        engine  = FileReplacementEngine(overwrite=args.overwrite, logger=self.logger)

        if not targets:
            self.logger.warning("No valid target files resolved.")
            return 0

        self._display_file_op_preview("COPY-FILE", source, targets, args)

        if args.dry_run:
            self.logger.info("[DRY RUN] No files were modified.")
            return 0

        if not args.yes:
            if not confirm_action(f"Copy '{source.name}' to {len(targets)} destination(s)?"):
                self.logger.warning("Operation cancelled.")
                return 1

        results = engine.replace_files(source, targets)
        self.logger.report(results)
        return 0

    # ─────────────────────────────────────────────────────────────
    # Inject pipeline  (NEW v3)
    # ─────────────────────────────────────────────────────────────

    def _run_inject(self) -> int:
        args     = self.args
        source   = Path(args.source)
        mode     = getattr(args, "inject_mode", "replace")
        overwrite = getattr(args, "overwrite", True)
        dry_run  = args.dry_run
        encoding = getattr(args, "encoding", "utf-8")

        exclude_dirs  = getattr(args, "exclude_dirs",  [])
        exclude_paths = getattr(args, "exclude_paths", [])

        self.logger.section(f"{'[DRY RUN] ' if dry_run else ''}INJECT PREVIEW")
        self.logger.info(f"  Template : {source}")
        self.logger.info(f"  Target   : {args.target}")
        self.logger.info(f"  Mode     : {mode}")

        rule = InjectRule(
            name="cli-inject",
            rule_type="inject",
            target=args.target,
            source=str(source),
            mode=mode,
            overwrite=overwrite,
            dry_run=dry_run,
            no_backup=getattr(args, "no_backup", False),
            encoding=encoding,
            exclude_dirs=exclude_dirs,
            exclude_paths=exclude_paths,
        )

        executor = RuleExecutor(self.logger, self.backup_manager)
        result = executor.execute(rule)

        if result.aborted:
            self.logger.error(f"Inject aborted: {result.abort_reason}")
            return 1

        # ── Dry-run gate: never call report() with "modified" files list ──────
        if dry_run:
            self.logger.info("\n[DRY RUN] No files were modified.")
            return 0

        self.logger.report(
            {"modified": result.modified, "skipped": result.skipped, "errors": result.errors}
        )
        return 0 if not result.errors else 1

    # ─────────────────────────────────────────────────────────────
    # Plan execution pipeline  (NEW v3)
    # ─────────────────────────────────────────────────────────────

    def _run_plan(self) -> int:
        args = self.args

        # Print example and exit
        if getattr(args, "example", False):
            print(generate_example_plan())
            return 0

        plan_path = args.plan_file

        # Load plugins BEFORE parsing so custom types are registered for validation
        try:
            from plugins.loader import load_plugins
            _loaded = load_plugins(logger=self.logger)
            if _loaded:
                self.logger.info(f"  Plugins  : {len(_loaded)} loaded")
        except Exception as _e:
            self.logger.warning(f"Plugin loading failed: {_e}")

        # Handle --list-plugins early (before plan parse)
        if getattr(args, "list_plugins", False):
            return self._list_plugins()

        # Parse & validate plan
        try:
            plan = parse_plan(plan_path)
        except FileNotFoundError as e:
            self.logger.error(str(e))
            return 1
        except PlanValidationError as e:
            self.logger.section("PLAN VALIDATION FAILED")
            for err in e.errors:
                self.logger.error(f"  • {err}")
            return 1
        except Exception as e:
            self.logger.error(f"Failed to parse plan: {e}")
            return 1

        # Validate-only mode
        if getattr(args, "validate", False):
            self.logger.section("PLAN VALIDATION PASSED")
            self.logger.info(f"  Plan     : {plan.name}")
            self.logger.info(f"  Source   : {plan.source_path}")
            self.logger.info(f"  Rules    : {len(plan.rules)} ({len(plan.enabled_rules)} enabled)")
            self.logger.divider()
            for i, r in enumerate(plan.rules):
                status = "✔ enabled" if r.enabled else "─ disabled"
                self.logger.info(f"  [{i+1:>2}] {status:<12} {r.rule_type:<14} {r.name}")
            self.logger.divider()
            return 0

        # Execute plan
        # Handle --list-plugins
        if getattr(args, "list_plugins", False):
            return self._list_plugins()

        # v5: instantiate history + state managers
        from core.history_manager import HistoryManager
        from utils.state_manager import StateManager
        from pathlib import Path as _Path

        history_mgr  = HistoryManager(self.logger)
        state_mgr    = StateManager(self.logger)
        incremental  = getattr(args, "incremental", False)
        export_arg   = getattr(args, "export_path", None)
        export_flag  = export_arg is not None
        export_path  = None
        if export_flag and export_arg != "__auto__":
            export_path = _Path(export_arg)

        run_record = history_mgr.start_run(
            command="run",
            plan_file=str(plan_path),
            plan_name=plan.name,
            dry_run=args.dry_run,
            group=getattr(args, "group", None),
            tag=getattr(args, "tag", None),
        )

        runner = PlanRunner(
            logger=self.logger,
            backup_manager=self.backup_manager,
            dry_run=args.dry_run,
            workers=getattr(args, "workers", None),
            strict=getattr(args, "strict", True),
            yes=getattr(args, "yes", False),
            group_filter=getattr(args, "group", None),
            tag_filter=getattr(args, "tag", None),
            export=export_flag,
            export_path=export_path,
            incremental=incremental,
            history_manager=history_mgr,
            state_manager=state_mgr if incremental else None,
        )

        results  = runner.run(plan)
        aborted  = any(r.aborted for r in results)
        history_mgr.finish_run(run_record, results, aborted=aborted)

        # Exit code: 0 if all rules succeeded
        if any(r.aborted or r.errors for r in results):
            return 1
        return 0

    # ─────────────────────────────────────────────────────────────
    # Preview pipeline  (NEW v4)
    # ─────────────────────────────────────────────────────────────

    def _run_preview(self) -> int:
        args = self.args
        # Load plugins before parsing so custom types are registered
        try:
            from plugins.loader import load_plugins
            load_plugins(logger=self.logger)
        except Exception:
            pass
        try:
            plan = parse_plan(args.plan_file)
        except Exception as e:
            self.logger.error(f"Failed to parse plan: {e}")
            return 1

        from core.dependency_resolver import DependencyResolver, DependencyError
        from engines.rule_engine import _resolve_glob
        from utils.conflict_manager import ConflictManager

        candidates = plan.enabled_rules
        if getattr(args, "group", None):
            candidates = [r for r in candidates if r.group == args.group]
        if getattr(args, "tag", None):
            candidates = [r for r in candidates if args.tag in (r.tags or [])]

        # Resolve execution order
        try:
            resolver = DependencyResolver(self.logger)
            ordered  = resolver.resolve(candidates)
        except DependencyError as e:
            self.logger.section("DEPENDENCY ERROR")
            self.logger.error(str(e))
            return 1

        # Pre-resolve file counts per rule
        file_counts: dict = {}
        for rule in ordered:
            try:
                base  = Path(rule.base_dir) if rule.base_dir else None
                files = _resolve_glob(
                    rule.target, rule.exclude_dirs, rule.exclude_paths, base_dir=base
                )
                file_counts[rule.name] = len(files)
            except Exception:
                file_counts[rule.name] = 0

        # Conflict detection
        cm = ConflictManager(self.logger)
        for rule in ordered:
            try:
                base  = Path(rule.base_dir) if rule.base_dir else None
                files = _resolve_glob(
                    rule.target, rule.exclude_dirs, rule.exclude_paths, base_dir=base
                )
                cm.register_rule_targets(rule.name, files)
            except Exception:
                pass
        conflicts = cm.detect_conflicts()

        # Render preview
        self.logger.section(f"PLAN PREVIEW: {plan.name}")
        self.logger.info(f"  Source   : {plan.source_path}")
        self.logger.info(f"  Rules    : {len(plan.rules)} total, {len(ordered)} to run")

        if any(getattr(r, "depends_on", []) for r in ordered):
            self.logger.divider("·")
            self.logger.info("  DEPENDENCY GRAPH:")
            self.logger.info(resolver.visualize_graph(ordered))

        self.logger.divider("·")
        self.logger.info(
            f"  {'#':<4} {'RULE':<32} {'TYPE':<14} {'FILES':>6}  {'GROUP':<16} DEPS"
        )
        self.logger.divider("·")

        total_files = 0
        for i, rule in enumerate(ordered):
            deps     = getattr(rule, "depends_on", []) or []
            dep_str  = f"← {', '.join(deps)}" if deps else ""
            n_files  = file_counts.get(rule.name, 0)
            total_files += n_files
            group    = rule.group or ""
            self.logger.info(
                f"  [{i+1:<2}] {rule.name:<32} {rule.rule_type:<14} {n_files:>6}  {group:<16} {dep_str}"
            )

        self.logger.divider()
        self.logger.info(f"  Total files affected : ~{total_files} (may overlap between rules)")

        if conflicts:
            self.logger.divider("·")
            self.logger.warning(f"  ⚠ {len(conflicts)} file conflict(s) (same file targeted by multiple rules):")
            for line in cm.conflict_report():
                self.logger.info(line)

        self.logger.divider()
        self.logger.info("  [PREVIEW] No files were modified. Run with: aurelion run")
        return 0

    def _list_plugins(self) -> int:
        try:
            from plugins.loader import get_registry
            registry = get_registry()
            entries  = registry.all_entries()
        except Exception as e:
            self.logger.error(f"Failed to load plugins: {e}")
            return 1

        self.logger.section("LOADED PLUGINS")
        if not entries:
            self.logger.info("  No plugins found in /plugins/ directory.")
        else:
            for e in entries:
                self.logger.info(
                    f"  {e.rule_type:<20} {e.source_file.name:<30} {e.description}"
                )
        self.logger.divider()
        return 0

    # ─────────────────────────────────────────────────────────────
    # History pipeline  (NEW v5)
    # ─────────────────────────────────────────────────────────────

    def _run_history(self) -> int:
        args = self.args
        from core.history_manager import HistoryManager
        from core.report_exporter import ReportExporter
        import json as _json
        from datetime import datetime

        hm = HistoryManager(self.logger)

        # ── --clear ───────────────────────────────────────────────
        if getattr(args, "history_clear", False):
            from utils.prompt import confirm_action
            if not getattr(args, "yes", False):
                if not confirm_action("Delete ALL history records? This cannot be undone."):
                    self.logger.warning("History clear cancelled.")
                    return 1
            count = hm.clear_history()
            self.logger.success(f"Deleted {count} history record(s).")
            return 0

        # ── --stats ───────────────────────────────────────────────
        if getattr(args, "history_stats", False):
            runs = hm.list_runs(limit=9999)
            if not runs:
                self.logger.info("No history records found.")
                return 0
            total     = len(runs)
            succeeded = sum(1 for r in runs if r.get("status") == "success")
            failed    = sum(1 for r in runs if r.get("status") in ("aborted","partial","failed"))
            dry_runs  = sum(1 for r in runs if r.get("dry_run", False))
            total_dur = sum(r.get("duration", 0) for r in runs)
            total_mod = sum(r.get("totals", {}).get("files_modified", 0) for r in runs)

            self.logger.section("HISTORY STATISTICS")
            self.logger.info(f"  Total runs       : {total}")
            self.logger.info(f"  Successful       : {succeeded}")
            self.logger.info(f"  Failed/Aborted   : {failed}")
            self.logger.info(f"  Dry runs         : {dry_runs}")
            self.logger.info(f"  Total duration   : {total_dur:.1f}s")
            self.logger.info(f"  Total files mod  : {total_mod}")
            self.logger.divider()
            return 0

        # ── --show RUN_ID ──────────────────────────────────────────
        if getattr(args, "history_show", None):
            record = hm.get_run(args.history_show)
            if record is None:
                self.logger.error(f"Run not found: {args.history_show}")
                return 1
            if getattr(args, "history_json", False):
                print(_json.dumps(record, indent=2))
                return 0
            self._print_run_detail(record)
            return 0

        # ── --last ────────────────────────────────────────────────
        if getattr(args, "history_last", False):
            record = hm.last()
            if record is None:
                self.logger.info("No history records found.")
                return 0
            if getattr(args, "history_json", False):
                print(_json.dumps(record, indent=2))
                return 0
            self._print_run_detail(record)
            return 0

        # ── Default: --list ────────────────────────────────────────
        limit = getattr(args, "limit", 20)
        runs  = hm.list_runs(limit=limit)

        if not runs:
            self.logger.info("No history records found. Run a plan to populate history.")
            return 0

        if getattr(args, "history_json", False):
            print(_json.dumps(runs, indent=2))
            return 0

        self.logger.section(f"EXECUTION HISTORY  (last {len(runs)} runs)")
        self.logger.info(
            f"  {'#':<4} {'RUN ID':<26} {'STATUS':<10} {'DRY':<4} {'TIME':>7}  {'PLAN'}"
        )
        self.logger.divider("·")

        for i, r in enumerate(runs):
            status  = r.get("status", "?")
            icon    = "✔" if status == "success" else ("~" if status in ("partial","empty") else "✖")
            dry     = "Y" if r.get("dry_run") else "N"
            dur     = f"{r.get('duration', 0):.1f}s"
            plan_nm = (r.get("plan_name") or r.get("command") or "")[:35]
            ts      = r.get("timestamp", "")[:19].replace("T", " ")
            self.logger.info(
                f"  {icon} {r['run_id']:<26} {status:<10} {dry:<4} {dur:>7}  {plan_nm}"
            )

        self.logger.divider()
        self.logger.info(f"  Use 'aurelion history --last' for full detail of the most recent run.")
        return 0

    def _print_run_detail(self, record: dict) -> None:
        """Print a full run record in human-readable form."""
        self.logger.section(f"RUN DETAIL: {record.get('run_id', '?')}")
        self.logger.info(f"  Plan     : {record.get('plan_name') or '(none)'}")
        self.logger.info(f"  File     : {record.get('plan_file') or '(none)'}")
        ts = record.get('timestamp', '')[:19].replace('T', ' ')
        self.logger.info(f"  Time     : {ts}")
        self.logger.info(f"  Status   : {record.get('status', '?')}")
        self.logger.info(f"  Duration : {record.get('duration', 0):.3f}s")
        self.logger.info(f"  Dry run  : {'yes' if record.get('dry_run') else 'no'}")

        totals = record.get("totals", {})
        self.logger.divider("·")
        self.logger.info(f"  Rules executed   : {totals.get('rules_run', 0)}")
        self.logger.info(f"  Rules succeeded  : {totals.get('rules_ok', 0)}")
        self.logger.info(f"  Rules failed     : {totals.get('rules_failed', 0)}")
        self.logger.info(f"  Files modified   : {totals.get('files_modified', 0)}")
        self.logger.info(f"  Files skipped    : {totals.get('files_skipped', 0)}")
        self.logger.info(f"  Errors           : {totals.get('errors', 0)}")

        rules = record.get("rules", [])
        if rules:
            self.logger.divider("·")
            self.logger.info(
                f"  {'RULE':<32} {'STATUS':<10} {'MOD':>5} {'ERR':>5} {'TIME':>7}"
            )
            self.logger.divider("·")
            for r in rules:
                status = r.get("status", "?")
                icon   = "✔" if status == "success" else "✖"
                self.logger.info(
                    f"  {icon} {r['name']:<30} {status:<10} "
                    f"{r.get('modified',0):>5} {r.get('errors',0):>5} "
                    f"{r.get('duration',0):>6.3f}s"
                )
        self.logger.divider()


    # ─────────────────────────────────────────────────────────────
    # AI Planner pipeline  (NEW v6)
    # ─────────────────────────────────────────────────────────────

    def _run_ai(self) -> int:
        args = self.args
        prompt      = getattr(args, "prompt", "").strip()
        save_path   = getattr(args, "save_path", None)
        run_after   = getattr(args, "run", False)
        explain     = getattr(args, "explain", False)
        context_dir = getattr(args, "context_dir", None)

        if not prompt:
            self.logger.error("AI prompt cannot be empty.")
            return 1

        from core.ai_planner import AIPlanner, AIPlannerError

        planner = AIPlanner(self.logger)
        self.logger.section("AI PLAN GENERATOR")
        self.logger.info(f"  Prompt : {prompt[:80]}")
        if context_dir:
            self.logger.info(f"  Context: {context_dir}")

        try:
            from pathlib import Path as _Path
            ctx = _Path(context_dir) if context_dir else None
            plan = planner.generate_plan_from_text(prompt, context_dir=ctx)
        except AIPlannerError as e:
            self.logger.error(f"AI generation failed: {e}")
            return 1
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return 1

        # Show generated plan
        self.logger.divider("·")
        self.logger.info(f"  Plan     : {plan.name}")
        self.logger.info(f"  Rules    : {len(plan.rules)}")
        self.logger.divider("·")
        for i, r in enumerate(plan.rules):
            self.logger.info(f"  [{i+1}] {r.name:<30} ({r.rule_type})  target={r.target}")

        # AI explanation
        if explain:
            self.logger.divider("·")
            self.logger.info("  AI EXPLANATION:")
            explanation = planner.explain_plan(plan)
            for line in explanation.split("\n"):
                self.logger.info(f"    {line}")

        # Save to file
        if save_path:
            toml_content = planner._plan_to_toml(plan)
            save = _Path(save_path)
            save.write_text(toml_content, encoding="utf-8")
            self.logger.success(f"  Plan saved → {save}")

        # Execute immediately
        if run_after:
            self.logger.divider()
            self.logger.info("  Executing generated plan...")
            from core.plan_runner import PlanRunner
            from core.history_manager import HistoryManager
            from utils.state_manager import StateManager
            hm = HistoryManager(self.logger)
            sm = StateManager(self.logger)
            run_record = hm.start_run(
                command="ai-run",
                plan_name=plan.name,
                dry_run=args.dry_run,
            )
            runner = PlanRunner(
                logger=self.logger,
                backup_manager=self.backup_manager,
                dry_run=getattr(args, "dry_run", False),
                yes=getattr(args, "yes", False),
            )
            results = runner.run(plan)
            aborted = any(r.aborted for r in results)
            hm.finish_run(run_record, results, aborted=aborted)

        return 0

    # ─────────────────────────────────────────────────────────────
    # Dashboard pipeline  (NEW v6)
    # ─────────────────────────────────────────────────────────────

    def _run_dashboard(self) -> int:
        from core.dashboard import Dashboard
        from core.history_manager import HistoryManager
        from utils.state_manager import StateManager

        hm = HistoryManager(self.logger)
        sm = StateManager(self.logger)

        profile_name = getattr(self.args, "profile", None)
        profile = self._profile
        if not profile and profile_name:
            try:
                from core.profile_manager import ProfileManager
                profile = ProfileManager().load(profile_name)
            except Exception:
                pass

        db = Dashboard(self.logger, history_manager=hm, state_manager=sm, profile=profile)
        db.render()
        return 0

    # ─────────────────────────────────────────────────────────────
    # Server pipeline  (NEW v6)
    # ─────────────────────────────────────────────────────────────

    def _run_server(self) -> int:
        from core.server import AurelionServer
        host = getattr(self.args, "host", "127.0.0.1")
        port = getattr(self.args, "port", 7070)
        srv  = AurelionServer(host=host, port=port, logger=self.logger)
        srv.start()
        return 0

    # ─────────────────────────────────────────────────────────────
    # Plugins pipeline  (NEW v6)
    # ─────────────────────────────────────────────────────────────

    def _run_plugins(self) -> int:
        from plugins.manager import MarketplaceManager
        mm     = MarketplaceManager(self.logger)
        action = getattr(self.args, "plugins_action", "list")

        if action == "list":
            plugins = mm.list()
            self.logger.section("INSTALLED PLUGINS")
            if not plugins:
                self.logger.info("  No plugins installed.")
            else:
                self.logger.info(
                    f"  {'NAME':<22} {'RULE TYPE':<16} {'VER':<8} {'STATUS':<10} DESCRIPTION"
                )
                self.logger.divider("·")
                for p in plugins:
                    status = "enabled" if p.enabled else "disabled"
                    self.logger.info(
                        f"  {p.name:<22} {p.rule_type:<16} {p.version:<8} "
                        f"{status:<10} {p.description[:35]}"
                    )
            self.logger.divider()

        elif action == "install":
            try:
                mm.install(
                    self.args.source,
                    name=getattr(self.args, "name", None),
                    version=getattr(self.args, "version", "1.0.0"),
                )
            except Exception as e:
                self.logger.error(f"Install failed: {e}")
                return 1

        elif action == "remove":
            if not mm.remove(self.args.name):
                self.logger.warning(f"Plugin not found: {self.args.name}")
                return 1

        elif action == "enable":
            mm.enable(self.args.name)

        elif action == "disable":
            mm.disable(self.args.name)

        elif action == "info":
            rec = mm.info(self.args.name)
            if not rec:
                self.logger.error(f"Plugin not found: {self.args.name}")
                return 1
            self.logger.section(f"PLUGIN: {rec.name}")
            self.logger.info(f"  Description  : {rec.description}")
            self.logger.info(f"  Rule type    : {rec.rule_type}")
            self.logger.info(f"  Version      : {rec.version}")
            self.logger.info(f"  Source       : {rec.source}")
            self.logger.info(f"  Installed    : {rec.installed_at[:19]}")
            self.logger.info(f"  File         : {rec.file}")
            self.logger.info(f"  Enabled      : {rec.enabled}")
            self.logger.divider()

        return 0

    # ─────────────────────────────────────────────────────────────
    # Profile pipeline  (NEW v6)
    # ─────────────────────────────────────────────────────────────

    def _run_profile(self) -> int:
        from core.profile_manager import ProfileManager
        pm     = ProfileManager(logger=self.logger)
        action = getattr(self.args, "profile_action", "list")

        if action == "list":
            profiles = pm.list()
            self.logger.section("AVAILABLE PROFILES")
            for name in profiles:
                try:
                    p = pm.load(name)
                    self.logger.info(f"  {name:<20} {p.description}")
                except Exception:
                    self.logger.info(f"  {name}")
            self.logger.divider()

        elif action == "show":
            try:
                p = pm.load(self.args.name)
                self.logger.section(f"PROFILE: {p.name}")
                self.logger.info(f"  Description : {p.description}")
                self.logger.info(f"  Source      : {p.source_path}")
                self.logger.divider("·")
                for key, val in p.config.items():
                    self.logger.info(f"  {key:<20} {val}")
                self.logger.divider()
            except FileNotFoundError as e:
                self.logger.error(str(e))
                return 1

        elif action == "create":
            try:
                pm.create(
                    self.args.name,
                    description=getattr(self.args, "description", ""),
                    base=getattr(self.args, "base", "default"),
                )
            except Exception as e:
                self.logger.error(f"Create failed: {e}")
                return 1

        elif action == "delete":
            if not getattr(self.args, "yes", False):
                from utils.prompt import confirm_action
                if not confirm_action(f"Delete profile '{self.args.name}'?"):
                    self.logger.warning("Cancelled.")
                    return 1
            try:
                pm.delete(self.args.name)
            except PermissionError as e:
                self.logger.error(str(e))
                return 1

        return 0


    # ─────────────────────────────────────────────────────────────
    # Auth pipeline  (NEW v7)
    # ─────────────────────────────────────────────────────────────

    def _run_auth(self) -> int:
        from core.auth import AuthConfig, AuthMiddleware, generate_api_key, _load_dotenv
        _load_dotenv()
        action = getattr(self.args, "auth_action", "status")

        if action == "generate-key":
            prefix = getattr(self.args, "prefix", "aur")
            key    = generate_api_key(prefix)
            self.logger.section("GENERATED API KEY")
            self.logger.info(f"  Key    : {key}")
            self.logger.info(f"  Add to : .env  →  AURELION_API_KEY={key}")
            self.logger.divider()
            return 0

        elif action == "status":
            cfg = AuthConfig()
            self.logger.section("AUTH CONFIGURATION")
            self.logger.info(f"  Auth enabled   : {'YES' if cfg.auth_enabled else 'NO (set AURELION_API_KEY)'}")
            self.logger.info(f"  Valid tokens   : {len(cfg.valid_tokens)}")
            self.logger.info(f"  Rate limit     : {cfg.rate_limit} req/min per IP")
            self.logger.info(f"  Max body       : {cfg.max_body // 1024} KB")
            self.logger.info(f"  Public paths   : {', '.join(sorted(cfg.public_paths))}")
            self.logger.divider()
            return 0

        return 0

    # ─────────────────────────────────────────────────────────────
    # Database pipeline  (NEW v7)
    # ─────────────────────────────────────────────────────────────

    def _run_db(self) -> int:
        from core.db import Database, DB_PATH
        action = getattr(self.args, "db_action", "stats")

        with Database(logger=self.logger) as db:
            if not db.available:
                self.logger.error("SQLite database unavailable.")
                return 1

            if action == "migrate":
                self.logger.section("DB MIGRATION")
                counts = db.migrate_from_json()
                self.logger.success(f"  Migrated: {counts.get('runs', 0)} runs, {counts.get('hashes', 0)} file hashes")
                self.logger.divider()

            elif action == "stats":
                self.logger.section("DATABASE STATS")
                run_count  = db.fetchone("SELECT COUNT(*) as c FROM runs") or {}
                hash_count = db.fetchone("SELECT COUNT(*) as c FROM file_hashes") or {}
                ai_count   = db.fetchone("SELECT COUNT(*) as c FROM ai_prompts") or {}
                kv_count   = db.fetchone("SELECT COUNT(*) as c FROM kv") or {}
                db_size    = DB_PATH.stat().st_size // 1024 if DB_PATH.exists() else 0
                self.logger.info(f"  DB path        : {DB_PATH}")
                self.logger.info(f"  DB size        : {db_size} KB")
                self.logger.info(f"  Run records    : {run_count.get('c', 0)}")
                self.logger.info(f"  File hashes    : {hash_count.get('c', 0)}")
                self.logger.info(f"  AI prompts     : {ai_count.get('c', 0)}")
                self.logger.info(f"  KV entries     : {kv_count.get('c', 0)}")
                self.logger.divider()

            elif action == "export":
                from pathlib import Path as _P
                out  = _P(getattr(self.args, "output", "aurelion_history.csv"))
                rows = db.export_runs_csv(out)
                self.logger.success(f"  Exported {rows} rows → {out}")

            elif action == "reset":
                from utils.prompt import confirm_action
                if not confirm_action("This will DELETE all database records. Confirm?"):
                    return 1
                db.execute("DROP TABLE IF EXISTS runs")
                db.execute("DROP TABLE IF EXISTS file_hashes")
                db.execute("DROP TABLE IF EXISTS ai_prompts")
                db.execute("DROP TABLE IF EXISTS kv")
                db.execute("DROP TABLE IF EXISTS schema_version")
                # Reopen to recreate schema
                self.logger.success("Database reset complete.")

        return 0

    # ─────────────────────────────────────────────────────────────
    # Restore pipeline
    # ─────────────────────────────────────────────────────────────

    def _run_restore(self) -> int:
        args = self.args

        if getattr(args, "restore_list", False):
            sessions = self.backup_manager.list_sessions()
            if not sessions:
                self.logger.info("No backup sessions found in ./backups/")
                return 0
            self.logger.section("AVAILABLE BACKUP SESSIONS")
            for i, s in enumerate(sessions):
                tag = "  ← latest" if i == 0 else ""
                self.logger.info(f"  [{i+1:>2}]  {s}{tag}")
            self.logger.divider()
            return 0

        if getattr(args, "restore_last", False):
            session_dir = self.backup_manager.get_latest_session()
            if session_dir is None:
                self.logger.error("No backup sessions found. Nothing to restore.")
                return 1
        else:
            session_dir = Path(args.restore_session)

        self.logger.section(f"RESTORE FROM: {session_dir.name}")

        manifest_path = session_dir / "aurelion_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.logger.info(f"  Session contains {len(manifest)} file(s):")
            for _, orig in list(manifest.items())[:10]:
                self.logger.info(f"    ← {orig}")
            if len(manifest) > 10:
                self.logger.info(f"    … and {len(manifest) - 10} more")
        else:
            self.logger.warning("No manifest found — legacy restore mode.")

        self.logger.divider()

        if not getattr(args, "yes", False):
            if not confirm_action(
                f"Restore from '{session_dir.name}'? This OVERWRITES current files."
            ):
                self.logger.warning("Restore cancelled.")
                return 1

        restored = self.backup_manager.restore_session(session_dir)
        return 0 if restored > 0 else 1

    # ─────────────────────────────────────────────────────────────
    # Display helpers
    # ─────────────────────────────────────────────────────────────

    def _display_diff_preview(self, matches, args, engine) -> None:
        dry   = args.dry_run
        tag   = "[DRY RUN] " if dry else ""
        total = sum(m["count"] for m in matches)

        self.logger.section(f"{tag}DIFF PREVIEW")
        self.logger.info(f"  Search  : '{args.old_text}'  →  '{args.new_text}'")
        self.logger.info(f"  Case    : {'Sensitive' if args.case_sensitive else 'Insensitive'}")
        self.logger.info(f"  Matched : {len(matches)} file(s) | {total} occurrence(s)")
        self.logger.divider()
        for m in matches:
            render_diff(m, self.logger, engine._pattern, args.new_text)
        self.logger.divider()

    def _display_file_op_preview(self, op, source, targets, args) -> None:
        tag = "[DRY RUN] " if args.dry_run else ""
        self.logger.section(f"{tag}{op} PREVIEW")
        self.logger.info(f"  Source : {source}")
        self.logger.info(f"  Targets: {len(targets)} file(s)")
        self.logger.divider()
        for t in targets:
            status = "EXISTS" if t.exists() else "NEW"
            action = "SKIP" if (t.exists() and not args.overwrite) else "WRITE"
            self.logger.preview_file(t, status, action)
        self.logger.divider()
