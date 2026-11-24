from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from oversight.ask.analyst import SnapshotQuery
from oversight.rules.catalog import RulesConfig
from oversight.settings import PROJECT_ROOT, Settings
from oversight.store.db import Store, open_readonly
from tests.conftest import CONFIG_FIXTURES, REAL_DATA, RealSnapshotOpened, into_real_data

REAL = PROJECT_ROOT / "data" / "oversight.db"


def stamp(path: Path) -> tuple[int, int] | None:
    try:
        found = os.stat(path)
    except FileNotFoundError:
        return None
    return found.st_size, found.st_mtime_ns


@pytest.mark.parametrize("run_sets_db_path", [False, True], ids=["run-as-it-is", "run-with-a-scratch-DB_PATH"])
def test_the_guard_refuses_every_way_into_the_real_snapshot(
    run_sets_db_path: bool, _no_real_snapshot: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if run_sets_db_path:
        monkeypatch.setenv("DB_PATH", str(tmp_path / "scratch.db"))
        assert Settings(_env_file=None).db_file == tmp_path / "scratch.db"
    default_path = Settings.model_construct().db_file
    assert default_path == REAL
    before = stamp(REAL)
    attempts = [
        lambda: Store.open(REAL),
        lambda: Store.open(str(REAL)),
        lambda: sqlite3.connect(REAL),
        lambda: sqlite3.connect(str(REAL).encode()),
        lambda: open_readonly(REAL),
        lambda: sqlite3.connect(REAL.resolve().as_uri() + "?mode=ro", uri=True),
        lambda: sqlite3.connect(PROJECT_ROOT / "data" / "backups" / "any.db"),
        lambda: Store.open(default_path),
        lambda: SnapshotQuery(REAL, project="AIL", sprint_field=None, rules=RulesConfig.load(CONFIG_FIXTURES / "rules.yaml")),
    ]
    for attempt in attempts:
        with pytest.raises(RealSnapshotOpened, match="tests never open the real snapshot"):
            attempt()
    monkeypatch.chdir(PROJECT_ROOT)
    with pytest.raises(RealSnapshotOpened):
        sqlite3.connect("data/oversight.db")
    assert len(_no_real_snapshot) == len(attempts) + 1
    assert all(Path(path) == REAL_DATA or REAL_DATA in Path(path).parents for path in _no_real_snapshot)
    _no_real_snapshot.clear()
    assert stamp(REAL) == before


def test_every_other_database_opens_as_before(tmp_path: Path) -> None:
    with Store.open(tmp_path / "data" / "oversight.db") as store:
        assert store.count("issues") == 0
    conn = open_readonly(tmp_path / "data" / "oversight.db")
    conn.close()
    sqlite3.connect(":memory:").close()
    sqlite3.connect("file::memory:", uri=True).close()
    with Store.open(":memory:") as store:
        assert store.count("issues") == 0


@pytest.mark.parametrize(
    ("database", "inside"),
    [
        (REAL, True), (str(REAL), True), (REAL_DATA, True), (str(REAL_DATA / "x" / "y.db"), True),
        (REAL.resolve().as_uri() + "?mode=ro", True), (":memory:", False), ("file::memory:", False),
        ("file:x?mode=memory&cache=shared", False), (PROJECT_ROOT / "config" / "x.db", False),
        (PROJECT_ROOT / "data-copy" / "oversight.db", False), (None, False), (42, False),
    ],
)
def test_what_the_guard_counts_as_the_real_data_folder(database: object, inside: bool) -> None:
    assert (into_real_data(database) is not None) is inside
