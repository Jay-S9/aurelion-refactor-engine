# Changelog

All notable changes to Aurelion Refactor Engine are documented here.

---

## [7.0.1] — 2026-05-07 — Bug Fix Release

### Fixed

- **`inject --dry-run` falsely reported `Modified: N file(s)`** — The inject
  pipeline now exits before calling `logger.report()` when `--dry-run` is
  active, printing `[DRY RUN] No files were modified.` instead. This matches
  the identical pattern already used by `replace`, `replace-file`, and
  `copy-file`. Affected file: `core/executor.py → _run_inject()`.

- **`--all` scanned and modified `./logs/`** — The tool's own session log
  files were being included in recursive scans, causing logged operation text
  to be replaced alongside user files. `"logs"` is now included in the default
  `exclude_dirs` list across all entry points: `core/parser.py` (replace +
  inject subparsers), `engines/rule_engine.py` (RuleBase dataclass default),
  and `utils/rule_parser.py` (plan defaults + `_build_rule()` fallback).

- **`plugins/loader.py` and `plugins/manager.py` emitted false-positive plugin
  warnings on every `run`** — The `PluginLoader` previously had no guard
  against infrastructure files that live in the `plugins/` directory but are
  not plugins. A new `INFRASTRUCTURE_FILES` frozenset (`loader.py`,
  `manager.py`, `__init__.py`) is now checked before attempting to load each
  file. Affected file: `plugins/loader.py → PluginLoader.load_all()`.

### Changed

- Version string updated consistently across `main.py`, `core/parser.py`,
  `core/dashboard.py`, `core/server.py`, `core/db.py`, and `pyproject.toml`.

---

## [7.0.0] — 2026-04-20 — v7 Major Release

### Added

- **SQLite persistence** — Full execution history stored in `history/aurelion.db`
  with WAL mode. `db stats`, `db migrate`, `db export` sub-commands.
- **Authentication middleware** — API key + Bearer token auth for server mode.
  `auth generate-key` command. Configurable via `AURELION_API_KEY` env var.
- **HTTP server with Web UI** — `server` command spins up a local REST API on
  port 7070. Bundled `web/index.html` (25 KB) served automatically.
- **Rate limiting** — Per-IP sliding window, configurable via
  `AURELION_RATE_LIMIT` env var.
- **`dashboard` command** — Full ANSI CLI dashboard showing system status,
  recent runs, statistics, and performance metrics.
- **`.env` file support** — Environment variables loaded from `.env` on startup.
- **CSV history export** — `GET /history/export/csv` endpoint.
- **Multi-user profile system** — `profile list/create` with `dev`, `prod`,
  `staging` pre-built profiles.

---

## [6.0.0] — 2026-03-10 — Plan Engine + AI

### Added

- **`run` command** — Execute TOML/JSON plan files with multiple typed rules.
- **`ai` command** — Natural language → TOML plan via Claude API.
- **`preview` command** — Visualise plan DAG, file counts, dependencies.
- **DAG dependency resolution** — Rules reordered via `depends_on` field.
- **Plugin system** — `plugins/` directory with marketplace-style
  install/enable/disable/remove commands.
- **Incremental execution** — SHA-256 file hashing to skip unchanged files
  (`--incremental` flag).
- **Performance report** — Per-rule timing table with `--perf` flag.

---

## [5.0.0] — 2026-02-01

### Added

- Multi-environment profiles.
- `restore` command with `--list` and `--last` flags.
- Parallel scan via `--workers N` (thread pool).

---

## [4.0.0] — 2026-01-10

### Added

- `inject` command — prepend/append/replace templates into glob-matched files.
- Config file support (`--config FILE` / `aurelion.toml`).

---

## [3.0.0] — 2025-12-15

### Added

- `copy-file` and `replace-file` commands.
- Conflict manager (thread-safe file locking).

---

## [2.0.0] — 2025-11-20

### Added

- Binary file guard, encoding detection, large-file streaming.
- Atomic write (`.tmp` → rename) to prevent partial writes.
- `history` command with SQLite-backed run log.

---

## [1.0.0] — 2025-10-01 — Initial Release

- Core `replace` command with `--all`, `--dir`, `--file`, `--ext`,
  `--dry-run`, `--ignore-case`, `--no-backup`.
- Timestamped backup system.
- Dual console + file logger.
