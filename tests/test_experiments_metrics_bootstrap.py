"""P10-a 预注册的**辅助判据**：按日聚类的 bootstrap CI。

要钉住三件事：

  1. **重采样单位是「日」**：同一天的多行必须**同进同出** —— 按行重采样会把
     当天全市场共同的涨跌当成独立证据，把区间**撑窄**（更容易「显著」），
     那正是本项目要防的方向；
  2. **可复现**：同输入两次调用给出**逐位相同**的区间（种子固定）——
     一个跑两次给出两个区间的判据不能被预注册；
  3. **与正态近似同量级**：在同分布的大样本上，两个区间应当接近；
     本函数**不**替换 `gate()` 的判定口径（`METRIC_VERSION` 未变）。
"""

from __future__ import annotations

import pytest

from stocklab.experiments.metrics import (BOOTSTRAP_N, BOOTSTRAP_SEED,
                                          bootstrap_daily_ci, daily_stats,
                                          paired_daily_delta)


def _rows(deltas_by_day: dict[str, list[float]], side: str):
    """造两边都可评分的行：`variant - baseline = delta`（逐行精确）。"""
    out = []
    for day, ds in deltas_by_day.items():
        for k, d in enumerate(ds):
            base = 0.5
            out.append({
                "target_date": day, "code": f"c{k}", "scorable": True,
                "hit_direction": (base + d) if side == "v" else base,
                "hit_range": 1.0, "notes": {"brier": (base + d) if side == "v" else base},
            })
    return out


def _pair(deltas_by_day):
    return _rows(deltas_by_day, "b"), _rows(deltas_by_day, "v")


def test_unknown_metric_is_refused():
    with pytest.raises(ValueError, match="未知指标"):
        bootstrap_daily_ci([], [], "sharpe")


def test_single_day_gives_no_interval_rather_than_a_fake_one():
    b, v = _pair({"2024-01-01": [0.1, 0.2]})
    out = bootstrap_daily_ci(b, v, "brier")
    assert out["n_days"] == 1
    assert out["ci95"] is None, "1 个交易日编不出区间 —— 不许给一个假的宽度"


def test_empty_gives_no_interval():
    out = bootstrap_daily_ci([], [], "brier")
    assert out["n_days"] == 0 and out["ci95"] is None and out["mean"] is None


def test_result_is_reproducible_bit_for_bit():
    deltas = {f"2024-01-{i:02d}": [0.01 * (i % 7) - 0.03] for i in range(1, 29)}
    b, v = _pair(deltas)
    a1 = bootstrap_daily_ci(b, v, "brier")
    a2 = bootstrap_daily_ci(b, v, "brier")
    assert a1 == a2
    assert a1["seed"] == BOOTSTRAP_SEED and a1["n_boot"] == BOOTSTRAP_N
    assert a1["method"] == "day_clustered_percentile_bootstrap"


def test_days_are_resampled_not_rows():
    """把某一天的**行数**撑到 50 倍，区间**不许**明显变窄。

    这是「按行重采样」与「按日重采样」的判决性对照：若单位是行，
    那一天的 50 行会被当成 50 份独立证据，区间立刻塌掉。
    """
    deltas = {f"2024-01-{i:02d}": [0.02 * ((i % 5) - 2)] for i in range(1, 21)}
    b, v = _pair(deltas)
    thin = bootstrap_daily_ci(b, v, "direction")
    fat = dict(deltas)
    fat["2024-01-05"] = [0.02 * 3] * 50          # 同一天塞 50 行，Δ 全是 +0.06
    b2, v2 = _pair(fat)
    thick = bootstrap_daily_ci(b2, v2, "direction")
    w_thin = thin["ci95"][1] - thin["ci95"][0]
    w_thick = thick["ci95"][1] - thick["ci95"][0]
    assert thick["n_days"] == thin["n_days"] == 20, "有效样本量必须仍是**交易日数**"
    assert w_thick > 0.6 * w_thin, (
        f"同一天多塞 50 行把区间从 {w_thin:.4f} 压到 {w_thick:.4f} —— "
        "重采样单位是「行」而不是「日」，按日聚类的纪律没作用到比较上"
    )


def test_bootstrap_straddles_the_point_estimate_and_is_ordered():
    deltas = {f"2024-02-{i:02d}": [0.01 * ((i % 9) - 4)] for i in range(1, 32)}
    b, v = _pair(deltas)
    out = bootstrap_daily_ci(b, v, "brier")
    lo, hi = out["ci95"]
    assert lo <= out["mean"] <= hi
    assert lo < hi


def test_bootstrap_is_close_to_the_normal_approximation_on_a_calm_sample():
    """正态近似与 bootstrap 在同一份「不太偏」的样本上应当接近。

    两者差异大**不是**错误（小样本分位 CI 本来就更宽），但若量级差一个数量级，
    说明其中一套实现坏了 —— 这条是那个量级的哨兵。
    """
    deltas = {f"2024-03-{i:02d}": [0.005 * ((i % 11) - 5)] for i in range(1, 32)}
    b, v = _pair(deltas)
    boot = bootstrap_daily_ci(b, v, "direction")
    norm = paired_daily_delta(b, v)["direction"]
    w_boot = boot["ci95"][1] - boot["ci95"][0]
    w_norm = norm["ci95"][1] - norm["ci95"][0]
    assert 0.5 < w_boot / w_norm < 2.0, (w_boot, w_norm)


def test_it_agrees_with_daily_stats_on_the_point_estimate():
    deltas = {f"2024-04-{i:02d}": [0.003 * (i % 6)] for i in range(1, 26)}
    b, v = _pair(deltas)
    assert bootstrap_daily_ci(b, v, "brier")["mean"] == pytest.approx(
        paired_daily_delta(b, v)["brier"]["mean"], abs=1e-12)


def test_daily_stats_is_untouched_by_the_new_estimator():
    """`gate()` 用的口径**没变** —— `METRIC_VERSION` 与 `daily_stats` 的输出形状不动。"""
    import inspect

    params = set(inspect.signature(daily_stats).parameters)
    assert params == {"values_by_day"}
    from stocklab.experiments.metrics import METRIC_VERSION

    assert METRIC_VERSION == "p8-metrics-v1"
