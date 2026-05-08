"""
Aurelion Refactor Engine v2 - Logger
Provides structured, colored console output and persistent file logging.

CHANGES IN v2:
  - report() now accepts an optional scan_stats dict for full summary:
      Total scanned / matched / modified / skipped / binary-skipped / errors
  - scan_summary() method for printing scan stats before the operation
  - preview_match() removed limit of 3 context lines (caller controls via diff_renderer)
  - binary_skipped() dedicated log method
  - _file() no longer crashes if logger is not initialised (defensive guard)
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── ANSI colour palette ────────────────────────────────────────────────────────
class _C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    GREY    = "\033[90m"


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


class AurelionLogger:
    """
    Dual-output logger:
      • Console  – coloured, human-readable
      • Log file – plain-text, timestamped (logs/aurelion_YYYY-MM-DD.log)
    """

    LOG_DIR = Path("logs")

    def __init__(self, log_to_file: bool = True):
        self._color = _supports_color()
        self._file_logger: Optional[logging.Logger] = None

        if log_to_file:
            self._init_file_logger()

        self._session_stats = {
            "modified": 0,
            "skipped": 0,
            "errors": 0,
        }

    # ──────────────────────────────────────────────────────────────
    # Core log methods
    # ──────────────────────────────────────────────────────────────

    def info(self, msg: str) -> None:
        self._console(msg)
        self._file(msg, "INFO")

    def success(self, msg: str) -> None:
        prefix = self._c(_C.GREEN + _C.BOLD, "✔ ")
        self._console(f"{prefix}{msg}")
        self._file(f"[OK] {msg}", "INFO")
        self._session_stats["modified"] += 1

    def warning(self, msg: str) -> None:
        prefix = self._c(_C.YELLOW + _C.BOLD, "⚠ ")
        self._console(f"{prefix}{msg}")
        self._file(f"[WARN] {msg}", "WARNING")

    def error(self, msg: str, exc_info: bool = False) -> None:
        prefix = self._c(_C.RED + _C.BOLD, "✖ ")
        self._console(f"{prefix}{msg}", file=sys.stderr)
        self._file(f"[ERROR] {msg}", "ERROR", exc_info=exc_info)
        self._session_stats["errors"] += 1

    def skipped(self, msg: str) -> None:
        prefix = self._c(_C.GREY, "─ ")
        self._console(f"{prefix}{msg}")
        self._file(f"[SKIP] {msg}", "INFO")
        self._session_stats["skipped"] += 1

    def binary_skipped(self, path: Path, reason: str) -> None:
        """Dedicated log entry for binary-guarded files."""
        tag  = self._c(_C.MAGENTA, "[BINARY] ")
        name = self._c(_C.GREY, str(path))
        self._console(f"  {tag}{name}  ({reason})")
        self._file(f"[BINARY] {path}  ({reason})", "INFO")

    # ──────────────────────────────────────────────────────────────
    # Structural helpers
    # ──────────────────────────────────────────────────────────────

    def section(self, title: str) -> None:
        line = self._c(_C.CYAN + _C.BOLD, f"\n{'═' * 54}\n  {title}\n{'═' * 54}")
        self._console(line)
        self._file(f"\n{'='*54}\n  {title}\n{'='*54}", "INFO")

    def divider(self, char: str = "─", width: int = 54) -> None:
        line = self._c(_C.DIM, char * width)
        self._console(line)
        self._file(char * width, "INFO")

    def preview_match(self, match: dict) -> None:
        """
        Print a compact match summary (file + count).
        Full line diff is handled by diff_renderer.render_diff().
        """
        file_str  = self._c(_C.CYAN, str(match["file"]))
        count_str = self._c(_C.YELLOW + _C.BOLD, f"×{match['count']}")
        self._console(f"  {file_str}  {count_str}")
        self._file(f"  MATCH: {match['file']} (×{match['count']})", "INFO")

    def preview_file(self, path: Path, status: str, action: str) -> None:
        status_color = _C.YELLOW if status == "EXISTS" else _C.GREEN
        action_color = _C.RED    if action == "SKIP"   else _C.GREEN

        path_str   = self._c(_C.CYAN, str(path))
        status_str = self._c(status_color, f"[{status}]")
        action_str = self._c(action_color + _C.BOLD, action)
        self._console(f"  {path_str}  {status_str}  →  {action_str}")
        self._file(f"  FILE: {path} [{status}] → {action}", "INFO")

    # ──────────────────────────────────────────────────────────────
    # v2: Scan summary (printed before diff preview)
    # ──────────────────────────────────────────────────────────────

    def scan_summary(self, scan_stats: dict, total_files: int) -> None:
        """
        Print a one-line scan summary before showing diff previews.
        scan_stats keys: scanned, matched, skipped_binary, skipped_encoding
        """
        scanned  = scan_stats.get("scanned", total_files)
        matched  = scan_stats.get("matched", 0)
        s_bin    = scan_stats.get("skipped_binary", 0)
        s_enc    = scan_stats.get("skipped_encoding", 0)

        parts = [
            self._c(_C.WHITE,  f"Scanned: {scanned}"),
            self._c(_C.GREEN,  f"Matched: {matched}"),
        ]
        if s_bin:
            parts.append(self._c(_C.MAGENTA, f"Binary-skipped: {s_bin}"))
        if s_enc:
            parts.append(self._c(_C.YELLOW,  f"Encoding-skipped: {s_enc}"))

        self._console("  " + "  │  ".join(parts))
        self._file(
            f"SCAN: scanned={scanned} matched={matched} "
            f"binary_skipped={s_bin} encoding_skipped={s_enc}",
            "INFO",
        )

    # ──────────────────────────────────────────────────────────────
    # v2: Enhanced report with full summary table
    # ──────────────────────────────────────────────────────────────

    def report(self, results: dict, scan_stats: Optional[dict] = None) -> None:
        """
        Print the final operation summary.

        Args:
            results:    Dict with keys: modified, skipped, errors
            scan_stats: Optional scan statistics from TextReplacementEngine.stats
        """
        modified = results.get("modified", [])
        skipped  = results.get("skipped",  [])
        errors   = results.get("errors",   [])

        self.section("OPERATION COMPLETE")

        # ── File lists ────────────────────────────────────────────
        if modified:
            self._console(self._c(_C.GREEN + _C.BOLD, f"  ✔ Modified  : {len(modified)} file(s)"))
            for f in modified:
                self._console(self._c(_C.GREEN, f"      {f}"))
                self._file(f"MODIFIED: {f}", "INFO")

        if skipped:
            self._console(self._c(_C.GREY, f"  ─ Skipped   : {len(skipped)} file(s)"))
            for f in skipped:
                self._console(self._c(_C.GREY, f"      {f}"))
                self._file(f"SKIPPED: {f}", "INFO")

        if errors:
            self._console(self._c(_C.RED + _C.BOLD, f"  ✖ Errors    : {len(errors)} file(s)"))
            for item in errors:
                f, err = item if isinstance(item, tuple) else (item, "")
                self._console(self._c(_C.RED, f"      {f}  —  {err}"))
                self._file(f"ERROR: {f} — {err}", "ERROR")

        # ── Summary table ─────────────────────────────────────────
        self.divider()
        self._console(self._c(_C.CYAN + _C.BOLD, "  SUMMARY"))
        self.divider("·")

        rows = []
        if scan_stats:
            rows.append(("Total scanned",   scan_stats.get("scanned", "—")))
            rows.append(("Binary skipped",  scan_stats.get("skipped_binary", 0)))
            rows.append(("Encoding skipped",scan_stats.get("skipped_encoding", 0)))

        rows.append(("Files modified",  len(modified)))
        rows.append(("Files skipped",   len(skipped)))
        rows.append(("Errors",          len(errors)))

        for label, value in rows:
            val_color = _C.RED if (label == "Errors" and value) else _C.WHITE
            label_str = self._c(_C.GREY,       f"  {label:<22}")
            value_str = self._c(val_color + _C.BOLD, str(value))
            self._console(f"{label_str}{value_str}")
            self._file(f"  {label}: {value}", "INFO")

        self.divider()

    # ──────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────

    def _console(self, msg: str, file=sys.stdout) -> None:
        print(msg, file=file)

    def _c(self, codes: str, text: str) -> str:
        if not self._color:
            return text
        return f"{codes}{text}{_C.RESET}"

    def _file(
        self,
        msg: str,
        level: str = "INFO",
        exc_info: bool = False,
    ) -> None:
        if self._file_logger is None:
            return
        log_fn = getattr(self._file_logger, level.lower(), self._file_logger.info)
        log_fn(msg, exc_info=exc_info)

    def _init_file_logger(self) -> None:
        self.LOG_DIR.mkdir(exist_ok=True)
        date_str  = datetime.now().strftime("%Y-%m-%d")
        log_path  = self.LOG_DIR / f"aurelion_{date_str}.log"

        self._file_logger = logging.getLogger("aurelion")
        self._file_logger.setLevel(logging.DEBUG)

        if not self._file_logger.handlers:
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] [%(levelname)-7s] %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            self._file_logger.addHandler(handler)

        self._file_logger.info(
            f"{'='*54}\nAurelion Session v2 Started — {datetime.now()}\n{'='*54}"
        )
