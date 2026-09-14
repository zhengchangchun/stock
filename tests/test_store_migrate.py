import pytest

from stocklab.store.db import connect, transaction
from stocklab.store.migrate import backup_db, init_db


def test_init_creates_db(tmp_db):
    assert not tmp_db.exists()
    init_db(tmp_db)
    assert tmp_db.exists()
    with connect(tmp_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='bars_daily'"
        ).fetchone()[0]
    assert n == 1


def test_init_is_idempotent(tmp_db):
    init_db(tmp_db)
    init_db(tmp_db)   # 第二次不得报错
    with connect(tmp_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0]
    assert n == 0


def test_init_does_not_backup_on_first_run(tmp_db):
    result = init_db(tmp_db)
    assert result is None


def test_init_backs_up_existing_db(tmp_db):
    init_db(tmp_db)
    backup = init_db(tmp_db)
    assert backup is not None
    assert backup.exists()


def test_init_backup_preserves_premigration_data(tmp_db):
    """迁移前备份必须真的含旧数据，否则备份是假的。"""
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        conn.execute(
            "INSERT INTO trading_calendar (date, source, created_at) VALUES (?,?,?)",
            ("2026-09-14", "test", "t"),
        )
        conn.commit()
    backup = init_db(tmp_db)
    with connect(backup) as bconn:
        n = bconn.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0]
    assert n == 1


def test_foreign_keys_enabled(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_wal_mode(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_transaction_rolls_back_on_error(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute(
                    "INSERT INTO trading_calendar (date, source, created_at) VALUES (?,?,?)",
                    ("2026-09-14", "test", "t"),
                )
                raise RuntimeError("boom")
        n = conn.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0]
    assert n == 0


def test_backup_creates_copy(tmp_db, tmp_path):
    init_db(tmp_db)
    bdir = tmp_path / "backups"
    out = backup_db(tmp_db, bdir, "test")
    assert out.exists()
    assert out.parent == bdir
