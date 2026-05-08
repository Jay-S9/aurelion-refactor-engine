"""
Aurelion Refactor Engine v6 - Profile Manager
Supports multiple named environment profiles (dev, prod, staging, etc.)
each with its own config, default paths, exclude rules, and state.

Profile storage:
  profiles/
    default.toml       ← shipped default profile
    dev.toml           ← user-defined development profile
    prod.toml          ← user-defined production profile
    {name}.toml        ← any named profile

Profile TOML schema:
  [profile]
  name        = "dev"
  description = "Local development environment"
  created_at  = "2026-04-14T10:00:00"

  [config]
  encoding       = "utf-8"
  workers        = 4
  dry_run        = false
  no_backup      = false
  exclude_dirs   = [".git", "__pycache__", "node_modules"]

  [paths]
  project_root   = "."
  plans_dir      = "./plans"
  output_dir     = "./output"
  templates_dir  = "./templates"

  [history]
  history_dir    = "./history"
  state_file     = "./history/state.json"

  [rules]
  default_group  = ""
  default_tags   = []

NEW IN v6:
  - ProfileManager.load(name)     — load a profile by name
  - ProfileManager.save(profile)  — save/update a profile
  - ProfileManager.list()         — list all available profiles
  - ProfileManager.create(name)   — create a new profile from defaults
  - ProfileManager.delete(name)   — remove a profile
  - ProfileManager.apply_to_config(profile, config) — merge profile into config
  - ProfileManager.active         — returns the currently active profile
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# TOML loader
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

try:
    import tomli_w  # type: ignore — optional write support
    _HAS_TOMLI_W = True
except ImportError:
    _HAS_TOMLI_W = False

PROFILES_DIR = Path("profiles")

# ── Default profile content ────────────────────────────────────────────────────
DEFAULT_PROFILE_TOML = """\
# Aurelion Refactor Engine v6 — Default Profile
# Copy and customise this file to create a new profile.

[profile]
name        = "default"
description = "Default Aurelion configuration"

[config]
encoding       = "utf-8"
workers        = 1
dry_run        = false
no_backup      = false
strict_mode    = true
include_binary = false
exclude_dirs   = [".git", "__pycache__", "node_modules", ".venv", "backups"]
exclude_paths  = []

[paths]
project_root   = "."
plans_dir      = "./plans"
output_dir     = "./output"
templates_dir  = "./templates"

[history]
history_dir    = "./history"

[rules]
default_group  = ""
default_tags   = []
"""

DEV_PROFILE_TOML = """\
# Aurelion — Development Profile
# Relaxed settings for local development iteration.

[profile]
name        = "dev"
description = "Development environment — verbose, no-backup, workers=4"

[config]
encoding       = "utf-8"
workers        = 4
dry_run        = false
no_backup      = true
strict_mode    = false
include_binary = false
exclude_dirs   = [".git", "__pycache__", "node_modules", ".venv", "backups", "dist", "build"]

[paths]
project_root   = "."
plans_dir      = "./plans"

[rules]
default_group  = "dev"
default_tags   = ["dev"]
"""

PROD_PROFILE_TOML = """\
# Aurelion — Production Profile
# Conservative settings for production deployments.

[profile]
name        = "prod"
description = "Production environment — strict, backup-always, workers=1"

[config]
encoding       = "utf-8"
workers        = 1
dry_run        = false
no_backup      = false
strict_mode    = true
include_binary = false
exclude_dirs   = [".git", "__pycache__", "node_modules", ".venv", "backups"]

[paths]
project_root   = "."
plans_dir      = "./plans"

[rules]
default_group  = "prod"
default_tags   = ["production"]
"""


# ── Profile data model ─────────────────────────────────────────────────────────

class Profile:
    """
    A loaded profile with typed accessors.
    Wraps a raw TOML dict with convenient property access.
    """

    def __init__(self, data: Dict[str, Any], source_path: Optional[Path] = None):
        self._data = data
        self.source_path = source_path

    @property
    def name(self) -> str:
        return self._data.get("profile", {}).get("name", "default")

    @property
    def description(self) -> str:
        return self._data.get("profile", {}).get("description", "")

    @property
    def config(self) -> Dict[str, Any]:
        return self._data.get("config", {})

    @property
    def paths(self) -> Dict[str, Any]:
        return self._data.get("paths", {})

    @property
    def history_config(self) -> Dict[str, Any]:
        return self._data.get("history", {})

    @property
    def rules_config(self) -> Dict[str, Any]:
        return self._data.get("rules", {})

    def to_dict(self) -> Dict[str, Any]:
        return self._data.copy()

    def __repr__(self) -> str:
        return f"Profile(name={self.name!r}, source={self.source_path})"


# ── Profile Manager ────────────────────────────────────────────────────────────

class ProfileManager:
    """
    Discovers, loads, saves, and manages Aurelion profiles.
    Profiles live in the profiles/ directory as TOML files.
    """

    BUILTIN_PROFILES = {
        "default": DEFAULT_PROFILE_TOML,
        "dev":     DEV_PROFILE_TOML,
        "prod":    PROD_PROFILE_TOML,
    }

    def __init__(self, profiles_dir: Optional[Path] = None, logger=None):
        self._dir    = profiles_dir or PROFILES_DIR
        self._logger = logger
        self._active: Optional[Profile] = None
        self._ensure_defaults()

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    def load(self, name: str) -> Profile:
        """Load a profile by name. Raises FileNotFoundError if not found."""
        path = self._dir / f"{name}.toml"
        if not path.exists():
            raise FileNotFoundError(
                f"Profile '{name}' not found in {self._dir}.\n"
                f"Available: {', '.join(self.list())}"
            )
        data = self._load_toml(path)
        profile = Profile(data, source_path=path)
        self._active = profile
        if self._logger:
            self._logger.info(f"  [PROFILE] Loaded: '{name}' ({profile.description})")
        return profile

    def save(self, name: str, data: Dict[str, Any]) -> Path:
        """Save/update a profile by name. Returns the saved path."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{name}.toml"
        # Ensure profile name matches
        data.setdefault("profile", {})["name"] = name
        content = self._dict_to_toml(data)
        path.write_text(content, encoding="utf-8")
        return path

    def create(self, name: str, description: str = "", base: str = "default") -> Profile:
        """
        Create a new profile based on an existing one.
        Returns the new Profile (also saves it to disk).
        """
        # Load base profile
        base_profile = self.load(base)
        data = base_profile.to_dict()

        # Override name and description
        data["profile"]["name"]        = name
        data["profile"]["description"] = description or f"Profile '{name}'"
        data["profile"]["created_at"]  = datetime.now(tz=timezone.utc).isoformat()

        path = self.save(name, data)
        if self._logger:
            self._logger.success(f"  [PROFILE] Created profile '{name}' → {path}")
        return Profile(data, source_path=path)

    def delete(self, name: str) -> bool:
        """Delete a profile. Raises PermissionError for built-in profiles."""
        if name == "default":
            raise PermissionError("Cannot delete the 'default' profile.")
        path = self._dir / f"{name}.toml"
        if path.exists():
            path.unlink()
            if self._logger:
                self._logger.success(f"  [PROFILE] Deleted profile '{name}'")
            return True
        return False

    def list(self) -> List[str]:
        """Return names of all available profiles (sorted, default first)."""
        if not self._dir.exists():
            return ["default"]
        profiles = [p.stem for p in sorted(self._dir.glob("*.toml"))]
        # Ensure default is first
        if "default" in profiles:
            profiles = ["default"] + [p for p in profiles if p != "default"]
        return profiles

    def apply_to_config(self, profile: Profile, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merge a profile's config section into an existing config dict.
        Profile values take lower precedence than explicit CLI overrides.
        Returns the merged config.
        """
        merged = {**profile.config, **config}

        # Apply path defaults
        paths = profile.paths
        if paths.get("project_root") and "project_root" not in config:
            merged["project_root"] = paths["project_root"]

        # Apply rules defaults
        rules_cfg = profile.rules_config
        if rules_cfg.get("default_group") and "default_group" not in config:
            merged["default_group"] = rules_cfg["default_group"]

        return merged

    @property
    def active(self) -> Optional[Profile]:
        """Return the currently loaded profile, or None."""
        return self._active

    # ──────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────

    def _ensure_defaults(self) -> None:
        """Write built-in profiles to disk if they don't exist."""
        self._dir.mkdir(parents=True, exist_ok=True)
        for name, content in self.BUILTIN_PROFILES.items():
            path = self._dir / f"{name}.toml"
            if not path.exists():
                path.write_text(content, encoding="utf-8")

    def _load_toml(self, path: Path) -> Dict[str, Any]:
        if tomllib is None:
            # Fallback: basic key=value parsing for simple profiles
            return self._parse_simple_toml(path.read_text(encoding="utf-8"))
        with open(path, "rb") as f:
            return tomllib.load(f)

    def _dict_to_toml(self, data: Dict[str, Any]) -> str:
        """
        Serialize a profile dict to TOML format.
        Uses tomli_w if available, otherwise builds manually.
        """
        if _HAS_TOMLI_W:
            return tomli_w.dumps(data)
        return self._manual_toml_serialise(data)

    def _manual_toml_serialise(self, data: Dict[str, Any], prefix: str = "") -> str:
        """Minimal TOML serialiser for profile dicts."""
        lines = []
        # First pass: scalar values at top level
        sections: Dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                sections[k] = v
            else:
                lines.append(f"{k} = {self._toml_value(v)}")
        # Second pass: sections
        for section_name, section_data in sections.items():
            lines.append(f"\n[{section_name}]")
            for k, v in section_data.items():
                lines.append(f"{k} = {self._toml_value(v)}")
        return "\n".join(lines) + "\n"

    def _toml_value(self, v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return f'"{v}"'
        if isinstance(v, list):
            items = ", ".join(self._toml_value(i) for i in v)
            return f"[{items}]"
        return f'"{v}"'

    def _parse_simple_toml(self, text: str) -> Dict[str, Any]:
        """Very basic TOML parser for when tomllib isn't available."""
        import re
        result: Dict[str, Any] = {}
        current_section = result
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                current_section = {}
                result[section] = current_section
            elif "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                # Very basic value parsing
                if val.startswith('"') and val.endswith('"'):
                    current_section[key] = val[1:-1]
                elif val in ("true", "false"):
                    current_section[key] = val == "true"
                elif val.startswith("["):
                    try:
                        import json
                        current_section[key] = json.loads(val)
                    except Exception:
                        current_section[key] = []
                else:
                    try:
                        current_section[key] = int(val)
                    except ValueError:
                        current_section[key] = val
        return result
