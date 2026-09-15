"""P10-a 第三条轴：`dist_mode`（预测分布的**形状来源**）。

五组必须被钉住的东西：

1. **默认路径零改动**：`dist_mode="gaussian"` 与 `spec=None` 逐字节等价 ——
   基线报告 sha256 `a61d026b…f4eb` 是回归红线，这条测试是它在单测里的哨兵；
2. **开关是唯一的变量**：`gaussian` 口径下**即使传进一个残差分布也不读它**
   （输出逐字节等于不传）—— 否则「这次跑的是哪个口径」就不再能从参数读出来；
3. **CDF 与分位同源**：`p_up/p_flat/p_down` 与 `range_80` 必须由**同一份**
   残差池算出（手算 `F(z)` 与 `Q(p)` 对照），不是各自一套；
4. **缺残差即拒绝**：`residuals=None` → `DegenerateInput`，
   **不许**静默回退成高斯（那会让变体报告里混进基线口径的行）；
5. **跨层契约**：`DIST_MODES` ←→ 变体注册表的 `changed_axis` 仍然恰好一个字段。
"""

from __future__ import annotations

import math

import pytest

from stocklab.data.models import Bar
from stocklab.errors import DegenerateInput
from stocklab.predict.model import (BASELINE_SPEC, DIST_MODES, FLAT_BAND,
                                    RANGE_COVERAGE, ForecastSpec,
                                    compute_forecast, degenerate_strategy_mix)
from stocklab.predict.residual import ResidualDistribution

CODE = "000333"
MIX = degenerate_strategy_mix()


def bars(n: int = 200, **kw):
    """确定性日 K（与 `tests/test_predict_model_sigma.bars` 同一份口径）。

    刻意**直接复用**它的实现而不是抄一遍：两处各写一份生成器，迟早会漂移，
    而「同一份输入」是这几条测试能比的前提。
    """
    from tests.test_predict_model_sigma import bars as _bars

    return _bars(n, **kw)


def _pool(values) -> ResidualDistribution:
    vals = tuple(sorted(float(v) for v in values))
    return ResidualDistribution(values=vals, codes=(CODE,), first_day="2020-01-02",
                                last_day="2020-10-01", n_days=200, n_skipped=0,
                                window=60)


def _fat_tailed_pool(n: int = 1000) -> ResidualDistribution:
    """确定性的「尖峰厚尾」池：中心比标准正态窄、尾部比它厚。"""
    vals = []
    for i in range(n):
        u = (i + 0.5) / n
        # 用 tanh 压缩中心、拉长尾部 —— 非对称（右尾更长）
        z = math.copysign(abs(math.log(2 * u if u < 0.5 else 2 * (1 - u))) ** 0.7, u - 0.5)
        vals.append(z * (1.18 if u > 0.5 else 0.92))
    return _pool(vals)


def _call(spec=None, residuals=None, *, b=None, asof="2020-06-30"):
    b = b if b is not None else bars()
    return compute_forecast(code=CODE, asof=asof, bars=b,
                            target_date=_next_day(asof), strategy_mix=MIX,
                            spec=spec, residuals=residuals)


def _next_day(d: str) -> str:
    from tests.test_predict_service import _to_date, _to_ord

    return _to_date(_to_ord(d) + 1)


# ---------- 1. 默认路径零改动 ----------

def test_gaussian_mode_is_byte_identical_to_the_baseline_spec():
    assert _call(None) == _call(ForecastSpec(dist_mode="gaussian"))
    assert ForecastSpec().dist_mode == "gaussian"
    assert BASELINE_SPEC.dist_mode == "gaussian"
    assert BASELINE_SPEC.is_baseline


def test_gaussian_branch_recomputes_the_same_numbers_by_hand():
    """基线分支**独立复算**一遍：`Φ` 解析式手算 → 必须逐位等于模型输出。

    这不是「自己等于自己」：复算走的是测试里另写的一段 `NormalDist` 调用，
    模型若在 `gaussian` 分支里被误改成读残差池，这条会红。
    """
    import statistics

    p = _call(None)
    inp = p["evidence"]["inputs"]
    mu, sigma, close = inp["mu"], inp["sigma"], inp["close"]
    nd = statistics.NormalDist(mu, sigma)
    assert p["direction"]["down"] == round(float(nd.cdf(math.log(1 - FLAT_BAND))), 6)
    assert p["direction"]["up"] == round(1.0 - float(nd.cdf(math.log(1 + FLAT_BAND))), 6)
    z80 = statistics.NormalDist().inv_cdf(0.50 + RANGE_COVERAGE / 2.0)
    assert p["range_80"] == [round(close * math.exp(mu - z80 * sigma), 2),
                             round(close * math.exp(mu + z80 * sigma), 2)]


# ---------- 2. 开关是唯一的变量 ----------

def test_gaussian_mode_ignores_a_supplied_residual_pool():
    """传了残差池但口径是 `gaussian` → 逐字节不读它。

    若这条红了，说明口径的「读什么」取决于**调用方传了什么**而不是 spec ——
    那「这次跑的是哪个口径」就不能从参数读出来了（单变量纪律失效）。
    """
    assert _call(ForecastSpec(dist_mode="gaussian"),
                 residuals=_fat_tailed_pool()) == _call(None)


def test_changed_fields_is_exactly_dist_mode():
    spec = ForecastSpec(dist_mode="resid_emp")
    assert spec.changed_fields() == ("dist_mode",)
    assert not spec.is_baseline


def test_dist_modes_registry_is_closed():
    assert DIST_MODES == ("gaussian", "resid_emp")
    with pytest.raises(ValueError):
        ForecastSpec(dist_mode="empirical")


# ---------- 3. CDF 与分位同源 ----------

def test_direction_probabilities_come_from_the_residual_ecdf():
    pool = _fat_tailed_pool()
    p = _call(ForecastSpec(dist_mode="resid_emp"), residuals=pool)
    inp = p["evidence"]["inputs"]
    mu, sigma = inp["mu"], inp["sigma"]
    z_lo = (math.log(1 - FLAT_BAND) - mu) / sigma
    z_hi = (math.log(1 + FLAT_BAND) - mu) / sigma
    assert p["direction"]["down"] == pytest.approx(pool.cdf(z_lo), abs=1e-6)
    assert p["direction"]["up"] == pytest.approx(1.0 - pool.cdf(z_hi), abs=1e-6)
    assert p["direction"]["flat"] == pytest.approx(
        1.0 - p["direction"]["up"] - p["direction"]["down"], abs=1e-9)
    total = sum(p["direction"].values())
    assert abs(total - 1.0) < 1e-9


def test_range_80_comes_from_the_same_pool_quantiles():
    pool = _fat_tailed_pool()
    p = _call(ForecastSpec(dist_mode="resid_emp"), residuals=pool)
    inp = p["evidence"]["inputs"]
    mu, sigma, close = inp["mu"], inp["sigma"], inp["close"]
    tail = (1.0 - RANGE_COVERAGE) / 2.0
    lo = round(close * math.exp(mu + sigma * pool.quantile(tail)), 2)
    hi = round(close * math.exp(mu + sigma * pool.quantile(1.0 - tail)), 2)
    assert p["range_80"] == [lo, hi]


def test_fat_tailed_pool_is_actually_fatter_than_the_gaussian_in_the_tails():
    """**对照**：证明这条轴真的有鉴别力 —— 若不成立，实验测的是空气。

    同一个 `(mu, sigma)` 下，厚尾池的 0.90 分位必须**大于** `Φ⁻¹(0.90)=1.2816`。
    """
    import statistics

    pool = _fat_tailed_pool()
    assert pool.quantile(0.90) > statistics.NormalDist().inv_cdf(0.90)
    assert pool.quantile(0.10) < statistics.NormalDist().inv_cdf(0.10)


def test_variant_evidence_records_the_pool_provenance():
    pool = _fat_tailed_pool()
    p = _call(ForecastSpec(dist_mode="resid_emp"), residuals=pool)
    inp = p["evidence"]["inputs"]
    assert inp["dist_mode"] == "resid_emp"
    assert inp["residuals"]["n"] == pool.n
    assert inp["residuals"]["first_day"] == pool.first_day
    # 形状摘要必须与池子同源
    assert inp["residuals"]["q_90"] == pytest.approx(pool.quantile(0.90), abs=1e-12)


def test_gaussian_evidence_block_is_untouched():
    """基线证据块里**不许**出现 `dist_mode` 键（那会改动 sha256 红线）。"""
    assert "dist_mode" not in _call(None)["evidence"]["inputs"]
    assert "residuals" not in _call(None)["evidence"]["inputs"]


# ---------- 4. 缺残差即拒绝 ----------

def test_missing_pool_is_refused_not_silently_degraded():
    with pytest.raises(DegenerateInput) as ei:
        _call(ForecastSpec(dist_mode="resid_emp"), residuals=None)
    assert "residuals" in str(ei.value)


def test_key_levels_are_not_touched_by_the_shape_axis():
    """`p_touch` / 关键位是**日内路径**量的近似，不在本条轴的定义域内 —— 逐位不动。

    与 `direction` / `range_80` 的对比就是这条轴的边界声明：
    **形状只作用于「次日收盘收益」这个分布**，不作用于日内延展。
    """
    base = _call(None)
    var = _call(ForecastSpec(dist_mode="resid_emp"), residuals=_fat_tailed_pool())
    assert var["key_levels"] == base["key_levels"]
    assert var["invalidate_if"] == base["invalidate_if"]
    # `action` / `size_pct` 是三分类概率的泛函 → **随形状变**（正是应当变的东西）
    assert var["size_pct"] != base["size_pct"]
