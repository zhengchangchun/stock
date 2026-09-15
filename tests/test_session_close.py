"""P11：收盘回填 —— 「历史 NULL 保持 NULL」这件事的**逐条**证明。

用户明确要求：历史补不了就从今天起积累，**任何表都不做滚动清理**，
历史 NULL 不许填 0 / 插值 / 估算。本文件把这条拆成可执行的断言。
"""

from __future__ import annotations

import pytest

from stocklab.data.models import Bar
from stocklab.session import close as close_mod
from stocklab.session.store import insert_snapshot
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T15:30:00+08:00"
CODE = "000333"
TODAY = "2026-09-15"
YESTERDAY = "2026-09-14"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def _bar(date: str, code: str = CODE, *, amount=None, turnover=None,
         adj_mode: str = "none", close: float = 10.0) -> Bar:
    return Bar(code=code, date=date, open=close, high=close, low=close, close=close,
               volume=1000, amount=amount, turnover=turnover, source="test",
               adj_mode=adj_mode)


def _snap(conn, *, ts: str, code: str = CODE, amount=925_191_406.0,
          turnover=0.15, trade_date: str = TODAY):
    return insert_snapshot(conn, {
        "code": code, "trade_date": trade_date, "ts": ts, "price": 87.56,
        "pre_close": 86.8, "open": 86.7, "high": 88.1, "low": 86.44,
        "volume": 10_558_300, "amount": amount, "turnover": turnover,
        "source": "tencent"}, now=NOW)[0]


def _amount_of(conn, date: str, code: str = CODE):
    row = conn.execute("SELECT amount, turnover FROM bars_daily WHERE code=? AND date=?",
                       (code, date)).fetchone()
    return None if row is None else (row["amount"], row["turnover"])


# ---------- 基本行为 ----------

def test_fills_amount_and_turnover_for_the_day(conn):
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 1
    assert out["filled"] == [{"code": CODE, "amount": 925_191_406.0,
                              "turnover": 0.15, "snapshot_id": 1,
                              "ts": "20260915150003"}]
    assert _amount_of(conn, TODAY) == (925_191_406.0, 0.15)


def test_latest_snapshot_of_the_day_wins(conn):
    """`amount` 是**当日累计**量：必须用最后一条，否则会写成「半天成交额」。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915100000", amount=100.0, turnover=0.01)
    _snap(conn, ts="20260915150003", amount=999.0, turnover=0.09)
    close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert _amount_of(conn, TODAY) == (999.0, 0.09)


def test_is_idempotent(conn):
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    assert close_mod.backfill_close_amounts(conn, TODAY, now=NOW)["updated"] == 1
    again = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert again["updated"] == 0
    assert again["skipped_already_filled"] == [CODE]
    assert _amount_of(conn, TODAY) == (925_191_406.0, 0.15)


# ---------- 红线：历史 NULL 一行不动 ----------

def test_history_stays_null(conn):
    """回填只作用于**指定的那一天**；其余日期的 NULL 逐行保持 NULL。"""
    repo.insert_bars(conn, [_bar(YESTERDAY), _bar("2026-09-11"), _bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    close_mod.backfill_close_amounts(conn, TODAY, now=NOW)

    nulls = conn.execute(
        "SELECT date FROM bars_daily WHERE amount IS NULL ORDER BY date").fetchall()
    assert [r["date"] for r in nulls] == ["2026-09-11", YESTERDAY]
    assert conn.execute("SELECT COUNT(amount) FROM bars_daily").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 3


def test_never_fills_a_date_that_has_no_snapshot(conn):
    """给 09-14 补跑：那天没有快照 → 什么都不做，且**不报 OK 假象**。"""
    repo.insert_bars(conn, [_bar(YESTERDAY)], now=NOW)
    out = close_mod.backfill_close_amounts(conn, YESTERDAY, now=NOW)
    assert out["updated"] == 0
    assert out["reason"] == "no_snapshots"
    assert _amount_of(conn, YESTERDAY) == (None, None)


def test_existing_values_are_not_overwritten(conn):
    """`COALESCE`：已有值的行原样不动（回填与源站修订**互不覆盖**）。"""
    repo.insert_bars(conn, [_bar(TODAY, amount=1.0, turnover=0.5)], now=NOW)
    _snap(conn, ts="20260915150003")
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 0 and out["skipped_already_filled"] == [CODE]
    assert _amount_of(conn, TODAY) == (1.0, 0.5)


def test_missing_value_is_not_fabricated(conn):
    """源站没给 amount/turnover → 保持 NULL，进 `skipped_no_value` 留痕。**不填 0**。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003", amount=None, turnover=None)
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 0 and out["skipped_no_value"] == [CODE]
    assert _amount_of(conn, TODAY) == (None, None)


def test_missing_bar_row_is_reported_not_papered_over(conn):
    """当日 bar 还没采到 → 显式留痕（含 system_events），**不**去造一根 bar 出来。"""
    _snap(conn, ts="20260915150003")
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["updated"] == 0 and out["skipped_missing_bar"] == [CODE]
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0
    events = conn.execute(
        "SELECT level, message FROM system_events WHERE module='session'").fetchall()
    assert len(events) == 1
    assert events[0]["level"] == "warn" and "无当日行" in events[0]["message"]


def test_refuses_to_touch_a_non_none_adj_mode_row(conn):
    """铁律①：不复权是 `bars_daily` 的唯一合法口径，回填不准绕过这把闸。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    conn.execute("UPDATE bars_daily SET adj_mode='qfq' WHERE code=? AND date=?",
                 (CODE, TODAY))
    conn.commit()
    _snap(conn, ts="20260915150003")
    with pytest.raises(ValueError, match="adj_mode"):
        close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert _amount_of(conn, TODAY) == (None, None)      # 拒绝了就一行没写


def test_only_the_codes_with_snapshots_are_touched(conn):
    repo.insert_bars(conn, [_bar(TODAY), _bar(TODAY, code="600690")], now=NOW)
    _snap(conn, ts="20260915150003")
    out = close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    assert out["codes_with_snapshots"] == [CODE]
    assert _amount_of(conn, TODAY, "600690") == (None, None)


def test_fill_is_recorded_as_an_event(conn):
    """回填是**改写既有行**（不像快照是 append-only），所以必须留下可查的痕迹。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    row = conn.execute("SELECT level, message, context_json FROM system_events"
                       " WHERE module='session'").fetchone()
    assert row["level"] == "info" and "回填" in row["message"]
    assert "925191406" in row["context_json"]


def test_repository_reinsert_does_not_wipe_a_filled_value(conn):
    """**回归**：`ingest bars` 重跑（窗口含昨天）不许把刚回填的值抹回 NULL。"""
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)
    _snap(conn, ts="20260915150003")
    close_mod.backfill_close_amounts(conn, TODAY, now=NOW)
    repo.insert_bars(conn, [_bar(TODAY)], now=NOW)      # 源站日K 里没有 amount 字段
    assert _amount_of(conn, TODAY) == (925_191_406.0, 0.15)


def test_no_rolling_cleanup_anywhere_in_the_module():
    """取消「只保留近 30 天」：任何表都不许有滚动清理路径。"""
    import inspect

    src = inspect.getsource(close_mod).upper()
    assert "DELETE" not in src               # 没有任何删除路径
    assert "LIMIT" not in src                # 也没有「只取最近 N 天」的写法
