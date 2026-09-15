"""P12 / Task 55：实盘账本写入口（校验 + 幂等 + 冲正）。"""

import sqlite3
from datetime import datetime

import pytest

from stocklab.portfolio.ledger import (
    CashFlowValidationError,
    DuplicateTradeError,
    LedgerError,
    TradeValidationError,
    latest_closed_trading_day,
    record_cash_flow,
    record_trade,
    reverse_trade,
)
from stocklab.portfolio.positions import replay_trades
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
#: 2026-09-15 是周二；日历只到 09-14（周一）—— 正是本仓的真实状态
CAL_DATES = ("2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14")


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, added_at)"
              " VALUES ('000333','美的集团','sz','main',?)", (NOW,))
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL_DATES])
    c.commit()
    yield c
    c.close()


def trades(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM real_trades ORDER BY date, trade_id")]


def buy(conn, **kw):
    kw.setdefault("date", "2026-09-14")
    kw.setdefault("code", "000333")
    kw.setdefault("side", "buy")
    kw.setdefault("price", 86.80)
    kw.setdefault("qty", 100)
    kw.setdefault("fee", 5.09)
    kw.setdefault("now", NOW)
    return record_trade(conn, **kw)


# ---------- 最近已收盘交易日 ----------

def test_latest_closed_before_1500_is_previous_day():
    """盘中录单：今天还没收盘，最近已收盘的只能是上一交易日。

    拿盘中半截价格当成交价，比录未来成交更隐蔽 —— 所以 15:00 是硬边界。
    """
    cal = _calendar(conn=None)
    now = datetime.fromisoformat("2026-09-15T13:51:00+08:00")
    assert latest_closed_trading_day(cal, now) == "2026-09-14"


def test_latest_closed_after_1500_is_today_if_trading_day():
    cal = _calendar(conn=None, dates=CAL_DATES + ("2026-09-15",))
    now = datetime.fromisoformat("2026-09-15T15:05:00+08:00")
    assert latest_closed_trading_day(cal, now) == "2026-09-15"


def test_latest_closed_after_1500_on_non_trading_day_is_previous():
    cal = _calendar(conn=None)
    now = datetime.fromisoformat("2026-09-15T15:05:00+08:00")
    assert latest_closed_trading_day(cal, now) == "2026-09-14"


def test_latest_closed_exactly_at_1500_is_today():
    """边界取「≥ 15:00」，不是「> 15:00」—— 收盘价 15:00 就定了。"""
    cal = _calendar(conn=None, dates=CAL_DATES + ("2026-09-15",))
    assert latest_closed_trading_day(cal, datetime.fromisoformat("2026-09-15T15:00:00+08:00")) \
        == "2026-09-15"


def test_latest_closed_raises_when_calendar_empty():
    from stocklab.calendar.trading_calendar import Calendar
    with pytest.raises(LedgerError, match="日历为空"):
        latest_closed_trading_day(Calendar.from_dates([]),
                                  datetime.fromisoformat(NOW))


def _calendar(conn, dates=CAL_DATES):
    """从日期元组建 Calendar（不依赖 DB）。"""
    from stocklab.calendar.trading_calendar import Calendar
    return Calendar.from_dates(dates)


# ---------- 校验：真实那笔必须通过 ----------

def test_real_trade_is_accepted(conn):
    out = buy(conn)
    assert out["state"] == "inserted"
    assert out["trade_id"] == 1
    rows = trades(conn)
    assert len(rows) == 1
    assert (rows[0]["date"], rows[0]["code"], rows[0]["side"]) == \
           ("2026-09-14", "000333", "buy")
    assert (rows[0]["price"], rows[0]["qty"], rows[0]["fee"]) == (86.80, 100, 5.09)


# ---------- 校验矩阵 ----------

def test_qty_must_be_positive(conn):
    with pytest.raises(TradeValidationError, match="qty 必须 > 0"):
        buy(conn, qty=0)


def test_buy_qty_must_be_whole_lots(conn):
    with pytest.raises(TradeValidationError, match="整手"):
        buy(conn, qty=150)


def test_sell_odd_lot_is_allowed(conn):
    """卖出允许零股残量（A 股卖出不受整手约束）。"""
    buy(conn)
    out = record_trade(conn, date="2026-09-14", code="000333", side="sell",
                       price=88.0, qty=30, fee=5.0, now=NOW)
    assert out["state"] == "inserted"


def test_price_must_be_positive(conn):
    with pytest.raises(TradeValidationError, match="price 必须 > 0"):
        buy(conn, price=0.0)


def test_negative_price_rejected(conn):
    with pytest.raises(TradeValidationError, match="price 必须 > 0"):
        buy(conn, price=-1.0)


def test_fee_must_be_non_negative(conn):
    with pytest.raises(TradeValidationError, match="fee 必须 ≥ 0"):
        buy(conn, fee=-0.01)


def test_zero_fee_is_allowed(conn):
    assert buy(conn, fee=0.0)["state"] == "inserted"


def test_unknown_code_rejected(conn):
    with pytest.raises(TradeValidationError, match="未在 instruments"):
        buy(conn, code="600690")


def test_date_outside_calendar_rejected(conn):
    with pytest.raises(TradeValidationError, match="不在 trading_calendar"):
        buy(conn, date="2026-09-12")          # 周六，日历里没有


def test_future_trade_rejected(conn):
    """禁录未来成交 —— 今天的成交在收盘前不存在。"""
    with pytest.raises(TradeValidationError, match="晚于最近已收盘交易日"):
        buy(conn, date="2026-09-15")


def test_bad_date_format_rejected(conn):
    with pytest.raises(TradeValidationError, match="date 格式"):
        buy(conn, date="2026/09/14")


def test_bad_side_rejected(conn):
    with pytest.raises(TradeValidationError, match="side"):
        buy(conn, side="short")


def test_sell_beyond_holding_rejected(conn):
    buy(conn)                                  # 持有 100
    with pytest.raises(TradeValidationError, match="超过当时持仓"):
        record_trade(conn, date="2026-09-14", code="000333", side="sell",
                     price=88.0, qty=200, fee=5.0, now=NOW)


def test_sell_exactly_holding_is_allowed(conn):
    buy(conn)
    out = record_trade(conn, date="2026-09-14", code="000333", side="sell",
                       price=88.0, qty=100, fee=5.0, now=NOW)
    assert out["state"] == "inserted"


def test_sell_before_the_buy_date_rejected(conn):
    """逐笔按日回放：09-11 卖出时还没买入，必须拒绝。"""
    buy(conn, date="2026-09-14")
    with pytest.raises(TradeValidationError, match="超过当时持仓"):
        record_trade(conn, date="2026-09-11", code="000333", side="sell",
                     price=88.0, qty=100, fee=5.0, now=NOW)


def test_calendar_empty_is_a_clear_error(conn):
    conn.execute("DELETE FROM trading_calendar")
    conn.commit()
    with pytest.raises(LedgerError, match="日历为空"):
        buy(conn)


# ---------- 重复录入防护 ----------

def test_identical_tuple_without_key_is_rejected(conn):
    """手滑敲两遍：无 key 且元组完全相同 → 拒绝，并告诉人怎么继续。"""
    buy(conn)
    with pytest.raises(DuplicateTradeError, match="allow-duplicate"):
        buy(conn)
    assert len(trades(conn)) == 1, "拒绝必须是硬拒，不能先写后报错"


def test_allow_duplicate_lets_a_genuine_second_trade_in(conn):
    """同价同量同费的两笔真单：显式确认后必须进得去。

    这就是**不能**给 (date,code,side,price,qty,fee) 加 UNIQUE 的原因 ——
    唯一约束会把第二张真单静默吃掉（ADR-006）。
    """
    buy(conn)
    out = buy(conn, allow_duplicate=True)
    assert out["state"] == "inserted"
    assert len(trades(conn)) == 2


def test_idempotency_key_hit_does_not_write_twice(conn):
    """命令重试/补跑：同 key 第二次 → 幂等命中，不写第二行，退出码照常成功。"""
    first = buy(conn, idem_key="2026-09-14-000333-buy")
    second = buy(conn, idem_key="2026-09-14-000333-buy")
    assert first["state"] == "inserted"
    assert second["state"] == "identical"
    assert second["trade_id"] == first["trade_id"]
    assert len(trades(conn)) == 1


def test_idempotency_key_does_not_block_a_different_trade(conn):
    buy(conn, idem_key="k1")
    out = buy(conn, qty=200, price=80.0, idem_key="k2", allow_duplicate=True)
    assert out["state"] == "inserted"
    assert len(trades(conn)) == 2


def test_different_tuple_is_not_flagged_as_duplicate(conn):
    """只有**完全相同**的元组才算疑似重复；价格差一分就是另一笔。"""
    buy(conn)
    out = buy(conn, price=86.81, allow_duplicate=False)
    assert out["state"] == "inserted"


# ---------- 冲正（append-only：不许 UPDATE） ----------

def test_reverse_buy_writes_an_opposite_row_not_an_update(conn):
    out = buy(conn)
    rev = reverse_trade(conn, out["trade_id"], reason="录错了券商", now=NOW)
    rows = trades(conn)
    assert len(rows) == 2, "冲正必须是**追加一行**，不是改写原行"
    assert rows[0]["side"] == "buy" and rows[0]["qty"] == 100, "原行原封不动"
    assert rows[1]["side"] == "sell" and rows[1]["qty"] == 100
    assert rows[1]["note"] and "冲正" in rows[1]["note"]
    assert "录错了券商" in rows[1]["note"]
    assert rev["trade_id"] == rows[1]["trade_id"]
    # 账回到原样：持仓归零
    assert replay_trades(rows)["000333"].qty == 0


def test_reverse_requires_a_reason(conn):
    out = buy(conn)
    with pytest.raises(LedgerError, match="reason"):
        reverse_trade(conn, out["trade_id"], reason="", now=NOW)


def test_reverse_unknown_trade_id(conn):
    with pytest.raises(LedgerError, match="不存在"):
        reverse_trade(conn, 999, reason="x", now=NOW)


def test_reverse_of_odd_lot_sell_is_allowed(conn):
    """冲正一笔零股卖出 = 买回零股，不能被「整手」规则挡住。"""
    buy(conn)
    s = record_trade(conn, date="2026-09-14", code="000333", side="sell",
                     price=88.0, qty=30, fee=5.0, now=NOW)
    rev = reverse_trade(conn, s["trade_id"], reason="卖错了", now=NOW)
    assert rev["state"] == "inserted"
    assert replay_trades(trades(conn))["000333"].qty == 100


def test_update_on_real_trades_is_blocked_by_trigger(conn):
    """冲正不是「我们选择不改」，而是**改不动**。"""
    out = buy(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE real_trades SET qty = 1 WHERE trade_id = ?",
                     (out["trade_id"],))


# ---------- 现金流 ----------

def deposit(conn, **kw):
    kw.setdefault("date", "2026-09-14")
    kw.setdefault("kind", "deposit")
    kw.setdefault("amount", 20000.0)
    kw.setdefault("now", NOW)
    return record_cash_flow(conn, **kw)


def test_real_deposit_is_accepted(conn):
    out = deposit(conn)
    assert out["state"] == "inserted"
    row = dict(conn.execute("SELECT * FROM cash_flows").fetchone())
    assert (row["date"], row["kind"], row["amount"]) == ("2026-09-14", "deposit", 20000.0)


def test_deposit_must_be_positive(conn):
    with pytest.raises(CashFlowValidationError, match="deposit 必须 > 0"):
        deposit(conn, amount=-100.0)


def test_withdraw_must_be_negative(conn):
    with pytest.raises(CashFlowValidationError, match="withdraw 必须 < 0"):
        deposit(conn, kind="withdraw", amount=100.0)


def test_withdraw_with_negative_amount_is_accepted(conn):
    out = deposit(conn, kind="withdraw", amount=-500.0)
    assert out["state"] == "inserted"


def test_unknown_cash_kind_rejected(conn):
    with pytest.raises(CashFlowValidationError, match="kind"):
        deposit(conn, kind="transfer")


def test_zero_amount_rejected(conn):
    with pytest.raises(CashFlowValidationError, match="不能为 0"):
        deposit(conn, kind="other", amount=0.0)


def test_cash_date_rules_match_trades(conn):
    with pytest.raises(CashFlowValidationError, match="晚于最近已收盘交易日"):
        deposit(conn, date="2026-09-15")
    with pytest.raises(CashFlowValidationError, match="不在 trading_calendar"):
        deposit(conn, date="2026-09-12")


def test_cash_duplicate_rejected_without_key(conn):
    deposit(conn)
    with pytest.raises(CashFlowValidationError):
        deposit(conn)


def test_cash_idempotency_key_hit(conn):
    first = deposit(conn, idem_key="principal-2026-09-14")
    second = deposit(conn, idem_key="principal-2026-09-14")
    assert first["state"] == "inserted"
    assert second["state"] == "identical"
    assert conn.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0] == 1


def test_cash_allow_duplicate(conn):
    deposit(conn)
    assert deposit(conn, allow_duplicate=True)["state"] == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0] == 2


def test_idem_key_scope_is_recorded(conn):
    buy(conn, idem_key="same-key")
    deposit(conn, idem_key="same-key")      # 不同 scope 可用同一个 key 字符串
    scopes = {r["scope"] for r in conn.execute("SELECT scope FROM ledger_idem")}
    assert scopes == {"trade", "cash"}
