"""P12 / Task 54：持仓与盈亏推导（纯函数）。"""

import pytest

from stocklab.portfolio.positions import (
    LedgerError,
    Position,
    open_positions,
    qty_held_before,
    replay_trades,
)


def trade(trade_id, date, code, side, price, qty, fee=0.0):
    return {"trade_id": trade_id, "date": date, "code": code, "side": side,
            "price": price, "qty": qty, "fee": fee}


# ---------- 真实那笔：含费 vs 不含费必须都在 ----------

def test_real_trade_both_cost_bases_are_reported():
    """000333 100 股 @86.80 费 5.09：含费 8685.09 / 不含费 8680.00。

    两个口径都必须在输出里，且字段名自带口径后缀 —— 下游不可能拿错还不知道。
    """
    pos = replay_trades([trade(1, "2026-09-14", "000333", "buy", 86.80, 100, 5.09)])["000333"]
    assert pos.qty == 100
    assert pos.cost_incl_fee == pytest.approx(8685.09)
    assert pos.cost_excl_fee == pytest.approx(8680.00)
    assert pos.avg_cost_incl_fee == pytest.approx(86.8509)
    assert pos.avg_cost_excl_fee == pytest.approx(86.80)
    assert pos.fees_paid == pytest.approx(5.09)
    assert pos.realized_incl_fee == 0.0
    assert pos.realized_excl_fee == 0.0


# ---------- 多笔买入：加权平均 ----------

def test_weighted_average_over_two_buys():
    """100@10.00(费5) + 200@11.00(费6) → 均价含费 (1005+2206)/300。"""
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 100, 5.0),
            trade(2, "2026-09-02", "X", "buy", 11.0, 200, 6.0)]
    pos = replay_trades(rows)["X"]
    assert pos.qty == 300
    assert pos.cost_incl_fee == pytest.approx(1005.0 + 2206.0)
    assert pos.cost_excl_fee == pytest.approx(1000.0 + 2200.0)
    assert pos.avg_cost_incl_fee == pytest.approx((1005.0 + 2206.0) / 300)
    assert pos.avg_cost_excl_fee == pytest.approx((1000.0 + 2200.0) / 300)
    assert pos.fees_paid == pytest.approx(11.0)


def test_buy_is_not_a_realized_event():
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 100, 5.0)]
    pos = replay_trades(rows)["X"]
    assert pos.realized_incl_fee == 0.0
    assert pos.realized_excl_fee == 0.0


# ---------- 部分卖出：按均价结转，剩余成本不变 ----------

def test_partial_sell_carries_cost_at_average_and_leaves_remainder_untouched():
    """买 300 股均价 10.70（含费 10700/300），卖 100 股。

    卖出 100@12.00 费 5 → 含费已实现 = (1200-5) - 均价100股 = 1195 - 3566.67 = -2371.67。
    剩余 200 股的**成本口径不变**（仍是同一均价），总成本减去结转部分。
    """
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 300, 10.0),   # 成本含费 3010
            trade(2, "2026-09-02", "X", "sell", 12.0, 100, 5.0)]
    pos = replay_trades(rows)["X"]
    avg_before = 3010.0 / 300
    carried = avg_before * 100
    assert pos.qty == 200
    assert pos.realized_incl_fee == pytest.approx((1200.0 - 5.0) - carried)
    assert pos.realized_excl_fee == pytest.approx(1200.0 - (3000.0 / 300) * 100)
    assert pos.cost_incl_fee == pytest.approx(3010.0 - carried)
    # 剩余持仓均价**不变** —— 这就是加权平均法，不是 FIFO
    assert pos.avg_cost_incl_fee == pytest.approx(avg_before)


def test_sell_fee_is_charged_to_realized_pnl_only():
    """卖出费用进已实现盈亏，不摊进剩余持仓成本。"""
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 200, 0.0),
            trade(2, "2026-09-02", "X", "sell", 10.0, 100, 7.0)]
    pos = replay_trades(rows)["X"]
    assert pos.cost_incl_fee == pytest.approx(1000.0), "剩余成本不含卖出费"
    assert pos.realized_incl_fee == pytest.approx(1000.0 - 7.0 - 1000.0)
    assert pos.realized_excl_fee == pytest.approx(0.0)


# ---------- 清仓后重建 ----------

def test_clear_then_reopen_has_no_float_residue():
    """清仓后成本必须归零，再建仓从零累计 —— 不能让上一轮的残渣渗进来。"""
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 100, 3.0),
            trade(2, "2026-09-02", "X", "sell", 12.0, 100, 4.0),
            trade(3, "2026-09-03", "X", "buy", 20.0, 100, 5.0)]
    pos = replay_trades(rows)["X"]
    assert pos.qty == 100
    assert pos.cost_incl_fee == pytest.approx(2005.0), "清仓未清零：上一轮残渣渗进了新仓"
    assert pos.cost_excl_fee == pytest.approx(2000.0)
    assert pos.avg_cost_incl_fee == pytest.approx(20.05)
    # 上一轮已实现 (1200-4) - 1003 = 193，本轮尚未实现任何东西
    assert pos.realized_incl_fee == pytest.approx(193.0)


def test_flat_position_reports_zero_average_not_nan():
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 100, 0.0),
            trade(2, "2026-09-02", "X", "sell", 10.0, 100, 0.0)]
    pos = replay_trades(rows)["X"]
    assert pos.qty == 0
    assert pos.avg_cost_incl_fee == 0.0
    assert pos.avg_cost_excl_fee == 0.0
    assert pos.cost_incl_fee == pytest.approx(0.0)
    assert pos.cost_excl_fee == pytest.approx(0.0)


def test_cleared_position_is_absent_from_open_positions():
    """清仓的标的**不**出现在持仓表里（否则 P13 会渲染一行 0 股持仓）。"""
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 100, 0.0),
            trade(2, "2026-09-02", "X", "sell", 10.0, 100, 0.0),
            trade(3, "2026-09-01", "Y", "buy", 5.0, 100, 0.0)]
    assert set(open_positions(rows)) == {"Y"}
    # 但清仓的标的仍在 replay 结果里，带着 qty=0 和已实现盈亏（不是消失）
    assert set(replay_trades(rows)) == {"X", "Y"}


def test_cleared_then_reopened_position_reappears():
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 100, 0.0),
            trade(2, "2026-09-02", "X", "sell", 10.0, 100, 0.0),
            trade(3, "2026-09-03", "X", "buy", 9.0, 100, 0.0)]
    positions = open_positions(rows)
    assert set(positions) == {"X"}
    assert positions["X"].qty == 100


# ---------- 多标的互不影响 ----------

def test_positions_are_per_code():
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 100, 0.0),
            trade(2, "2026-09-01", "Y", "buy", 20.0, 200, 0.0)]
    positions = replay_trades(rows)
    assert positions["X"].qty == 100
    assert positions["X"].cost_incl_fee == pytest.approx(1000.0)
    assert positions["Y"].qty == 200
    assert positions["Y"].cost_incl_fee == pytest.approx(4000.0)


# ---------- 回放顺序：按 (date, trade_id) ----------

def test_replay_ignores_input_order():
    """输入顺序不影响结果：回放一律按 (date, trade_id) 排。"""
    rows = [trade(3, "2026-09-03", "X", "buy", 20.0, 100, 0.0),
            trade(1, "2026-09-01", "X", "buy", 10.0, 100, 0.0),
            trade(2, "2026-09-02", "X", "sell", 15.0, 100, 0.0)]
    shuffled = list(reversed(rows))
    a = replay_trades(rows)["X"]
    b = replay_trades(shuffled)["X"]
    assert (a.qty, a.cost_incl_fee, a.realized_incl_fee) == \
           (b.qty, b.cost_incl_fee, b.realized_incl_fee)
    assert a.qty == 100 and a.cost_incl_fee == pytest.approx(2000.0)


# ---------- 超卖必须炸 ----------

def test_oversell_raises():
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 100, 0.0),
            trade(2, "2026-09-02", "X", "sell", 10.0, 200, 0.0)]
    with pytest.raises(LedgerError, match="超卖"):
        replay_trades(rows)


def test_sell_without_any_position_raises():
    with pytest.raises(LedgerError, match="超卖"):
        replay_trades([trade(1, "2026-09-01", "X", "sell", 10.0, 100, 0.0)])


# ---------- qty_held_before（卖出校验用） ----------

def test_qty_held_before_counts_only_up_to_that_date():
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 100, 0.0),
            trade(2, "2026-09-03", "X", "buy", 10.0, 100, 0.0)]
    assert qty_held_before(rows, "X", "2026-09-01") == 100
    assert qty_held_before(rows, "X", "2026-09-02") == 100
    assert qty_held_before(rows, "X", "2026-09-03") == 200
    assert qty_held_before(rows, "Y", "2026-09-03") == 0


def test_qty_held_before_nets_sells():
    rows = [trade(1, "2026-09-01", "X", "buy", 10.0, 300, 0.0),
            trade(2, "2026-09-02", "X", "sell", 10.0, 100, 0.0)]
    assert qty_held_before(rows, "X", "2026-09-02") == 200


def test_empty_ledger_gives_no_positions():
    assert replay_trades([]) == {}
    assert open_positions([]) == {}


def test_position_is_frozen_dataclass():
    pos = replay_trades([trade(1, "2026-09-01", "X", "buy", 10.0, 100, 0.0)])["X"]
    assert isinstance(pos, Position)
    with pytest.raises(Exception):
        pos.qty = 5
