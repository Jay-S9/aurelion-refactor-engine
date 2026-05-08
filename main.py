#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║           AURELION REFACTOR ENGINE  v7.0.1               ║
║   Intelligent Automation Engine                          ║
║                                                          ║
║   v7: Auth · SQLite DB · Web UI · Sandbox · Packaging · .env      ║
║   Author  : Aurelion Project  │  License : MIT           ║
╚══════════════════════════════════════════════════════════╝

Entry point. Parses CLI args, validates, and dispatches to Executor.
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, str(Path(__file__).parent))

from core.parser import build_parser, validate_args
from core.executor import Executor


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    validate_args(args)

    executor = Executor(args, config_path=getattr(args, "config", None))
    return executor.run()


if __name__ == "__main__":
    sys.exit(main())
