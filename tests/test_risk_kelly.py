"""P14 / Task 63：凯利核心 —— 公式、保守化、**NO_BET 的两道门**。

本文件的第一性目标不是「凯利算得对」，而是「**不该给仓位时它给不出仓位**」。
"""

from __future__ import annotations

import pytest

from stocklab.risk.kelly import (BOOTSTRAP_SEED, DEFAULT_F_CAP, EdgeStats,
                                 binary_kelly, breakeven_p, continuous_kelly,
                                 daily_clustered_winrate_ci, evaluate,
                                 lower_quartile, quantile, upper_quartile,
                                 verdict_label, wilson_lower_bound)


def _amps(base: float, k: int, spread: float) -> tuple[float, ...]:
    """k 个围绕 base 均匀铺开的幅度；`spread=0` → 全相等（赔率恰好 = 1）。"""
    if k <= 0:
        return ()
    if spread == 0 or k == 1:
        return tuple([base] * k)
    step = spread / (k - 1)
    return tuple(round(base - spread / 2 + i * step, 6) for i in range(k))


def stats(*, wins: int, losses: int, n_days: int = 200,
          win_amp: float = 0.05, loss_amp: float = 0.05, spread: float = 0.0,
          cost_bps: float = 26.0) -> EdgeStats:
    """造一份回放统计。`spread` > 0 时幅度有分布 —— 否则四分位保守化无从体现。"""
    w = _amps(win_amp, wins, spread)
    l = _amps(loss_amp, losses, spread)
    nets = w + tuple(-x for x in l)
    return EdgeStats(
        rule="test", code="000333", window_start="2020-01-01", window_end="2026-01-01",
        n_trades=len(nets), n_days=n_days,
        win_amplitudes=w, loss_amplitudes=l,
        day_winrates=tuple((1.0 if i < wins else 0.0,) for i in range(len(nets))),
        net_returns=nets, cost_bps=cost_bps)


# ---------- 公式 ----------

def test_wilson_lower_bound_is_below_the_point_estimate():
    """保守化方向只能是**向下** —— 下界高于点估就说明公式抄错了。"""
    lo = wilson_lower_bound(55, 100)
    assert 0.0 < lo < 0.55
    assert lo == pytest.approx(0.4679, abs=1e-3)


def test_wilson_lower_bound_tightens_with_more_samples():
    assert wilson_lower_bound(550, 1000) > wilson_lower_bound(55, 100)


def test_wilson_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        wilson_lower_bound(0, 0)
    with pytest.raises(ValueError):
        wilson_lower_bound(11, 10)


def test_quantiles_use_linear_interpolation():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert quantile(xs, 0.25) == pytest.approx(1.75)
    assert lower_quartile(xs) == pytest.approx(1.75)
    assert upper_quartile(xs) == pytest.approx(3.25)
    assert quantile([5.0], 0.25) == 5.0


def test_binary_kelly_matches_the_textbook_formula():
    # f* = p − (1−p)/b
    assert binary_kelly(0.6, 2.0) == pytest.approx(0.4)
    assert binary_kelly(0.5, 1.0) == pytest.approx(0.0)
    assert binary_kelly(0.4, 1.0) == pytest.approx(-0.2)   # 负 → 不下注
    with pytest.raises(ValueError):
        binary_kelly(0.5, 0.0)


def test_breakeven_p_is_the_inverse_of_odds():
    assert breakeven_p(1.0) == pytest.approx(0.5)
    assert breakeven_p(2.0) == pytest.approx(1 / 3)
    # 在 p_be 上，凯利恰好为 0 —— 两个式子的口径必须自洽
    assert binary_kelly(breakeven_p(1.5), 1.5) == pytest.approx(0.0)


def test_continuous_kelly_and_its_zero_variance_guard():
    assert continuous_kelly(0.01, 0.10) == pytest.approx(1.0)
    assert continuous_kelly(0.01, 0.0) is None      # 不是 0：0 会被读成「算出来是 0」
    assert continuous_kelly(-0.01, 0.10) < 0


def test_verdict_label_is_chinese():
    assert verdict_label("NO_BET") == "不下注"
    assert verdict_label("unknown") == "unknown"


# ---------- NO_BET：edge 门 ----------

def test_no_edge_gives_no_bet():
    """`p_used ≤ p_be` → f* = 0、NO_BET、edge_significant=False、f_final = 0。

    「赢 40 输 60、赔率 1」：点估胜率 0.40 < 平衡胜率 0.50。
    """
    k = evaluate(stats(wins=40, losses=60))
    assert k["verdict"] == "NO_BET"
    assert k["verdict_reason"] == "edge_not_significant"
    assert k["edge_significant"] is False
    assert k["f_star"] == 0.0
    assert k["f_final"] == 0.0
    assert k["gates"]["edge"]["meets"] is False
    assert k["p_used"] <= k["p_be"]


def test_point_estimate_alone_would_have_bet_but_conservative_does_not():
    """这正是保守化的意义：点估看着能赢，下界一压就没了。

    赢 55 输 45、赔率 1 → 点估 0.55 > 0.50（「有 edge」），
    但 Wilson 下界 ≈ 0.4679 < 0.50 → 不下注。
    """
    k = evaluate(stats(wins=55, losses=45, n_days=500))
    assert k["p_point"] > k["p_be"] > k["p_used"]
    assert k["verdict"] == "NO_BET"


def test_odds_are_conservative_too():
    """`b_used` < `b_point`：赢幅取**下**四分位、亏幅取**上**四分位。"""
    k = evaluate(stats(wins=90, losses=10, n_days=500, spread=0.04))
    assert k["b_used"] is not None and k["b_point"] is not None
    assert k["b_used"] < k["b_point"]


def test_missing_side_makes_the_edge_gate_fail():
    """一次都没亏过 → 赔率估不出来 → 不许下注（这类样本通常是窗口太短）。"""
    k = evaluate(stats(wins=10, losses=0, n_days=300))
    assert k["b_used"] is None
    assert k["verdict"] == "NO_BET"
    assert any("一次都没亏过" in w for w in k["warnings"])


def test_no_trades_at_all():
    k = evaluate(stats(wins=0, losses=0, n_days=0))
    assert k["verdict"] == "NO_BET"
    assert k["p_used"] is None and k["f_star"] == 0.0
    assert any("没有任何一次完整往返" in w for w in k["warnings"])


# ---------- NO_BET：样本门（**独立于 edge 门**）----------

def test_sample_gate_alone_blocks_a_positive_edge():
    """样本不足 → 哪怕算出来有 edge 也**不给仓位**（不得据此下注）。

    构造一份「点估与下界都过 p_be」的统计，但有效交易日只有 30 天。
    """
    k = evaluate(stats(wins=90, losses=10, n_days=30))
    assert k["gates"]["edge"]["meets"] is True      # edge 门是过的
    assert k["gates"]["sample"]["meets"] is False   # 样本门不过
    assert k["verdict"] == "NO_BET"
    assert k["verdict_reason"] == "sample_insufficient"
    assert k["f_final"] == 0.0
    assert k["gates"]["sample"]["label"] == "样本不足，仅供观察"


def test_both_gates_pass_gives_a_position():
    """两道门都过 → 给仓位；且 f_final = k·f*（分数凯利），不超过 f_cap。"""
    k = evaluate(stats(wins=95, losses=5, n_days=300, win_amp=0.06,
                       loss_amp=0.06, spread=0.04), frac=0.25)
    assert k["verdict"] == "BET"
    assert k["edge_significant"] is True
    assert k["f_star"] > 0
    assert k["f_final"] == pytest.approx(k["f_fractional"])
    assert k["f_final"] <= DEFAULT_F_CAP


# ---------- 分数凯利 / 过注 / 上限 ----------

def _bet_stats():
    return stats(wins=95, losses=5, n_days=300, win_amp=0.06,
                 loss_amp=0.06, spread=0.04)


def test_fractional_kelly_scales_linearly():
    a = evaluate(_bet_stats(), frac=0.25)
    b = evaluate(_bet_stats(), frac=0.5)
    # 输出按 6 位小数取整（stable JSON 的一部分），容差取 1e-6 而不是默认相对容差
    assert b["f_fractional"] == pytest.approx(2 * a["f_fractional"], abs=1e-6)
    assert b["k"] == 0.5


def test_overbet_beyond_2f_is_rejected_and_clipped():
    """`f > 2·f*` → 期望对数增长为负 → clip 到 2f* 并留痕。"""
    f_star = evaluate(_bet_stats(), frac=1.0)["f_star"]
    k = evaluate(_bet_stats(), frac=3.0)
    assert k["overbet_rejected"] is True
    assert k["f_fractional"] == pytest.approx(2 * f_star)
    assert any("过注拒绝" in w for w in k["warnings"])


def test_f_cap_is_applied_and_reported():
    """k=1 时 f_fractional 远大于 40% → 必须被 f_cap 截断并留痕。"""
    k = evaluate(_bet_stats(), frac=1.0, f_cap=0.40)
    assert k["f_cap"] == 0.40
    assert k["f_fractional"] > 0.40
    assert k["f_final"] == 0.40 and k["f_cap_applied"] is True


def test_invalid_parameters_are_rejected():
    with pytest.raises(ValueError):
        evaluate(stats(wins=5, losses=5), frac=0.0)
    with pytest.raises(ValueError):
        evaluate(stats(wins=5, losses=5), f_cap=1.5)


# ---------- 按日聚类 CI ----------

def test_ci_is_day_clustered_and_deterministic():
    by_day = [(1.0,), (0.0,), (1.0,), (1.0,), (0.0,)] * 4
    a = daily_clustered_winrate_ci(by_day)
    b = daily_clustered_winrate_ci(by_day)
    assert a == b                       # 固定种子 → 可复现
    assert a[0] < 0.6 < a[1]
    assert daily_clustered_winrate_ci([]) is None


def test_clustering_widens_the_ci_versus_row_resampling():
    """同一天 20 个标的不能用行重采样去当 20 个独立样本。"""
    by_day = [(1.0,) * 20, (0.0,) * 20] * 5      # 10 个交易日、每天 20 行
    clustered = daily_clustered_winrate_ci(by_day)
    rows = [x for day in by_day for x in day]
    row_ci = daily_clustered_winrate_ci([(x,) for x in rows])
    assert clustered[1] - clustered[0] > row_ci[1] - row_ci[0]


def test_bootstrap_seed_is_pinned():
    assert BOOTSTRAP_SEED == 20260915


# ---------- 连续近似 ----------

def test_continuous_approximation_is_reported_but_never_overrides():
    k = evaluate(_bet_stats())
    assert "f_star_continuous" in k
    # 报告了它，但 f_final 只由二元口径 + 分数 + 上限决定
    assert k["f_final"] <= min(k["f_fractional"], k["f_cap"])
