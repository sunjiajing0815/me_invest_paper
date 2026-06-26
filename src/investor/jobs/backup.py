"""Weekly DB backup job (soak-window P0.3).

Thin, error-isolated wrapper around ``services.backup.backup_database`` so a backup failure logs
and is swallowed rather than crashing the scheduler thread.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..services.backup import backup_database

logger = logging.getLogger(__name__)


def run_backup(settings: Settings) -> None:
    """Take one DB snapshot into ``settings.backup_dir`` (no-op when disabled)."""
    if not settings.backup_enabled:
        logger.info("run_backup: backups disabled (BACKUP_ENABLED=false); skipping")
        return
    try:
        backup_database(settings.sqlite_path, settings.backup_dir, settings.backup_keep)
    except Exception:
        logger.exception("run_backup: DB backup failed")
