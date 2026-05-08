"""
Aurelion Refactor Engine v3 - Config Loader
Loads aurelion.toml for persistent CLI defaults.

CHANGES IN v3:
  - DEFAULT_CONFIG extended: workers, inject defaults, safety rules
  - load_config() now returns the full merged config including [safety] block
  - get_cli_defaults() extracts the subset relevant to CLI argument defaults
  - generate_default_config() updated with v3 fields
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

# tomllib is in stdlib for Python 3.11+; fall back to tomli for 3.9/3.10
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


DEFAULT_CONFIG: Dict[str, Any] = {
    # Text processing
    "encoding":       "utf-8",
    "case_sensitive": True,

    # Operation behaviour
    "dry_run":        False,
    "no_backup":      False,
    "workers":        1,

    # File targeting
    "exclude_dirs": [
        ".git", "__pycache__", "node_modules", ".venv", "backups",
        "dist", "build", ".idea", ".vscode",
    ],
    "exclude_paths": [],

    # Safety
    "include_binary": False,

    # Inject defaults
    "inject_mode": "replace",

    # Plan runner
    "strict_mode": True,   # abort plan on first rule error
}


def load_config(config_path: str) -> Dict[str, Any]:
    """
    Load a TOML config file and merge with defaults.
    Returns merged config dict.
    """
    path = Path(config_path)

    if not path.exists():
        print(f"[WARN] Config file not found: {config_path}", file=sys.stderr)
        return DEFAULT_CONFIG.copy()

    if tomllib is None:
        print(
            "[WARN] TOML support requires Python 3.11+ or 'tomli' package. "
            "Ignoring config file.",
            file=sys.stderr,
        )
        return DEFAULT_CONFIG.copy()

    try:
        with open(path, "rb") as f:
            user_config = tomllib.load(f)
    except Exception as e:
        print(f"[WARN] Failed to parse config file: {e}", file=sys.stderr)
        return DEFAULT_CONFIG.copy()

    merged = DEFAULT_CONFIG.copy()

    # Merge [aurelion] section
    merged.update(user_config.get("aurelion", {}))

    # Merge [safety] section into top-level config
    safety = user_config.get("safety", {})
    if "include_binary" in safety:
        merged["include_binary"] = safety["include_binary"]

    return merged


def get_cli_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract CLI-relevant defaults from a loaded config dict.
    Used by the executor to apply config-driven defaults to argparse args.
    """
    return {
        "encoding":       config.get("encoding",       DEFAULT_CONFIG["encoding"]),
        "case_sensitive": config.get("case_sensitive",  DEFAULT_CONFIG["case_sensitive"]),
        "dry_run":        config.get("dry_run",         DEFAULT_CONFIG["dry_run"]),
        "no_backup":      config.get("no_backup",       DEFAULT_CONFIG["no_backup"]),
        "workers":        config.get("workers",         DEFAULT_CONFIG["workers"]),
        "exclude_dirs":   config.get("exclude_dirs",    DEFAULT_CONFIG["exclude_dirs"]),
        "exclude_paths":  config.get("exclude_paths",   DEFAULT_CONFIG["exclude_paths"]),
        "include_binary": config.get("include_binary",  DEFAULT_CONFIG["include_binary"]),
        "inject_mode":    config.get("inject_mode",     DEFAULT_CONFIG["inject_mode"]),
        "strict_mode":    config.get("strict_mode",     DEFAULT_CONFIG["strict_mode"]),
    }


def generate_default_config() -> str:
    """Return a full annotated aurelion.toml template as a string."""
    return """\
# ══════════════════════════════════════════════════════════════
#  Aurelion Refactor Engine v3 — Configuration File
#  Place as aurelion.toml in your project root.
#  Pass with:  aurelion --config aurelion.toml <command>
# ══════════════════════════════════════════════════════════════

[aurelion]

# File encoding for text operations
encoding = "utf-8"

# Default matching behaviour
case_sensitive = true

# Parallel worker threads (1 = serial, >1 = parallel per-rule)
workers = 4

# Safety: skip backups? (not recommended)
no_backup = false

# Always preview without applying
dry_run = false

# Default inject mode: replace | prepend | append
inject_mode = "replace"

# Abort plan execution on first rule error
strict_mode = true

# Directories always excluded from recursive traversal
exclude_dirs = [
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "backups",
    "dist",
    "build",
    ".idea",
    ".vscode",
]

# Specific paths to exclude (relative or absolute)
exclude_paths = []

# ── Safety rules ────────────────────────────────────────────
[safety]
# Override binary file guard — process ALL files (dangerous!)
include_binary = false
"""
