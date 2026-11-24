"""`python -m oversight backup` (oversight/backup.py)"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from oversight import backup as backup_module
from oversight.backup import BACKUP_NAME, BackupError, backup_name, list_backups, run_backup, size_text
from oversight.cli import EXIT_CHECKS_FAILED, EXIT_CONFIG, EXIT_OK, main
from oversight.rules.catalog import RulesConfig
from oversight.rules.settings import Change, load_board_settings, read_history, save_board_settings
from oversight.settings import Settings
from oversight.store.db import SCHEMA_VERSION, Store, open_readonly
from tests.conftest import CONFIG_FIXTURES

NOW = datetime(2026, 11, 12, 14, 30, 5, tzinfo=UTC)


def seed(db_file: Path, rows: int = 250) -> Store:
    """A snapshot at this version's schema with rows still in its WAL"""
    store = Store.open(db_file)
    store.conn.execute("CREATE TABLE extra (n INTEGER NOT NULL)")
    store.conn.executemany("INSERT INTO extra (n) VALUES (?)", [(n,) for n in range(rows)])
    return store


def count(db_file: Path, sql: str) -> Any:
    conn = sqlite3.connect(db_file)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def touch_backup(folder: Path, at: datetime) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / backup_name(at)
    path.write_bytes(b"an older backup")
    return path


def test_the_copy_is_the_whole_snapshot_in_one_self_contained_file(settings: Settings) -> None:
    store = seed(settings.db_file)
    try:
        assert Path(str(settings.db_file) + "-wal").exists()  # the rows are still in the WAL
        result = run_backup(settings.db_file, settings.backups_path, keep=14, now=lambda: NOW)
    finally:
        store.close()

    assert result.path == settings.backups_path / "oversight-20261112-143005.db"
    assert result.path.parent == settings.db_file.parent / "backups"
    assert result.size_bytes == result.path.stat().st_size > 0
    assert result.removed == ()
    assert count(result.path, "SELECT COUNT(*) FROM extra") == 250
    assert count(result.path, "PRAGMA user_version") == SCHEMA_VERSION
    assert result.path.read_bytes()[18:20] == b"\x01\x01"
    assert sorted(p.name for p in settings.backups_path.iterdir()) == [result.path.name]


def test_a_backup_holds_each_boards_rule_settings_and_their_history(settings: Settings) -> None:
    store = seed(settings.db_file, rows=1)
    try:
        catalogue = RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")
        changes = [Change("R-EFF-01", params={"threshold": 2.0}), Change("W-AGE-01", enabled=False)]
        for revision, change in enumerate(changes):
            save_board_settings(store, catalogue, "AIL", revision=revision, changes=[change], now=NOW,
                                viewer_hash="local", viewer_name="Local (sign-in off)", reason="Agreed with the team")
        result = run_backup(settings.db_file, settings.backups_path, keep=14, now=lambda: NOW)
    finally:
        store.close()

    conn = open_readonly(result.path)
    try:
        backed_up = load_board_settings(conn, "AIL")
        history = read_history(conn, "AIL")
    finally:
        conn.close()
    assert (backed_up.revision, [setting.rule_id for setting in backed_up.rules]) == (2, ["R-EFF-01", "W-AGE-01"])
    assert [(entry["revision"], entry["reason"], entry["by"]["name"]) for entry in history] == [
        (2, "Agreed with the team", "Local (sign-in off)"), (1, "Agreed with the team", "Local (sign-in off)")]


GUIDE = Path(__file__).resolve().parent.parent / "docs" / "DEPLOYMENT.md"
NO_GUIDE = "docs/DEPLOYMENT.md is local only, not on this machine"


def test_the_guides_restore_keeps_the_chosen_backup_and_the_live_snapshot_until_the_swap(settings: Settings) -> None:
    if GUIDE.exists():
        text = GUIDE.read_text(encoding="utf-8")
        restore = text[text.index("4. **Restore.**"):text.index("5. Test a restore")]
        aside = restore.index("cp backups/oversight-20261110-020000.db oversight.db.restore")
        safety = restore.index("dc run --rm -e BACKUP_KEEP=1000 tools backup")
        swap = restore.index("test -s oversight.db.restore && rm -f oversight.db-wal oversight.db-shm && mv -f")
        assert aside < safety < swap
        assert "rm -f oversight.db oversight.db-wal" not in restore  # never the live snapshot before the copy

    folder = settings.backups_path
    oldest = touch_backup(folder, datetime(2026, 11, 10, 2, 0, tzinfo=UTC))
    for day in (11, 12):
        touch_backup(folder, datetime(2026, 11, day, 2, 0, tzinfo=UTC))
    store = seed(settings.db_file)
    store.close()
    restored = settings.db_file.with_name("oversight.db.restore")
    restored.write_bytes(oldest.read_bytes())  # step 2: the chosen backup, aside
    run_backup(settings.db_file, folder, keep=1000, now=lambda: NOW)  # step 3: nothing is pruned
    assert oldest.exists() and len(list_backups(folder)) == 4
    assert restored.stat().st_size > 0  # step 4: only now is the live snapshot replaced
    restored.replace(settings.db_file)
    assert settings.db_file.read_bytes() == oldest.read_bytes()


@pytest.mark.skipif(not GUIDE.exists(), reason=NO_GUIDE)
def test_the_guides_rollback_puts_back_the_config_the_old_release_reads() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    upgrades = text[text.index("## 9. Upgrades"):text.index("## 10. Monitoring")]
    assert upgrades.index('cp -a config "config.') < upgrades.index("dc build")
    rollback = upgrades[upgrades.index("**Rolling back:**"):]
    assert rollback.index('cp -a "config.<previous tag>" config') < rollback.index("dc up -d")


def test_the_name_is_the_utc_time() -> None:
    chicago = datetime(2026, 11, 12, 8, 30, 5, tzinfo=ZoneInfo("America/Chicago"))  # 14:30:05 UTC
    assert backup_name(chicago) == "oversight-20261112-143005.db"
    assert BACKUP_NAME.fullmatch(backup_name(NOW))


def test_the_source_is_read_only_and_never_migrated(tmp_path: Path) -> None:
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE issues (key TEXT)")
    conn.execute("INSERT INTO issues VALUES ('AIL-1')")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    before = old.read_bytes()

    result = run_backup(old, tmp_path / "backups", keep=3, now=lambda: NOW)

    assert old.read_bytes() == before
    assert count(old, "PRAGMA user_version") == 3
    assert count(result.path, "PRAGMA user_version") == 3
    assert count(result.path, "SELECT COUNT(*) FROM issues") == 1


def test_only_its_own_newest_backups_are_kept_and_nothing_else_is_touched(settings: Settings) -> None:
    seed(settings.db_file).close()
    folder = settings.backups_path
    oldest = touch_backup(folder, NOW - timedelta(days=3))
    older = touch_backup(folder, NOW - timedelta(days=2))
    newer = touch_backup(folder, NOW - timedelta(days=1))
    others = [
        folder / "oversight-20261112-010719-before-member-sync.db",  # a copy made by hand
        folder / "oversight-20261399-000000.db",  # no real time: not one of ours
        folder / "notes.txt",
    ]
    for other in others:
        other.write_bytes(b"keep me")

    result = run_backup(settings.db_file, folder, keep=2, now=lambda: NOW)

    assert result.removed == (oldest, older)
    assert [backup.path for backup in list_backups(folder)] == [newer, result.path]
    assert all(other.read_bytes() == b"keep me" for other in others)


def test_keep_one_leaves_only_the_new_backup(settings: Settings) -> None:
    seed(settings.db_file).close()
    touch_backup(settings.backups_path, NOW - timedelta(hours=1))
    result = run_backup(settings.db_file, settings.backups_path, keep=1, now=lambda: NOW)
    assert [backup.path for backup in list_backups(settings.backups_path)] == [result.path]


def test_two_backups_in_one_second_never_overwrite(settings: Settings) -> None:
    seed(settings.db_file).close()
    first = run_backup(settings.db_file, settings.backups_path, keep=5, now=lambda: NOW)
    content = first.path.read_bytes()
    with pytest.raises(BackupError, match="already exists"):
        run_backup(settings.db_file, settings.backups_path, keep=5, now=lambda: NOW)
    assert first.path.read_bytes() == content


def test_a_failed_copy_leaves_no_file_behind(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings.db_file).close()

    class BrokenSource:
        def backup(self, target: sqlite3.Connection) -> None:
            target.execute("CREATE TABLE half (n INTEGER)")
            raise sqlite3.OperationalError("disk I/O error")

        def close(self) -> None:
            pass

    monkeypatch.setattr(backup_module, "open_readonly", lambda path: BrokenSource())
    with pytest.raises(BackupError, match="the copy failed"):
        run_backup(settings.db_file, settings.backups_path, keep=5, now=lambda: NOW)
    assert list(settings.backups_path.iterdir()) == []


def test_the_command_prints_the_path_and_size_only(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    seed(settings.db_file).close()
    code = main(["backup"], settings=settings, now=lambda: NOW)
    printed = capsys.readouterr().out.strip().splitlines()
    path = settings.backups_path / "oversight-20261112-143005.db"
    assert code == EXIT_OK
    assert printed == [f"backup: {path} ({size_text(path.stat().st_size)})"]


def test_the_log_line_carries_counts_and_sizes_never_a_path(settings: Settings) -> None:
    seed(settings.db_file).close()
    touch_backup(settings.backups_path, NOW - timedelta(days=1))
    with capture_logs() as logs:
        result = run_backup(settings.db_file, settings.backups_path, keep=1, now=lambda: NOW)
    written = [entry for entry in logs if entry["event"] == "backup_written"]
    assert written == [{"event": "backup_written", "log_level": "info", "logger": "backup",
                        "size_bytes": result.size_bytes, "kept": 1, "removed": 1}]


def test_the_command_keeps_backup_keep(settings: Settings) -> None:
    seed(settings.db_file).close()
    for days in (3, 2, 1):
        touch_backup(settings.backups_path, NOW - timedelta(days=days))
    assert main(["backup"], settings=settings.model_copy(update={"backup_keep": 2}), now=lambda: NOW) == EXIT_OK
    assert len(list_backups(settings.backups_path)) == 2


def test_no_snapshot_is_a_config_error_and_makes_no_folder(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["backup"], settings=settings, now=lambda: NOW) == EXIT_CONFIG
    assert "no snapshot to back up" in capsys.readouterr().err
    assert not settings.backups_path.exists()


def test_a_failed_backup_exits_1(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    seed(settings.db_file).close()
    run_backup(settings.db_file, settings.backups_path, keep=5, now=lambda: NOW)
    assert main(["backup"], settings=settings, now=lambda: NOW) == EXIT_CHECKS_FAILED
    assert "backup failed:" in capsys.readouterr().err


def test_backup_keep_and_the_sync_interval_are_checked_at_start() -> None:
    assert Settings().backup_keep == 14
    assert Settings().sync_interval_minutes == 60
    assert Settings(backup_keep=1).backup_keep == 1
    with pytest.raises(ValidationError, match="BACKUP_KEEP must be at least 1"):
        Settings(backup_keep=0)
    assert Settings(sync_interval_minutes=5).sync_interval_minutes == 5
    assert Settings(sync_interval_minutes=1440).sync_interval_minutes == 1440
    for bad in (4, 1441, 0, -60):
        with pytest.raises(ValidationError, match="SYNC_INTERVAL_MINUTES must be 5 to 1440"):
            Settings(sync_interval_minutes=bad)


@pytest.mark.parametrize(
    ("size", "text"),
    [(0, "0 bytes"), (999, "999 bytes"), (1000, "1.0 KB"), (48_213_504, "48.2 MB"), (3_100_000_000, "3.1 GB")],
)
def test_sizes_read_as_people_read_them(size: int, text: str) -> None:
    assert size_text(size) == text
