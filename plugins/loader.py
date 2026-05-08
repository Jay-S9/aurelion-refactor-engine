"""
Aurelion Refactor Engine v4 - Plugin Loader
Dynamically discovers and registers custom rule types from the /plugins directory.

Plugin contract:
  Each plugin is a Python file in /plugins/ that defines:
    RULE_TYPE: str              — the type string used in plan files
    RULE_CLASS: type            — a RuleBase subclass (dataclass)
    execute(rule, logger, backup_manager) → RuleResult

Example plugin (plugins/uppercase_plugin.py):
─────────────────────────────────────────────────────
from dataclasses import dataclass
from engines.rule_engine import RuleBase, RuleResult

RULE_TYPE = "uppercase"

@dataclass
class UppercaseRule(RuleBase):
    extensions: list = None

def execute(rule, logger, backup_manager):
    result = RuleResult(rule.name, rule.rule_type)
    # ... implementation ...
    return result
─────────────────────────────────────────────────────

The PluginRegistry is a thread-safe singleton that the RuleExecutor
queries before raising "Unknown rule type". RuleParser queries it too
to accept the new type strings during validation.

NEW IN v4:
  - PluginRegistry singleton
  - PluginLoader that walks /plugins/*.py and registers them
  - Plugin validation (required attributes, execute() signature)
  - PluginError with clear diagnostics
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type

from engines.rule_engine import RuleBase, RuleResult


# ── Plugin error ──────────────────────────────────────────────────────────────

class PluginError(Exception):
    """Raised when a plugin fails to load or violates the contract."""
    pass


# ── Registered plugin entry ───────────────────────────────────────────────────

class PluginEntry:
    """Holds metadata and callable for one registered plugin."""

    def __init__(
        self,
        rule_type:   str,
        rule_class:  Type[RuleBase],
        execute_fn:  Callable,
        source_file: Path,
        description: str = "",
    ):
        self.rule_type   = rule_type
        self.rule_class  = rule_class
        self.execute_fn  = execute_fn
        self.source_file = source_file
        self.description = description

    def __repr__(self) -> str:
        return f"Plugin(type={self.rule_type!r}, source={self.source_file.name!r})"


# ── Plugin Registry ───────────────────────────────────────────────────────────

class PluginRegistry:
    """
    Thread-safe singleton registry for custom rule types.
    Queried by RuleExecutor and RuleParser.
    """
    _instance: Optional["PluginRegistry"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "PluginRegistry":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._plugins: Dict[str, PluginEntry] = {}
                cls._instance._registry_lock = threading.RLock()
            return cls._instance

    def register(self, entry: PluginEntry) -> None:
        """Register a plugin. Overwrites if type already registered."""
        with self._registry_lock:
            self._plugins[entry.rule_type] = entry

    def get(self, rule_type: str) -> Optional[PluginEntry]:
        """Retrieve a plugin by its rule type string."""
        with self._registry_lock:
            return self._plugins.get(rule_type)

    def is_registered(self, rule_type: str) -> bool:
        with self._registry_lock:
            return rule_type in self._plugins

    def all_types(self) -> List[str]:
        with self._registry_lock:
            return sorted(self._plugins.keys())

    def all_entries(self) -> List[PluginEntry]:
        with self._registry_lock:
            return list(self._plugins.values())

    def clear(self) -> None:
        """For testing only."""
        with self._registry_lock:
            self._plugins.clear()


# ── Plugin Loader ─────────────────────────────────────────────────────────────

class PluginLoader:
    """
    Discovers and loads plugin files from a directory.
    Each .py file in the plugins directory is inspected for the plugin contract.
    """

    REQUIRED_ATTRIBUTES = ("RULE_TYPE", "RULE_CLASS", "execute")

    # Internal infrastructure files that live in the plugins/ directory but are
    # NOT plugins — silently skipped so they never produce spurious warnings.
    INFRASTRUCTURE_FILES = frozenset({
        "loader.py",
        "manager.py",
        "__init__.py",
    })

    def __init__(self, plugins_dir: Optional[Path] = None, logger=None):
        self._dir    = plugins_dir or (Path(__file__).parent.parent / "plugins")
        self._logger = logger
        self._registry = PluginRegistry()

    def load_all(self) -> List[PluginEntry]:
        """
        Scan plugins directory and load all valid plugins.
        Returns list of successfully loaded PluginEntry objects.
        Invalid plugins are logged and skipped (never crash the engine).
        """
        if not self._dir.exists():
            return []

        loaded: List[PluginEntry] = []

        for py_file in sorted(self._dir.glob("*.py")):
            # Skip private/dunder files (__init__.py, _private.py, etc.)
            if py_file.name.startswith("_"):
                continue

            # Skip known infrastructure files that are not plugins
            if py_file.name in self.INFRASTRUCTURE_FILES:
                continue

            try:
                entry = self._load_plugin(py_file)
                if entry:
                    self._registry.register(entry)
                    loaded.append(entry)
                    if self._logger:
                        self._logger.success(
                            f"Plugin loaded: '{entry.rule_type}' ← {py_file.name}"
                        )
            except PluginError as e:
                if self._logger:
                    self._logger.warning(f"Plugin skipped ({py_file.name}): {e}")
            except Exception as e:
                if self._logger:
                    self._logger.error(
                        f"Plugin error ({py_file.name}): {e}"
                    )

        return loaded

    def _load_plugin(self, path: Path) -> Optional[PluginEntry]:
        """
        Load a single plugin file.
        Validates the plugin contract before registering.
        Returns PluginEntry or None.
        """
        # Load module dynamically
        module_name = f"aurelion_plugin_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise PluginError(f"Cannot create module spec from {path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        try:
            spec.loader.exec_module(module)
        except Exception as e:
            raise PluginError(f"Error executing plugin module: {e}") from e

        # Validate required attributes
        for attr in self.REQUIRED_ATTRIBUTES:
            if not hasattr(module, attr):
                raise PluginError(
                    f"Missing required attribute '{attr}'. "
                    f"Plugin must define: {', '.join(self.REQUIRED_ATTRIBUTES)}"
                )

        rule_type   = getattr(module, "RULE_TYPE")
        rule_class  = getattr(module, "RULE_CLASS")
        execute_fn  = getattr(module, "execute")
        description = getattr(module, "DESCRIPTION", "")

        # Type validation
        if not isinstance(rule_type, str) or not rule_type.strip():
            raise PluginError("RULE_TYPE must be a non-empty string.")

        if not (isinstance(rule_class, type) and issubclass(rule_class, RuleBase)):
            raise PluginError(
                f"RULE_CLASS must be a subclass of RuleBase, got: {rule_class!r}"
            )

        if not callable(execute_fn):
            raise PluginError("execute must be a callable.")

        # Check execute() signature: execute(rule, logger, backup_manager) → RuleResult
        sig    = inspect.signature(execute_fn)
        params = list(sig.parameters.keys())
        if len(params) < 3:
            raise PluginError(
                f"execute() must accept at least 3 parameters "
                f"(rule, logger, backup_manager), got: {params}"
            )

        return PluginEntry(
            rule_type=rule_type.strip().lower(),
            rule_class=rule_class,
            execute_fn=execute_fn,
            source_file=path,
            description=description,
        )


# ── Convenience accessor ──────────────────────────────────────────────────────

def get_registry() -> PluginRegistry:
    """Return the global PluginRegistry singleton."""
    return PluginRegistry()


def load_plugins(plugins_dir: Optional[Path] = None, logger=None) -> List[PluginEntry]:
    """
    Load all plugins from the plugins directory.
    Safe to call multiple times (idempotent via registry).
    """
    loader = PluginLoader(plugins_dir=plugins_dir, logger=logger)
    return loader.load_all()


def generate_example_plugin() -> str:
    """Return an annotated example plugin as a Python string."""
    return '''\
"""
Example Aurelion Plugin: uppercase_plugin.py
Place in: aurelion_refactor_engine/plugins/uppercase_plugin.py

This plugin converts all text in matched files to uppercase.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from engines.rule_engine import RuleBase, RuleResult

# Required: rule type string (used in plan files as  type = "uppercase")
RULE_TYPE = "uppercase"

# Optional: human-readable description shown in --list-plugins
DESCRIPTION = "Convert matched file content to uppercase"


# Required: dataclass extending RuleBase
@dataclass
class UppercaseRule(RuleBase):
    # Add any extra fields your plugin needs
    extensions: List[str] = field(default_factory=lambda: [".txt", ".md"])


# Required alias
RULE_CLASS = UppercaseRule


# Required: execute(rule, logger, backup_manager) → RuleResult
def execute(rule: UppercaseRule, logger, backup_manager) -> RuleResult:
    from engines.rule_engine import _resolve_glob
    from pathlib import Path

    result = RuleResult(rule.name, rule.rule_type)
    base   = Path(rule.base_dir) if rule.base_dir else None
    files  = _resolve_glob(rule.target, rule.exclude_dirs, rule.exclude_paths, base_dir=base)

    if not files:
        logger.info(f"  Plugin \'{rule.name}\': no files matched \'{rule.target}\'")
        return result

    for path in files:
        try:
            if not rule.dry_run:
                if not rule.no_backup:
                    backup_manager.backup_files([path])
                content = path.read_text(encoding=rule.encoding, errors="replace")
                path.write_text(content.upper(), encoding=rule.encoding)
                result.modified.append(str(path))
                logger.success(f"Uppercased: {path}")
            else:
                result.modified.append(str(path))
        except Exception as e:
            result.errors.append((str(path), str(e)))
            logger.error(f"Plugin error: {path} — {e}")

    return result
'''
