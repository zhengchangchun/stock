"""`predict.residual.ResidualDistribution`（P10-a）：经验分位分布的形状与边界。

这是 P10-a 唯一的**新统计对象**，它替换的是 `Normal(mu, sigma)` 的解析分位。
所以这一层要钉住的不是「能算」，而是：

  1. **它是个分布**：`cdf` 单调不减、值域 `[0,1]`、`quantile` 是它的逆
     （`cdf(quantile(p)) >= p`，且在**没有并列值**时取等）；
  2. **非对称**：左右分位各取各的 —— 这正是「形状」要与高斯比的东西，
     若实现里悄悄做了对称化，本实验就变成了「重跑缩放」，必须被抓住；
  3. **退化输入即拒绝**：空池 / 少于门槛 / 未排序 / 非有限值 → 构造期报错，
     **不许**静默降级成一个「看起来正常」的分布；
  4. **证据块可复算**：`as_evidence()` 里的分位必须能被 `quantile()` 重新算出。
"""

from __future__ import annotations

import math

import pytest

from stocklab.predict.residual import (RESIDUAL_MIN_SAMPLES,
                                       InsufficientResiduals,
                                       ResidualDistribution)

#: 一个够长的、**非对称**的确定性残差池：右尾比左尾长（A 股常见的偏斜形态）。
SYMMETRIC = tuple(sorted(
    [-1.5 - 0.01 * i for i in range(200)] + [1.5 + 0.01 * i for i in range(200)]
    + [0.001 * (i - 100) for i in range(200)]
))
SKEWED = tuple(sorted(
    [-1.2 - 0.004 * i for i in range(300)] + [1.8 + 0.02 * i for i in range(300)]
))


def dist(values=SYMMETRIC, **kw) -> ResidualDistribution:
    base = dict(codes=("000333",), first_day="2013-04-17", last_day="2021-04-29",
                n_days=1000, n_skipped=3, window=60)
    base.update(kw)
    return ResidualDistribution(values=tuple(values), **base)


# ---------- 1. 它是个分布 ----------

def test_cdf_is_monotone_and_stays_inside_the_unit_interval():
    d = dist()
    zs = [-9.0, -3.0, -1.3, -0.33, 0.0, 0.33, 1.3, 3.0, 9.0]
    vals = [d.cdf(z) for z in zs]
    assert all(0.0 <= v <= 1.0 for v in vals)
    assert vals == sorted(vals), f"cdf 不单调：{vals}"


def test_cdf_below_and_above_the_pool_saturates():
    d = dist()
    assert d.cdf(-100.0) == 0.0
    assert d.cdf(100.0) == 1.0


def test_quantile_is_the_inverse_of_cdf():
    d = dist()
    for p in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        q = d.quantile(p)
        assert d.cdf(q) >= p, f"cdf(quantile({p}))={d.cdf(q)} < {p}"
        # 逆的**最小性**：比 q 略小的值，其 cdf 必须 < p（否则 q 不是最小解）
        lower = [v for v in d.values if v < q]
        if lower:
            assert d.cdf(lower[-1]) < p or d.cdf(lower[-1]) == d.cdf(q)


def test_quantile_matches_a_hand_computed_index_on_a_known_pool():
    # 1..500 升序：p 分位 = 第 ceil(p*500) 小的值
    d = dist(values=tuple(float(i) for i in range(1, 501)))
    assert d.quantile(0.10) == 50.0      # ceil(50) = 50
    assert d.quantile(0.50) == 250.0
    assert d.quantile(0.90) == 450.0
    assert d.quantile(1.00) == 500.0
    assert d.quantile(0.002) == 1.0      # 极小 p 仍落在池内，不越界


def test_quantile_monotone_in_p():
    d = dist(values=SKEWED)
    qs = [d.quantile(p) for p in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)]
    assert qs == sorted(qs)


# ---------- 2. 非对称必须真的非对称 ----------

def test_skewed_pool_gives_asymmetric_quantiles():
    d = dist(values=SKEWED)
    lo, hi = d.quantile(0.10), d.quantile(0.90)
    assert abs(hi) > abs(lo) + 0.2, (
        f"偏斜池的 10%/90% 分位应当明显不对称，实测 {lo} / {hi} —— "
        "若被对称化，本实验就退化成「重跑缩放」，结论不再归因于形状"
    )


def test_symmetric_pool_gives_symmetric_quantiles():
    d = dist()
    lo, hi = d.quantile(0.10), d.quantile(0.90)
    assert math.isclose(lo, -hi, rel_tol=0.15), (lo, hi)


# ---------- 3. 退化输入即拒绝 ----------

def test_empty_pool_is_refused():
    with pytest.raises(InsufficientResiduals):
        dist(values=())


def test_pool_below_the_minimum_is_refused():
    with pytest.raises(InsufficientResiduals):
        dist(values=tuple(float(i) for i in range(RESIDUAL_MIN_SAMPLES - 1)))


def test_exactly_the_minimum_is_accepted():
    d = dist(values=tuple(float(i) for i in range(RESIDUAL_MIN_SAMPLES)))
    assert d.n == RESIDUAL_MIN_SAMPLES


def test_unsorted_pool_is_refused():
    with pytest.raises(ValueError):
        dist(values=(0.5, -0.5, 1.0) + tuple(float(i) for i in range(300)))


def test_non_finite_value_is_refused():
    with pytest.raises(ValueError):
        dist(values=tuple(float(i) for i in range(300)) + (float("nan"),))


def test_negative_or_zero_sigma_flag_is_not_a_silent_skip():
    # `n_skipped` 只作溯源计数；它进证据块，不参与任何分布计算
    d = dist(n_skipped=17)
    assert d.as_evidence()["n_skipped"] == 17


# ---------- 4. 证据块可复算 ----------

def test_evidence_quantiles_are_reproducible_from_quantile():
    d = dist(values=SKEWED)
    ev = d.as_evidence()
    for key, p in (("q_10", 0.10), ("q_50", 0.50), ("q_90", 0.90)):
        assert ev[key] == pytest.approx(d.quantile(p), abs=1e-12), key
    assert ev["n"] == d.n
    assert ev["codes"] == ["000333"]
    assert ev["first_day"] == "2013-04-17"
    assert ev["last_day"] == "2021-04-29"
    assert ev["window"] == 60


def test_evidence_is_json_serialisable_plain_data():
    import json

    ev = dist().as_evidence()
    assert json.loads(json.dumps(ev, ensure_ascii=False)) == ev
