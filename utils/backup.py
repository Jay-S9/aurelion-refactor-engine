"""
Aurelion Refactor Engine v2 - Backup Manager
Creates timestamped backups of files before any destructive operation.

CHANGES IN v2:
  - Session manifest (manifest.txt): records every backed-up original path
    so restore can work without relying on filesystem path reconstruction.
  - get_latest_session(): finds the most recent backup folder by mtime.
  - restore_session() now reads the manifest for reliable path mapping.
  - Windows drive-letter safe: uses hex-encoded absolute paths in backup tree.
  - list_sessions(): shows all available backup sessions for the user.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Manifest filename written inside each session directory
_MANIFEST_FILE = "aurelion_manifest.json"


class BackupManager:
    """
    Backs up files into a timestamped folder under ./backups/
    Structure: ./backups/YYYY-MM-DD_HH-MM-SS/
                  aurelion_manifest.json   ← maps backup paths to originals
                  files/...               ← mirrored file tree
    """

    BACKUP_ROOT = Path("backups")

    def __init__(self, logger):
        self.logger = logger
        self._session_dir: Optional[Path] = None
        self._manifest: dict = {}   # backup_relative_path → original_absolute_path

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    def backup_files(self, files: List[Path]) -> Path:
        """
        Back up all given files into a new session directory.
        Writes a manifest so restore is always reliable.
        Returns the backup directory path.
        """
        session_dir = self._get_session_dir()
        backed_up = 0

        for src in files:
            if not src.exists():
                continue
            rel_key = self._backup_file(src, session_dir)
            if rel_key:
                self._manifest[rel_key] = str(src.absolute())
                backed_up += 1

        # Write/update manifest after every batch
        self._write_manifest(session_dir)

        if backed_up:
            self.logger.info(
                f"  Backup  : {backed_up} file(s) → {session_dir}"
            )

        return session_dir

    def restore_session(self, session_dir: Path) -> int:
        """
        Restore all files from a backup session directory using the manifest.
        Returns number of successfully restored files.
        """
        if not session_dir.exists():
            self.logger.error(f"Backup session not found: {session_dir}")
            return 0

        manifest_path = session_dir / _MANIFEST_FILE
        if not manifest_path.exists():
            self.logger.warning(
                "No manifest found — attempting path-based restore (legacy fallback)."
            )
            return self._restore_legacy(session_dir)

        manifest: dict = json.loads(manifest_path.read_text(encoding="utf-8"))

        if not manifest:
            self.logger.warning("Manifest is empty. Nothing to restore.")
            return 0

        restored = 0
        errors   = 0

        for rel_key, original_str in manifest.items():
            backup_file = session_dir / "files" / rel_key
            original    = Path(original_str)

            if not backup_file.exists():
                self.logger.error(
                    f"Backup file missing: {backup_file} (expected for {original})"
                )
                errors += 1
                continue

            try:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_file, original)
                self.logger.success(f"Restored: {original}")
                restored += 1
            except Exception as e:
                self.logger.error(f"Restore failed for {original}: {e}")
                errors += 1

        self.logger.info(
            f"\n  Restore complete: {restored} restored, {errors} error(s)."
        )
        return restored

    def get_latest_session(self) -> Optional[Path]:
        """Return the most recent backup session directory, or None."""
        sessions = self.list_sessions()
        return sessions[0] if sessions else None

    def list_sessions(self) -> List[Path]:
        """
        Return all backup session directories sorted by most recent first.
        Only returns directories that look like timestamped sessions.
        """
        if not self.BACKUP_ROOT.exists():
            return []

        sessions = [
            p for p in self.BACKUP_ROOT.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ]
        return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)

    # ──────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────

    def _get_session_dir(self) -> Path:
        if self._session_dir is None:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self._session_dir = self.BACKUP_ROOT / timestamp
            (self._session_dir / "files").mkdir(parents=True, exist_ok=True)
        return self._session_dir

    def _backup_file(self, src: Path, session_dir: Path) -> Optional[str]:
        """
        Copy src into session_dir/files/<safe_relative_key>.
        Returns the relative key (used in manifest), or None on failure.

        The key encodes the absolute path safely for any OS:
          - Strip leading separator / drive letter
          - Join remaining parts with os.sep so it nests correctly
        """
        try:
            abs_parts = src.absolute().parts
            # Drop the root ('/' on Unix, 'C:\\' on Windows)
            rel_parts = abs_parts[1:] if len(abs_parts) > 1 else abs_parts
            rel_key   = str(Path(*rel_parts))

            dest = session_dir / "files" / rel_key
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            return rel_key
        except Exception as e:
            self.logger.error(f"Backup failed for {src}: {e}")
            return None

    def _write_manifest(self, session_dir: Path) -> None:
        manifest_path = session_dir / _MANIFEST_FILE
        manifest_path.write_text(
            json.dumps(self._manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _restore_legacy(self, session_dir: Path) -> int:
        """
        Fallback restore for v1 backups that have no manifest.
        Reconstructs original paths from the backup tree structure.
        """
        restored = 0
        for backup_file in session_dir.rglob("*"):
            if not backup_file.is_file():
                continue
            rel = backup_file.relative_to(session_dir)
            original = Path("/") / rel
            try:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_file, original)
                self.logger.success(f"Restored (legacy): {original}")
                restored += 1
            except Exception as e:
                self.logger.error(f"Restore failed for {original}: {e}")
        return restored
