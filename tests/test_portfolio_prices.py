"""P12 / Task 56：现价解析（snapshot → bars → missing，绝不猜）。"""

import pytest

from stocklab.portfolio.prices import resolve_price
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def add_snapshot(conn, code, trade_date, ts, price):
    conn.execute(
        "INSERT INTO quote_snapshots (code, trade_date, ts, price, volume, source,"
        " fetched_at) VALUES (?,?,?,?,?,?,?)",
        (code, trade_date, ts, price, 1000, "tencent", NOW))


def add_bar(conn, code, date, close, adj_mode="none"):
    conn.execute(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (code, date, close, close, close, close, 1000, adj_mode, "tencent", NOW))


# ---------- 分支 1：同日快照优先 ----------

def test_same_day_snapshot_wins_over_bars(conn):
    add_bar(conn, "000333", "2026-09-15", 86.00)
    add_snapshot(conn, "000333", "2026-09-15", "20260915135109", 87.45)
    p = resolve_price(conn, "000333", "2026-09-15")
    assert p.source == "snapshot"
    assert p.price == 87.45
    assert p.price_asof == "2026-09-15"


def test_snapshot_picks_latest_ts(conn):
    """同一天多个截面 → 取 ts 最大的一条（最新看到的那一眼）。"""
    add_snapshot(conn, "000333", "2026-09-15", "20260915135048", 87.44)
    add_snapshot(conn, "000333", "2026-09-15", "20260915135109", 87.45)
    add_snapshot(conn, "000333", "2026-09-15", "20260915120000", 86.10)
    p = resolve_price(conn, "000333", "2026-09-15")
    assert p.price == 87.45
    assert p.detail == "20260915135109"


def test_snapshot_leaves_the_price_asof_as_the_trade_date(conn):
    add_snapshot(conn, "000333", "2026-09-15", "20260915135109", 87.45)
    p = resolve_price(conn, "000333", "2026-09-15")
    assert p.price_asof == "2026-09-15"


# ---------- 分支 2：bars 兜底 ----------

def test_bars_used_when_no_same_day_snapshot(conn):
    add_bar(conn, "000333", "2026-09-14", 86.80)
    p = resolve_price(conn, "000333", "2026-09-14")
    assert p.source == "bars"
    assert p.price == 86.80
    assert p.price_asof == "2026-09-14"


def test_snapshot_from_another_day_is_not_used_for_this_date(conn):
    """09-15 的快照**不能**当 09-14 的现价 —— 那是未来信息。

    先按「同日」找，找不到才回落 bars；不是「找最近一条快照」。
    """
    add_snapshot(conn, "000333", "2026-09-15", "20260915135109", 87.45)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    p = resolve_price(conn, "000333", "2026-09-14")
    assert p.source == "bars"
    assert p.price == 86.80
    assert p.price_asof == "2026-09-14"


def test_bars_uses_latest_close_at_or_before_asof(conn):
    add_bar(conn, "000333", "2026-09-10", 86.20)
    add_bar(conn, "000333", "2026-09-11", 86.26)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    add_bar(conn, "000333", "2026-09-15", 99.00)      # 晚于 asof，不许用
    p = resolve_price(conn, "000333", "2026-09-14")
    assert p.price == 86.80
    assert p.price_asof == "2026-09-14"


def test_bars_falls_back_to_earlier_date_when_asof_has_no_bar(conn):
    """asof 当天停牌/无 bar → 用最近一个 ≤asof 的收盘价，并**如实标注**是哪天的。"""
    add_bar(conn, "000333", "2026-09-11", 86.26)
    p = resolve_price(conn, "000333", "2026-09-14")
    assert p.source == "bars"
    assert p.price == 86.26
    assert p.price_asof == "2026-09-11", "必须标注价格实际是哪天的，不能标成 asof"


# ---------- 复权口径：qfq 行不算现价 ----------

def test_qfq_bars_are_not_used_as_a_live_price(conn):
    """复权价不是「今天这只票值多少钱」。只有 adj_mode='none' 可以当现价。"""
    add_bar(conn, "000333", "2026-09-14", 50.00, adj_mode="qfq")
    assert resolve_price(conn, "000333", "2026-09-14") is None


def test_qfq_bar_is_skipped_and_an_earlier_real_close_is_used(conn):
    """asof 当天只有 qfq 行 → 跳过它，回落到更早的**真实**收盘。

    注意 `bars_daily` 的 PK 是 `(code, date)`，同一天不可能同时存在 qfq 与 none
    两行 —— 所以这里不是「谁赢」，而是「qfq 行被整个跳过」。
    写入侧 `repo.insert_bars` 本就拒绝 qfq，本守卫是第二道防线。
    """
    add_bar(conn, "000333", "2026-09-11", 86.26, adj_mode="none")
    add_bar(conn, "000333", "2026-09-14", 50.00, adj_mode="qfq")
    p = resolve_price(conn, "000333", "2026-09-14")
    assert p.price == 86.26
    assert p.price_asof == "2026-09-11"


# ---------- 分支 3：都没有 → None（不用成本价冒充，不插值） ----------

def test_missing_when_nothing_at_all(conn):
    assert resolve_price(conn, "000333", "2026-09-14") is None


def test_missing_when_only_future_data_exists(conn):
    add_snapshot(conn, "000333", "2026-09-15", "20260915135109", 87.45)
    add_bar(conn, "000333", "2026-09-15", 87.00)
    assert resolve_price(conn, "000333", "2026-09-14") is None


def test_price_is_per_code(conn):
    add_bar(conn, "000333", "2026-09-14", 86.80)
    assert resolve_price(conn, "600690", "2026-09-14") is None
