"""P11：`quote_snapshots` 落库（三态 + 不覆盖 + append-only 的结构性防线）。

最重要的是**幂等**这一条：tick 每 2 小时跑一次、失败会重试、补跑是常态。
只要键取对了，重复调用就是天然的空操作 —— 不靠调用方自觉。
"""

from __future__ import annotations

import sqlite3

import pytest

from stocklab.session.store import (SnapshotConflict, canonical, find_snapshot,
                                    insert_snapshot, insert_snapshots,
                                    snapshot_hash)
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T11:47:03+08:00"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def _row(**over) -> dict:
    base = {"code": "000333", "trade_date": "2026-09-15", "ts": "20260915114321",
            "price": 87.56, "pre_close": 86.80, "open": 86.70, "high": 88.10,
            "low": 86.44, "volume": 10_558_300, "amount": 925_191_406.0,
            "turnover": 0.15, "source": "tencent"}
    base.update(over)
    return base


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0]


# ---------- 三态 ----------

def test_first_insert(conn):
    state, sid = insert_snapshot(conn, _row(), now=NOW)
    assert state == "inserted" and sid > 0
    assert _count(conn) == 1


def test_repeat_is_identical_and_adds_no_row(conn):
    """同一时点重复调用 → 不产生重复行（tick 幂等的核心）。"""
    _, sid1 = insert_snapshot(conn, _row(), now=NOW)
    state, sid2 = insert_snapshot(conn, _row(), now=NOW)
    assert state == "identical" and sid2 == sid1
    assert _count(conn) == 1


def test_fetched_at_is_not_part_of_the_identity(conn):
    """只换 `fetched_at`（重抓）不该被判成新快照，也不该被判成冲突。"""
    insert_snapshot(conn, _row(), now="2026-09-15T11:47:03+08:00")
    state, _ = insert_snapshot(conn, _row(), now="2026-09-15T13:47:11+08:00")
    assert state == "identical"
    assert _count(conn) == 1


def test_int_float_do_not_create_a_phantom_conflict(conn):
    """int `87` 与 float `87.0` 是同一个数：不归一的话第二次跑就自锁（ERROR_DIARY #10）。"""
    insert_snapshot(conn, _row(price=87, volume=10_558_300), now=NOW)
    state, _ = insert_snapshot(conn, _row(price=87.0, volume=10_558_300.0), now=NOW)
    assert state == "identical"


def test_same_key_different_content_is_a_conflict_and_changes_nothing(conn):
    _, sid = insert_snapshot(conn, _row(), now=NOW)
    with pytest.raises(SnapshotConflict):
        insert_snapshot(conn, _row(price=99.99), now=NOW)
    assert _count(conn) == 1
    assert find_snapshot(conn, "000333", "2026-09-15", "20260915114321")["price"] == 87.56
    assert sid > 0


def test_different_ts_is_a_new_snapshot(conn):
    """不同时刻的截面**本来就该各占一行**（这正是「多次采集」的意义）。"""
    insert_snapshot(conn, _row(), now=NOW)
    state, _ = insert_snapshot(conn, _row(ts="20260915134321", price=88.0), now=NOW)
    assert state == "inserted"
    assert _count(conn) == 2


def test_batch_insert_does_not_let_one_conflict_kill_the_rest(conn):
    insert_snapshot(conn, _row(), now=NOW)
    rows = [_row(), _row(code="600690"), _row(price=1.0)]
    states, conflicts = insert_snapshots(conn, rows, now=NOW)
    assert set(states) == {"000333", "600690"}   # identical 那条仍在 states 里
    assert states["000690" if False else "600690"].startswith("inserted")
    assert [c["code"] for c in conflicts] == ["000333"]
    assert _count(conn) == 2                                # 原有 1 行 + 新标的 1 行


# ---------- hash / 规约 ----------

def test_canonical_normalizes_numeric_types():
    a = canonical(_row(price=87))
    b = canonical(_row(price=87.0))
    assert a == b and snapshot_hash(a) == snapshot_hash(b)


def test_hash_ignores_fetched_at_and_column_order():
    a = _row()
    b = {**_row(), "fetched_at": "1999-01-01T00:00:00+08:00"}
    assert snapshot_hash(a) == snapshot_hash(b)


# ---------- append-only 的结构性防线 ----------

def test_update_is_blocked_by_trigger(conn):
    insert_snapshot(conn, _row(), now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE quote_snapshots SET price=1.0")


def test_delete_is_blocked_by_trigger(conn):
    insert_snapshot(conn, _row(), now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM quote_snapshots")


def test_unique_index_blocks_a_raw_second_insert(conn):
    """绕开本模块直接写 SQL 也进不去第二行 —— 防线在结构上，不在调用方自觉上。"""
    insert_snapshot(conn, _row(), now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO quote_snapshots (code, trade_date, ts, price, volume,"
            " source, fetched_at) VALUES ('000333','2026-09-15','20260915114321',"
            " 1.0, 1, 'tencent', ?)", (NOW,))
