"""
Aurelion Refactor Engine v3 - CLI Parser
CHANGES IN v3: run command, inject command, --workers, --strict, v7.0.1
"""

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aurelion",
        description=(
            "\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557\n"
            "\u2551   AURELION REFACTOR ENGINE v7.0.1    \u2551\n"
            "\u2551   Intelligent Automation Engine      \u2551\n"
            "\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_build_examples(),
    )
    parser.add_argument("--version", action="version", version="Aurelion Refactor Engine v7.0.1")
    parser.add_argument("--config",  metavar="FILE",    help="Path to aurelion.toml", default=None)
    parser.add_argument("--profile", metavar="PROFILE", help="Active configuration profile (e.g. dev, prod)", default=None)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    _add_replace_command(sub)
    _add_replace_file_command(sub)
    _add_copy_file_command(sub)
    _add_inject_command(sub)
    _add_run_command(sub)
    _add_preview_command(sub)
    _add_history_command(sub)
    _add_auth_command(sub)
    _add_db_command(sub)
    _add_ai_command(sub)
    _add_dashboard_command(sub)
    _add_server_command(sub)
    _add_plugins_command(sub)
    _add_profile_command(sub)
    _add_restore_command(sub)
    return parser


def _add_replace_command(sub):
    p = sub.add_parser("replace", help="Replace text across files")
    p.add_argument("old_text", metavar="OLD")
    p.add_argument("new_text", metavar="NEW")
    tg = p.add_mutually_exclusive_group(required=True)
    tg.add_argument("--all",  dest="target_all",  action="store_true")
    tg.add_argument("--dir",  metavar="PATH", dest="target_dir")
    tg.add_argument("--file", metavar="FILE", dest="target_file")
    p.add_argument("--ext",         metavar="EXT", dest="extensions", nargs="+")
    p.add_argument("--exclude-dir", metavar="DIR", dest="exclude_dirs", nargs="+",
        default=[".git", "__pycache__", "node_modules", ".venv", "backups", "logs"])
    p.add_argument("--case-sensitive", dest="case_sensitive", action="store_true", default=True)
    p.add_argument("--ignore-case",    dest="case_sensitive", action="store_false")
    p.add_argument("--workers", metavar="N", dest="workers", type=int, default=1,
        help="Parallel worker threads (default: 1)")
    p.add_argument("--all-diff", dest="all_diff", action="store_true", default=False)
    p.add_argument("--dry-run",   action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--encoding",  default="utf-8")


def _add_replace_file_command(sub):
    p = sub.add_parser("replace-file", help="Replace a file with another file")
    p.add_argument("source", metavar="SOURCE")
    p.add_argument("target", metavar="TARGET")
    p.add_argument("--overwrite",     action="store_true",  default=True)
    p.add_argument("--skip-existing", dest="overwrite",     action="store_false")
    p.add_argument("--dry-run",       action="store_true")
    p.add_argument("--no-backup",     action="store_true")
    p.add_argument("--yes", "-y",     action="store_true")


def _add_copy_file_command(sub):
    p = sub.add_parser("copy-file", help="Copy a file to multiple targets")
    p.add_argument("source",  metavar="SOURCE")
    p.add_argument("targets", metavar="TARGET", nargs="+")
    p.add_argument("--overwrite",     action="store_true", default=True)
    p.add_argument("--skip-existing", dest="overwrite",    action="store_false")
    p.add_argument("--dry-run",       action="store_true")
    p.add_argument("--yes", "-y",     action="store_true")


def _add_inject_command(sub):
    p = sub.add_parser("inject", help="Inject a template into matching targets")
    p.add_argument("source", metavar="TEMPLATE")
    p.add_argument("--target", metavar="GLOB", required=True,
        help="Glob pattern  e.g. \'**/*.py\'")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--replace", dest="inject_mode", action="store_const", const="replace")
    mode.add_argument("--prepend", dest="inject_mode", action="store_const", const="prepend")
    mode.add_argument("--append",  dest="inject_mode", action="store_const", const="append")
    p.set_defaults(inject_mode="replace")
    p.add_argument("--exclude-dir", metavar="DIR", dest="exclude_dirs", nargs="+",
        default=[".git", "__pycache__", "node_modules", ".venv", "backups", "logs"])
    p.add_argument("--encoding",  default="utf-8")
    p.add_argument("--dry-run",   action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")


def _add_run_command(sub):
    p = sub.add_parser("run", help="Execute a TOML/JSON rule plan",
        description="Run a plan file with multiple refactor rules.")
    p.add_argument("plan_file", metavar="PLAN", nargs="?")
    p.add_argument("--dry-run",   action="store_true")
    p.add_argument("--workers",   metavar="N", type=int, default=None)
    p.add_argument("--strict",    dest="strict", action="store_true",  default=True)
    p.add_argument("--no-strict", dest="strict", action="store_false")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--example",   action="store_true")
    p.add_argument("--validate",  action="store_true")
    p.add_argument("--group",     metavar="NAME", default=None,
        help="Run only rules in this group")
    p.add_argument("--tag",       metavar="TAG",  default=None,
        help="Run only rules with this tag")
    p.add_argument("--list-plugins", action="store_true",
        help="List all loaded plugins and exit")
    # v5 flags
    p.add_argument("--export",    metavar="FILE", dest="export_path", nargs="?",
        const="__auto__", default=None,
        help="Export JSON report (optional: specify output path)")
    p.add_argument("--incremental", action="store_true", default=False,
        help="Only process files changed since last run")
    p.add_argument("--perf",      action="store_true", default=False,
        help="Show detailed performance metrics after execution")


def _add_preview_command(sub):
    p = sub.add_parser("preview",
        help="Preview plan execution order and affected files",
        description=(
            "Show the plan execution order after dependency resolution,\n"
            "the number of files each rule would affect, and potential conflicts.\n"
            "No files are modified."
        ),
    )
    p.add_argument("plan_file", metavar="PLAN", help="Plan file to preview")
    p.add_argument("--group", metavar="NAME", default=None,
        help="Filter to a specific group")
    p.add_argument("--tag",   metavar="TAG",  default=None,
        help="Filter to a specific tag")


def _add_history_command(sub):
    p = sub.add_parser("history",
        help="Show execution history",
        description=(
            "Display a log of past Aurelion runs with status and timing.\n"
            "Each run is stored in history/runs/ as a JSON file."
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--list",  dest="history_list",  action="store_true", default=True,
        help="List recent runs (default)")
    mode.add_argument("--last",  dest="history_last",  action="store_true",
        help="Show full detail of the most recent run")
    mode.add_argument("--show",  metavar="RUN_ID", dest="history_show",
        help="Show full detail of a specific run ID")
    mode.add_argument("--clear", dest="history_clear", action="store_true",
        help="Delete all history records")
    mode.add_argument("--stats", dest="history_stats", action="store_true",
        help="Show aggregate statistics across all runs")
    p.add_argument("--limit",   metavar="N",  type=int, default=20,
        help="Maximum runs to display (default: 20)")
    p.add_argument("--json",    dest="history_json", action="store_true",
        help="Output in JSON format")
    p.add_argument("--yes", "-y", action="store_true")


def _add_auth_command(sub):
    p = sub.add_parser("auth",
        help="Authentication management",
        description="Generate API keys and manage server authentication.")
    a = p.add_subparsers(dest="auth_action", metavar="ACTION")
    a.required = True
    ag = a.add_parser("generate-key", help="Generate a new API key")
    ag.add_argument("--prefix", default="aur", help="Key prefix (default: aur)")
    a.add_parser("status", help="Show auth configuration status")


def _add_db_command(sub):
    p = sub.add_parser("db",
        help="Database management",
        description="SQLite database operations: migrate, stats, export.")
    a = p.add_subparsers(dest="db_action", metavar="ACTION")
    a.required = True
    a.add_parser("migrate",  help="Import JSON history into SQLite")
    a.add_parser("stats",    help="Show database statistics")
    ec = a.add_parser("export",   help="Export history as CSV")
    ec.add_argument("--output", metavar="FILE", default="aurelion_history.csv")
    a.add_parser("reset",    help="Drop and recreate the database")


def _add_ai_command(sub):
    p = sub.add_parser("ai",
        help="Generate a plan from natural language (AI)",
        description="Use Claude AI to convert a natural language instruction into a plan.")
    p.add_argument("prompt", metavar="INSTRUCTION",
        help="Natural language instruction, e.g. \'Replace TODO with DONE in markdown files\'")
    p.add_argument("--save",  metavar="FILE", dest="save_path", default=None,
        help="Save generated plan to this file path")
    p.add_argument("--run",   action="store_true", default=False,
        help="Execute the generated plan immediately after generation")
    p.add_argument("--explain", action="store_true", default=False,
        help="Ask AI to explain what the generated plan does")
    p.add_argument("--context", metavar="DIR", dest="context_dir", default=None,
        help="Project directory for context (helps AI choose correct file extensions)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")


def _add_dashboard_command(sub):
    p = sub.add_parser("dashboard",
        help="Show live CLI dashboard",
        description="Display a live overview of recent runs, stats, and performance.")
    p.add_argument("--profile", metavar="NAME", default=None,
        help="Profile to show in dashboard status")


def _add_server_command(sub):
    p = sub.add_parser("server",
        help="Start Aurelion as an HTTP service",
        description="Run a lightweight REST server (POST /run, GET /history, POST /ai).")
    p.add_argument("--host",  default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p.add_argument("--port",  type=int, default=7070, help="Port number (default: 7070)")
    p.add_argument("--no-auth", action="store_true", default=False,
        help="Disable authentication even if AURELION_API_KEY is set")


def _add_plugins_command(sub):
    p = sub.add_parser("plugins",
        help="Manage installed plugins",
        description="List, install, remove, enable, and disable Aurelion plugins.")
    action = p.add_subparsers(dest="plugins_action", metavar="ACTION")
    action.required = True

    pl = action.add_parser("list",    help="List all installed plugins")
    pi = action.add_parser("install", help="Install a plugin from a .py file")
    pi.add_argument("source", metavar="FILE", help="Path to plugin .py file")
    pi.add_argument("--name",    metavar="NAME", default=None)
    pi.add_argument("--version", metavar="VER",  default="1.0.0")

    pr = action.add_parser("remove",  help="Remove an installed plugin")
    pr.add_argument("name", metavar="PLUGIN_NAME")

    pe = action.add_parser("enable",  help="Enable a disabled plugin")
    pe.add_argument("name", metavar="PLUGIN_NAME")

    pd = action.add_parser("disable", help="Disable a plugin (keeps files)")
    pd.add_argument("name", metavar="PLUGIN_NAME")

    pin = action.add_parser("info",   help="Show details for a plugin")
    pin.add_argument("name", metavar="PLUGIN_NAME")


def _add_profile_command(sub):
    p = sub.add_parser("profile",
        help="Manage environment profiles",
        description="Create, switch, and manage named configuration profiles.")
    action = p.add_subparsers(dest="profile_action", metavar="ACTION")
    action.required = True

    pl = action.add_parser("list",   help="List available profiles")
    ps = action.add_parser("show",   help="Show profile configuration")
    ps.add_argument("name", metavar="PROFILE_NAME")
    pc = action.add_parser("create", help="Create a new profile")
    pc.add_argument("name",        metavar="PROFILE_NAME")
    pc.add_argument("--description", metavar="DESC", default="")
    pc.add_argument("--base",      metavar="BASE", default="default")
    pd = action.add_parser("delete", help="Delete a profile")
    pd.add_argument("name", metavar="PROFILE_NAME")
    pd.add_argument("--yes", "-y", action="store_true")


def _add_restore_command(sub):
    p = sub.add_parser("restore", help="Restore from a backup session")
    mg = p.add_mutually_exclusive_group(required=True)
    mg.add_argument("--last",    dest="restore_last",    action="store_true")
    mg.add_argument("--session", metavar="PATH", dest="restore_session")
    mg.add_argument("--list",    dest="restore_list",    action="store_true")
    p.add_argument("--yes", "-y", action="store_true")


def _build_examples():
    return """
EXAMPLES:
  aurelion replace "OLD_API" "NEW_API" --all
  aurelion replace "TODO" "DONE" --all --ext .md --ignore-case --dry-run
  aurelion replace "v1" "v2" --dir ./src --workers 8
  aurelion inject header.py --target "**/*.py" --prepend
  aurelion inject LICENSE.md --target "**/LICENSE.md" --replace
  aurelion run plan.toml
  aurelion run plan.toml --dry-run --workers 8
  aurelion run --example
  aurelion run plan.toml --validate
  aurelion run plan.toml --group core
  aurelion run plan.toml --tag safe
  aurelion run plan.toml --export
  aurelion run plan.toml --incremental
  aurelion run plan.toml --perf

  ── History ──────────────────────────────────────────────
  aurelion history
  aurelion history --last
  aurelion history --show 20260414-152301-abc123
  aurelion history --stats
  aurelion history --clear

  ── AI Planner ───────────────────────────────────────────────
  aurelion ai "replace TODO with DONE in markdown files"
  aurelion ai "update API URLs from v1 to v2 in Python" --save plan.toml
  aurelion ai "bump version to 2.0" --run --dry-run

  ── Dashboard / Server ───────────────────────────────────────
  aurelion dashboard
  aurelion server --port 7070

  ── Plugins ──────────────────────────────────────────────────
  aurelion plugins list
  aurelion plugins install ./my_plugin.py
  aurelion plugins remove  my_plugin

  ── Profiles ─────────────────────────────────────────────────
  aurelion profile list
  aurelion profile create staging --base dev
  aurelion --profile prod run plan.toml

  ── Auth & Security ───────────────────────────────────────────
  aurelion auth generate-key
  aurelion auth status
  aurelion server --port 7070         # serves web UI + API

  ── Database ─────────────────────────────────────────────────
  aurelion db migrate
  aurelion db stats
  aurelion db export --output runs.csv

  ── Plan Preview ─────────────────────────────────────────────
  aurelion preview plan.toml
  aurelion preview plan.toml --group core
  aurelion replace-file template.md ./PROJECTS/README.md
  aurelion restore --last
  aurelion restore --list
"""


def validate_args(args: argparse.Namespace) -> None:
    if args.command == "replace":
        if getattr(args, "target_file", None):
            p = Path(args.target_file)
            if not p.exists(): _fail(f"Target file does not exist: {args.target_file}")
            if not p.is_file(): _fail(f"Not a file: {args.target_file}")
        if getattr(args, "target_dir", None):
            p = Path(args.target_dir)
            if not p.exists(): _fail(f"Directory does not exist: {args.target_dir}")
            if not p.is_dir(): _fail(f"Not a directory: {args.target_dir}")
        if getattr(args, "extensions", None):
            args.extensions = [
                ext if ext.startswith(".") else f".{ext}"
                for ext in args.extensions
            ]
        if args.workers < 1:
            _fail(f"--workers must be >= 1, got {args.workers}")

    elif args.command in ("replace-file", "copy-file"):
        src = Path(args.source)
        if not src.exists(): _fail(f"Source not found: {args.source}")
        if not src.is_file(): _fail(f"Not a file: {args.source}")

    elif args.command == "inject":
        src = Path(args.source)
        if not src.exists(): _fail(f"Template not found: {args.source}")
        if not src.is_file(): _fail(f"Not a file: {args.source}")

    elif args.command == "run":
        if getattr(args, "example", False):
            return
        if getattr(args, "list_plugins", False):
            return
        if getattr(args, "plan_file", None) is None:
            _fail("run requires a plan file. Use --example to see a template.")
        p = Path(args.plan_file)
        if not p.exists(): _fail(f"Plan file not found: {args.plan_file}")

    elif args.command == "preview":
        p = Path(args.plan_file)
        if not p.exists(): _fail(f"Plan file not found: {args.plan_file}")

    elif args.command == "history":
        pass   # history requires no pre-validation

    elif args.command == "ai":
        pass   # prompt validated in executor

    elif args.command in ("dashboard", "server", "plugins", "profile", "auth", "db"):
        pass   # handled in executor

    elif args.command == "restore":
        if getattr(args, "restore_session", None):
            p = Path(args.restore_session)
            if not p.exists(): _fail(f"Session not found: {args.restore_session}")
            if not p.is_dir(): _fail(f"Not a directory: {args.restore_session}")


def _fail(message: str) -> None:
    print(f"\n[ERROR] {message}\n", file=sys.stderr)
    sys.exit(1)
