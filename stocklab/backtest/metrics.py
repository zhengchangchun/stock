"""绩效指标与 buy_and_hold 基准（Task 23，R5）。

三条纪律：
  ① **扣成本才算数**（R4）：`buy_and_hold_nav` 与回测走同一个 `CostModel`，
     报出的基准收益与策略收益都是「同一把尺子」。
  ② 指标**提前定死**：收益 / 最大回撤 / 波动 / 夏普（无风险利率 0）；
     不允许事后换指标救结果（准确率优先章的度量纪律）。
  ③ 净值序列**缺价不许当 0**：缺价由引擎按「上一有效收盘价」前推，
     本模块只消费已完整的净值序列。
"""

from __future__ import annotations

import math

from stocklab.backtest.portfolio import NavPoint, Portfolio
from stocklab.config.costs import CostModel

#: 年化交易日数（A 股约 242~244，业界惯例取 252）
TRADING_DAYS = 252


def summarize(nav_points: list[NavPoint], initial_cash: float) -> dict:
    """净值序列 → 绩效指标。

    `max_drawdown` 为负数（如 -0.25 表示从峰值回撤 25%）；单调上升为 0.0。
    """
    if not nav_points:
        return {"total_return": 0.0, "max_drawdown": 0.0, "volatility": 0.0,
                "sharpe": 0.0, "n_sessions": 0}
    navs = [p.nav for p in nav_points]
    total_return = navs[-1] / initial_cash - 1.0 if initial_cash > 0 else 0.0

    peak = navs[0]
    max_dd = 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1.0)

    rets = [navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs))
            if navs[i - 1] > 0]
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)
        sharpe = (mean * TRADING_DAYS) / vol if vol > 0 else 0.0
    else:
        vol, sharpe = 0.0, 0.0

    return {"total_return": total_return, "max_drawdown": max_dd,
            "volatility": vol, "sharpe": sharpe, "n_sessions": len(navs)}


def excess_return(strategy_return: float, benchmark_return: float) -> float:
    """R5：跑不赢躺平就没有存在价值 —— 超额 = 策略 − 基准。"""
    return strategy_return - benchmark_return


def win_rate(returns) -> float:
    rs = list(returns)
    if not rs:
        return 0.0
    return sum(1 for r in rs if r > 0) / len(rs)


def daily_returns(nav_points: list[NavPoint]) -> list[float]:
    navs = [p.nav for p in nav_points]
    return [navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs))
            if navs[i - 1] > 0]


def buy_and_hold_nav(bars, initial_cash: float, costs: CostModel) -> list[NavPoint]:
    """基准 R5：首日开盘买入并持有到末日（**含成本**）。

    `bars` 必须是**复权价**序列（引擎侧同一口径），否则除权日的假跌幅会把
    基准也做出一条假的暴跌 —— 基准失真会直接毁掉「跑不赢就明说」的判断。

    买入股数按整手向下取整，并在现金不足以覆盖费用时逐手回退，
    保证「一定成交」而不是静默返回一条空仓净值曲线。
    """
    if not bars:
        return []
    ordered = sorted(bars, key=lambda b: b.date)
    first = ordered[0]
    p = Portfolio(cash=initial_cash, positions={}, costs=costs)
    price = costs.fill_price("buy", first.open)
    qty = int(initial_cash / price / 100) * 100 if price > 0 else 0
    trade = None
    while qty > 0 and trade is None:
        trade = p.buy(first.date, first.code, first.open, qty, "buy_and_hold")
        if trade is None:
            qty -= 100
    out: list[NavPoint] = []
    last_close = first.close
    for b in ordered:
        last_close = b.close
        out.append(NavPoint(b.date, p.nav({first.code: last_close})))
    return out
