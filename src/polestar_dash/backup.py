"""Daily SQLite backup using the safe `.backup` API (handles WAL correctly)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta

from .config import get_settings

log = logging.getLogger(__name__)


def run_backup() -> None:
    s = get_settings()
    if not s.db_path.exists():
        return

    target = s.backup_dir / f"polestar-{date.today().isoformat()}.sqlite"
    src = None
    dst = None
    try:
        src = sqlite3.connect(str(s.db_path))
        dst = sqlite3.connect(str(target))
        with dst:
            src.backup(dst)
        log.info("Wrote backup %s (%.1f MB)", target, target.stat().st_size / 1e6)
    except Exception:  # noqa: BLE001
        log.exception("Backup failed")
        return
    finally:
        if src:
            src.close()
        if dst:
            dst.close()

    # prune
    cutoff = datetime.now() - timedelta(days=s.backup_keep_days)
    for f in s.backup_dir.glob("polestar-*.sqlite"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                log.info("Pruned old backup %s", f.name)
        except Exception:  # noqa: BLE001
            log.debug("Could not prune %s", f, exc_info=True)
