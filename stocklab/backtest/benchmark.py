"""基准对照（Task 23，R5）。

**基准是可插拔接口**，不是写死的一条曲线：

    Benchmark.protocol:  name + nav_points(initial_cash, costs) -> [NavPoint]

三种实现：
  - `BuyAndHoldBenchmark` —— 同一标的的买入持有（**含成本**），最贴近「躺平」的真实机会成本；
  - `IndexBenchmark`      —— 指数（如沪深300 `sh000300`）点位收益（**不含成本**：指数不可直接交易，
                             这是与策略对照时唯一诚实的口径，必须在报告里写明）；
  - `UndeterminedBenchmark` —— 基准**不可得**时的显式占位：比较函数返回
                             `status="UNDETERMINED"` 与具体缺口原因，
                             **绝不**用「不比」冒充「跑赢」。

指数行情落在 `bars_daily`，代码用**源站符号**（`sh000300`）而不是 6 位数字：
`000300` 在 A 股同时是基金代码，用 6 位会把指数与基金混进同一个键空间；
指数也没有复权概念（无分红送转），因此 `adj_mode='none'` 是正确口径，
正好与「抓取层只落不复权」的铁律①一致。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from stocklab.backtest.metrics import buy_and_hold_nav, excess_return, summarize
from stocklab.backtest.portfolio import NavPoint
from stocklab.config.costs import CostModel
from stocklab.data.models import Bar

#: 沪深300 的腾讯符号（本项目基准锚：index_300）
INDEX_300_SYMBOL = "sh000300"
INDEX_SYMBOLS: dict[str, str] = {"index_300": INDEX_300_SYMBOL}


class BenchmarkUnavailable(RuntimeError):
    """基准数据不可得（本地无行情/区间不覆盖），或基准未确定。"""


class Benchmark(Protocol):
    name: str

    def nav_points(self, *, initial_cash: float, costs: CostModel) -> list[NavPoint]:
        ...


@dataclass
class BuyAndHoldBenchmark:
    """买入持有基准（含成本）。`bars` 必须是**复权价**（与策略同一口径）。"""

    bars: list
    name: str = "buy_and_hold"

    def nav_points(self, *, initial_cash: float, costs: CostModel) -> list[NavPoint]:
        return buy_and_hold_nav(self.bars, initial_cash, costs)


@dataclass
class IndexBenchmark:
    """指数点位基准（不含成本，指数不可直接交易）。"""

    symbol: str
    bars: list
    name: str = "index_300"
    #: 指数不能直接买卖 → 口径上不含成本；对照时必须声明，否则是拿「含成本策略」
    #: 与「零成本指数」比较，看似公平实则高估策略。
    costs_included: bool = False

    def nav_points(self, *, initial_cash: float, costs: CostModel) -> list[NavPoint]:
        del costs
        if not self.bars:
            raise BenchmarkUnavailable(f"{self.symbol} 无行情，无法构造基准")
        first_close = self.bars[0].close
        if first_close <= 0:
            raise BenchmarkUnavailable(f"{self.symbol} 首日收盘价非正，基准不可用")
        return [NavPoint(b.date, initial_cash * b.close / first_close)
                for b in self.bars]


@dataclass
class UndeterminedBenchmark:
    """基准未确定的**显式**占位：把缺口写清楚，而不是悄悄不比较。"""

    name: str
    reason: str

    def nav_points(self, *, initial_cash: float, costs: CostModel) -> list[NavPoint]:
        del initial_cash, costs
        raise BenchmarkUnavailable(self.reason)


def load_index_bars(conn: sqlite3.Connection, symbol: str, *, start: str = "",
                    end: str = "") -> list[Bar]:
    """从 `bars_daily` 读指数点位（`adj_mode='none'`：指数无复权概念）。

    缺数据时返回**空列表**，由调用方转成 `UndeterminedBenchmark`（写明缺口），
    而不是抛「查无此表」式的含糊错误。
    """
    sql = "SELECT * FROM bars_daily WHERE code=?"
    params: list = [symbol]
    if start:
        sql += " AND date>=?"
        params.append(start)
    if end:
        sql += " AND date<=?"
        params.append(end)
    sql += " ORDER BY date"
    return [Bar(code=r["code"], date=r["date"], open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=r["volume"],
                amount=r["amount"], turnover=r["turnover"], source=r["source"],
                adj_mode=r["adj_mode"])
            for r in conn.execute(sql, params)]


def resolve_benchmark(conn: sqlite3.Connection, key: str = "index_300", *,
                      start: str = "", end: str = "") -> Benchmark:
    """按名字取基准；数据缺失时返回 `UndeterminedBenchmark`（带具体原因）。"""
    symbol = INDEX_SYMBOLS.get(key)
    if symbol is None:
        return UndeterminedBenchmark(key, f"未知基准 {key!r}；可用：{sorted(INDEX_SYMBOLS)}")
    bars = load_index_bars(conn, symbol, start=start, end=end)
    if not bars:
        return UndeterminedBenchmark(
            key,
            f"{symbol} 在 {start}~{end} 无行情 —— 需先跑 "
            f"`stocklab ingest index --symbol {symbol}`（bars_daily 目前只有个股）",
        )
    return IndexBenchmark(symbol=symbol, bars=bars, name=key)


@dataclass
class BenchmarkComparison:
    """对照结果。`status` 只有两种：OK / UNDETERMINED（没有第三种含糊状态）。"""

    status: str                        # "OK" | "UNDETERMINED"
    benchmark: str
    strategy_return: float
    benchmark_return: float | None = None
    excess_return: float | None = None
    benchmark_costs_included: bool = False
    strategy_costs_included: bool = True
    note: str = ""
    benchmark_metrics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "benchmark": self.benchmark,
            "strategy_return": self.strategy_return,
            "benchmark_return": self.benchmark_return,
            "excess_return": self.excess_return,
            "strategy_costs_included": self.strategy_costs_included,
            "benchmark_costs_included": self.benchmark_costs_included,
            "note": self.note,
            "benchmark_metrics": self.benchmark_metrics,
        }


def compare_to_benchmark(nav_points: list[NavPoint], initial_cash: float,
                         benchmark: Benchmark, costs: CostModel) -> BenchmarkComparison:
    """策略净值 vs 基准净值。缺基准 → `UNDETERMINED`，绝不返回一个假的超额。"""
    strat_metrics = summarize(nav_points, initial_cash)
    strat_ret = strat_metrics["total_return"]
    if isinstance(benchmark, UndeterminedBenchmark):
        return BenchmarkComparison(status="UNDETERMINED", benchmark=benchmark.name,
                                   strategy_return=strat_ret, note=benchmark.reason)
    try:
        bench_navs = benchmark.nav_points(initial_cash=initial_cash, costs=costs)
    except BenchmarkUnavailable as exc:
        return BenchmarkComparison(status="UNDETERMINED", benchmark=benchmark.name,
                                   strategy_return=strat_ret, note=str(exc))
    bench_metrics = summarize(bench_navs, initial_cash)
    bench_ret = bench_metrics["total_return"]
    return BenchmarkComparison(
        status="OK",
        benchmark=benchmark.name,
        strategy_return=strat_ret,
        benchmark_return=bench_ret,
        excess_return=excess_return(strat_ret, bench_ret),
        benchmark_costs_included=getattr(benchmark, "costs_included", True),
        note=("基准为指数点位（不含成本），策略为扣成本；指数不可直接交易，"
              "两者口径差异已在报告中披露"
              if not getattr(benchmark, "costs_included", True) else ""),
        benchmark_metrics=bench_metrics,
    )
