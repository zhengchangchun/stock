import sqlite3

import pytest

from stocklab.store.db import connect, transaction
from stocklab.store.migrate import backup_db, init_db, migrate_p32_predictions_origin


#: P32 之前的旧 shape（无 origin 列），用来模拟「加列前已存在的老库」。
#: 与 schema.sql 里 predictions 的旧定义同文（feature_snapshot_id 的外键略去，
#: 迁移测试只关心 origin 列的追加与历史行不受扰）。
_LEGACY_PREDICTIONS_DDL = """
CREATE TABLE predictions (
    pred_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT NOT NULL,
    asof_date           TEXT NOT NULL,
    target_date         TEXT NOT NULL,
    direction_up        REAL NOT NULL,
    direction_flat      REAL NOT NULL,
    direction_down      REAL NOT NULL,
    range_lo            REAL,
    range_hi            REAL,
    key_levels_json     TEXT,
    action              TEXT NOT NULL CHECK (action IN ('hold','trim','add','exit','wait')),
    size_pct            REAL NOT NULL,
    invalidate_if       TEXT NOT NULL,
    strategy_mix_json   TEXT NOT NULL,
    regime_label        TEXT,
    feature_snapshot_id INTEGER,
    model_version       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','failed')),
    created_at          TEXT NOT NULL
)
"""


def _make_legacy_db(path, n_rows: int = 1):
    """建一个只有旧-shape predictions 表（无 origin）的库，并塞入 n_rows 行。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(_LEGACY_PREDICTIONS_DDL)
    for i in range(n_rows):
        conn.execute(
            "INSERT INTO predictions (code, asof_date, target_date, direction_up,"
            " direction_flat, direction_down, action, size_pct, invalidate_if,"
            " strategy_mix_json, model_version, created_at)"
            " VALUES (?,?,?,0.5,0.2,0.3,'hold',50.0,'x','{}',?,?)",
            (f"00000{i}", "2026-09-14", "2026-09-15", "v1", "t"),
        )
    conn.commit()
    conn.close()


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


# ---------- P32：predictions.origin 列 ----------

def _cols(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_p32_fresh_schema_has_origin_column_with_check(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        assert "origin" in _cols(conn, "predictions")
        # 非法 origin 被 CHECK 拦下（结构性防线，不是靠代码检查）
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO predictions (code, asof_date, target_date, direction_up,"
                " direction_flat, direction_down, action, size_pct, invalidate_if,"
                " strategy_mix_json, model_version, created_at, origin)"
                " VALUES ('000333','2026-09-14','2026-09-15',0.5,0.2,0.3,'hold',50.0,"
                " 'x','{}','v1','t','bogus')")
        # 合法 origin 可写
        conn.execute(
            "INSERT INTO predictions (code, asof_date, target_date, direction_up,"
            " direction_flat, direction_down, action, size_pct, invalidate_if,"
            " strategy_mix_json, model_version, created_at, origin)"
            " VALUES ('000333','2026-09-14','2026-09-15',0.5,0.2,0.3,'hold',50.0,"
            " 'x','{}','v1','t','live')")
        row = conn.execute("SELECT origin FROM predictions").fetchone()
        assert row["origin"] == "live"


def test_p32_migration_adds_origin_with_null_for_existing_rows(tmp_path):
    """老库迁移：加 origin 列，历史行 origin 一律 NULL（不许回填冒充事实）。"""
    db = tmp_path / "legacy.db"
    _make_legacy_db(db, n_rows=3)
    backup = init_db(db)
    assert backup is not None          # 已存在 → 迁移前自动备份
    with connect(db) as conn:
        assert "origin" in _cols(conn, "predictions")
        assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE origin IS NULL").fetchone()[0] == 3
        # 老行一个字节都没动（抽查一列）
        codes = {r[0] for r in conn.execute("SELECT code FROM predictions")}
        assert codes == {"000000", "000001", "000002"}


def test_p32_migration_is_idempotent(tmp_db):
    init_db(tmp_db)                    # 已是新 shape（含 origin）
    with connect(tmp_db) as conn:
        assert migrate_p32_predictions_origin(conn) == []
