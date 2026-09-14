"""Task 22：事件驱动回测引擎（T 日信号 → T+1 开盘成交）。"""

import pytest

from stocklab.backtest.engine import Signal, UnadjustedBars, run_backtest
from stocklab.calendar.trading_calendar import Calendar
from stocklab.config.costs import CostModel
from stocklab.config.universe import Instrument
from stocklab.data.models import Bar

DATES = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
CAL = Calendar.from_dates(DATES)
FREE = CostModel(slippage_bps=0.0, min_commission=0.0, commission_rate=0.0,
                 transfer_fee_rate=0.0, stamp_tax_rate=0.0)
MAIN = [Instrument("000333", "美的集团", "sz", "main")]


def mk_bars(code, closes, opens=None, volumes=None, adj_mode="qfq"):
    out = []
    for i, (d, c) in enumerate(zip(DATES, closes)):
        o = opens[i] if opens else c
        v = volumes[i] if volumes else 10_000
        out.append(Bar(code=code, date=d, open=o, high=max(o, c), low=min(o, c),
                       close=c, volume=v, amount=c * v, turnover=1.0,
                       source="test", adj_mode=adj_mode))
    return out


def buy_first_day(code="000333"):
    """首日出买入信号的策略（code 可参数化，便于验证板别取自 instruments）。"""

    class _S:
        name = "test_buy_hold"

        def generate(self, date, history, features):
            if date == DATES[0]:
                return {code: Signal("buy", 100.0, "entry")}
            return {}

    return _S()


def run(bars, strategy=None, universe=None, costs=FREE, cash=100_000.0):
    return run_backtest(bars, strategy or buy_first_day(), start=DATES[0],
                        end=DATES[-1], initial_cash=cash, costs=costs,
                        calendar=CAL, universe=MAIN if universe is None else universe)


def test_buy_and_hold_return_matches_hand_calculation():
    """首日信号 → 次日开盘买入，持有到结束。

    开盘价序列 10,10,11,12；次日开盘(=09-02 开盘 10)买入 10000 股，
    期末 09-04 收盘 12 → 10000 * 12 = 120000（零成本）。
    """
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0])}
    res = run(bars)
    assert res.nav_points[-1].nav == pytest.approx(120_000.0)
    assert res.metrics["total_return"] == pytest.approx(0.20)
    assert len(res.trades) == 1
    assert res.trades[0].side == "buy"
    assert res.trades[0].date == "2026-09-02"
    assert res.trades[0].price == pytest.approx(10.0)
    assert res.trades[0].qty == 10_000


def test_costs_reduce_return_and_are_reported():
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0])}
    res = run(bars, costs=CostModel())
    assert res.nav_points[-1].nav < 120_000.0
    assert res.trades[0].fee > 0
    assert res.metrics["costs_total"] == pytest.approx(res.costs_total)
    assert res.metrics["costs_total"] > 0


def test_no_lookahead_signal_uses_next_open():
    """T 日信号绝不能用 T 日收盘价成交（回测作弊的头号来源）。"""
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0],
                              opens=[9.0, 10.0, 11.0, 12.0])}
    res = run(bars)
    assert res.trades[0].price == pytest.approx(10.0)   # 09-02 开盘，不是 09-01 的 9.0


def test_nav_points_cover_all_sessions():
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0])}
    res = run(bars)
    assert [p.date for p in res.nav_points] == DATES


def test_flat_price_keeps_nav_at_initial():
    bars = {"000333": mk_bars("000333", [10.0] * 4)}
    res = run(bars)
    assert res.nav_points[-1].nav == pytest.approx(100_000.0)


def test_missing_bars_carry_forward_last_close():
    """后半段没有 K 线（停牌/未采集）：按上一有效收盘价估值，而不是当 0。"""
    bars = {"000333": mk_bars("000333", [10.0, 10.0])}
    res = run(bars)
    assert len(res.nav_points) == 4
    assert [p.nav for p in res.nav_points] == pytest.approx([100_000.0] * 4)
    assert res.metrics["n_missing_price_days"] > 0      # 缺口被计数，没有静默


def test_unadjusted_bars_are_rejected():
    """结构性防线：不复权价不许进回测（除权日假跌幅 + 假跌停）。"""
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0], adj_mode="none")}
    with pytest.raises(UnadjustedBars, match="复权"):
        run(bars)


def test_limit_up_blocks_buy_on_main_board():
    """主板 +10% 即涨停：09-02 开盘/收盘 11.0（前收 10.0）→ 买不到。"""
    bars = {"000333": mk_bars("000333", [10.0, 11.0, 11.0, 12.0],
                              opens=[10.0, 11.0, 11.0, 12.0])}
    res = run(bars)
    assert res.trades == []
    assert res.metrics["n_rejected"] == 1
    assert "涨停" in res.metrics["rejected"][0]


def test_same_bars_trade_on_gem_board():
    """同一根 K 线在创业板（20%）不是涨停 —— 证明涨跌停幅度取自 instruments.board。

    如果引擎里写死 board="main"，本条会退化成与主板一致（无成交），测试变红。
    """
    gem = [Instrument("300750", "宁德时代", "sz", "gem")]
    bars = {"300750": mk_bars("300750", [10.0, 11.0, 11.0, 12.0],
                              opens=[10.0, 11.0, 11.0, 12.0])}
    res = run(bars, strategy=buy_first_day("300750"), universe=gem)
    assert len(res.trades) == 1
    assert res.trades[0].price == pytest.approx(11.0)


def test_missing_board_in_universe_raises():
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0])}
    with pytest.raises(ValueError, match="板别"):
        run(bars, universe=[])


def test_suspended_day_expires_the_signal():
    """09-02 无量（停牌）→ 该信号作废（**不追单**），当日之后也不会补成交。"""
    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0],
                              volumes=[10_000, 0, 10_000, 10_000])}
    res = run(bars)
    assert res.trades == []
    assert res.metrics["n_rejected"] == 1
    assert "停牌" in res.metrics["rejected"][0]


def test_signal_sees_only_history_up_to_today():
    """策略看到的 history 只含 ≤T 的行（前视在这里结构上不可能）。"""
    seen: dict[str, list[str]] = {}

    class Spy:
        name = "spy"

        def generate(self, date, history, features):
            seen[date] = [b.date for b in history.get("000333", [])]
            return {}

    bars = {"000333": mk_bars("000333", [10.0, 10.0, 11.0, 12.0])}
    run(bars, strategy=Spy())
    for date, dates in seen.items():
        assert dates == [d for d in DATES if d <= date], f"{date} 看到了未来数据"


def test_sell_signal_executes_next_open_and_closes_position():
    class BuyThenSell:
        name = "buy_sell"

        def generate(self, date, history, features):
            if date == DATES[0]:
                return {"000333": Signal("buy", 100.0, "entry")}
            if date == DATES[1]:
                return {"000333": Signal("sell", 100.0, "exit")}
            return {}

    bars = {"000333": mk_bars("000333", [10.0, 10.0, 12.0, 12.0],
                             opens=[10.0, 10.0, 12.0, 12.0])}
    res = run(bars, strategy=BuyThenSell())
    assert [t.side for t in res.trades] == ["buy", "sell"]
    assert res.trades[1].date == "2026-09-03"
    assert res.nav_points[-1].nav == pytest.approx(100_000.0 + 20_000.0)
    assert res.metrics["n_trades"] == 2
