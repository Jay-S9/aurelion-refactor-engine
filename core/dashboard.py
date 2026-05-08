"""
Aurelion Refactor Engine v6 - Dashboard
Renders a rich, information-dense CLI dashboard using pure ANSI sequences.
No third-party dependencies — built entirely on the existing logger infrastructure.

Dashboard sections:
  ┌─ SYSTEM STATUS ─────────────────────────────────────────────────┐
  │ Version · Active profile · State tracked files · Last run       │
  ├─ RECENT RUNS ────────────────────────────────────────────────────┤
  │ Last 5 runs with status icons, plan name, duration, file counts │
  ├─ STATISTICS ─────────────────────────────────────────────────────┤
  │ Total runs · Success rate · Total files modified · Avg duration │
  ├─ TOP MODIFIED FILES ──────────────────────────────────────────────┤
  │ Files that appear most often in modification lists              │
  ├─ PERFORMANCE SUMMARY ─────────────────────────────────────────────┤
  │ Avg rule duration · Fastest/slowest rule ever recorded          │
  └─────────────────────────────────────────────────────────────────┘

NEW IN v6:
  - Dashboard.render() — full dashboard render to console
  - Dashboard.render_section(name) — render a single section
  - _top_modified_files() — aggregate across all run records
  - _perf_summary() — aggregate timing across all runs
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class Dashboard:
    """
    CLI-based execution dashboard.
    Reads from HistoryManager and StateManager to build a live snapshot.
    """

    WIDTH = 62

    def __init__(self, logger, history_manager=None, state_manager=None, profile=None):
        self._logger  = logger
        self._hm      = history_manager
        self._sm      = state_manager
        self._profile = profile

    # ──────────────────────────────────────────────────────────────
    # Public
    # ──────────────────────────────────────────────────────────────

    def render(self) -> None:
        """Render the full dashboard to the console."""
        from core.logger import _C

        self._header("AURELION REFACTOR ENGINE  v7.0.1  —  DASHBOARD")
        self._render_status()
        self._render_recent_runs()
        self._render_statistics()
        self._render_top_files()
        self._render_performance()
        self._footer()

    # ──────────────────────────────────────────────────────────────
    # Sections
    # ──────────────────────────────────────────────────────────────

    def _render_status(self) -> None:
        from core.logger import _C

        profile_name = self._profile.name if self._profile else "default"
        tracked      = self._sm.stats().get("tracked_files", 0) if self._sm else "—"

        runs    = self._hm.list_runs(limit=1) if self._hm else []
        last_ts = runs[0]["timestamp"][:19].replace("T", " ") if runs else "never"

        self._section_header("SYSTEM STATUS")
        self._row("Version",         "v7.0.1")
        self._row("Active profile",  profile_name)
        self._row("Tracked files",   str(tracked))
        self._row("Last run",        last_ts)

    def _render_recent_runs(self) -> None:
        from core.logger import _C

        runs = self._hm.list_runs(limit=5) if self._hm else []
        self._section_header("RECENT RUNS  (last 5)")

        if not runs:
            self._line("  No runs recorded yet.")
            return

        # Header row
        self._line(
            self._c(_C.GREY,
                f"  {'STATUS':<10} {'PLAN':<26} {'TIME':>6}  {'MOD':>5}  DATE"
            )
        )
        self._divider("·")

        for r in runs:
            status   = r.get("status", "?")
            icon     = ("✔" if status == "success"
                        else ("~" if status in ("partial", "empty")
                              else "✖"))
            icon_col = (_C.GREEN if icon == "✔"
                        else (_C.YELLOW if icon == "~" else _C.RED))
            icon_str = self._c(icon_col + _C.BOLD, icon)

            dry_tag  = self._c(_C.GREY, " [dry]") if r.get("dry_run") else ""
            plan     = (r.get("plan_name") or r.get("command") or "")[:24]
            dur      = f"{r.get('duration', 0):.1f}s"
            modified = r.get("totals", {}).get("files_modified", 0)
            ts       = r.get("timestamp", "")[:10]

            self._line(
                f"  {icon_str} {status:<9}{dry_tag}  "
                f"{self._c(_C.CYAN, plan):<26}  "
                f"{dur:>6}  {modified:>5}  {ts}"
            )

    def _render_statistics(self) -> None:
        from core.logger import _C

        runs = self._hm.list_runs(limit=9999) if self._hm else []
        self._section_header("STATISTICS")

        if not runs:
            self._line("  No data yet.")
            return

        total      = len(runs)
        succeeded  = sum(1 for r in runs if r.get("status") == "success")
        failed     = total - succeeded
        dry_runs   = sum(1 for r in runs if r.get("dry_run"))
        success_rt = f"{(succeeded / total * 100):.0f}%" if total else "—"
        total_mod  = sum(r.get("totals", {}).get("files_modified", 0) for r in runs)
        total_dur  = sum(r.get("duration", 0) for r in runs)
        avg_dur    = total_dur / total if total else 0

        self._row("Total runs",        str(total))
        self._row("Success rate",      success_rt)
        self._row("Succeeded",         str(succeeded))
        self._row("Failed / aborted",  str(failed))
        self._row("Dry runs",          str(dry_runs))
        self._row("Files modified",    str(total_mod))
        self._row("Total exec time",   f"{total_dur:.1f}s")
        self._row("Avg run duration",  f"{avg_dur:.2f}s")

    def _render_top_files(self) -> None:
        from core.logger import _C

        if not self._hm:
            return

        counter: Counter = Counter()
        for run in self._hm.list_runs(limit=50):
            record = self._hm.get_run(run["run_id"])
            if not record:
                continue
            for rule in record.get("rules", []):
                for fpath in rule.get("files_modified", []):
                    counter[Path(fpath).name] += 1

        self._section_header("TOP MODIFIED FILES  (by filename, last 50 runs)")

        if not counter:
            self._line("  No file modification data yet.")
            return

        for fname, count in counter.most_common(8):
            bar_filled = min(int(count / max(counter.most_common(1)[0][1], 1) * 20), 20)
            bar        = "▓" * bar_filled + "░" * (20 - bar_filled)
            self._line(
                f"  {self._c(_C.CYAN, fname):<35}  "
                f"{count:>3}×  [{self._c(_C.GREEN, bar)}]"
            )

    def _render_performance(self) -> None:
        from core.logger import _C

        if not self._hm:
            return

        slowest_rule: Tuple[str, float] = ("—", 0.0)
        fastest_rule: Tuple[str, float] = ("—", 9999.0)
        all_durations: List[float]      = []

        for run in self._hm.list_runs(limit=30):
            record = self._hm.get_run(run["run_id"])
            if not record:
                continue
            for rule in record.get("rules", []):
                dur  = rule.get("duration", 0.0)
                name = rule.get("name", "?")
                all_durations.append(dur)
                if dur > slowest_rule[1]:
                    slowest_rule = (name, dur)
                if dur < fastest_rule[1] and dur > 0:
                    fastest_rule = (name, dur)

        self._section_header("PERFORMANCE SUMMARY  (last 30 runs)")

        if not all_durations:
            self._line("  No timing data yet.")
            self._divider()
            return

        avg_rule_dur = sum(all_durations) / len(all_durations)

        if fastest_rule[1] == 9999.0:
            fastest_rule = ("—", 0.0)

        self._row("Avg rule duration",  f"{avg_rule_dur:.3f}s")
        self._row("Slowest rule ever",  f"{slowest_rule[0]}  ({slowest_rule[1]:.3f}s)")
        self._row("Fastest rule ever",  f"{fastest_rule[0]}  ({fastest_rule[1]:.3f}s)")
        self._row("Rule executions",    str(len(all_durations)))

        self._divider()

    # ──────────────────────────────────────────────────────────────
    # Rendering helpers
    # ──────────────────────────────────────────────────────────────

    def _header(self, title: str) -> None:
        from core.logger import _C
        border = "═" * self.WIDTH
        inner  = f"  {title}"
        self._line("")
        self._line(self._c(_C.CYAN + _C.BOLD, f"╔{border}╗"))
        self._line(self._c(_C.CYAN + _C.BOLD, f"║  {title:<{self.WIDTH - 2}}║"))
        self._line(self._c(_C.CYAN + _C.BOLD, f"╚{border}╝"))

    def _footer(self) -> None:
        from core.logger import _C
        self._line(self._c(_C.DIM, "─" * (self.WIDTH + 2)))
        self._line(self._c(_C.GREY, "  aurelion history · aurelion run · aurelion ai"))
        self._line("")

    def _section_header(self, title: str) -> None:
        from core.logger import _C
        self._divider()
        self._line(self._c(_C.YELLOW + _C.BOLD, f"  ▸ {title}"))
        self._divider("·")

    def _row(self, label: str, value: str) -> None:
        from core.logger import _C
        lbl = self._c(_C.GREY,  f"  {label:<22}")
        val = self._c(_C.WHITE, value)
        self._line(f"{lbl}{val}")

    def _line(self, text: str) -> None:
        self._logger._console(text)
        self._logger._file(text.replace("\033[", "").replace("m", "", 1), "INFO")

    def _divider(self, char: str = "─") -> None:
        from core.logger import _C
        self._line(self._c(_C.DIM, char * (self.WIDTH + 2)))

    def _c(self, codes: str, text: str) -> str:
        return self._logger._c(codes, text)
