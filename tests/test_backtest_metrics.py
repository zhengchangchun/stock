"""Task 23：绩效指标 + buy_and_hold 基准（R5）。"""

import pytest

from stocklab.backtest.metrics import (buy_and_hold_nav, daily_returns,
                                       excess_return, summarize, win_rate)
from stocklab.backtest.portfolio import NavPoint
from stocklab.config.costs import CostModel
from stocklab.data.models import Bar

DATES = ["2026-09-01", "2026-09-02", "2026-09-03"]
FREE = CostModel(slippage_bps=0.0, min_commission=0.0, commission_rate=0.0,
                 transfer_fee_rate=0.0, stamp_tax_rate=0.0)


def navs_of(values):
    return [NavPoint(d, v) for d, v in zip(DATES, values)]


def bar(date, close):
    return Bar(code="000333", date=date, open=close, high=close, low=close,
               close=close, volume=100, amount=close * 100, turnover=1.0,
               source="test", adj_mode="qfq")


def test_total_return():
    m = summarize(navs_of([100.0, 110.0, 121.0]), 100.0)
    assert m["total_return"] == pytest.approx(0.21)
    assert m["n_sessions"] == 3


def test_max_drawdown():
    m = summarize(navs_of([100.0, 120.0, 90.0]), 100.0)
    assert m["max_drawdown"] == pytest.approx(-0.25)     # 90/120 - 1


def test_max_drawdown_zero_when_monotonic():
    assert summarize(navs_of([100.0, 110.0]), 100.0)["max_drawdown"] == pytest.approx(0.0)


def test_zero_drawdown_of_flat_curve_is_not_a_rounding_artifact():
    """全平净值：回撤必须是 0.0，而不是浮点噪声（如 -1e-16）。"""
    assert summarize(navs_of([100.0, 100.0, 100.0]), 100.0)["max_drawdown"] == 0.0


def test_excess_return():
    assert excess_return(0.10, 0.04) == pytest.approx(0.06)
    assert excess_return(0.01, 0.04) == pytest.approx(-0.03)   # 跑不赢就是负的


def test_win_rate():
    assert win_rate([0.1, -0.05, 0.2, -0.01]) == pytest.approx(0.5)
    assert win_rate([]) == 0.0


def test_daily_returns_skips_zero_base():
    assert daily_returns([NavPoint(DATES[0], 0.0), NavPoint(DATES[1], 50.0)]) == []


def test_buy_and_hold_nav_without_costs():
    bars = [bar(d, c) for d, c in zip(DATES, [10.0, 11.0, 12.0])]
    navs = buy_and_hold_nav(bars, 100_000.0, FREE)
    assert navs[-1].nav == pytest.approx(120_000.0)


def test_buy_and_hold_nav_with_costs_is_lower():
    bars = [bar(d, c) for d, c in zip(DATES, [10.0, 11.0, 12.0])]
    navs = buy_and_hold_nav(bars, 100_000.0, CostModel())
    assert navs[-1].nav < 120_000.0


def test_buy_and_hold_always_fills_even_when_fees_break_the_round_lot():
    """费用会让「刚好满仓」不足额 —— 必须逐手回退成交，不能静默空仓。"""
    bars = [bar(DATES[0], 10.0), bar(DATES[1], 10.0)]
    navs = buy_and_hold_nav(bars, 100_000.0, CostModel())
    assert navs[0].nav < 100_000.0          # 已建仓：净值 = 现金 + 市值（扣了费）
    assert navs[0].nav > 99_000.0           # 不是空仓等死


def test_buy_and_hold_empty_bars():
    assert buy_and_hold_nav([], 100_000.0, FREE) == []


def test_summarize_empty_nav():
    m = summarize([], 100.0)
    assert m["total_return"] == 0.0 and m["n_sessions"] == 0


def test_sharpe_is_zero_for_flat_curve():
    m = summarize(navs_of([100.0, 100.0, 100.0]), 100.0)
    assert m["volatility"] == pytest.approx(0.0)
    assert m["sharpe"] == 0.0
