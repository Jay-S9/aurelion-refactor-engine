"""
Aurelion Refactor Engine v6 - Plugin Marketplace Manager
Manages plugin lifecycle: discovery, installation, versioning, and removal.

Plugin registry format (plugins/marketplace/registry.json):
  {
    "version": "1",
    "plugins": {
      "line_counter": {
        "name":        "line_counter",
        "description": "Count lines in matched files",
        "version":     "1.0.0",
        "source":      "local",
        "installed_at":"2026-04-14T10:00:00",
        "file":        "line_counter.py",
        "enabled":     true
      }
    }
  }

Installation sources:
  local     — copy a local .py file into plugins/
  url       — download from a URL (future)
  pypi      — install from PyPI as a plugin package (future)

CLI commands (via parser/executor):
  aurelion plugins list
  aurelion plugins install /path/to/plugin.py
  aurelion plugins install /path/to/plugin.py --name my_plugin
  aurelion plugins remove  line_counter
  aurelion plugins enable  line_counter
  aurelion plugins disable line_counter
  aurelion plugins info    line_counter

NEW IN v6:
  - MarketplaceManager.list()     — list all installed plugins
  - MarketplaceManager.install()  — install from local file
  - MarketplaceManager.remove()   — uninstall plugin
  - MarketplaceManager.enable()   — re-enable a disabled plugin
  - MarketplaceManager.disable()  — disable without removing
  - Registry persistence in plugins/marketplace/registry.json
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


PLUGINS_DIR      = Path("plugins")
MARKETPLACE_DIR  = PLUGINS_DIR / "marketplace"
REGISTRY_FILE    = MARKETPLACE_DIR / "registry.json"


# ── Registry helpers ───────────────────────────────────────────────────────────

def _load_registry() -> Dict[str, Any]:
    if not REGISTRY_FILE.exists():
        return {"version": "1", "plugins": {}}
    try:
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"version": "1", "plugins": {}}


def _save_registry(registry: Dict[str, Any]) -> None:
    MARKETPLACE_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Plugin entry ───────────────────────────────────────────────────────────────

class PluginRecord:
    """Lightweight representation of an installed plugin."""

    def __init__(self, data: Dict[str, Any]):
        self.name         = data["name"]
        self.description  = data.get("description", "")
        self.version      = data.get("version", "0.0.0")
        self.source       = data.get("source", "local")
        self.installed_at = data.get("installed_at", "")
        self.file         = data.get("file", "")
        self.enabled      = data.get("enabled", True)
        self.rule_type    = data.get("rule_type", self.name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":         self.name,
            "description":  self.description,
            "version":      self.version,
            "source":       self.source,
            "installed_at": self.installed_at,
            "file":         self.file,
            "enabled":      self.enabled,
            "rule_type":    self.rule_type,
        }


# ── Marketplace Manager ────────────────────────────────────────────────────────

class MarketplaceManager:
    """
    Manages plugin installation, removal, and metadata.
    Plugins are stored as .py files in the plugins/ directory.
    The registry tracks metadata and enabled/disabled state.
    """

    def __init__(self, logger=None):
        self._logger = logger
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        MARKETPLACE_DIR.mkdir(parents=True, exist_ok=True)
        self._sync_registry_with_disk()

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    def list(self) -> List[PluginRecord]:
        """Return all plugins in the registry (installed + disabled)."""
        registry = _load_registry()
        return [PluginRecord(p) for p in registry["plugins"].values()]

    def install(
        self,
        source_path: str,
        name: Optional[str] = None,
        version: str = "1.0.0",
    ) -> PluginRecord:
        """
        Install a plugin from a local .py file.

        Args:
            source_path: Path to the plugin .py file.
            name:        Override plugin name (default: stem of filename).
            version:     Plugin version string.

        Returns:
            PluginRecord for the installed plugin.

        Raises:
            ValueError: if the plugin file fails contract validation.
        """
        src = Path(source_path)
        if not src.exists():
            raise FileNotFoundError(f"Plugin file not found: {source_path}")
        if src.suffix != ".py":
            raise ValueError(f"Plugin must be a .py file, got: {src.suffix}")

        # Validate plugin contract before installing
        plugin_name, description, rule_type = self._validate_plugin_file(src)
        final_name = name or plugin_name

        # Copy into plugins directory
        dest = PLUGINS_DIR / f"{final_name}.py"
        if dest.exists() and dest != src:
            # Back up existing
            backup = PLUGINS_DIR / f"{final_name}.py.bak"
            shutil.copy2(dest, backup)

        if src != dest:
            shutil.copy2(src, dest)

        # Update registry
        registry = _load_registry()
        registry["plugins"][final_name] = {
            "name":         final_name,
            "description":  description,
            "version":      version,
            "source":       "local",
            "source_path":  str(src),
            "installed_at": datetime.now(tz=timezone.utc).isoformat(),
            "file":         dest.name,
            "enabled":      True,
            "rule_type":    rule_type,
        }
        _save_registry(registry)

        record = PluginRecord(registry["plugins"][final_name])

        if self._logger:
            self._logger.success(
                f"  [PLUGINS] Installed '{final_name}' "
                f"(type={rule_type}) → {dest}"
            )

        # Reload into live registry
        try:
            from plugins.loader import load_plugins
            load_plugins(logger=self._logger)
        except Exception:
            pass

        return record

    def remove(self, name: str) -> bool:
        """
        Remove a plugin (deletes .py file and registry entry).
        Returns True if removed, False if not found.
        """
        registry = _load_registry()
        if name not in registry["plugins"]:
            if self._logger:
                self._logger.warning(f"  [PLUGINS] Plugin '{name}' not found in registry.")
            return False

        # Delete file
        plugin_file = PLUGINS_DIR / f"{name}.py"
        if plugin_file.exists():
            plugin_file.unlink()

        del registry["plugins"][name]
        _save_registry(registry)

        # Purge from live registry
        try:
            from plugins.loader import get_registry
            reg = get_registry()
            reg.clear()
        except Exception:
            pass

        if self._logger:
            self._logger.success(f"  [PLUGINS] Removed plugin '{name}'")
        return True

    def enable(self, name: str) -> bool:
        return self._set_enabled(name, True)

    def disable(self, name: str) -> bool:
        return self._set_enabled(name, False)

    def info(self, name: str) -> Optional[PluginRecord]:
        """Return detailed info for a specific plugin."""
        registry = _load_registry()
        data = registry["plugins"].get(name)
        return PluginRecord(data) if data else None

    # ──────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────

    def _set_enabled(self, name: str, enabled: bool) -> bool:
        registry = _load_registry()
        if name not in registry["plugins"]:
            if self._logger:
                self._logger.warning(f"  [PLUGINS] Plugin '{name}' not found.")
            return False
        registry["plugins"][name]["enabled"] = enabled
        _save_registry(registry)
        state = "enabled" if enabled else "disabled"
        if self._logger:
            self._logger.success(f"  [PLUGINS] Plugin '{name}' {state}.")
        return True

    def _validate_plugin_file(self, path: Path):
        """
        Validate a plugin .py file against the plugin contract.
        Returns (name, description, rule_type) tuple.
        Raises ValueError if contract is violated.
        """
        # Ensure project root is importable (needed for engines.rule_engine etc.)
        project_root = str(Path(__file__).parent.parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        module_name = f"_aurelion_validate_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if not spec or not spec.loader:
            raise ValueError(f"Cannot load plugin file: {path}")

        module = importlib.util.module_from_spec(spec)
        # Register module in sys.modules BEFORE exec so dataclasses can resolve __module__
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            sys.modules.pop(module_name, None)
            raise ValueError(f"Plugin execution error: {e}")

        required = ("RULE_TYPE", "RULE_CLASS", "execute")
        for attr in required:
            if not hasattr(module, attr):
                raise ValueError(
                    f"Plugin missing required attribute '{attr}'. "
                    f"Required: {', '.join(required)}"
                )

        rule_type   = getattr(module, "RULE_TYPE")
        description = getattr(module, "DESCRIPTION", "")
        name        = path.stem

        if not isinstance(rule_type, str) or not rule_type.strip():
            raise ValueError("RULE_TYPE must be a non-empty string.")

        return name, description, rule_type.strip().lower()

    def _sync_registry_with_disk(self) -> None:
        """
        Ensure the registry matches actual .py files on disk.
        Adds missing files to registry; removes registry entries for deleted files.
        """
        registry = _load_registry()
        changed  = False

        # Remove entries for deleted files
        for pname in list(registry["plugins"].keys()):
            plugin_file = PLUGINS_DIR / f"{pname}.py"
            if not plugin_file.exists():
                del registry["plugins"][pname]
                changed = True

        # Discover unregistered plugins
        for py_file in PLUGINS_DIR.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            stem = py_file.stem
            if stem not in registry["plugins"]:
                try:
                    pname, desc, rtype = self._validate_plugin_file(py_file)
                    registry["plugins"][pname] = {
                        "name":         pname,
                        "description":  desc,
                        "version":      "auto",
                        "source":       "discovered",
                        "installed_at": "",
                        "file":         py_file.name,
                        "enabled":      True,
                        "rule_type":    rtype,
                    }
                    changed = True
                except Exception:
                    pass

        if changed:
            _save_registry(registry)
