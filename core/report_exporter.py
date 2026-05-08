"""
Aurelion Refactor Engine v5 - Report Exporter
Converts execution results into machine-readable JSON reports.

The exporter produces self-contained reports that can be:
  - Ingested by CI/CD pipelines
  - Stored as audit artifacts
  - Fed into dashboards or monitoring systems
  - Diffed between runs for regression tracking

Export schema (report.json):
  {
    "meta": {
      "aurelion_version": "5.0.0",
      "exported_at": "2026-04-14T15:23:01Z",
      "run_id": "20260414-152301-abc123",
      "plan_file": "/path/to/plan.toml",
      "plan_name": "My Migration",
      "dry_run": false
    },
    "summary": {
      "status": "success",
      "duration_seconds": 4.231,
      "rules_total": 6,
      "rules_ok": 5,
      "rules_failed": 1,
      "files_modified": 42,
      "files_skipped": 8,
      "errors": 0
    },
    "rules": [
      {
        "name": "update-api",
        "type": "replace",
        "group": "core",
        "status": "success",
        "duration_seconds": 1.05,
        "files_modified": ["src/app.py", "src/auth.py"],
        "files_skipped": [],
        "errors": [],
        "scan_stats": {"scanned": 3, "matched": 2}
      }
    ],
    "performance": {
      "slowest_rule": {"name": "...", "duration": 2.1},
      "files_per_second": 10.2,
      "total_files_processed": 45
    }
  }

NEW IN v5:
  - ReportExporter.export()      — write JSON report to file
  - ReportExporter.to_dict()     — build report dict (for programmatic use)
  - ReportExporter.print_perf()  — print performance summary to console
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from engines.rule_engine import RuleResult

VERSION = "5.0.0"


class ReportExporter:
    """
    Builds and writes machine-readable execution reports.

    Usage:
        exporter = ReportExporter(run_record, results, logger)
        path = exporter.export()          # writes to history/exports/
        data = exporter.to_dict()         # in-memory dict
        exporter.print_perf()             # prints to console
    """

    EXPORTS_DIR = Path("history") / "exports"

    def __init__(
        self,
        run_record:  Dict[str, Any],
        results:     List[RuleResult],
        logger=None,
        plan=None,                    # optional Plan object for extra metadata
    ):
        self._record  = run_record
        self._results = results
        self._logger  = logger
        self._plan    = plan

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    def export(self, output_path: Optional[Path] = None) -> Path:
        """
        Serialize the report to JSON and write to disk.

        Args:
            output_path: Optional explicit path. Defaults to
                         history/exports/{run_id}.json

        Returns:
            Path to the written report file.
        """
        self.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

        if output_path is None:
            run_id      = self._record.get("run_id", "unknown")
            output_path = self.EXPORTS_DIR / f"{run_id}.json"

        data = self.to_dict()
        self._atomic_write(output_path, data)

        if self._logger:
            self._logger.success(f"Report exported → {output_path}")

        return output_path

    def to_dict(self) -> Dict[str, Any]:
        """Build the complete report dictionary (no I/O)."""
        now = datetime.now(tz=timezone.utc).isoformat()

        return {
            "meta":        self._build_meta(now),
            "summary":     self._build_summary(),
            "rules":       self._build_rules(),
            "performance": self._build_performance(),
        }

    def print_perf(self) -> None:
        """Print a performance summary table to the console."""
        if not self._logger:
            return

        results = self._results
        if not results:
            return

        total_modified = sum(len(r.modified) for r in results)
        total_duration = sum(r.duration_seconds for r in results)
        fps            = total_modified / total_duration if total_duration > 0 else 0.0
        slowest        = max(results, key=lambda r: r.duration_seconds)

        self._logger.section("PERFORMANCE METRICS")
        self._logger.info(f"  {'Total duration':<30} {total_duration:.3f}s")
        self._logger.info(f"  {'Files modified':<30} {total_modified}")
        self._logger.info(f"  {'Files/second (modified)':<30} {fps:.1f}")
        self._logger.info(f"  {'Slowest rule':<30} {slowest.rule_name}  ({slowest.duration_seconds:.3f}s)")
        self._logger.divider("·")

        # Per-rule timing bar chart (normalized to longest rule)
        max_dur = max((r.duration_seconds for r in results), default=1.0)
        bar_width = 20
        self._logger.info(f"  {'Rule':<32} {'Time':>7}  {'Bar'}")
        for r in sorted(results, key=lambda r: r.duration_seconds, reverse=True):
            dur    = r.duration_seconds
            filled = int((dur / max(max_dur, 0.001)) * bar_width)
            bar    = "▓" * filled + "░" * (bar_width - filled)
            self._logger.info(
                f"  {r.rule_name:<32} {dur:>6.3f}s  [{bar}]"
            )
        self._logger.divider()

    # ──────────────────────────────────────────────────────────────
    # Builder methods
    # ──────────────────────────────────────────────────────────────

    def _build_meta(self, now: str) -> Dict[str, Any]:
        r = self._record
        return {
            "aurelion_version": VERSION,
            "exported_at":      now,
            "run_id":           r.get("run_id"),
            "plan_file":        r.get("plan_file"),
            "plan_name":        r.get("plan_name"),
            "dry_run":          r.get("dry_run", False),
            "command":          r.get("command"),
            "group_filter":     r.get("group"),
            "tag_filter":       r.get("tag"),
            "environment":      r.get("environment", {}),
        }

    def _build_summary(self) -> Dict[str, Any]:
        results  = self._results
        totals   = self._record.get("totals", {})
        duration = self._record.get("duration", 0.0)
        return {
            "status":           self._record.get("status", "unknown"),
            "duration_seconds": duration,
            "rules_total":      len(results),
            "rules_ok":         totals.get("rules_ok",       0),
            "rules_failed":     totals.get("rules_failed",   0),
            "files_modified":   totals.get("files_modified", 0),
            "files_skipped":    totals.get("files_skipped",  0),
            "errors":           totals.get("errors",          0),
        }

    def _build_rules(self) -> List[Dict[str, Any]]:
        """Build per-rule detailed section."""
        rule_map: Dict[str, Any] = {}

        # If plan is available, pull in group/tags
        if self._plan:
            for rule in self._plan.rules:
                rule_map[rule.name] = {
                    "group": rule.group,
                    "tags":  rule.tags or [],
                }

        entries = []
        for r in self._results:
            status = (
                "aborted" if r.aborted
                else ("failed" if r.errors else "success")
            )
            meta  = rule_map.get(r.rule_name, {})
            entry = {
                "name":             r.rule_name,
                "type":             r.rule_type,
                "group":            meta.get("group", ""),
                "tags":             meta.get("tags", []),
                "status":           status,
                "duration_seconds": r.duration_seconds,
                "files_modified":   r.modified,
                "files_skipped":    r.skipped,
                "errors":           [
                    {"file": e[0], "reason": e[1]}
                    for e in r.errors
                ],
                "scan_stats":       r.scan_stats or {},
            }
            if r.aborted:
                entry["abort_reason"] = r.abort_reason
            entries.append(entry)

        return entries

    def _build_performance(self) -> Dict[str, Any]:
        results = self._results
        if not results:
            return {}

        total_modified = sum(len(r.modified) for r in results)
        total_duration = sum(r.duration_seconds for r in results)
        fps            = total_modified / total_duration if total_duration > 0 else 0.0

        slowest  = max(results, key=lambda r: r.duration_seconds)
        fastest  = min(results, key=lambda r: r.duration_seconds)

        return {
            "total_duration_seconds":     total_duration,
            "total_files_processed":      total_modified + sum(len(r.skipped) for r in results),
            "files_modified_per_second":  round(fps, 2),
            "slowest_rule": {
                "name":     slowest.rule_name,
                "duration": slowest.duration_seconds,
            },
            "fastest_rule": {
                "name":     fastest.rule_name,
                "duration": fastest.duration_seconds,
            },
            "rule_timings": [
                {"name": r.rule_name, "duration": r.duration_seconds}
                for r in sorted(results, key=lambda r: r.duration_seconds, reverse=True)
            ],
        }

    def _atomic_write(self, path: Path, data: Dict[str, Any]) -> None:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(dir=parent, suffix=".tmp")
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
