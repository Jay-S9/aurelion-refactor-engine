"""
Aurelion Refactor Engine v2 - Target Resolver
Resolves CLI targeting arguments into concrete file lists.

CHANGES IN v2:
  - exclude_paths: supports both directory names AND explicit path patterns
    e.g. --exclude logs/ node_modules/ src/legacy/
  - _walk() now prunes by both dir name and resolved absolute path
  - resolve_target_files() signature extended with exclude_paths parameter
  - Hidden directories (starting with '.') still excluded unless --all-hidden added
"""

import os
from pathlib import Path
from typing import List, Optional, Set


def resolve_target_files(
    target_all: bool,
    target_dir: Optional[str],
    target_file: Optional[str],
    extensions: Optional[List[str]],
    exclude_dirs: List[str],
    exclude_paths: Optional[List[str]] = None,
) -> List[Path]:
    """
    Convert targeting arguments into a flat list of Path objects.

    Args:
        exclude_dirs:  Directory *names* to skip (e.g. ["node_modules", ".git"])
        exclude_paths: Explicit relative or absolute paths to exclude
                       (e.g. ["logs/", "src/legacy/old_module.py"])
    """
    if target_file:
        return [Path(target_file)]

    if target_dir:
        root = Path(target_dir).resolve()
    elif target_all:
        root = Path.cwd()
    else:
        return []

    # Build a set of resolved absolute paths to exclude
    resolved_excludes: Set[Path] = _resolve_exclude_paths(exclude_paths or [])

    return _walk(root, extensions, set(exclude_dirs), resolved_excludes)


def resolve_file_copy_targets(
    source: Path,
    raw_targets: List[Path],
) -> List[Path]:
    """
    Resolve each raw target.
    - If a target is an existing directory → append source filename
    - If a target is a file path (existing or not) → use as-is
    """
    resolved = []
    for t in raw_targets:
        if t.is_dir():
            resolved.append(t / source.name)
        else:
            resolved.append(t)
    return resolved


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_exclude_paths(raw: List[str]) -> Set[Path]:
    """
    Convert raw exclude path strings to a set of resolved absolute Paths.
    Handles both directory paths (with or without trailing slash) and file paths.
    Silently ignores paths that don't exist (they may still be valid name filters).
    """
    resolved: Set[Path] = set()
    for p_str in raw:
        p = Path(p_str.rstrip("/\\")).resolve()
        resolved.add(p)
    return resolved


def _walk(
    root: Path,
    extensions: Optional[List[str]],
    exclude_dir_names: Set[str],
    exclude_paths: Set[Path],
) -> List[Path]:
    results: List[Path] = []

    for dirpath_str, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath_str)

        # ── Prune excluded directories in-place ──────────────────
        # This prevents os.walk from descending into excluded subtrees.
        # Check both: directory name match AND absolute path match.
        kept_dirs = []
        for d in dirnames:
            full = current_dir / d

            # Skip by name (e.g. "node_modules", "__pycache__")
            if d in exclude_dir_names:
                continue

            # Skip hidden directories (starting with '.')
            if d.startswith("."):
                continue

            # Skip by resolved absolute path match
            if full.resolve() in exclude_paths:
                continue

            kept_dirs.append(d)

        dirnames[:] = kept_dirs

        # ── Filter files ─────────────────────────────────────────
        for filename in filenames:
            path = current_dir / filename

            # Skip non-regular files
            if not path.is_file():
                continue

            # Skip if this specific file is in the exclude set
            if path.resolve() in exclude_paths:
                continue

            # Extension filter
            if extensions and path.suffix.lower() not in extensions:
                continue

            results.append(path)

    return sorted(results)
