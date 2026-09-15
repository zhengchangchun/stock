"""P15 / 补 P14 step4：风险面板 `risk.panel.build_risk_block`。

面板是 CLI、`dashboard build` 与 Web 页面**共用**的那一层。它的价值在于
「同一份口径只有一份实现」，所以本文件钉住两件事：

1. **数据不足 ≠ 不下注**：K 线不够 → `UNDETERMINED`（算不出来），
   不是 `NO_BET`（算过了，答案是不下注）。把两者混成一个会让人以为
   系统评估过这笔交易。
2. **结论与凯利同源**：`block["verdict"]` 必须**就是** `block["kelly"]["verdict"]`，
   不许面板自己再判一次（那就是第二个真相来源）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stocklab.risk.panel import UNDETERMINED, build_risk_block
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
CODE = "000333"


def _sessions(n: int, end: str = "2026-09-14") -> list[str]:
    """`end` 往前 `n` 个工作日（够用即可，不查日历）。"""
    out: list[str] = []
    d = date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, added_at)"
              " VALUES (?, '美的集团','sz','main',?)", (CODE, NOW))
    days = _sessions(300)
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in days])
    # 一条会涨也会跌的序列：保证既赢过也亏过（否则 b 不可估计、
    # 凯利只能报「无法估计赔率」，那是另一条分支）
    closes = [10.0 + 3.0 * ((i % 40) / 40.0) for i in range(len(days))]
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(CODE, d, x, x, x, x, 1000, "none", "tencent", NOW)
         for d, x in zip(days, closes)])
    c.commit()
    yield c
    c.close()


def test_block_carries_kelly_metrics_and_stops(conn):
    b = build_risk_block(conn, CODE, asof="2026-09-14", price=12.0)
    assert b["data_status"] == "ok"
    assert b["errors"] == []
    assert b["kelly"] is not None and b["metrics"] is not None
    assert b["stops"] is not None
    assert b["verdict"] in ("BET", "NO_BET")


def test_block_verdict_is_the_kelly_verdict_not_a_second_opinion(conn):
    """面板不许自己再判一次 —— 结论必须**就是**凯利的结论。"""
    b = build_risk_block(conn, CODE, asof="2026-09-14", price=12.0)
    assert b["verdict"] == b["kelly"]["verdict"]
    assert b["verdict_reason"] == b["kelly"]["verdict_reason"]
    assert b["verdict_label"] == b["kelly"]["verdict_label"]


def test_no_bet_notes_are_forwarded_to_the_panel(conn):
    """凯利的告警（样本不足 / 无 edge）必须在面板层也拿得到 —— 页面靠它渲染。"""
    b = build_risk_block(conn, CODE, asof="2026-09-14", price=12.0)
    joins = " ".join(b["notes"])
    if b["verdict"] == "NO_BET":
        assert "不下注" in joins or "不得据此给仓位" in joins


def test_insufficient_history_is_undetermined_not_no_bet(conn):
    """只有 30 根 K 线 → 回放不可用。

    **这必须与 `NO_BET` 区分开**：`NO_BET` 是「算过了，答案是不下注」，
    这里是「算不出来」。混为一谈等于谎称系统评估过这笔交易。
    """
    conn.execute("DELETE FROM bars_daily WHERE date < '2026-08-01'")
    conn.commit()
    b = build_risk_block(conn, CODE, asof="2026-09-14", price=12.0)
    assert b["data_status"] == "unavailable"
    assert b["verdict"] == UNDETERMINED
    assert b["verdict"] != "NO_BET"
    assert b["kelly"] is None
    assert b["errors"]                        # 必须给出**具体**原因
    assert not b["notes"] or "算不出来" in " ".join(b["notes"])


def test_unknown_code_does_not_raise(conn):
    """面板是页面的一段，不能因为一个标的数据不足就把整页打 500。"""
    b = build_risk_block(conn, "999999", asof="2026-09-14")
    assert b["verdict"] == UNDETERMINED
    assert b["data_status"] == "unavailable"
    assert b["errors"]


def test_panel_is_exported_from_the_package():
    import stocklab.risk as risk

    assert "build_risk_block" in risk.__all__
    assert risk.build_risk_block is build_risk_block
