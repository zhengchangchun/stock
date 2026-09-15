"""PIT 规则回放（P14 / Task 63）：给凯利提供 **p / b 的唯一合法来源**。

## 为什么 p/b 只能这么来

凯利看起来像一个「告诉你该买多少」的答案，实际它的输出完全由输入决定 ——
而 p（胜率）恰恰是本项目**测不出来**的那部分。手设一个 p=0.55 就能得到任何
想要的仓位，那不是计算结果，是输入者的观点伪装成算术。

所以 p/b 只能来自：**明确规则 + PIT 历史数据 + 扣成本 + 按日聚类 + 报样本量**。

## 三条内置规则（都只用 `≤ t` 的数据）

| 规则 | 定义 |
|---|---|
| `trend-state` | 状态 `UP`：`close > MA20 且 MA20 > MA60`；非 UP → UP 买入；转出 UP 卖出（默认） |
| `ma-cross` | `MA20` 上穿 `MA60` 买入、下穿卖出 |
| `buy-hold` | 第一根买入、最后一根卖出（对照：给「无择时」的 p/b 基准） |

## 成交约定（**不许乐观**）

信号在 `t` 日**收盘后**才知道（收盘价算的均线），所以成交价取 `t+1` 日的收盘。
用「当日收盘信号 + 当日收盘成交」会让回放用一个你当时还不知道的价格成交 ——
那类乐观偏差足以把 `NO_BET` 变成「有仓位」。

## 价格口径

用**复权**价（`data.adjust.load_bars_adjusted`，PIT：只用 `cqr <= asof` 的事件）。
不复权价跨除权日会把分红/送转的除权缺口记成亏损。若该标的复权链有不可定价缺口，
**显式抬高窗口起点并在输出里披露**（不静默放弃历史，也不静默吞缺口）。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from statistics import fmean

from stocklab.config.costs import CostModel
from stocklab.data import adjust
from stocklab.data.models import Bar
from stocklab.risk.kelly import EdgeStats

#: 内置规则（**新增规则必须同时更新这里、CLI 的 `--rule` 取值与文档**）。
RULES = ("trend-state", "ma-cross", "buy-hold")

DEFAULT_RULE = "trend-state"

#: 回放的名义本金（元）。固定名义本金是**必要的**：成本拖累依赖仓位大小，
#: 而仓位大小正是我们要算的东西 —— 不固定就循环了。
DEFAULT_NOTIONAL = 10_000.0

#: 最小交易单位（A 股 1 手 = 100 股）。
LOT = 100

MA_SHORT = 20
MA_LONG = 60


@dataclass(frozen=True)
class Trade:
    entry_date: str
    entry_ref: float            # 买入参考价（收盘价，未含滑点）
    entry_price: float          # 实际成交价（含滑点）
    exit_date: str
    exit_ref: float
    exit_price: float
    qty: int
    entry_cost: float           # 买入花掉的钱（价×量 + 费）
    exit_proceeds: float        # 卖出拿回的钱（价×量 − 费）
    gross_return: float         # 不含成本的价格收益
    net_return: float           # 含全部成本（费 + 滑点）
    still_open: bool = False    # 窗口结束时仍持仓 → 按最后一根收盘平掉（已标记）

    @property
    def fees(self) -> float:
        return ((self.entry_cost - self.entry_price * self.qty)
                + (self.exit_price * self.qty - self.exit_proceeds))

    @property
    def slippage(self) -> float:
        return (self.qty * (self.entry_price - self.entry_ref)
                + self.qty * (self.exit_ref - self.exit_price))

    @property
    def cost_bps(self) -> float:
        base = self.entry_ref * self.qty
        return (self.fees + self.slippage) / base * 10_000.0 if base else 0.0

    @property
    def amplitude(self) -> float:
        return abs(self.net_return)


@dataclass(frozen=True)
class RuleReplay:
    rule: str
    code: str
    asof: str
    trades: tuple[Trade, ...]
    stats: EdgeStats
    price_window: dict = field(default_factory=dict)
    cost_model: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


# ---------- 价格 ----------

def load_qfq_bars(conn: sqlite3.Connection, code: str, asof: str) -> tuple[list[Bar], dict]:
    """该标的 `≤ asof` 的**复权**日 K + 窗口披露。

    复权链有不可定价缺口时把起点抬到 `usable_from`，并**如实返回** `clipped=True`
    —— 「放弃了哪段历史」是一个决定，必须出现在输出里。
    """
    flo = adjust.usable_from(conn, code)
    bars = adjust.load_bars_adjusted(conn, code, asof, start=flo)
    window = {
        "first_bar": bars[0].date if bars else None,
        "last_bar": bars[-1].date if bars else None,
        "n_bars": len(bars),
        "usable_from": flo,
        "clipped": flo is not None,
        "note": ("复权链在 " + str(flo) + " 之前有不可定价事件，窗口起点已抬高"
                 if flo else "复权链无缺口"),
    }
    return bars, window


# ---------- 信号 ----------

def trend_state(closes: list[float], i: int) -> str | None:
    """`i` 日的趋势状态（只用 `closes[:i+1]`）。不足 `MA_LONG` 根 → `None`。"""
    if i < MA_LONG - 1:
        return None
    c = closes[i]
    ma_s = fmean(closes[i - MA_SHORT + 1:i + 1])
    ma_l = fmean(closes[i - MA_LONG + 1:i + 1])
    if c > ma_s and ma_s > ma_l:
        return "UP"
    if c < ma_s and ma_s < ma_l:
        return "DOWN"
    return "FLAT"


def ma_pair(closes: list[float], i: int) -> tuple[float, float] | None:
    if i < MA_LONG - 1:
        return None
    return (fmean(closes[i - MA_SHORT + 1:i + 1]),
            fmean(closes[i - MA_LONG + 1:i + 1]))


def signals(rule: str, closes: list[float]) -> list[tuple[int, str]]:
    """返回 `[(signal_index, "buy"|"sell"), ...]`，索引是**信号日**。

    买卖信号在**信号日的下一根**成交（见模块 docstring 的成交约定）。
    """
    out: list[tuple[int, str]] = []
    if rule == "buy-hold":
        return [(0, "buy")] if closes else []
    if rule == "trend-state":
        prev: str | None = None
        for i in range(len(closes)):
            st = trend_state(closes, i)
            if st is None:
                prev = st
                continue
            if prev is not None and prev != "UP" and st == "UP":
                out.append((i, "buy"))
            elif prev == "UP" and st != "UP":
                out.append((i, "sell"))
            prev = st
        return out
    if rule == "ma-cross":
        for i in range(1, len(closes)):
            now = ma_pair(closes, i)
            before = ma_pair(closes, i - 1)
            if not now or not before:
                continue
            if before[0] <= before[1] and now[0] > now[1]:
                out.append((i, "buy"))
            elif before[0] >= before[1] and now[0] < now[1]:
                out.append((i, "sell"))
        return out
    raise ValueError(f"未知规则 {rule!r}；可选：{'/'.join(RULES)}")


# ---------- 回放 ----------

def replay(conn: sqlite3.Connection, code: str, *, rule: str = DEFAULT_RULE,
           asof: str, notional: float = DEFAULT_NOTIONAL,
           costs: CostModel | None = None, horizon: int = 5) -> RuleReplay:
    """在 `≤ asof` 的 PIT 复权价上回放 `rule`，产出 `EdgeStats`（喂凯利）。"""
    if rule not in RULES:
        raise ValueError(f"未知规则 {rule!r}；可选：{'/'.join(RULES)}")
    costs = costs or CostModel()
    bars, window = load_qfq_bars(conn, code, asof)
    if len(bars) < MA_LONG + 2:
        raise ValueError(
            f"{code} 在 {asof} 前的复权 K 线只有 {len(bars)} 根，"
            f"不足 {MA_LONG + 2} 根 —— 样本太短，回放没有意义")

    closes = [b.close for b in bars]
    sig = signals(rule, closes)

    trades: list[Trade] = []
    pending: tuple[int, str] | None = None
    entry: dict | None = None
    for idx, side in sig:
        if side == "buy" and entry is None:
            pending = (idx, "buy")
        elif side == "sell" and entry is not None:
            pending = (idx, "sell")
        else:
            pending = None if side == "sell" else pending
        if pending is None:
            continue
        fill_i = pending[0] + 1                 # **t+1 成交**（见模块 docstring）
        if fill_i >= len(bars):
            continue                            # 最后一根出信号 → 无法成交
        bar = bars[fill_i]
        if pending[1] == "buy":
            qty = _qty(bar.close, notional)
            price, fee = costs.total("buy", bar.close, qty)
            entry = {"i": fill_i, "bar": bar, "price": price, "fee": fee, "qty": qty}
            pending = None
        else:
            trades.append(_close_trade(entry, bar, costs))
            entry = None
            pending = None

    still_open = entry is not None
    if still_open:
        # 窗口结束仍持仓 → 按最后一根收盘平掉，并**标记**（不许当已实现业绩藏着）
        trades.append(_close_trade(entry, bars[-1], costs, still_open=True))

    stats = _stats(trades, rule=rule, code=code, window=window, costs=costs,
                   notional=notional, horizon=horizon, still_open=still_open)
    return RuleReplay(
        rule=rule, code=code, asof=asof, trades=tuple(trades), stats=stats,
        price_window=window,
        cost_model={
            "notional": notional,
            "commission_rate": costs.commission_rate,
            "min_commission": costs.min_commission,
            "stamp_tax_rate": costs.stamp_tax_rate,
            "transfer_fee_rate": costs.transfer_fee_rate,
            "slippage_bps_per_side": costs.slippage_bps,
            "roundtrip_bps_measured": round(_roundtrip_bps(trades), 2),
            "fees_bps_measured": round(
                fmean(t.fees / (t.entry_ref * t.qty) * 10_000.0 for t in trades), 2)
            if trades else None,
            "slippage_bps_measured": round(
                fmean(t.slippage / (t.entry_ref * t.qty) * 10_000.0 for t in trades), 2)
            if trades else None,
        },
        meta={"still_open_at_end": still_open, "n_bars": len(bars),
              "lot": LOT})


def _qty(price: float, notional: float) -> int:
    """按名义本金取整到手（至少 1 手）。"""
    return max(LOT, int(math.floor(notional / price / LOT)) * LOT)


def _close_trade(entry: dict, exit_bar: Bar, costs: CostModel,
                 *, still_open: bool = False) -> Trade:
    qty = entry["qty"]
    fill, fee = costs.total("sell", exit_bar.close, qty)
    entry_cost = entry["price"] * qty + entry["fee"]
    proceeds = fill * qty - fee
    gross = (exit_bar.close - entry["bar"].close) / entry["bar"].close
    return Trade(
        entry_date=entry["bar"].date, entry_ref=entry["bar"].close,
        entry_price=entry["price"],
        exit_date=exit_bar.date, exit_ref=exit_bar.close, exit_price=fill, qty=qty,
        entry_cost=round(entry_cost, 4), exit_proceeds=round(proceeds, 4),
        gross_return=round(gross, 8),
        net_return=round(proceeds / entry_cost - 1.0, 8),
        still_open=still_open)


def _roundtrip_bps(trades: tuple[Trade, ...] | list[Trade]) -> float:
    """实测平均往返成本（基点）= 手续费 + 滑点，除以买入名义金额。

    滑点必须计进来：它和手续费一样是真金白银，只是不显示在交割单上。
    """
    if not trades:
        return 0.0
    return fmean(t.cost_bps for t in trades)


def _stats(trades: list[Trade], *, rule: str, code: str, window: dict,
           costs: CostModel, notional: float, horizon: int,
           still_open: bool) -> EdgeStats:
    wins = [t.net_return for t in trades if t.net_return > 0]
    losses = [-t.net_return for t in trades if t.net_return <= 0]
    by_day: dict[str, list[float]] = {}
    for t in trades:
        by_day.setdefault(t.exit_date, []).append(1.0 if t.net_return > 0 else 0.0)
    extra: list[str] = []
    if still_open:
        extra.append("窗口结束时仍持仓 —— 最后一笔按窗口最后一根收盘平掉（已标记 "
                     "still_open），这是**未实现**收益，不是已实现业绩")
    if window.get("clipped"):
        extra.append(f"复权链在 {window['usable_from']} 之前不可定价，"
                     f"窗口起点已抬到 {window['first_bar']} —— 早于该日的历史**未参与**统计")
    return EdgeStats(
        rule=rule, code=code,
        window_start=trades[0].entry_date if trades else None,
        window_end=trades[-1].exit_date if trades else None,
        n_trades=len(trades),
        n_days=len(by_day),
        win_amplitudes=tuple(wins),
        loss_amplitudes=tuple(losses),
        day_winrates=tuple(tuple(v) for _, v in sorted(by_day.items())),
        net_returns=tuple(t.net_return for t in trades),
        cost_bps=_roundtrip_bps(trades),
        horizon=horizon,
        extra_warnings=tuple(extra),
        meta={
            "notional": notional,
            "still_open_at_end": still_open,
            "price_window": window,
            "n_bars": window.get("n_bars"),
            "lot": LOT,
            "signal_to_fill": "信号日 t 的收盘信号 → t+1 收盘成交",
            "price_mode": "qfq（PIT：只用 cqr <= asof 的事件）",
        })
