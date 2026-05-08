# ⚡ Aurelion Refactor Engine

> **Production-grade codebase refactoring automation. Bulk text replacement,
> file operations, AI-powered plan generation, REST API, Web UI — zero
> mandatory dependencies.**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-7.0.1-purple)](CHANGELOG.md)
[![Zero Deps](https://img.shields.io/badge/dependencies-zero-brightgreen)](pyproject.toml)

---

## What It Does

Aurelion lets you migrate entire codebases in seconds. Replace strings across
10 000 files, distribute config templates, inject headers, or run a multi-step
migration plan — all with dry-run preview, automatic backup, full history,
and optional AI planning via the Claude API.

```bash
aurelion replace "http://api.v1" "https://api.v2" --all --ext .py .toml
aurelion run migration.toml --dry-run
aurelion ai "rename all DEBUG flags to LOG_LEVEL across Python files" --run
aurelion server                       # Web UI → http://localhost:7070
```

---

## Feature Matrix

| Category      | Feature                                                                 |
|---------------|-------------------------------------------------------------------------|
| **Text ops**  | Bulk find/replace · case-insensitive · extension filter · recursive     |
| **File ops**  | `copy-file` · `replace-file` · multi-target distribution                |
| **Template**  | `inject` — prepend / append / replace into glob-matched files           |
| **Plans**     | TOML/JSON plan files · DAG dependency resolution · group/tag filters    |
| **Safety**    | Dry-run preview · diff view · timestamped backup · `restore`            |
| **Parallel**  | `--workers N` thread pool for large projects                            |
| **AI**        | `ai` — Claude API converts plain English to a TOML plan                 |
| **Web UI**    | `server` — full dashboard at `http://localhost:7070`                    |
| **History**   | SQLite-backed execution log · `history --stats`                         |
| **Plugins**   | Drop `.py` files into `plugins/` to add custom rule types               |
| **Profiles**  | Named env configs: `dev`, `prod`, `staging`                             |
| **Auth**      | API key + Bearer token for server mode                                  |
| **Zero deps** | Pure Python stdlib — no pip install required on 3.11+                   |

---

## Quick Start

```bash
# Python 3.11+ — no install needed
python main.py --help

# Python 3.9 / 3.10 — one optional backport
pip install tomli

# Permanent alias (Linux/macOS)
alias aurelion="python /path/to/aurelion_refactor_engine/main.py"

# Windows — run as Administrator
install.bat
```

---

## Commands

### `replace`

```bash
aurelion replace "OldClass" "NewClass" --all --dry-run
aurelion replace "todo" "DONE"  --all --ext .py --ignore-case
aurelion replace "v1" "v2"      --dir ./src --workers 8 --yes

# Key flags
#   --all | --dir PATH | --file FILE   target (one required)
#   --ext .py .md ...                  extension filter
#   --exclude-dir DIR ...              skip dirs (default: .git __pycache__ node_modules .venv backups logs)
#   --ignore-case                      case-insensitive match
#   --workers N                        parallel threads
#   --dry-run                          preview only — no writes
#   --no-backup                        skip backup creation
#   --yes / -y                         skip confirmation prompt
```

### `copy-file` / `replace-file`

```bash
aurelion copy-file   config/base.toml  services/api/  services/worker/  --dry-run
aurelion replace-file templates/header.py  src/module.py
```

### `inject`

```bash
aurelion inject copyright.py --target "**/*.py" --prepend --dry-run
# Modes: --prepend | --append | --replace
```

### `run` — Plan execution

```bash
aurelion run migration.toml
aurelion run migration.toml --dry-run
aurelion run migration.toml --workers 8 --group phase-1 --perf
aurelion run migration.toml --validate       # validate only
aurelion run migration.toml --incremental    # skip unchanged files (SHA-256)
aurelion run --example                       # print annotated example plan
```

**Example `migration.toml`:**

```toml
[plan]
name = "API v1 → v2 Migration"

[defaults]
workers      = 4
exclude_dirs = [".git", "__pycache__", "backups", "logs"]

[[rules]]
name    = "update-url"
type    = "replace"
find    = "http://api.internal/v1"
replace = "https://api.internal/v2"
target  = "**/*.py"

[[rules]]
name       = "update-config"
type       = "replace"
find       = "api_version = 1"
replace    = "api_version = 2"
target     = "**/*.toml"
depends_on = ["update-url"]
```

### Other commands

```bash
aurelion preview  migration.toml          # visualise DAG
aurelion ai       "describe migration"    # NL → plan (needs ANTHROPIC_API_KEY)
aurelion history  --stats
aurelion restore  --list
aurelion server   --port 7070
aurelion dashboard
aurelion db       stats
aurelion auth     generate-key
aurelion plugins  list
aurelion profile  list
```

---

## Configuration

**`aurelion.toml`** (project root or `--config FILE`):

```toml
[defaults]
encoding     = "utf-8"
workers      = 4
no_backup    = false
exclude_dirs = [".git", "__pycache__", "node_modules", ".venv", "backups", "logs"]

[server]
port = 7070
host = "127.0.0.1"

[ai]
model = "claude-sonnet-4-20250514"
```

**`.env`** (loaded automatically):

```
ANTHROPIC_API_KEY=sk-ant-...
AURELION_API_KEY=aur_...
AURELION_RATE_LIMIT=60
```

---

## Plugin API

```python
# plugins/my_plugin.py
from dataclasses import dataclass
from engines.rule_engine import RuleBase, RuleResult

RULE_TYPE   = "my_type"
DESCRIPTION = "What this plugin does"

@dataclass
class MyRule(RuleBase):
    pass

RULE_CLASS = MyRule

def execute(rule: MyRule, logger, backup_manager) -> RuleResult:
    result = RuleResult(rule.name, rule.rule_type)
    # ... your logic ...
    return result
```

---

## License

MIT — see [LICENSE](LICENSE).

## Author

Built and designed by Jay Solanki — [Aurelion Elite](https://github.com/Jay-S9)