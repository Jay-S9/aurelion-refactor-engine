"""
Aurelion Refactor Engine v3 - Rule Parser
Parses TOML or JSON plan files into validated typed Rule objects.

Supported formats:
  TOML:  plan.toml   (recommended — readable, supports comments)
  JSON:  plan.json   (for programmatic generation)

Plan file schema (TOML):
─────────────────────────────────────────────────────────────────
[defaults]
encoding        = "utf-8"
no_backup       = false
exclude_dirs    = [".git", "__pycache__", "node_modules"]
workers         = 4

[[rules]]
name            = "rule1"
type            = "replace"
find            = "OLD_API"
replace         = "NEW_API"
target          = "**/*.py"

[[rules]]
name            = "rule2"
type            = "replace"
find            = "TODO"
replace         = "DONE"
target          = "**/*.md"
case_insensitive = true

[[rules]]
name            = "rule3"
type            = "replace_file"
source          = "templates/header.py"
target          = "**/header.py"
overwrite       = true

[[rules]]
name            = "rule4"
type            = "inject"
source          = "templates/PROJECT_CORE.md"
target          = "**/PROJECT_CORE.md"
mode            = "replace"
─────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from engines.rule_engine import (
    RuleBase, ReplaceRule, ReplaceFileRule, InjectRule,
    VALID_RULE_TYPES, RULE_REPLACE, RULE_REPLACE_FILE, RULE_INJECT,
    INJECT_MODE_REPLACE, INJECT_MODE_PREPEND, INJECT_MODE_APPEND,
    get_all_valid_types,
)

# TOML loader — stdlib 3.11+, else tomli
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


# ── Plan data model ───────────────────────────────────────────────────────────

class Plan:
    """
    A validated, executable refactor plan.
    Contains a list of typed Rule objects and plan-level defaults.
    """

    def __init__(
        self,
        name: str,
        rules: List[RuleBase],
        defaults: Dict[str, Any],
        source_path: Path,
    ):
        self.name        = name
        self.rules       = rules
        self.defaults    = defaults
        self.source_path = source_path

    @property
    def enabled_rules(self) -> List[RuleBase]:
        return [r for r in self.rules if r.enabled]

    def rules_for_group(self, group: str) -> List[RuleBase]:
        """Return enabled rules belonging to a specific group."""
        return [r for r in self.enabled_rules if r.group == group]

    def rules_for_tag(self, tag: str) -> List[RuleBase]:
        """Return enabled rules that have a specific tag."""
        return [r for r in self.enabled_rules if tag in (r.tags or [])]

    @property
    def groups(self) -> List[str]:
        """All unique group values across enabled rules."""
        seen = []
        for r in self.enabled_rules:
            if r.group and r.group not in seen:
                seen.append(r.group)
        return seen

    def __repr__(self) -> str:
        return (
            f"Plan(name={self.name!r}, rules={len(self.rules)}, "
            f"enabled={len(self.enabled_rules)})"
        )


# ── Validation errors ─────────────────────────────────────────────────────────

class PlanValidationError(Exception):
    """Raised when a plan file fails validation. Contains all errors."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} validation error(s):\n"
            + "\n".join(f"  • {e}" for e in errors)
        )


# ── Public API ────────────────────────────────────────────────────────────────

def parse_plan(plan_path: str | Path) -> Plan:
    """
    Load and validate a plan file (TOML or JSON).
    Returns a Plan object on success.
    Raises PlanValidationError on any schema / semantic errors.
    Raises FileNotFoundError if the plan file does not exist.
    """
    path = Path(plan_path)
    if not path.exists():
        raise FileNotFoundError(f"Plan file not found: {path}")

    suffix = path.suffix.lower()
    raw: Dict[str, Any]

    if suffix == ".json":
        raw = _load_json(path)
    elif suffix in (".toml", ""):
        raw = _load_toml(path)
    else:
        # Try TOML first, then JSON
        try:
            raw = _load_toml(path)
        except Exception:
            raw = _load_json(path)

    return _build_plan(raw, path)


def generate_example_plan() -> str:
    """Return an annotated example plan.toml as a string."""
    return """\
# ══════════════════════════════════════════════════════════
#  Aurelion Refactor Engine v3 — Example Plan File
#  Run with:  aurelion run plan.toml
#             aurelion run plan.toml --dry-run
#             aurelion run plan.toml --workers 8
# ══════════════════════════════════════════════════════════

[plan]
name = "My Refactor Plan"

# ── Plan-wide defaults (overridden per-rule) ──────────────
[defaults]
encoding     = "utf-8"
no_backup    = false
workers      = 4
exclude_dirs = [".git", "__pycache__", "node_modules", ".venv", "backups", "logs"]

# ── Rules (executed top-to-bottom) ───────────────────────

[[rules]]
name    = "update-api-url"
type    = "replace"
find    = "http://old-api.internal/v1"
replace = "https://new-api.internal/v2"
target  = "**/*.py"

[[rules]]
name             = "mark-todos-done"
type             = "replace"
find             = "TODO"
replace          = "DONE"
target           = "**/*.md"
case_insensitive = true

[[rules]]
name      = "distribute-license-header"
type      = "replace_file"
source    = "templates/LICENSE_HEADER.py"
target    = "**/header.py"
overwrite = true

[[rules]]
name   = "inject-project-template"
type   = "inject"
source = "templates/PROJECT_CORE.md"
target = "**/PROJECT_CORE.md"
mode   = "replace"

# ── Disabled rule example ─────────────────────────────────
[[rules]]
name    = "legacy-migration"
type    = "replace"
find    = "legacy_module"
replace = "new_module"
target  = "**/*.py"
enabled = false
"""


# ── Loaders ───────────────────────────────────────────────────────────────────

def _load_toml(path: Path) -> Dict[str, Any]:
    if tomllib is None:
        raise RuntimeError(
            "TOML support requires Python 3.11+ or the 'tomli' package.\n"
            "  Install: pip install tomli"
        )
    with open(path, "rb") as f:
        return tomllib.load(f)


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Plan builder ──────────────────────────────────────────────────────────────

def _build_plan(raw: Dict[str, Any], source_path: Path) -> Plan:
    errors: List[str] = []

    # Plan-level metadata
    plan_meta   = raw.get("plan", {})
    plan_name   = plan_meta.get("name", source_path.stem)
    defaults    = raw.get("defaults", {})

    # Validate defaults block
    _validate_defaults(defaults, errors)

    # Rule entries
    raw_rules: List[Dict[str, Any]] = raw.get("rules", [])
    if not raw_rules:
        errors.append("Plan contains no [[rules]] entries.")

    if errors:
        raise PlanValidationError(errors)

    rules: List[RuleBase] = []
    seen_names: set[str]  = set()

    plan_base_dir = str(source_path.parent.resolve())

    for i, raw_rule in enumerate(raw_rules):
        rule_id = raw_rule.get("name", f"rule_{i+1}")
        rule_errors: List[str] = []

        # Check for duplicate names
        if rule_id in seen_names:
            rule_errors.append(f"Duplicate rule name: '{rule_id}'")
        seen_names.add(rule_id)

        rule = _build_rule(rule_id, raw_rule, defaults, rule_errors, plan_base_dir)
        errors.extend(rule_errors)

        if rule is not None:
            rules.append(rule)

    if errors:
        raise PlanValidationError(errors)

    return Plan(name=plan_name, rules=rules, defaults=defaults, source_path=source_path)


def _build_rule(
    name: str,
    raw: Dict[str, Any],
    defaults: Dict[str, Any],
    errors: List[str],
    base_dir: str = "",
) -> Optional[RuleBase]:
    """
    Build a typed Rule from a raw dict, merging plan-level defaults.
    Appends validation errors to the errors list (caller checks emptiness).
    """
    rule_type = raw.get("type", "").strip().lower()

    if not rule_type:
        errors.append(f"Rule '{name}': missing required field 'type'.")
        return None

    all_types = get_all_valid_types()
    if rule_type not in all_types:
        errors.append(
            f"Rule '{name}': unknown type '{rule_type}'. "
            f"Valid types: {sorted(all_types)}"
        )
        return None

    # ── Common fields (defaults → rule override) ──────────────────
    def get(key: str, fallback: Any = None) -> Any:
        return raw.get(key, defaults.get(key, fallback))

    common = dict(
        name           = name,
        rule_type      = rule_type,
        target         = raw.get("target", ""),
        enabled        = raw.get("enabled", True),
        dry_run        = get("dry_run", False),
        no_backup      = get("no_backup", False),
        encoding       = get("encoding", "utf-8"),
        exclude_dirs   = get("exclude_dirs", [".git", "__pycache__", "node_modules", ".venv", "backups", "logs"]),
        exclude_paths  = raw.get("exclude_paths", []),
        include_binary = raw.get("include_binary", False),
        workers        = int(get("workers", 1)),
        base_dir       = base_dir,
        # v4 fields
        depends_on     = raw.get("depends_on", []),
        group          = raw.get("group", ""),
        tags           = raw.get("tags", []),
    )

    if not common["target"]:
        errors.append(f"Rule '{name}': missing required field 'target'.")
        return None

    # ── Type-specific fields ──────────────────────────────────────
    if rule_type == RULE_REPLACE:
        find    = raw.get("find", "")
        replace = raw.get("replace", "")
        if not find:
            errors.append(f"Rule '{name}' (replace): missing required field 'find'.")
            return None
        return ReplaceRule(
            **common,
            find=find,
            replace=replace,
            case_insensitive=raw.get("case_insensitive", False),
        )

    elif rule_type == RULE_REPLACE_FILE:
        source = raw.get("source", "")
        if not source:
            errors.append(f"Rule '{name}' (replace_file): missing required field 'source'.")
            return None
        return ReplaceFileRule(
            **common,
            source=source,
            overwrite=raw.get("overwrite", True),
        )

    elif rule_type == RULE_INJECT:
        source = raw.get("source", "")
        mode   = raw.get("mode", INJECT_MODE_REPLACE)
        if not source:
            errors.append(f"Rule '{name}' (inject): missing required field 'source'.")
            return None
        valid_modes = {INJECT_MODE_REPLACE, INJECT_MODE_PREPEND, INJECT_MODE_APPEND}
        if mode not in valid_modes:
            errors.append(
                f"Rule '{name}' (inject): invalid mode '{mode}'. "
                f"Valid: {sorted(valid_modes)}"
            )
            return None
        return InjectRule(
            **common,
            source=source,
            mode=mode,
            overwrite=raw.get("overwrite", True),
        )

    # Try to handle plugin-registered rule types
    try:
        from plugins.loader import get_registry
        plugin = get_registry().get(rule_type)
        if plugin:
            # Instantiate the plugin's rule class with common fields
            # Filter common fields to only those accepted by the plugin's class
            import dataclasses
            accepted = {f.name for f in dataclasses.fields(plugin.rule_class)}
            filtered = {k: v for k, v in common.items() if k in accepted}
            try:
                return plugin.rule_class(**filtered)
            except Exception as e:
                errors.append(f"Rule '{name}': failed to instantiate plugin rule class: {e}")
                return None
    except ImportError:
        pass

    return None


# ── Defaults validator ────────────────────────────────────────────────────────

def _validate_defaults(defaults: Dict[str, Any], errors: List[str]) -> None:
    if "workers" in defaults:
        w = defaults["workers"]
        if not isinstance(w, int) or w < 1:
            errors.append(f"[defaults] workers must be a positive integer, got: {w!r}")

    if "encoding" in defaults:
        enc = defaults["encoding"]
        try:
            "test".encode(enc)
        except LookupError:
            errors.append(f"[defaults] encoding '{enc}' is not a valid Python codec.")
