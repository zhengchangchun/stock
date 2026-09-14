"""Task 21：持仓推进内核。"""

import pytest

from stocklab.backtest.portfolio import Portfolio, replay
from stocklab.config.costs import CostModel
from stocklab.data.models import Bar

#: 零成本模型（只用于验证「股数/现金」推进本身，费用另行单独验证）
CM = CostModel(slippage_bps=0.0, min_commission=0.0, commission_rate=0.0,
               transfer_fee_rate=0.0, stamp_tax_rate=0.0)
NOW = "2026-09-14"
NEXT = "2026-09-15"


def bar(close=10.0, high=None, low=None, vol=1000, code="000333", date=NOW):
    return Bar(code=code, date=date, open=close, high=high or close,
               low=low or close, close=close, volume=vol, amount=close * vol,
               turnover=1.0, source="test", adj_mode="qfq")


def test_buy_reduces_cash_and_adds_position():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    t = p.buy(NOW, "000333", 10.0, 1000, "test")
    assert t is not None
    assert p.cash == pytest.approx(100_000.0 - 10_000.0 - t.fee)
    assert p.positions["000333"].qty == 1000
    assert p.positions["000333"].cost_price == pytest.approx(10_000.0 / 1000)


def test_buy_rejected_when_cash_insufficient():
    p = Portfolio(cash=100.0, positions={}, costs=CM)
    assert p.buy(NOW, "000333", 10.0, 1000, "test") is None
    assert p.cash == pytest.approx(100.0)
    assert p.positions == {}


def test_sell_increases_cash_and_removes_position():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "seed")
    p.settle(NOW)
    cash_before = p.cash
    t = p.sell(NEXT, "000333", 12.0, 1000, "exit")
    assert t is not None
    assert p.cash == pytest.approx(cash_before + 12_000.0 - t.fee)
    assert "000333" not in p.positions


def test_sell_rejected_when_position_insufficient():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    assert p.sell(NOW, "000333", 10.0, 100, "x") is None
    assert p.cash == pytest.approx(100_000.0)


def test_sell_rejected_when_t_plus_1_locked():
    """A 股 T+1：当日买入当日不可卖。"""
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "seed")
    assert p.sell(NOW, "000333", 11.0, 1000, "same-day") is None
    assert p.sellable_qty("000333") == 0


def test_t_plus_1_locks_only_todays_shares():
    """T+1 按**股数**锁定：昨日持仓当日仍可卖，只有今日买入的部分被锁。"""
    p = Portfolio(cash=200_000.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "yesterday")
    p.settle(NOW)
    p.buy(NEXT, "000333", 10.0, 500, "today")
    assert p.sellable_qty("000333") == 1000
    t = p.sell(NEXT, "000333", 11.0, 1000, "sell-old-only")
    assert t is not None
    assert p.positions["000333"].qty == 500
    assert p.sell(NEXT, "000333", 11.0, 500, "locked") is None


def test_settle_unlocks_next_day_sell():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "seed")
    p.settle(NOW)
    assert p.sell(NEXT, "000333", 11.0, 1000, "ok") is not None


def test_nav_uses_market_prices():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "seed")
    # 现金 90000，持仓 1000 股 @12 = 12000 → 102000
    assert p.nav({"000333": 12.0}) == pytest.approx(102_000.0)
    assert p.market_value({"000333": 12.0}) == pytest.approx(12_000.0)


def test_missing_price_is_reported_not_silently_zeroed():
    """缺价不许静默当 0（那是一次假爆仓）——必须能被调用方看见。"""
    p = Portfolio(cash=90_000.0, positions={}, costs=CM)
    p.buy(NOW, "000333", 10.0, 1000, "seed")
    assert p.missing_prices({}) == ["000333"]


def test_cannot_trade_suspended():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    assert p.can_trade("000333", bar(vol=0), board="main") is False


def test_cannot_buy_at_limit_up():
    """涨停不可买入（买不到）。"""
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    b = bar(close=11.0)
    assert p.can_trade("000333", b, pre_close=10.0, board="main") is False
    assert p.can_trade("000333", b, pre_close=10.0, board="main", side="sell") is True


def test_cannot_sell_at_limit_down():
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    b = bar(close=9.0)
    assert p.can_trade("000333", b, pre_close=10.0, board="main", side="sell") is False
    assert p.can_trade("000333", b, pre_close=10.0, board="main", side="buy") is True


def test_gem_board_limit_is_20_percent():
    """创业板 20%：主板会判涨停的 +10%，创业板仍是可成交的。"""
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    b = bar(close=11.0, code="300750")
    assert p.can_trade("300750", b, pre_close=10.0, board="main") is False
    assert p.can_trade("300750", b, pre_close=10.0, board="gem") is True


def test_unknown_board_raises_instead_of_defaulting_to_main():
    """板别未知必须报错 —— 悄悄按主板 10% 判就是静默降级（计划点名的坑）。"""
    from stocklab.backtest.portfolio import BoardUnknown

    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    with pytest.raises(BoardUnknown):
        p.can_trade("000333", bar(), pre_close=10.0, board="nasdaq")


def test_min_commission_five_yuan_on_small_trade():
    """成本可手算复核（默认 CostModel：佣金 0.025% 但**最低 5 元**）。

    买入 100 股 @10.00：
      成交价 = 10.00 * (1 + 5bp) = 10.005
      金额   = 1000.50；佣金 = max(1000.50*0.00025, 5) = 5.00（最低佣金生效）
      过户费 = 1000.50 * 0.00001 = 0.010005；印花税 = 0（买入）
      费用   = 5.010005 → round 2 位 = 5.01
    """
    p = Portfolio(cash=100_000.0, positions={}, costs=CostModel())
    t = p.buy(NOW, "000333", 10.0, 100, "small")
    assert t.price == pytest.approx(10.005)
    assert t.fee == pytest.approx(5.01)
    # 卖出 100 股 @12.00：印花税只在卖出计
    p.settle(NOW)
    s = p.sell(NEXT, "000333", 12.0, 100, "small")
    assert s.price == pytest.approx(11.994)
    assert s.fee == pytest.approx(5.61)      # 5.00 佣金 + 0.011994 过户 + 0.5997 印花


def test_big_trade_commission_is_proportional_not_flat():
    """大额成交佣金按费率走（> 最低 5 元），否则「最低佣金」会被误当成固定费。"""
    p = Portfolio(cash=2_000_000.0, positions={}, costs=CostModel())
    t = p.buy(NOW, "000333", 10.0, 100_000, "big")
    amount = t.price * t.qty
    assert amount * 0.00025 > 5.0
    assert t.fee == pytest.approx(round(amount * 0.00025 + amount * 0.00001, 2))


def test_replay_reproduces_nav_from_trades():
    """评审 D1：净值必须能由成交流水完整重放得到。"""
    p = Portfolio(cash=100_000.0, positions={}, costs=CM)
    trades = [p.buy(NOW, "000333", 10.0, 1000, "seed")]
    prices = {NOW: {"000333": 12.0}}
    navs = replay(100_000.0, trades, prices, CM)
    assert navs[-1].nav == pytest.approx(p.nav({"000333": 12.0}))


def test_replay_matches_portfolio_over_multiple_days():
    """多日多笔（含加仓与部分卖出）重放净值必须逐点等于 Portfolio 推进结果。"""
    costs = CostModel()
    p = Portfolio(cash=100_000.0, positions={}, costs=costs)
    trades = []
    plan = [
        (NOW, "buy", 10.0, 1000),
        (NEXT, "buy", 12.0, 500),
        ("2026-09-16", "sell", 11.0, 800),
    ]
    prices_by_date = {NOW: {"000333": 10.5}, NEXT: {"000333": 12.5},
                      "2026-09-16": {"000333": 11.2}}
    forward = []
    for date, side, price, qty in plan:
        t = (p.buy(date, "000333", price, qty, "replay")
             if side == "buy" else p.sell(date, "000333", price, qty, "replay"))
        assert t is not None
        trades.append(t)
        forward.append(p.nav(prices_by_date[date]))
        p.settle(date)

    navs = replay(100_000.0, trades, prices_by_date, costs)
    assert [n.nav for n in navs] == pytest.approx(forward)


def test_replay_rejects_inconsistent_flow():
    """流水自相矛盾（卖出但无对应买入）必须报错，而不是给出一条假净值。"""
    from stocklab.backtest.portfolio import Trade

    flow = [Trade(NOW, "000333", "sell", 10.0, 100, 0.0, "phantom")]
    with pytest.raises(ValueError, match="流水不自洽"):
        replay(100_000.0, flow, {NOW: {"000333": 10.0}}, CM)
    flow = [Trade(NOW, "000333", "buy", 10.0, 100, 0.0, "seed"),
            Trade(NOW, "000333", "sell", 10.0, 200, 0.0, "too-many")]
    with pytest.raises(ValueError, match="流水不自洽"):
        replay(100_000.0, flow, {NOW: {"000333": 10.0}}, CM)
