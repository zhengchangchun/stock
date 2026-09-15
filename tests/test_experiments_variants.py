"""Task 37：变体注册表、单变量校验、PIT 指数方向。

三条必须被钉住的东西：

  1. **单变量是机械可校验的**，不是口头纪律 —— 改两个变量的「变体」跑不起来；
  2. **指数方向函数拿不到未来**：它只允许用 `<= asof` 的指数行，且
     `asof` 当天没有数据时返回 `None`（**不是**「取最近一根」）；
  3. **标签带没有旋钮**：`ForecastSpec` 里不存在改 `FLAT_BAND` / 窗口 / 标的集合的字段，
     并有一条测试直接断言「那些字段名不在 fields 里」。
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from stocklab.data.models import Bar
from stocklab.predict.model import DIST_MODES as _DIST_MODES


def test_resid_quantile_variant_changes_exactly_one_field():
    """P10-a 的形状轴：注册表里那条变体相对基线**恰好改 1 个字段**。"""
    import dataclasses

    from stocklab.experiments.variants import (VARIANTS, assert_single_variable,
                                               get_variant)
    from stocklab.predict.model import ForecastSpec

    v = get_variant("residual-quantile-interval")            # 内部已 assert_single_variable
    assert v.changed_axis == "dist_mode"
    assert v.spec.changed_fields() == ("dist_mode",)
    assert v.spec.dist_mode in _DIST_MODES
    assert v.spec.mu_mode == "sample_mean" and v.spec.sigma_mode == "const", (
        "形状轴**不许**同时动 mu 或 sigma —— 那是第二条/第三条变量"
    )
    assert v.spec == ForecastSpec(dist_mode="resid_emp")
    assert VARIANTS["residual-quantile-interval"].prereg_doc.endswith(
        "2026-09-15-residual-quantile-interval.md")


def test_dist_mode_is_validated_at_construction():
    from stocklab.predict.model import ForecastSpec

    with pytest.raises(ValueError, match="未知 dist_mode"):
        ForecastSpec(dist_mode="empirical")


from stocklab.experiments.variants import (VARIANTS, MultiVariableVariant,
                                           UnknownVariant, Variant,
                                           assert_single_variable, get_variant,
                                           index_direction, load_index_direction)
from stocklab.predict.model import (BASELINE_SPEC, FLAT_BAND, ForecastSpec,
                                    MU_MODES, compute_forecast)

from tests.test_predict_service import _to_date, _to_ord, bars, seed

CODE = "000333"
INDEX = "sh000300"


def _idx_series(returns, *, start: str = "2024-01-01", base: float = 3000.0):
    """造指数 K 线：`returns[i]` 是第 i 天到第 i+1 天的**简单**收益。

    刻意用「逐日收益」而不是「累计漂移」当参数：`tests/test_predict_service.bars()`
    的 `drift=0.004` 是**线性**价格 `base*(1+0.004*i)`，日收益随 i 递减到 ~0.002、
    σ 只有 1e-4 —— 拿它做方向断言会得到 `p_up = p_down = 0.0`（两侧都饱和）。
    脚手架里的量必须从实际数据反算，不能凭「看起来对」（ERROR_DIARY 2026-09-15）。
    """
    out = [Bar(code=INDEX, date=_to_date(_to_ord(start)), open=base, high=base,
               low=base, close=base, volume=1, amount=1.0, turnover=0.0,
               source="test", adj_mode="none")]
    c = base
    d0 = _to_ord(start)
    for i, r in enumerate(returns, start=1):
        c *= (1 + r)
        out.append(Bar(code=INDEX, date=_to_date(d0 + i), open=c, high=c, low=c,
                       close=c, volume=1, amount=1.0, turnover=0.0,
                       source="test", adj_mode="none"))
    return out


def _trend_bars(*, mu: float = 0.008, sigma: float = 0.004, n: int = 120,
                start: str = "2024-01-01", base: float = 10.0):
    """造一段**日对数收益均值 ≈ `mu`、有明显波动**的个股 K 线。

    不能用 `tests.test_predict_service.bars(drift=...)`：那是线性价格
    `base*(1+drift*i)`，日收益随 i 递减、σ 退化到 **1e-4** ——
    实测 `mu=0.0024 / sigma=0.0001`，`p_up` 与 `p_down` 两侧一起饱和成 0.0，
    任何方向断言都恒不成立（本次实测确认）。

    这里的噪声用**黄金角正弦**（确定性、无随机种子、相位近似均匀），
    实测该参数下 `mu≈0.008 / sigma_returns≈0.0052` →
    `base.direction ≈ {up 0.72, flat 0.28, down 0.01}`（argmax = up），
    `zero` 变体则回到 `{up≈0.17, flat≈0.66, down≈0.17}`（argmax = flat）。
    两个口径的结论**明确不同**，测试才有鉴别力。
    脚手架里的量一律从实际数据反算（ERROR_DIARY 2026-09-15）。
    """
    out = []
    d0 = _to_ord(start)
    for i in range(n):
        eps = sigma * math.sin(i * 2.399963)         # 黄金角 → 相位近似均匀
        c = base * math.exp(mu * i + eps)
        out.append(Bar(code=CODE, date=_to_date(d0 + i), open=c, high=c * 1.01,
                       low=c * 0.99, close=c, volume=1000, amount=c * 1000,
                       turnover=1.0, source="test", adj_mode="none"))
    return out


# ---------- 1. 单变量 ----------

def test_baseline_spec_changes_nothing():
    assert BASELINE_SPEC.changed_fields() == ()
    assert BASELINE_SPEC.is_baseline
    assert ForecastSpec().is_baseline


def test_every_registered_variant_changes_exactly_one_variable():
    """注册表里**每一个**变体都必须只改一个变量 —— 新增变体时自动受检。"""
    for name, v in VARIANTS.items():
        assert v.spec.changed_fields() == (v.changed_axis,), name
        assert v.model_tag.startswith("pit-rw-v1.0.1+"), name
        assert v.prereg_doc.endswith(f"{name}.md"), name
        assert len(v.hypothesis) > 20, name


def test_variant_that_declares_the_wrong_axis_is_refused():
    """声明改的是 A、实际改的是 B → 拒绝（否则台账会写错归因）。"""
    bad = Variant(name="liar", changed_axis="flat_band",
                  spec=ForecastSpec(mu_mode="zero"),
                  hypothesis="故意把声明写错，用来验证校验器真的会拒", prereg_doc="x.md")
    with pytest.raises(MultiVariableVariant, match="声明与实际不符"):
        assert_single_variable(bad)


def test_variant_that_changes_nothing_is_refused():
    """「改了 0 个变量」= 拿基线冒充变体 —— 必须拒绝，否则实验报告会是假的。"""
    noop = Variant(name="noop", changed_axis="mu_mode", spec=ForecastSpec(),
                   hypothesis="故意不改任何东西，用来验证校验器真的会拒", prereg_doc="x.md")
    with pytest.raises(MultiVariableVariant, match="改了 0 个字段"):
        assert_single_variable(noop)


def test_unknown_variant_is_refused_not_silently_fallen_back():
    with pytest.raises(UnknownVariant, match="未注册的变体"):
        get_variant("does-not-exist")


def test_unknown_mu_mode_is_refused_at_construction():
    with pytest.raises(ValueError, match="未知 mu_mode"):
        ForecastSpec(mu_mode="whatever")


def test_mu_modes_are_exactly_the_documented_three():
    assert MU_MODES == ("sample_mean", "zero", "index_sign")


# ---------- 2. 口径冻结：没有那些旋钮 ----------

def test_spec_has_no_knob_for_the_frozen_quantities():
    """标签带 / 窗口 / 标的集合 / 区间**不在 spec 里** → 结构上改不了。

    这比「记得别动它」强：没有字段，就没有「不小心传了个 flat_band=0.01」这种可能。
    """
    names = {f.name for f in dataclasses.fields(ForecastSpec)}
    for forbidden in ("flat_band", "window", "level_window", "codes", "universe",
                      "from_date", "to_date", "min_days", "train", "validate",
                      "test", "range_coverage", "touch_threshold"):
        assert forbidden not in names, (
            f"{forbidden} 出现在 ForecastSpec 里 —— 变体就能改口径了，"
            "而「口径冻结」必须靠「没有那个旋钮」而不是靠自觉")
    # 集合是**逐个点名**的：新增轴必须在这里显式加进去（= 一次被看见的决定），
    # 不能靠 `<=` 之类的宽松断言让新字段「顺手」混进来。
    # `sigma_mode` 是 P9-a 明示新增的第二条轴（条件化 sigma），
    # 取值 `SIGMA_MODES`；默认 `"const"` = 基线口径，见 docs/plans/2026-09-15-p9a-信息扩展.md。
    # `dist_mode` 是 P10-a 明示新增的第三条轴（预测分布的形状来源），
    # 取值 `DIST_MODES`；默认 `"gaussian"` = 基线口径，
    # 见 docs/experiments/2026-09-15-residual-quantile-interval.md。
    assert names == {"mu_mode", "sigma_mode", "dist_mode"}


def test_index_direction_uses_the_frozen_flat_band():
    """方向分类的阈值与打分口径**同源**（默认参数就是 `predict.model.FLAT_BAND`）。"""
    import inspect

    sig = inspect.signature(index_direction)
    assert sig.parameters["flat_band"].default == FLAT_BAND


# ---------- 3. PIT：指数方向拿不到未来 ----------

def test_index_direction_reads_only_bars_up_to_asof():
    """往序列尾部塞一根**暴涨的未来 bar**，`asof` 处的方向必须纹丝不动。"""
    hist = _idx_series([0.0, -0.02])
    asof = hist[-1].date
    before = index_direction(hist, asof)
    spike = [Bar(code=INDEX, date=_to_date(_to_ord(asof) + i), open=1e9, high=1e9,
                 low=1e9, close=1e9, volume=1, amount=1.0, turnover=0.0,
                 source="test", adj_mode="none") for i in (1, 2)]
    assert index_direction(hist + spike, asof) == before
    assert before == -1


def test_index_direction_follows_the_asof_day_not_the_target_day():
    """`asof` 涨、下一根跌 → 在 `asof` 处必须返回 +1、在 target 处返回 -1。

    这是「偷看未来」的**判别性**测试：若实现改成用 `target` 那天的指数，
    这里会返回 -1 而立刻变红。
    """
    hist = _idx_series([0.02, -0.05])
    asof, target = hist[1].date, hist[2].date
    assert index_direction(hist, asof) == 1
    assert index_direction(hist, target) == -1


def test_index_direction_missing_asof_bar_is_none_not_the_nearest_one():
    """`asof` 当天指数没有 K 线 → 缺口。返回 `None`，**不许**拿最近一根冒充。"""
    hist = _idx_series([0.01, 0.01])
    gap_day = _to_date(_to_ord(hist[-1].date) + 5)          # 中间隔了几天
    assert index_direction(hist, gap_day) is None


def test_index_direction_needs_two_bars():
    hist = _idx_series([])
    assert len(hist) == 1
    assert index_direction(hist, hist[0].date) is None


def test_index_direction_classifies_with_the_flat_band():
    assert index_direction(_idx_series([0.0]), "2024-01-02") == 0
    flat = _idx_series([FLAT_BAND * 0.5])                   # 半个标签带内 → flat
    assert index_direction(flat, flat[-1].date) == 0
    up = _idx_series([FLAT_BAND * 2])                       # 超过标签带 → up
    assert index_direction(up, up[-1].date) == 1
    down = _idx_series([-FLAT_BAND * 2])
    assert index_direction(down, down[-1].date) == -1


def test_load_index_direction_reads_the_index_from_db(tmp_path):
    hist = _idx_series([0.0, 0.02])
    conn = seed(tmp_path / "a.db", {CODE: bars(n=10), INDEX: hist})
    try:
        assert load_index_direction(conn, hist[-1].date) == 1
        assert load_index_direction(conn, hist[0].date) is None
    finally:
        conn.close()


# ---------- 4. spec 真的会改变载荷（否则实验是空的） ----------

def test_zero_drift_variant_changes_the_direction_probabilities():
    hist = _trend_bars()                          # 明确上行漂移 → 基线 mu > 0
    asof = hist[-1].date
    base = compute_forecast(code=CODE, asof=asof, bars=hist,
                            target_date=asof, strategy_mix={})
    variant = compute_forecast(code=CODE, asof=asof, bars=hist,
                               target_date=asof, strategy_mix={},
                               spec=ForecastSpec(mu_mode="zero"))
    assert base["direction"]["up"] > 10 * base["direction"]["down"]
    assert base["direction"]["up"] > base["direction"]["flat"]   # 基线 argmax = up
    # mu=0 → 两侧概率只差**标签带本身的不对称**（ln1.005 ≠ −ln0.995），故用容差
    assert abs(variant["direction"]["up"] - variant["direction"]["down"]) < 0.002
    assert variant["direction"]["flat"] > variant["direction"]["up"]   # argmax 变 flat
    assert variant["direction"] != base["direction"]
    # 区间中心随 mu 移动（σ 没动）：mu=0 时区间关于收盘价对称
    close = hist[-1].close
    lo, hi = variant["range_80"]
    assert lo + hi == pytest.approx(2 * close, abs=0.02)


def test_index_sign_variant_flips_the_drift_sign_only():
    """`index_sign` 只换**方向的来源**，漂移的**幅度**仍是 `|mu_sample|`。"""
    hist = _trend_bars()
    asof = hist[-1].date
    up = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                          strategy_mix={}, spec=ForecastSpec(mu_mode="index_sign"),
                          index_dir=1)
    down = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                            strategy_mix={}, spec=ForecastSpec(mu_mode="index_sign"),
                            index_dir=-1)
    assert up["direction"]["up"] > 10 * up["direction"]["down"]
    assert down["direction"]["down"] > 10 * down["direction"]["up"]
    # |mu| 相同 → 两侧镜像（差异只来自标签带不对称），实测 0.3905 vs 0.3879
    assert up["direction"]["up"] == pytest.approx(down["direction"]["down"], abs=0.005)
    # 幅度相同：区间**半宽**一致，只有中心随 mu 移动。
    # 容差 0.01 而不是 1e-6：`range_80` 的两个端点在载荷里就 `round(..., 2)`
    # （实测 0.18 vs 0.175 —— 差异全部来自两位小数的量化，不是 σ 变了）。
    def half_width(r):
        return (r[1] - r[0]) / 2
    assert half_width(up["range_80"]) == pytest.approx(half_width(down["range_80"]),
                                                       abs=0.01)
    assert up["range_80"][1] > down["range_80"][1]       # 中心随 mu 上移/下移


def test_index_sign_zero_direction_matches_the_zero_variant():
    """指数当日 flat（`index_dir=0`）→ 漂移项恰好为 0，与 `rw-mu0` 同口径。

    这条把「`index_dir` 是数据、不是配置」钉住：`0` 是一个**真实的**当日方向
    （指数几乎没动），它给出的预测必须与「把漂移置 0」完全一样。
    """
    hist = _trend_bars()
    asof = hist[-1].date
    a = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                         strategy_mix={}, spec=ForecastSpec(mu_mode="index_sign"),
                         index_dir=0)
    b = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                         strategy_mix={}, spec=ForecastSpec(mu_mode="zero"))
    assert a["direction"] == b["direction"]
    assert a["range_80"] == b["range_80"]


def test_index_sign_variant_refuses_when_the_index_is_missing():
    """指数缺口 → `DegenerateInput` 硬拒绝。**不许**静默退化成 mu=0 或基线。"""
    from stocklab.predict.model import DegenerateInput

    hist = _trend_bars()
    with pytest.raises(DegenerateInput, match="index_dir=None"):
        compute_forecast(code=CODE, asof=hist[-1].date, bars=hist,
                         target_date=hist[-1].date, strategy_mix={},
                         spec=ForecastSpec(mu_mode="index_sign"), index_dir=None)


def test_non_baseline_spec_records_the_variant_in_evidence_only():
    """变体口径把 `mu_sample` / `mu_mode` / `index_dir` 记进 evidence，
    而**基线路径不追加任何键**（回归红线的成因就在这里）。"""
    hist = _trend_bars()
    asof = hist[-1].date
    base = compute_forecast(code=CODE, asof=asof, bars=hist,
                            target_date=asof, strategy_mix={})
    var = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                           strategy_mix={}, spec=ForecastSpec(mu_mode="index_sign"),
                           index_dir=1)
    assert "mu_mode" not in base["evidence"]["inputs"]
    assert var["evidence"]["inputs"]["mu_mode"] == "index_sign"
    assert var["evidence"]["inputs"]["index_dir"] == 1
    assert "mu_sample" in var["evidence"]["inputs"]


def test_explicit_baseline_spec_is_byte_identical_to_the_default():
    """`spec=None` 与 `spec=BASELINE_SPEC` 必须完全一样 —— 默认路径零改动。"""
    hist = _trend_bars()
    asof = hist[-1].date
    a = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                         strategy_mix={})
    b = compute_forecast(code=CODE, asof=asof, bars=hist, target_date=asof,
                         strategy_mix={}, spec=BASELINE_SPEC)
    assert a == b
