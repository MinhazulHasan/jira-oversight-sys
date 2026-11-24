from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from oversight.errors import ConfigError, OversightError
from oversight.logs import get_logger
from oversight.store.db import open_readonly
from oversight.timeutil import utcnow

# The name of a backup this command made; the time is UTC.
BACKUP_NAME = re.compile(r"^oversight-(\d{8}-\d{6})\.db$")
_STAMP = "%Y%m%d-%H%M%S"


class BackupError(OversightError):
    """The copy could not be made or failed its check; nothing with a backup's name was left behind."""


@dataclass(frozen=True)
class Backup:
    """One backup this command made: its file and the UTC time in its name."""

    path: Path
    taken_at: datetime


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size_bytes: int
    # Older backups deleted to keep BACKUP_KEEP.
    removed: tuple[Path, ...]

    def render(self) -> str:
        return f"backup: {self.path} ({size_text(self.size_bytes)})"


def backup_name(at: datetime) -> str:
    """The file name of a backup taken at `at` (any zone; the name is UTC)."""
    return f"oversight-{at.astimezone(UTC).strftime(_STAMP)}.db"


def list_backups(folder: Path) -> list[Backup]:
    """The backups this command made in `folder`, oldest first."""
    found: list[Backup] = []
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return []
    for entry in entries:
        match = BACKUP_NAME.fullmatch(entry.name)
        if match is None or not entry.is_file():
            continue
        try:
            taken_at = datetime.strptime(match.group(1), _STAMP).replace(tzinfo=UTC)
        except ValueError:
            continue
        found.append(Backup(entry, taken_at))
    return sorted(found, key=lambda backup: backup.taken_at)


def newest_backup(folder: Path) -> Backup | None:
    backups = list_backups(folder)
    return backups[-1] if backups else None


def run_backup(db_file: Path, folder: Path, *, keep: int, now: Callable[[], datetime] = utcnow) -> BackupResult:
    """Copy the snapshot at `db_file` into `folder`, then keep the `keep` newest backups."""
    if keep < 1:
        raise ConfigError(f"BACKUP_KEEP must be at least 1, not {keep}")
    db_file = Path(db_file)
    if not db_file.is_file():
        raise ConfigError(f"no snapshot to back up at {db_file}: the first sync makes it")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / backup_name(now())
    if target.exists():
        raise BackupError(f"{target.name} already exists (two backups in one second): run it again")
    temporary = folder / f".{target.name}.tmp"
    try:
        _copy(db_file, temporary)
        os.replace(temporary, target)
    finally:
        _remove(temporary)
    size = target.stat().st_size
    removed = _prune(folder, keep)
    get_logger("backup").info("backup_written", size_bytes=size, kept=len(list_backups(folder)), removed=len(removed))
    return BackupResult(target, size, tuple(removed))


def size_text(size_bytes: int) -> str:
    """A size as people read it: 512 bytes, 48.2 KB, 3.1 MB, 1.2 GB (1 KB = 1,000 bytes)."""
    if size_bytes < 1000:
        return f"{size_bytes} bytes"
    value = float(size_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 1000 or unit == "TB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


# --- the copy ---------------------------------------------------------------------------------------------------


def _copy(db_file: Path, temporary: Path) -> None:
    _remove(temporary)  # a temporary file left by a run that was killed
    try:
        source = open_readonly(db_file)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot read the snapshot at {db_file}: {exc}") from None
    try:
        copy = sqlite3.connect(temporary)
        try:
            source.backup(copy)
            # One self-contained file: the copy carries the source's WAL flag until this.
            copy.execute("PRAGMA journal_mode = DELETE")
            verdict = copy.execute("PRAGMA quick_check").fetchone()
        finally:
            copy.close()
    except sqlite3.Error as exc:
        raise BackupError(f"the copy failed: {exc}") from None
    finally:
        source.close()
    if verdict is None or verdict[0] != "ok":
        raise BackupError("the copy failed its integrity check (PRAGMA quick_check); nothing was kept")


def _prune(folder: Path, keep: int) -> list[Path]:
    removed: list[Path] = []
    backups = list_backups(folder)
    for backup in backups[: max(0, len(backups) - keep)]:
        try:
            backup.path.unlink()
        except OSError as exc:
            # Kept for the next run to try again; the new backup is made either way.
            get_logger("backup").warning("backup_prune_failed", error_type=type(exc).__name__)
            continue
        removed.append(backup.path)
    return removed


def _remove(path: Path) -> None:
    """A temporary copy and anything SQLite left beside it."""
    for leftover in (path, *(path.with_name(path.name + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass
