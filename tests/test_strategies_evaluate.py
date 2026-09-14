"""Task 28：walk-forward 评估链（P5）—— 策略 → 引擎 → 折 → 样本外绩效。

覆盖四件必须成立的事（其中三条是变异反证的目标）：
  1. 未注册策略**在接口层**就被拒绝（`NotRegistered`）；
  2. 成本真的被扣了（零成本模型下的净值必须显著更高）；
  3. 每折用**全新**策略实例（跨折共享状态 = 隐性泄漏）；
  4. 样本量按**交易日**报，行数只作核对。
"""

import math

import pytest

from stocklab.backtest.engine import Signal
from stocklab.backtest.metrics import annualize
from stocklab.backtest.walkforward import split_walk_forward
from stocklab.calendar.trading_calendar import Calendar
from stocklab.config.costs import CostModel
from stocklab.config.universe import Instrument
from stocklab.data.models import Bar
from stocklab.strategies.base import Strategy
from stocklab.strategies.evaluate import (NoFoldEvaluated, evaluate_walk_forward,
                                          render_markdown)
from stocklab.strategies.registry import (NotRegistered, strategy_registry)

CASH = 1_000_000.0
N_DAYS = 300
TRAIN, TEST = 100, 20
ZERO_COST = CostModel(commission_rate=0.0, min_commission=0.0, stamp_tax_rate=0.0,
                      transfer_fee_rate=0.0, slippage_bps=0.0)
UNIVERSE = (Instrument("000001", "甲", "sz", "main"),
            Instrument("000002", "乙", "sz", "main"))
CODES = ("000001", "000002")


def dates(n=N_DAYS):
    return [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]


def make_bars(code, offset=0.0):
    """正弦行情：均线会反复交叉，trend_ma 才有成交可看（不是一条直线）。"""
    out = []
    for i, d in enumerate(dates()):
        c = round(10.0 + offset + 3.0 * math.sin(i / 9.0), 4)
        out.append(Bar(code=code, date=d, open=c, high=c * 1.01, low=c * 0.99,
                       close=c, volume=10_000, amount=c * 10_000, turnover=1.0,
                       source="test", adj_mode="qfq"))
    return out


@pytest.fixture
def env():
    bars = {c: make_bars(c, offset=0.0 if c == "000001" else 1.0) for c in CODES}
    axis = dates()
    return {
        "bars_by_code": bars,
        "axis": axis,
        "calendar": Calendar.from_dates(axis),
        "universe": UNIVERSE,
        "folds": split_walk_forward(axis, train=TRAIN, test=TEST),
    }


def flat_bars(code):
    """价格恒定 10.00 → 市场收益为 0，净值的变化**只能**来自成本。

    成本类断言必须建在这种环境上：正弦行情下「净值 < 本金」可能只是因为
    行情本身跌了，那种断言证明不了「成本被扣了」。
    """
    return [Bar(code=code, date=d, open=10.0, high=10.0, low=10.0, close=10.0,
                volume=10_000, amount=100_000, turnover=1.0, source="test",
                adj_mode="qfq") for d in dates()]


@pytest.fixture
def flat_env():
    bars = {c: flat_bars(c) for c in CODES}
    axis = dates()
    return {
        "bars_by_code": bars,
        "axis": axis,
        "calendar": Calendar.from_dates(axis),
        "universe": UNIVERSE,
        "folds": split_walk_forward(axis, train=TRAIN, test=TEST),
    }


def run(strategy_id, env, costs=None, **params):
    return evaluate_walk_forward(strategy_id, folds=env["folds"],
                                 bars_by_code=env["bars_by_code"],
                                 calendar=env["calendar"],
                                 universe=env["universe"], initial_cash=CASH,
                                 costs=costs or CostModel(), **params)


# ---------- 1. 注册表是唯一的入口 ----------

def test_unregistered_strategy_is_rejected(env):
    with pytest.raises(NotRegistered) as e:
        run("no_such_strategy", env)
    assert "未注册" in str(e.value)


def test_empty_folds_fail_loudly(env):
    with pytest.raises(NoFoldEvaluated):
        evaluate_walk_forward("trend_ma", folds=[], bars_by_code=env["bars_by_code"],
                              calendar=env["calendar"], universe=env["universe"],
                              initial_cash=CASH, costs=CostModel())


# ---------- 2. 端到端：样本外数字真的产出了 ----------

def test_evaluation_produces_oos_curve(env):
    perf = run("trend_ma", env)
    assert len(perf.folds) == len(env["folds"]) == 10
    assert not perf.skipped_folds
    assert perf.oos_start == env["folds"][0].test_start
    assert perf.oos_end == env["folds"][-1].test_end
    assert len(perf.stitched_nav) == sum(len(f.test_dates) for f in env["folds"])
    for key in ("total_return", "max_drawdown", "volatility", "sharpe",
                "annualized_return", "n_sessions"):
        assert key in perf.metrics
    assert perf.metrics["n_sessions"] == len(perf.stitched_nav)
    # 参数是文档默认值（本任务不做任何搜索）
    assert perf.params == {"fast": 20, "slow": 60, "atr_mult": 2.0}


def test_stitched_curve_is_continuous_at_fold_boundaries(env):
    """拼接不许在折边界上跳空：后一折的首点相对前一折末点的收益必须
    等于那一折自己的折收益（缩放正确）。"""
    perf = run("buy_and_hold", env)
    for prev, cur in zip(perf.folds, perf.folds[1:]):
        i = perf.stitched_nav.index([p for p in perf.stitched_nav
                                     if p.date == cur.nav_points[0].date][0])
        j = perf.stitched_nav.index([p for p in perf.stitched_nav
                                     if p.date == prev.nav_points[-1].date][0])
        assert i == j + 1
        step = perf.stitched_nav[i].nav / perf.stitched_nav[j].nav - 1.0
        first = cur.nav_points[0].nav / cur.nav_before - 1.0
        assert step == pytest.approx(first, rel=1e-9)


# ---------- 3. 成本必须真被扣（变异反证目标） ----------

def test_oos_costs_are_deducted(env):
    """样本外窗内的成本必须真被扣掉（变异反证目标：把 cost 换成零成本 → 本测试红）。

    用 `trend_ma` 而不是 `buy_and_hold`：前者的成交**发生在样本外窗内**，
    成本才会进入 `nav_after / nav_before` 这个比值；
    `buy_and_hold` 建仓后就再也不交易，样本外成本恒为 0，证明不了什么。
    """
    costly = run("trend_ma", env, costs=CostModel())
    free = run("trend_ma", env, costs=ZERO_COST)
    assert costly.n_trades > 0, "样本外窗内没有成交，无法验证成本"
    assert costly.costs_total > 0, "样本外成本为 0 —— 成本根本没被扣"
    gap = free.stitched_nav[-1].nav - costly.stitched_nav[-1].nav
    assert gap > costly.costs_total, \
        f"零成本与含成本的净值差 {gap:.2f} 未超过成本合计 {costly.costs_total:.2f}"


def test_warmup_costs_reported_but_not_charged_per_fold(flat_env):
    """预热窗成本**只报不摊派**：训练窗互相重叠（train=250/step=21 时同一天属于
    约 12 折），按折摊派会把同一笔成本重复计 ~12 次、系统性**高估**成本。

    本条必须建在**恒定价格**上：行情一动，「含成本 vs 零成本」的净值差里就混进了
    「买入股数不同 → 现金拖累不同」的效应（实测非平坦行情下这一项约 653 元，
    与成本口径无关）。价格恒定时市场收益为 0，净值差就只剩成本口径本身。

    `buy_and_hold` 的成交全在预热窗（`costs_total == 0`），是干净探针：
    若改成「按折摊派预热成本」，净值差会从 ~0 跳到整个预热窗成本的量级 → 变红。
    """
    costly = run("buy_and_hold", flat_env, costs=CostModel())
    free = run("buy_and_hold", flat_env, costs=ZERO_COST)
    assert costly.costs_warmup > 0
    assert costly.costs_total == 0                       # 成交全在预热窗
    assert costly.n_trades_warmup == len(costly.folds)   # 每折一次建仓
    gap = abs(free.stitched_nav[-1].nav - costly.stitched_nav[-1].nav)
    assert gap < 0.01 * costly.costs_warmup, \
        f"净值差 {gap:.2f} 已达预热成本的量级 —— 预热成本被按折重复摊派了"


def test_zero_cost_flat_market_leaves_nav_untouched(flat_env):
    """价格恒定 + 零成本 → 净值必须**原封不动**（一分钱都不许凭空出现）。"""
    perf = run("buy_and_hold", flat_env, costs=ZERO_COST)
    assert perf.costs_total == 0 and perf.costs_warmup == 0
    assert perf.stitched_nav[-1].nav == pytest.approx(CASH, rel=1e-9)


def test_costs_scope_is_disclosed(env):
    perf = run("trend_ma", env)
    d = perf.disclosure
    assert "costs_scope" in d and "样本外" in d["costs_scope"]
    assert "12" in d["costs_scope"]                      # 说明为什么不做按折摊派
    assert "fold_boundary_artifact" in d


# ---------- 4. 每折全新实例 ----------

@strategy_registry.register
class OncePerFold(Strategy):
    """只在「自己见到的第一根 K 线」买入 —— 若实例跨折复用，后续折就不会再买。"""

    strategy_id = "tests_once_per_fold"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen = 0

    def _generate(self, date, pit_history, pit_features):
        self.seen += 1
        if self.seen == 1:
            return {c: Signal("buy", 100.0, "first_bar") for c in pit_history}
        return {}


def test_fresh_strategy_instance_per_fold(env):
    perf = run("tests_once_per_fold", env)
    assert perf.n_trades_warmup == len(perf.folds)
    assert perf.folds[0].n_trades_warmup == 1


# ---------- 5. 样本量口径 ----------

def test_sample_size_is_trading_days_not_rows(env):
    perf = run("trend_ma", env)
    ss = perf.sample_size
    expected_days = sum(len(f.test_dates) for f in env["folds"])
    assert ss["unit"] == "trading_day"
    assert ss["effective_n"] == expected_days
    assert ss["oos_rows"] > ss["effective_n"]          # 2 标的 → 行数天然更大
    assert "不得" in ss["note"]


# ---------- 6. 报告 ----------

def test_report_states_verdict_and_disclosures(env):
    perf = run("trend_ma", env)
    md = render_markdown(perf, comparison=None, generated="2026-09-15")
    assert "trend_ma" in md
    assert "样本外" in md
    assert f"effective_n`）：**{sum(len(f.test_dates) for f in env['folds'])}**" in md
    assert "folds_not_independent" in md and "per_fold_reentry" in md
    assert "预热" in md


def test_report_is_deterministic(env):
    perf = run("trend_ma", env)
    a = render_markdown(perf, generated="2026-09-15")
    b = render_markdown(perf, generated="2026-09-15")
    assert a == b


# ---------- 7. 年化 ----------

def test_annualize_geometric():
    assert annualize(0.0, 252) == pytest.approx(0.0)
    assert annualize(0.21, 252) == pytest.approx(0.21)          # 一年 → 原样
    assert annualize(0.1, 126) == pytest.approx(0.21)           # 半年 10% → 年化 21%
    assert annualize(-2.0, 252) == -1.0                         # 亏光 → -100%，不开负数方
    assert annualize(0.5, 0) == 0.0
