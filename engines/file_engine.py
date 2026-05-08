"""
Aurelion Refactor Engine - File Engine
Handles file copy, overwrite, and multi-target distribution operations.
"""

import shutil
from pathlib import Path
from typing import List, Dict, Any


class FileReplacementEngine:
    """
    Responsible for copying a source file to one or more target paths.
    Respects overwrite policy and logs all outcomes.
    """

    def __init__(self, overwrite: bool, logger):
        self.overwrite = overwrite
        self.logger = logger

    def replace_files(
        self,
        source: Path,
        targets: List[Path],
    ) -> Dict[str, Any]:
        """
        Copy source file to every target path.
        Returns a result report dict.
        """
        modified = []
        skipped = []
        errors = []

        for target in targets:
            try:
                result = self._copy_to(source, target)
                if result == "modified":
                    modified.append(str(target))
                    self.logger.success(f"Wrote: {target}")
                elif result == "skipped":
                    skipped.append(str(target))
                    self.logger.skipped(f"{target}  (already exists, skip-existing mode)")
            except Exception as e:
                errors.append((str(target), str(e)))
                self.logger.error(f"Failed to copy to {target}: {e}")

        return {
            "modified": modified,
            "skipped": skipped,
            "errors": errors,
        }

    def _copy_to(self, source: Path, target: Path) -> str:
        if target.exists() and not self.overwrite:
            return "skipped"

        # Ensure parent directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(source, target)
        return "modified"
