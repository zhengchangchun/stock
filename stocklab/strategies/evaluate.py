"""Walk-forward 评估：把「策略 → 引擎 → 折 → 绩效报告」串成一条链（Task 28，P5）。

## 这个模块只回答一个问题

    「把注册表里的某个策略，放在**它从没见过的交易日**上，跑出什么绩效？」

为此它做了四件事，每一件都对应一条本项目纪律：

1. **只接受 `strategy_id`，内部经注册表取实例** → 未注册的策略在接口层就进不来
   （`NotRegistered`），「评估对象必须可追溯」不再依赖调用方自觉。
2. **逐折调用 `engine.run_backtest`，运行窗 = `train_start ~ test_end`**：
   训练窗只用来给 MA60 / ATR14 预热（**不做任何参数拟合** —— 本任务参数是文档默认值），
   报出的每一个数字都只取 `test_dates` 那段。样本外 NAV 按折**复利拼接**成连续曲线。
3. **每折一个全新策略实例**：跨折共享状态会让第 k 折的仓位受第 k−1 折影响，
   那是隐性泄漏，且看报告的人不会知道。
4. **样本量按交易日报**：`effective_n` 来自 `walkforward.sample_size_report`，
   `oos_rows`（标的-日行数）只作数据覆盖核对，**不得**当样本量。

## 必须披露的三个口径（报告里写死，不靠读者推断）

- **折与折不独立**：相邻折的训练窗大部分重叠、测试窗首尾相接，
  所以「2845 个样本外交易日」不是 2845 个独立样本，统计上要按**日聚类**看。
- **每折起点空仓**：拼接曲线在折边界上会重新建仓一次（成本被折数放大）。
- **意图仓位 ≠ 实际持仓**：涨停买不到 / 跌停卖不掉时引擎丢弃信号，
  策略并不知道（`n_rejected` 是这条差异的可见计量）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from stocklab.backtest.benchmark import BenchmarkComparison
from stocklab.backtest.engine import run_backtest
from stocklab.backtest.metrics import annualize, summarize
from stocklab.backtest.portfolio import NavPoint
from stocklab.backtest.walkforward import Fold, sample_size_report
from stocklab.config.costs import CostModel
from stocklab.config.universe import Instrument
from stocklab.data.models import Bar
from stocklab.strategies.registry import strategy_registry


class NoFoldEvaluated(RuntimeError):
    """所有折都没能产生样本外净值 —— 显式失败，不返回一条空曲线当结论。"""


@dataclass(frozen=True)
class FoldPerformance:
    """单折的**样本外**绩效（收益只取 `test_dates` 段；成本含预热窗）。"""

    index: int
    test_start: str
    test_end: str
    n_test_days: int
    nav_before: float          # 样本外窗口**之前**最后一个净值点
    nav_after: float           # 样本外窗口最后一个净值点
    fold_return: float
    n_trades: int              # 样本外窗内的成交笔数
    costs: float               # 样本外窗内的成本
    n_trades_warmup: int       # 预热（训练）窗内的成交笔数（**不**计入样本外成绩）
    costs_warmup: float        # 预热窗内的成本（**不**计入拼接曲线，见 disclosure）
    turnover_amount: float     # 全运行窗（预热 + 样本外）成交额
    n_rejected: int
    nav_points: tuple[NavPoint, ...]

    def as_dict(self) -> dict:
        return {
            "index": self.index, "test_start": self.test_start,
            "test_end": self.test_end, "n_test_days": self.n_test_days,
            "nav_before": round(self.nav_before, 4),
            "nav_after": round(self.nav_after, 4),
            "fold_return": round(self.fold_return, 6),
            "n_trades": self.n_trades, "costs": round(self.costs, 2),
            "n_trades_warmup": self.n_trades_warmup,
            "costs_warmup": round(self.costs_warmup, 2),
            "turnover_amount": round(self.turnover_amount, 2),
            "n_rejected": self.n_rejected,
        }


@dataclass
class WalkForwardPerformance:
    strategy_id: str
    params: dict
    initial_cash: float
    folds: list[FoldPerformance] = field(default_factory=list)
    skipped_folds: list[dict] = field(default_factory=list)
    stitched_nav: list[NavPoint] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    n_trades: int = 0
    costs_total: float = 0.0
    costs_warmup: float = 0.0
    n_trades_warmup: int = 0
    turnover_x: float = 0.0
    n_rejected: int = 0
    sample_size: dict = field(default_factory=dict)
    disclosure: dict = field(default_factory=dict)

    @property
    def oos_start(self) -> str | None:
        return self.stitched_nav[0].date if self.stitched_nav else None

    @property
    def oos_end(self) -> str | None:
        return self.stitched_nav[-1].date if self.stitched_nav else None

    def as_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "params": self.params,
            "initial_cash": self.initial_cash,
            "n_folds": len(self.folds),
            "n_skipped_folds": len(self.skipped_folds),
            "skipped_folds": self.skipped_folds,
            "oos_start": self.oos_start, "oos_end": self.oos_end,
            "metrics": self.metrics,
            "n_trades": self.n_trades,
            "n_trades_warmup": self.n_trades_warmup,
            "costs_total": round(self.costs_total, 2),
            "costs_warmup": round(self.costs_warmup, 2),
            "costs_all": round(self.costs_total + self.costs_warmup, 2),
            "turnover_x": round(self.turnover_x, 4),
            "n_rejected": self.n_rejected,
            "sample_size": self.sample_size,
            "folds": [f.as_dict() for f in self.folds],
            "disclosure": self.disclosure,
        }


def evaluate_walk_forward(strategy_id: str, *, folds: Sequence[Fold],
                          bars_by_code: Mapping[str, Sequence[Bar]], calendar,
                          universe: Sequence[Instrument], initial_cash: float,
                          costs: CostModel, features_by_date: Mapping | None = None,
                          **params) -> WalkForwardPerformance:
    """逐折跑回测，汇总**样本外**绩效。

    - `strategy_id`：必须是注册表里的 id（未注册 → `NotRegistered`）。
    - `folds`：`walkforward.split_walk_forward` 的产物（纯函数，可复现）。
    - `bars_by_code`：**复权**日K（读取层 `adjust.load_bars_adjusted` 的产物）。
    """
    meta = strategy_registry.meta(strategy_id)      # 未注册在这里就失败
    if not folds:
        raise NoFoldEvaluated("没有任何折可跑（folds 为空）—— 0 折不是「没有结论」，是「没跑」")

    perfs: list[FoldPerformance] = []
    skipped: list[dict] = []
    for fold in folds:
        # 每折**全新实例**：跨折共享状态 = 隐性泄漏
        strategy = strategy_registry.get(strategy_id, **params)
        run_start, run_end = fold.train_start, fold.test_end
        sub = {c: [b for b in bars if run_start <= b.date <= run_end]
               for c, bars in bars_by_code.items()}
        sub = {c: v for c, v in sub.items() if v}
        if not sub:
            skipped.append({"index": fold.index, "reason": "运行窗内无任何 K 线"})
            continue

        res = run_backtest(sub, strategy, start=run_start, end=run_end,
                           initial_cash=initial_cash, costs=costs, calendar=calendar,
                           universe=tuple(universe),
                           features_by_date=features_by_date)
        test_set = set(fold.test_dates)
        oos = [p for p in res.nav_points if p.date in test_set]
        prior = [p for p in res.nav_points if p.date < fold.test_start]
        if not oos or not prior:
            skipped.append({
                "index": fold.index,
                "reason": f"样本外净值缺失（oos={len(oos)} 点，"
                          f"起点前净值={len(prior)} 点）",
            })
            continue

        trades = [t for t in res.trades if t.date in test_set]
        nav_before = prior[-1].nav
        nav_after = oos[-1].nav
        run_costs = sum(t.fee for t in res.trades)
        oos_costs = sum(t.fee for t in trades)
        perfs.append(FoldPerformance(
            index=fold.index, test_start=fold.test_start, test_end=fold.test_end,
            n_test_days=len(oos), nav_before=nav_before, nav_after=nav_after,
            fold_return=(nav_after / nav_before - 1.0) if nav_before > 0 else 0.0,
            n_trades=len(trades), costs=oos_costs,
            n_trades_warmup=len(res.trades) - len(trades),
            costs_warmup=run_costs - oos_costs,
            turnover_amount=sum(t.price * t.qty for t in res.trades),
            n_rejected=res.metrics.get("n_rejected", 0),
            nav_points=tuple(oos),
        ))

    if not perfs:
        raise NoFoldEvaluated(
            f"策略 {strategy_id!r} 的 {len(folds)} 折全部没能产出样本外净值；"
            f"跳过原因：{skipped[:5]}"
        )

    # 拼接 = 逐折**收益率**链（基准是本折样本外起点前的净值）。
    # 预热窗成本刻意**不**按折摊派：训练窗互相重叠（train=250 / step=21 时
    # 同一天属于约 12 折），按折摊派会把同一笔成本重复计 12 次、系统性**高估**成本。
    # 样本外窗内的成本则如实计入 —— 它就在 `nav_after / nav_before` 这个比值里。
    stitched: list[NavPoint] = []
    running = initial_cash
    for fp in perfs:
        scale = running / fp.nav_before if fp.nav_before > 0 else 0.0
        for p in fp.nav_points:
            stitched.append(NavPoint(p.date, p.nav * scale))
        running = stitched[-1].nav

    metrics = summarize(stitched, initial_cash)
    metrics["annualized_return"] = annualize(metrics["total_return"],
                                             metrics["n_sessions"])
    costs_total = sum(f.costs for f in perfs)
    costs_warmup = sum(f.costs_warmup for f in perfs)
    turnover = sum(f.turnover_amount for f in perfs)
    n_trades = sum(f.n_trades for f in perfs)

    sample = sample_size_report(
        [f for f in folds if f.test_dates],
        dates_by_code={c: [b.date for b in bars] for c, bars in bars_by_code.items()},
    )
    return WalkForwardPerformance(
        strategy_id=strategy_id,
        params={**{p["name"]: p["default"] for p in meta["params"]}, **params},
        initial_cash=initial_cash,
        folds=perfs, skipped_folds=skipped, stitched_nav=stitched, metrics=metrics,
        n_trades=n_trades, costs_total=costs_total, costs_warmup=costs_warmup,
        n_trades_warmup=sum(f.n_trades_warmup for f in perfs),
        turnover_x=(turnover / initial_cash) if initial_cash > 0 else 0.0,
        n_rejected=sum(f.n_rejected for f in perfs),
        sample_size=sample,
        disclosure={
            "insample": False,
            "adjusted_prices": True,
            "costs_included": True,
            "costs_scope": (
                "**样本外窗内的成本已扣**（它就在 nav_after/nav_before 这个比值里）；"
                "预热（训练）窗的成本是**水平效应** —— 只影响起点净值，不改变该窗内的"
                "收益率，因此不计入拼接曲线。刻意不按折摊派：训练窗互相重叠"
                "（train=250 / step=21 时同一天属于约 12 折），摊派会把同一笔成本"
                "重复计 12 次、系统性**高估**成本。故本报告的成本合计口径是"
                "「样本外窗内实际支付」，预热窗成本单独列出、供读者自行判断。"
            ),
            "fold_boundary_artifact": (
                "每折独立运行 → 折边界处的起始持仓由**本折自己的预热窗**产生，"
                "与「连续持有」的位置可能不同。这是路径依赖策略在 walk-forward 下的"
                "固有近似；折数越多、近似越粗。"
            ),
            "warmup_window": "训练窗仅用于指标预热（MA/ATR），**不做参数拟合**",
            "folds_not_independent": True,
            "per_fold_reentry": True,
            "intent_vs_fill": (
                "策略维护的是**意图仓位**；涨停买不到/跌停卖不掉/停牌时引擎丢弃信号，"
                "策略并不知情。n_rejected 是这条差异的可见计量。"
            ),
            "effective_n_unit": "trading_day",
            "note": (
                "样本量按**交易日**聚类（A 股同涨同跌）；相邻折训练窗重叠、测试窗首尾相接，"
                "故样本外交易日数**不是**独立样本数。每一折起点空仓重新建仓，"
                "成本随折数放大 —— 与「真实躺平只有一次买入」不可直接比成本。"
            ),
        },
    )


# ---------- 报告渲染 ----------

def render_markdown(perf: WalkForwardPerformance, *,
                    comparison: BenchmarkComparison | None = None,
                    component: WalkForwardPerformance | None = None,
                    generated: str = "") -> str:
    """渲染 Markdown 报告（纯函数：同一输入 → 同一文本，但 `generated` 是入参）。"""
    ss = perf.sample_size
    m = perf.metrics
    lines = [
        f"# Walk-forward 绩效报告 · `{perf.strategy_id}`（{generated}）",
        "",
        "> **本报告全部数字均为样本外**（只取各折 `test_dates` 段，复利拼接）。",
        "> 训练窗只用于 MA/ATR 预热，**不含任何参数拟合** —— 参数是文档默认值，未经搜索。",
        "",
        "## 参数（未搜索，文档默认值）",
        "",
        "| 参数 | 取值 |", "|---|---:|",
    ]
    for k, v in sorted(perf.params.items()):
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "## 折与样本量",
        "",
        f"- 折数：**{len(perf.folds)}**（另有 {len(perf.skipped_folds)} 折被跳过）",
        f"- 样本外区间：**{perf.oos_start} ~ {perf.oos_end}**",
        f"- 样本外**交易日**（有效样本量 `effective_n`）：**{ss.get('effective_n')}**",
        f"- 标的-日行数（**仅供参考，不得当样本量**）：{ss.get('oos_rows')}"
        f"（{ss.get('n_codes')} 个标的）",
        "",
        "## 样本外绩效（扣成本）",
        "",
        "| 指标 | 值 |", "|---|---:|",
        f"| 总收益 | {m['total_return'] * 100:.2f}% |",
        f"| 年化收益 | {m.get('annualized_return', 0.0) * 100:.2f}% |",
        f"| 最大回撤 | {m['max_drawdown'] * 100:.2f}% |",
        f"| 年化波动 | {m['volatility'] * 100:.2f}% |",
        f"| Sharpe（无风险利率 0） | {m['sharpe']:.3f} |",
        f"| 成交笔数（样本外 / 预热） | {perf.n_trades} / {perf.n_trades_warmup}"
        f"（合计 {perf.n_trades + perf.n_trades_warmup}） |",
        f"| 换手（全运行窗成交额 / 初始本金） | {perf.turnover_x:.3f}× |",
        f"| **成本合计（元，样本外已扣）** | **{perf.costs_total:.2f}**"
        f"（另有预热窗成本 {perf.costs_warmup:.2f}，属水平效应、**不计入**本曲线） |",
        f"| 期末净值 | {perf.stitched_nav[-1].nav:,.2f}（初始 {perf.initial_cash:,.0f}） |",
        f"| 被引擎拒绝的信号（涨停/停牌/缺钱） | {perf.n_rejected} |",
        "",
        "## 对照（同区间）",
        "",
    ]
    if comparison is not None:
        c = comparison
        lines += ["| 对象 | 总收益 | 年化 | 最大回撤 | 含成本 |", "|---|---:|---:|---:|---|"]
        lines.append(f"| `{perf.strategy_id}` | {m['total_return'] * 100:.2f}% "
                     f"| {m.get('annualized_return', 0.0) * 100:.2f}% "
                     f"| {m['max_drawdown'] * 100:.2f}% | 是 |")
        verdicts: list[str] = []
        if component is not None:
            cm = component.metrics
            lines.append(f"| `{component.strategy_id}`（同一 walk-forward 口径） "
                         f"| {cm['total_return'] * 100:.2f}% "
                         f"| {cm.get('annualized_return', 0.0) * 100:.2f}% "
                         f"| {cm['max_drawdown'] * 100:.2f}% | 是 |")
            gap = m["total_return"] - cm["total_return"]
            verdicts.append(
                f"- 对 `buy_and_hold`（**同尺**：同一 walk-forward、同一成本口径）："
                f"{'✅ 跑赢' if gap > 0 else '❌ **跑不赢**'}"
                f"（超额 {gap * 100:+.2f} 个百分点）"
                + ("" if gap > 0 else " —— 扣成本后**不如什么都不做**，这条原样上报，不修饰")
                + "。注意该对照每折重新建仓，折数会放大它的成本"
                  "（见 `per_fold_reentry`），因此这个比较对策略**偏有利**。")
        if c.status == "OK":
            bm = c.benchmark_metrics
            lines.append(f"| `{c.benchmark}`（指数点位） | {c.benchmark_return * 100:.2f}% "
                         f"| {bm.get('annualized_return', 0.0) * 100:.2f}% "
                         f"| {bm['max_drawdown'] * 100:.2f}% | 否（指数不可直接交易） |")
        lines += ["", *verdicts, f"- 基准状态：**{c.status}**"]
        if c.status == "OK":
            verdict = "✅ 跑赢" if (c.excess_return or 0) > 0 else "❌ **跑不赢**"
            lines += [
                f"- 策略 − index_300 超额：**{c.excess_return * 100:.2f}%** → {verdict}",
                f"- 口径差异：{c.note or '无'}",
            ]
        else:
            lines += [f"- ⚠️ 基准不可得：{c.note} —— **不比较不等于跑赢**"]
    else:
        lines.append("（未提供基准对照）")
    lines += [
        "",
        "## 口径披露（必须与数字同时读）",
        "",
    ]
    for k, v in perf.disclosure.items():
        lines.append(f"- `{k}`：{v}")
    lines += [
        "",
        "## 折明细",
        "",
        "| 折 | test 区间 | 天数 | 折收益 | 成交 | 成本 | 拒绝 |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    show = perf.folds if len(perf.folds) <= 12 else (perf.folds[:6] + perf.folds[-6:])
    for i, f in enumerate(show):
        if len(perf.folds) > 12 and i == 6:
            lines.append(f"| … | （中间 {len(perf.folds) - 12} 折见 JSON） | | | | | |")
        lines.append(f"| {f.index} | {f.test_start} ~ {f.test_end} | {f.n_test_days} "
                     f"| {f.fold_return * 100:.2f}% | {f.n_trades} "
                     f"| {f.costs:.2f} | {f.n_rejected} |")
    lines.append("")
    return "\n".join(lines)
