"""事件驱动回测引擎（Task 22）。

**时点约定（防前视的核心）**：

    T 日收盘后 → 生成信号 → **T+1 日开盘价成交** → T+1 日收盘估值

用 T 日收盘价成交是回测作弊的头号来源：信号里已经包含了 T 日收盘的信息，
再用 T 日收盘价成交等于「用结果定价自己的成交」。本引擎在结构上禁止这一点：
信号只进 `pending`，**只能在下一个交易日的开盘价上成交**。

**信号一天有效，未成交即作废（不追单）**：停牌 / 涨停买不到 / 跌停卖不掉 /
现金不足 / 当日无 K 线时，该信号被丢弃并记进 `metrics["rejected"]`。
「顺延到能成交为止」看着更贴近人性，实则是隐性前视：它假设信号发出后
你还能以未来的价格决策，且会把「买不到」美化成一笔更划算的成交。

三道结构性防线：
  ① **只吃复权价**：任何 `adj_mode != "qfq"` 的 K 线直接拒绝（`UnadjustedBars`）。
     否则除权日的假跌幅会被当成真实下跌，且它会同时污染涨跌停判定 ——
     不复权价在除权日「跌」10% 就被判成跌停，一次正常调仓被静默吃掉。
  ② **策略只能看到 ≤ T**：`history` 在生成 T 日信号**之后**才追加 T 日 K 线的事实
     顺序是反的 —— 追加发生在生成之前，但追加的只有 `<= date` 的行，
     未来行从未进入数据结构（与 Task 19 同款「先裁剪、后计算」）。
  ③ **缺价不前推为 0**：当日无 K 线（停牌/未采集）时按**上一有效收盘价**估值，
     并把缺口计数记进 `metrics["n_missing_price_days"]`，不许静默假爆仓。

涨跌停幅度取自 `instruments.board`（`Portfolio.can_trade`），引擎**只负责传板别**，
不设默认板。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from stocklab.backtest.metrics import summarize
from stocklab.backtest.portfolio import NavPoint, Portfolio, Trade
from stocklab.config.costs import CostModel
from stocklab.config.universe import Instrument


class UnadjustedBars(ValueError):
    """输入里含不复权价 —— 回测只允许在复权价上算收益（ADR-004 铁律①）。"""


@dataclass(frozen=True)
class Signal:
    action: str        # "buy" | "sell" | "hold"
    size_pct: float    # buy: 目标市值占净值 %；sell: 卖出持仓的 %
    reason: str = ""


class Strategy(Protocol):
    """策略协议：`generate(date, history, features) -> {code: Signal}`。

    `history[code]` 只含 `date <= T` 的复权 K 线（引擎保证），
    `features` 是 `date` 当日的特征快照（P3 产物，可为空 dict）。
    """

    name: str

    def generate(self, date: str, history: dict[str, list],
                 features: dict) -> dict[str, Signal]:
        ...


@dataclass
class BacktestResult:
    nav_points: list[NavPoint] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def costs_total(self) -> float:
        """累计交易成本（元）—— 汇报时必须与收益一起给出（R4）。"""
        return sum(t.fee for t in self.trades)


def _lot_floor(value: float, price: float) -> int:
    """按整手（100 股）向下取整的可买股数。"""
    if price <= 0:
        return 0
    return int(value / price / 100) * 100


def run_backtest(bars_by_code: dict[str, list], strategy, *, start: str, end: str,
                 initial_cash: float, costs: CostModel, calendar,
                 universe: Sequence[Instrument],
                 features_by_date: dict[str, dict] | None = None,
                 carry_forward: bool = True) -> BacktestResult:
    """跑一次回测。

    `bars_by_code`：**复权**日K（`adjust_mode == "qfq"`），按 code 分组。
    `universe`：`Instrument` 列表 —— 板别（涨跌停幅度）由此取得，
    **不接受裸代码字符串**，避免调用方在别处硬编码 `"main"`。
    """
    offenders = sorted({b.code for bars in bars_by_code.values() for b in bars
                        if b.adj_mode != "qfq"})
    if offenders:
        raise UnadjustedBars(
            f"回测只接受复权价（adj_mode='qfq'），以下标的是不复权价：{offenders}。"
            "不复权价在除权日会留下假跌幅，并会把正常成交误判成跌停（ADR-004）"
        )
    board_by_code = {i.code: i.board for i in universe}
    missing_board = sorted({c for c in bars_by_code if c not in board_by_code})
    if missing_board:
        raise ValueError(
            f"universe 未登记这些标的，无法取得板别（涨跌停幅度）：{missing_board}"
        )

    sessions = calendar.sessions(start, end)
    bars_by_date: dict[str, dict[str, object]] = {}
    for code, bars in bars_by_code.items():
        for b in bars:
            if start <= b.date <= end:
                bars_by_date.setdefault(b.date, {})[code] = b

    portfolio = Portfolio(cash=initial_cash, positions={}, costs=costs)
    result = BacktestResult()
    pending: dict[str, Signal] = {}
    history: dict[str, list] = {c: [] for c in bars_by_code}
    last_close: dict[str, float] = {}
    n_missing = 0
    rejected: list[str] = []

    for i, date in enumerate(sessions):
        todays = bars_by_date.get(date, {})
        prev_close = dict(last_close)          # 昨日及更早的收盘（用于涨跌停判定）

        # ---- 1) 执行昨日信号（今日开盘价成交；未成交即作废，不追单）----
        for code, sig in pending.items():
            bar = todays.get(code)
            if bar is None:
                rejected.append(f"{date} {code} {sig.action} 当日无K线（停牌/缺口）")
                continue
            board = board_by_code[code]
            pre_close = prev_close.get(code)
            if sig.action == "buy":
                nav = portfolio.nav(_valuation_prices(todays, last_close,
                                                      carry_forward))
                target = min(nav * sig.size_pct / 100.0, portfolio.cash)
                qty = portfolio.max_buy_qty(bar.open, target)
                if qty <= 0:
                    rejected.append(f"{date} {code} buy 目标仓位不足一手")
                    continue
                if not portfolio.can_trade(code, bar, board=board,
                                           pre_close=pre_close, side="buy"):
                    rejected.append(f"{date} {code} buy 停牌/涨停不可成交")
                    continue
                t = portfolio.buy(date, code, bar.open, qty, sig.reason)
                if t is None:
                    rejected.append(f"{date} {code} buy 现金不足")
                else:
                    result.trades.append(t)
            elif sig.action == "sell":
                pos = portfolio.positions.get(code)
                if pos is None:
                    continue
                if not portfolio.can_trade(code, bar, board=board,
                                           pre_close=pre_close, side="sell"):
                    rejected.append(f"{date} {code} sell 停牌/跌停不可成交")
                    continue
                qty = (pos.qty if sig.size_pct >= 100.0
                       else _lot_floor(pos.qty * sig.size_pct / 100.0, bar.open))
                qty = min(qty, portfolio.sellable_qty(code))
                if qty <= 0:
                    rejected.append(f"{date} {code} sell 无 T+1 可卖股数")
                    continue
                t = portfolio.sell(date, code, bar.open, qty, sig.reason)
                if t is not None:
                    result.trades.append(t)
        pending = {}

        # ---- 2) 收盘后：把 ≤T 的行追加进历史，再让策略出 T 日信号 ----
        for code, bar in todays.items():
            history.setdefault(code, []).append(bar)
        if i < len(sessions) - 1:              # 最后一天不必出信号（无次日可成交）
            feats = (features_by_date or {}).get(date, {})
            signals = strategy.generate(date, history, feats) or {}
            for code, sig in signals.items():
                if sig.action != "hold":
                    pending[code] = sig

        # ---- 3) 收盘估值 ----
        prices = _valuation_prices(todays, last_close, carry_forward)
        n_missing += sum(1 for c in portfolio.positions if c not in todays)
        result.nav_points.append(NavPoint(date, portfolio.nav(prices)))
        for code, bar in todays.items():
            last_close[code] = bar.close

        portfolio.settle(date)

    result.metrics = summarize(result.nav_points, initial_cash)
    result.metrics["n_trades"] = len(result.trades)
    result.metrics["costs_total"] = round(result.costs_total, 2)
    result.metrics["n_missing_price_days"] = n_missing
    result.metrics["n_rejected"] = len(rejected)
    result.metrics["rejected"] = rejected[:50]
    return result


def _valuation_prices(todays: dict, last_close: dict, carry_forward: bool) -> dict:
    """估值用价格：当日收盘价优先，缺失时按上一有效收盘价前推。

    **绝不用 0 代替缺失价格**：那会把停牌日变成一次假爆仓（净值直接归零），
    且会在净值曲线上留下一个假的 -100% 回撤。
    """
    prices = {c: b.close for c, b in todays.items()}
    if carry_forward:
        for code, close in last_close.items():
            prices.setdefault(code, close)
    return prices
