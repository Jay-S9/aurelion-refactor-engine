"""
Aurelion Refactor Engine v2 - Text Engine
Handles scanning and applying text replacements across files.

CHANGES IN v2:
  - Binary file guard: skips non-text files by default (--include-binary to override)
  - Encoding detection: probes each file before reading; falls back gracefully
  - Large file streaming: files > 5 MB are processed line-by-line to avoid OOM
  - Scan tracks scanned/skipped_binary counts for the final summary
  - apply() uses atomic write (write to .tmp, then rename) to prevent partial writes
"""

import re
import os
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from utils.file_safety import (
    is_safe_text_file,
    detect_encoding,
    is_large_file,
)


class TextReplacementEngine:
    """
    Two-phase engine:
      Phase 1: scan()  — read-only; returns match metadata + context lines
      Phase 2: apply() — write; replaces text using safe atomic writes
    """

    def __init__(
        self,
        old_text: str,
        new_text: str,
        case_sensitive: bool,
        encoding: str,
        logger,
        include_binary: bool = False,
    ):
        self.old_text = old_text
        self.new_text = new_text
        self.case_sensitive = case_sensitive
        self.preferred_encoding = encoding
        self.logger = logger
        self.include_binary = include_binary

        flags = 0 if case_sensitive else re.IGNORECASE
        self._pattern = re.compile(re.escape(old_text), flags)

        # Populated during scan() — used by executor for summary
        self.stats: Dict[str, int] = {
            "scanned": 0,
            "skipped_binary": 0,
            "skipped_encoding": 0,
            "matched": 0,
        }

    # ──────────────────────────────────────────────────────────────
    # Phase 1: Scan
    # ──────────────────────────────────────────────────────────────

    def scan(self, files: List[Path]) -> List[Dict[str, Any]]:
        """
        Read each file, count matches, collect line-level context.
        Returns only files that have at least one match.
        """
        self.stats = {"scanned": 0, "skipped_binary": 0, "skipped_encoding": 0, "matched": 0}
        matches: List[Dict[str, Any]] = []

        for path in files:
            self.stats["scanned"] += 1
            result = self._scan_file(path)
            if result == "binary":
                self.stats["skipped_binary"] += 1
            elif result == "encoding_error":
                self.stats["skipped_encoding"] += 1
            elif result is not None:
                self.stats["matched"] += 1
                matches.append(result)

        return matches

    def _scan_file(self, path: Path) -> Optional[Dict[str, Any]]:
        # ── 1. Binary safety check ────────────────────────────────
        safe, reason = is_safe_text_file(path, self.include_binary)
        if not safe:
            self.logger.skipped(f"{path}  [{reason}]")
            return "binary"

        # ── 2. Encoding detection ─────────────────────────────────
        try:
            encoding, was_fallback = detect_encoding(path, self.preferred_encoding)
        except (OSError, PermissionError) as e:
            self.logger.error(f"Cannot read {path}: {e}")
            return "encoding_error"

        if was_fallback:
            self.logger.warning(
                f"{path}  (encoding fallback: using '{encoding}')"
            )

        # ── 3. Read & scan ────────────────────────────────────────
        if is_large_file(path):
            return self._scan_large_file(path, encoding)
        else:
            return self._scan_normal_file(path, encoding)

    def _scan_normal_file(
        self, path: Path, encoding: str
    ) -> Optional[Dict[str, Any]]:
        """Standard in-memory scan for files under the size threshold."""
        try:
            content = path.read_text(encoding=encoding, errors="replace")
        except (OSError, PermissionError) as e:
            self.logger.error(f"Cannot read {path}: {e}")
            return None

        if not self._pattern.search(content):
            return None

        lines = content.splitlines()
        context_entries = _build_context(lines, self._pattern, self.new_text)
        total_count = len(self._pattern.findall(content))

        return {
            "file": path,
            "count": total_count,
            "context": context_entries,
            "content": content,       # kept in memory for apply()
            "encoding": encoding,
            "large": False,
        }

    def _scan_large_file(
        self, path: Path, encoding: str
    ) -> Optional[Dict[str, Any]]:
        """
        Streaming scan for large files (> LARGE_FILE_THRESHOLD_BYTES).
        Reads line-by-line to avoid loading the full file into RAM.
        Match content is NOT stored — apply() will re-stream the file.
        """
        context_entries: List[Dict[str, Any]] = []
        total_count = 0
        size_mb = path.stat().st_size / (1024 * 1024)

        self.logger.info(
            f"  [LARGE FILE] {path.name}  ({size_mb:.1f} MB) — streaming mode"
        )

        try:
            with open(path, "r", encoding=encoding, errors="replace") as fh:
                for line_no, line in enumerate(fh, start=1):
                    matches_in_line = self._pattern.findall(line)
                    if matches_in_line:
                        total_count += len(matches_in_line)
                        replaced = self._pattern.sub(self.new_text, line)
                        context_entries.append({
                            "line": line_no,
                            "old": line.rstrip(),
                            "new": replaced.rstrip(),
                        })
        except (OSError, PermissionError) as e:
            self.logger.error(f"Cannot stream {path}: {e}")
            return None

        if total_count == 0:
            return None

        return {
            "file": path,
            "count": total_count,
            "context": context_entries,
            "content": None,         # NOT stored — too large
            "encoding": encoding,
            "large": True,
        }

    # ──────────────────────────────────────────────────────────────
    # Phase 2: Apply
    # ──────────────────────────────────────────────────────────────

    def apply(self, matches: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Apply replacements to each matched file.
        Uses atomic writes (tmp → rename) to prevent partial-write corruption.
        """
        modified: List[str] = []
        skipped: List[str]  = []
        errors: List[Tuple[str, str]] = []

        for m in matches:
            path: Path    = m["file"]
            encoding: str = m.get("encoding", self.preferred_encoding)
            large: bool   = m.get("large", False)

            try:
                if large:
                    changed = self._apply_large_file(path, encoding)
                else:
                    changed = self._apply_normal_file(path, m["content"], encoding)

                if changed:
                    modified.append(str(path))
                    self.logger.success(f"Modified: {path}  (×{m['count']})")
                else:
                    skipped.append(str(path))
                    self.logger.skipped(f"{path}  (no change after apply)")
            except Exception as e:
                errors.append((str(path), str(e)))
                self.logger.error(f"Write failed: {path} — {e}")

        return {"modified": modified, "skipped": skipped, "errors": errors}

    def _apply_normal_file(
        self, path: Path, original: str, encoding: str
    ) -> bool:
        """In-memory replace + atomic write for normal-sized files."""
        new_content = self._pattern.sub(self.new_text, original)
        if new_content == original:
            return False
        _atomic_write(path, new_content, encoding)
        return True

    def _apply_large_file(self, path: Path, encoding: str) -> bool:
        """
        Streaming replace for large files:
          - Reads input line-by-line
          - Writes to a temp file in the same directory
          - Atomically renames on success
        """
        parent = path.parent
        changed = False

        fd, tmp_path_str = tempfile.mkstemp(dir=parent, prefix=".aurelion_tmp_")
        tmp_path = Path(tmp_path_str)

        try:
            with (
                open(fd, "w", encoding=encoding, errors="replace") as out_fh,
                open(path, "r", encoding=encoding, errors="replace") as in_fh,
            ):
                for line in in_fh:
                    new_line = self._pattern.sub(self.new_text, line)
                    if new_line != line:
                        changed = True
                    out_fh.write(new_line)

            if changed:
                tmp_path.replace(path)
            else:
                tmp_path.unlink()

        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        return changed


# ── Module-level helpers ───────────────────────────────────────────────────────

def _build_context(
    lines: List[str],
    pattern: re.Pattern,
    new_text: str,
) -> List[Dict[str, Any]]:
    entries = []
    for line_no, line in enumerate(lines, start=1):
        if pattern.search(line):
            replaced = pattern.sub(new_text, line)
            entries.append({
                "line": line_no,
                "old": line.rstrip(),
                "new": replaced.rstrip(),
            })
    return entries


def _atomic_write(path: Path, content: str, encoding: str) -> None:
    """
    Write content to path atomically:
      1. Write to a sibling .tmp file
      2. os.replace() (atomic rename on POSIX; best-effort on Windows)
    """
    parent = path.parent
    fd, tmp_str = tempfile.mkstemp(dir=parent, prefix=".aurelion_tmp_")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding=encoding, errors="replace") as f:
            f.write(content)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
