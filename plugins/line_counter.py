"""
Aurelion Plugin: line_counter
Counts lines in matched files and appends a summary comment.
This demonstrates a complete, working plugin implementation.

Usage in plan.toml:
  [[rules]]
  name    = "count-lines"
  type    = "line_counter"
  target  = "**/*.py"
  append_comment = true
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from engines.rule_engine import RuleBase, RuleResult

# ── Plugin metadata ────────────────────────────────────────────────────────────

RULE_TYPE   = "line_counter"
DESCRIPTION = "Count lines in matched files; optionally append a summary comment"


# ── Plugin rule class ──────────────────────────────────────────────────────────

@dataclass
class LineCounterRule(RuleBase):
    """Counts lines in each matched file."""
    append_comment: bool = False     # if True, append a comment with the line count
    comment_prefix: str  = "#"       # comment character  (# for Python, // for JS, etc.)


RULE_CLASS = LineCounterRule


# ── Plugin execute function ────────────────────────────────────────────────────

def execute(rule: LineCounterRule, logger, backup_manager) -> RuleResult:
    """
    Scan matched files, count their lines.
    Optionally append a comment: # lines: 42
    """
    from engines.rule_engine import _resolve_glob

    result = RuleResult(rule.name, rule.rule_type)
    base   = Path(rule.base_dir) if rule.base_dir else None
    files  = _resolve_glob(rule.target, rule.exclude_dirs, rule.exclude_paths, base_dir=base)

    if not files:
        logger.info(f"  Plugin '{rule.name}': no files matched '{rule.target}'")
        return result

    total_lines = 0

    for path in files:
        try:
            content = path.read_text(encoding=rule.encoding, errors="replace")
            line_count = len(content.splitlines())
            total_lines += line_count

            if rule.append_comment and not rule.dry_run:
                comment = f"\n{rule.comment_prefix} aurelion: {line_count} lines\n"
                if comment.strip() not in content:
                    if not rule.no_backup:
                        backup_manager.backup_files([path])
                    path.write_text(content + comment, encoding=rule.encoding)
                    result.modified.append(str(path))
                    logger.success(f"Annotated ({line_count} lines): {path.name}")
                else:
                    result.skipped.append(str(path))
            else:
                # Read-only mode: just log
                logger.info(f"  {line_count:>6} lines  {path}")
                result.skipped.append(str(path))

        except Exception as e:
            result.errors.append((str(path), str(e)))
            logger.error(f"Plugin error: {path} — {e}")

    logger.info(f"  Total: {total_lines} lines across {len(files)} file(s)")
    return result
