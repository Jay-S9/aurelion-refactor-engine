"""
Aurelion Refactor Engine v2 - Diff Preview Renderer
Produces rich, line-level before/after diff output for text replacements.

NEW IN v2:
  - Full line-level diff with line numbers and visual separator
  - Highlights the exact changed substring (not just the whole line)
  - Context lines (N lines before/after each change for readability)
  - Truncates very large diffs to avoid terminal flooding
  - Works identically in both dry-run and live-run preview modes
"""

import re
from pathlib import Path
from typing import List, Dict, Any

# How many unchanged context lines to show around each changed block
CONTEXT_LINES: int = 2

# Maximum changed-line entries to show per file before truncating
MAX_DIFF_LINES_PER_FILE: int = 30


def render_diff(match: Dict[str, Any], logger, pattern: re.Pattern, new_text: str) -> None:
    """
    Render a rich diff preview for one matched file.

    Args:
        match:    A match dict from TextReplacementEngine.scan()
        logger:   AurelionLogger instance
        pattern:  Compiled regex pattern (same one used for replacement)
        new_text: The replacement string
    """
    from core.logger import _C  # local import to avoid circular

    path: Path = match["file"]
    content: str = match["content"]
    lines: List[str] = content.splitlines()
    total_lines = len(lines)

    # Build a set of changed line indices (0-based)
    changed_indices: set[int] = set()
    for ctx in match.get("context", []):
        changed_indices.add(ctx["line"] - 1)  # context stores 1-based

    # Expand to include CONTEXT_LINES around each changed line
    display_indices: set[int] = set()
    for idx in changed_indices:
        for offset in range(-CONTEXT_LINES, CONTEXT_LINES + 1):
            neighbour = idx + offset
            if 0 <= neighbour < total_lines:
                display_indices.add(neighbour)

    sorted_display = sorted(display_indices)

    # Header
    file_str  = logger._c(_C.CYAN + _C.BOLD, str(path))
    count_str = logger._c(_C.YELLOW + _C.BOLD, f"  [{match['count']} occurrence(s)]")
    logger._console(f"\n  📄 {file_str}{count_str}")

    if not sorted_display:
        logger._console(logger._c(_C.GREY, "     (no line context available)"))
        return

    # Track whether we've printed a gap marker
    prev_idx: int = -2
    lines_shown: int = 0
    truncated: bool = False

    for idx in sorted_display:
        if lines_shown >= MAX_DIFF_LINES_PER_FILE:
            truncated = True
            break

        line_no = idx + 1  # display as 1-based
        raw_line = lines[idx]

        # Gap marker between non-contiguous blocks
        if idx > prev_idx + 1 and prev_idx >= 0:
            logger._console(logger._c(_C.GREY, "       ┄ ┄ ┄"))

        if idx in changed_indices:
            # Changed line: show OLD (red) then NEW (green)
            replaced_line = pattern.sub(new_text, raw_line)

            old_highlighted = _highlight_match(raw_line, pattern, logger, "old")
            new_highlighted = _highlight_match(replaced_line, pattern, logger, "new")

            line_label = logger._c(_C.GREY, f"  L{line_no:>5} │ ")
            minus      = logger._c(_C.RED,          "  - ")
            plus       = logger._c(_C.GREEN,         "  + ")

            logger._console(f"{line_label}{minus}{old_highlighted}")
            logger._console(f"{'':<9}  {plus}{new_highlighted}")

            # Also write plain version to log file
            logger._file(
                f"  L{line_no:>5} │ - {raw_line.rstrip()}", "INFO"
            )
            logger._file(
                f"  {'':>5}     + {replaced_line.rstrip()}", "INFO"
            )
        else:
            # Context line (unchanged)
            line_label = logger._c(_C.GREY, f"  L{line_no:>5} │ ")
            ctx_text   = logger._c(_C.DIM,  f"    {raw_line.rstrip()}")
            logger._console(f"{line_label}{ctx_text}")

        prev_idx = idx
        lines_shown += 1

    if truncated:
        remaining = len(changed_indices) - MAX_DIFF_LINES_PER_FILE
        msg = logger._c(
            _C.YELLOW,
            f"\n     … {remaining} more changed line(s) not shown (use --all-diff to see all)"
        )
        logger._console(msg)


def _highlight_match(
    line: str,
    pattern: re.Pattern,
    logger,
    mode: str,          # "old" | "new"
) -> str:
    """
    Return the line string with the matched/replaced segment visually
    bolded using ANSI codes. Falls back gracefully if color is off.
    """
    from core.logger import _C

    if not logger._color:
        return line.rstrip()

    # For "old": highlight what the pattern found (the match)
    # For "new": we just return the line as-is (already substituted)
    # The colour (red/green) is applied by the caller; here we add bold
    # to the portion that changed.
    if mode == "old":
        def bold_match(m: re.Match) -> str:
            return f"{_C.BOLD}{m.group(0)}{_C.RESET}{_C.RED}"
        return _C.RED + pattern.sub(bold_match, line.rstrip()) + _C.RESET
    else:
        # "new" line — bold it entirely so it stands out
        return _C.GREEN + _C.BOLD + line.rstrip() + _C.RESET
