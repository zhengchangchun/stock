"""P88 T4：`announcements` / `northbound_holdings` 两张表的 DDL 与 append-only。

列名与顺序**逐字**照任务书 D2/D3（顺序错了下游 SELECT * 的读法会漂）。
"""

from __future__ import annotations

import pytest

from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-27T10:00:00+08:00"

ANN_COLS = ("code", "art_code", "notice_date", "display_time", "title",
            "column_name", "ann_type", "source", "fetched_at", "created_at",
            "resp_sha256")

NB_COLS = ("code", "trade_date", "hold_shares", "hold_market_cap",
           "a_shares_ratio", "hold_shares_ratio", "free_shares_ratio",
           "close_price", "frequency", "source", "fetched_at", "created_at",
           "resp_sha256")

ANN_INSERT = (
    "INSERT INTO announcements (code, art_code, notice_date, display_time,"
    " title, column_name, ann_type, source, fetched_at, created_at, resp_sha256)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?)")

ANN_VALUES = ("000333", "AN202609151829423536", "2026-09-15",
              "2026-09-15 19:35:36:855", "美的集团:公告", "调研活动", "A",
              "eastmoney-ann", NOW, NOW, "a" * 64)

NB_INSERT = (
    "INSERT INTO northbound_holdings (code, trade_date, hold_shares,"
    " hold_market_cap, a_shares_ratio, hold_shares_ratio, free_shares_ratio,"
    " close_price, frequency, source, fetched_at, created_at, resp_sha256)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")

NB_VALUES = ("000333", "2026-06-30", 893478198, 67484408294.94, 12.83, 12.83,
             13.0201, 75.53, "quarterly", "eastmoney-northbound", NOW, NOW,
             "b" * 64)


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def _cols(conn, table: str) -> tuple[str, ...]:
    return tuple(r[1] for r in conn.execute(f"PRAGMA table_info({table})"))


# ---------- 公告表 ----------


def test_announcements_table_and_column_order(conn):
    assert _cols(conn, "announcements") == ANN_COLS


def test_announcements_accepts_insert(conn):
    conn.execute(ANN_INSERT, ANN_VALUES)
    assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1


def test_announcements_is_append_only(conn):
    """D2：两条触发器都要 `RAISE(ABORT)`。"""
    conn.execute(ANN_INSERT, ANN_VALUES)
    with pytest.raises(Exception) as e:
        conn.execute("UPDATE announcements SET title = 'x'")
    assert "append-only" in str(e.value)
    with pytest.raises(Exception) as e:
        conn.execute("DELETE FROM announcements")
    assert "append-only" in str(e.value)


def test_announcements_pk_is_code_and_art_code(conn):
    """`art_code` 是源站的稳定唯一号 ⇒ 同键只能一行（幂等的结构保证）。"""
    conn.execute(ANN_INSERT, ANN_VALUES)
    with pytest.raises(Exception):
        conn.execute(ANN_INSERT, ANN_VALUES)
    # 同一 art_code 换一个 code 是另一行（不同标的的同名公告互不覆盖）
    other = list(ANN_VALUES)
    other[0] = "600690"
    conn.execute(ANN_INSERT, tuple(other))
    assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 2


def test_announcements_has_code_notice_date_index(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND tbl_name='announcements'")}
    assert "idx_announcements_code_notice" in names


# ---------- 北向表 ----------


def test_northbound_table_and_column_order(conn):
    assert _cols(conn, "northbound_holdings") == NB_COLS


def test_northbound_accepts_insert(conn):
    conn.execute(NB_INSERT, NB_VALUES)
    row = conn.execute("SELECT * FROM northbound_holdings").fetchone()
    assert row["trade_date"] == "2026-06-30"
    assert row["hold_shares"] == 893478198
    assert row["frequency"] == "quarterly"


def test_northbound_is_append_only(conn):
    conn.execute(NB_INSERT, NB_VALUES)
    with pytest.raises(Exception) as e:
        conn.execute("UPDATE northbound_holdings SET hold_shares = 1")
    assert "append-only" in str(e.value)
    with pytest.raises(Exception) as e:
        conn.execute("DELETE FROM northbound_holdings")
    assert "append-only" in str(e.value)


def test_northbound_pk_is_code_and_trade_date(conn):
    conn.execute(NB_INSERT, NB_VALUES)
    with pytest.raises(Exception):
        conn.execute(NB_INSERT, NB_VALUES)


def test_northbound_frequency_rejects_daily(conn):
    """D3：`frequency` 恒 `quarterly`，**不许**写 `daily`（公开源上它不存在）。

    这条在 DDL 里就堵死，不靠调用方自觉（§0.3：编一个日度序列是禁区）。
    """
    bad = list(NB_VALUES)
    bad[8] = "daily"
    with pytest.raises(Exception):
        conn.execute(NB_INSERT, tuple(bad))


def test_both_tables_are_reachable_from_a_fresh_init(tmp_db):
    """新库（`init_db`）直接就有这两张表 ⇒ 老库靠 `ensure_schema` 前滚同一份 DDL。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    try:
        got = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()
    assert {"announcements", "northbound_holdings"} <= got
