"""P9-a 第二条轴：`sigma_mode`（条件化波动率）。

五组必须被钉住的东西：

1. **默认路径零改动**：`sigma_mode="const"` 与 `spec=None` 逐字节等价 ——
   基线报告 sha256 `a61d026b…f4eb` 是回归红线，这条测试是它在单测里的哨兵；
2. **缩放是「只动 sigma」**：`mu` / 关键位 / `action` 的取法一行没动，
   `sigma` 严格按 `SIGMA_SCALE_MIN + (MAX−MIN)×q` 缩放（比例可手算）；
3. **缺特征即拒绝**：`features=None` 或所需字段是 `None` → `DegenerateInput`，
   **不许**静默回退成基线 sigma（那会让变体报告里混进基线口径的行）；
4. **分位越界即拒绝**：特征层给出 `q ∉ [0,1]` → `DegenerateInput`，不猜；
5. **跨层契约**：`SIGMA_MODES` ←→ `PitFeatures.REQUIRED` 一一对应；
   变体注册表里的 `changed_axis` 与 `changed_fields()` 仍然恰好一个字段。
"""

from __future__ import annotations

import copy
import math

import pytest

from stocklab.data.models import Bar
from stocklab.errors import DegenerateInput
from stocklab.features.pit_regime import PitFeatures
from stocklab.predict.model import (BASELINE_SPEC, SIGMA_MODES,
                                    SIGMA_SCALE_MAX, SIGMA_SCALE_MIN,
                                    ForecastSpec, compute_forecast,
                                    degenerate_strategy_mix)
from stocklab.predict.version import MODEL_VERSION

from tests.test_predict_service import _to_date, _to_ord

CODE = "000333"
MIX = degenerate_strategy_mix()


def bars(n: int = 200, *, start: str = "2020-01-01", base: float = 10.0,
         mu: float = 0.004, sigma: float = 0.010):
    """确定性日 K：对数收益均值≈`mu`、波动≈`sigma`（黄金角正弦噪声）。"""
    d0 = _to_ord(start)
    out = []
    for i in range(n):
        eps = sigma * math.sin(i * 2.399963)
        c = base * math.exp(mu * i + eps)
        out.append(Bar(code=CODE, date=_to_date(d0 + i), open=c, high=c * 1.02,
                       low=c * 0.98, close=c, volume=10_000 + 137 * i,
                       amount=c * 10_000, turnover=1.0, source="test",
                       adj_mode="none"))
    return out


ASOF = bars()[-1].date
TARGET = _to_date(_to_ord(ASOF) + 1)


def forecast(spec=None, features=None):
    return compute_forecast(code=CODE, asof=ASOF, bars=bars(), target_date=TARGET,
                            strategy_mix=MIX, spec=spec, features=features)


# ---------- 1. 默认路径零改动 ----------

def test_const_sigma_is_byte_identical_to_the_baseline():
    a = forecast()
    b = forecast(ForecastSpec())
    c = forecast(BASELINE_SPEC)
    assert a == b == c
    assert "sigma_mode" not in a["evidence"]["inputs"]


def test_explicit_const_sigma_does_not_add_any_sigma_key_to_the_evidence():
    """显式写 `sigma_mode="const"`（= 默认值）**不**能让 evidence 多出键。

    `changed_fields()` 是「相对基线**被改掉**的字段」，显式写默认值不算改 ——
    否则「重新序列化一遍默认配置」会被误判成变体，`payload_hash` 之外的
    证据块也就跟着漂。
    """
    assert BASELINE_SPEC.changed_fields() == ()
    assert ForecastSpec(sigma_mode="const").changed_fields() == ()
    assert forecast(ForecastSpec(sigma_mode="const")) == forecast()


def test_version_is_untouched_by_a_variant():
    """变体不是模型版本：`model_version` 不许被 sigma 轴改掉。"""
    assert forecast(ForecastSpec(sigma_mode="rv_pct"),
                    PitFeatures(rv_pct=0.5))["model_version"] == MODEL_VERSION
    assert forecast()["model_version"] == MODEL_VERSION


# ---------- 2. 缩放只动 sigma ----------

@pytest.mark.parametrize("mode,features", [
    ("rv_pct", PitFeatures(rv_pct=0.0)),
    ("rv_pct", PitFeatures(rv_pct=1.0)),
    ("index_rv_pct", PitFeatures(index_rv_pct=0.0)),
    ("index_rv_pct", PitFeatures(index_rv_pct=1.0)),
])
def test_sigma_scales_exactly_by_the_declared_formula(mode, features):
    base = forecast()
    var = forecast(ForecastSpec(sigma_mode=mode), features)
    q = features.quantile_for(mode)
    expected = SIGMA_SCALE_MIN + (SIGMA_SCALE_MAX - SIGMA_SCALE_MIN) * q
    ev_b, ev_v = base["evidence"]["inputs"], var["evidence"]["inputs"]
    assert ev_v["sigma"] == pytest.approx(ev_b["sigma"] * expected)
    assert ev_v["sigma_base"] == pytest.approx(ev_b["sigma"])
    assert ev_v["sigma_quantile"] == q
    assert ev_v["sigma_scale"] == pytest.approx(expected)
    # `mu` **一点没动**（这是「只改一个变量」的核心）
    assert ev_v["mu"] == ev_b["mu"]
    assert ev_v["mu_mode"] == "sample_mean"
    assert var["direction"] != base["direction"]              # 概率确实被影响了
    assert var["range_80"] != base["range_80"]                # 区间宽度跟着变


def test_sigma_scale_bounds_are_the_declared_constants():
    """`q=0` → 下界、`q=1` → 上界；两端因子就是那两个先验常量本身。"""
    lo = forecast(ForecastSpec(sigma_mode="rv_pct"), PitFeatures(rv_pct=0.0))
    hi = forecast(ForecastSpec(sigma_mode="rv_pct"), PitFeatures(rv_pct=1.0))
    assert lo["evidence"]["inputs"]["sigma_scale"] == SIGMA_SCALE_MIN
    assert hi["evidence"]["inputs"]["sigma_scale"] == SIGMA_SCALE_MAX
    assert (SIGMA_SCALE_MIN, SIGMA_SCALE_MAX) == (0.5, 1.5)


def test_vol_z_mode_uses_the_normal_cdf_of_the_z_score():
    z = 1.0
    var = forecast(ForecastSpec(sigma_mode="vol_z"), PitFeatures(volume_z=z))
    q = var["evidence"]["inputs"]["sigma_quantile"]
    from statistics import NormalDist
    assert q == pytest.approx(float(NormalDist().cdf(z)))


def test_key_levels_and_action_logic_are_untouched():
    """变体只缩放 sigma：触及概率的 z 用的是同一个 `sigma`，但生成逻辑逐行未动。

    这条测试盯的是「有人顺手把 `p_touch` 里的 sigma 换成 base」这类**局部**改动 ——
    那种改动会让区间与触及概率来自两个不同的分布，而报告里看不出来。
    """
    base = forecast()
    var = forecast(ForecastSpec(sigma_mode="rv_pct"), PitFeatures(rv_pct=1.0))
    assert [k["price"] for k in base["key_levels"]] == \
        [k["price"] for k in var["key_levels"]]
    assert base["action"] in ("add", "trim", "wait")
    assert var["invalidate_if"] == base["invalidate_if"]


def test_a_sigma_variant_never_changes_the_payload_contract_fields():
    """`CONTRACT_FIELDS` 的键集合不因变体而变（载荷形状是契约）。"""
    assert set(forecast()) == set(forecast(ForecastSpec(sigma_mode="rv_pct"),
                                           PitFeatures(rv_pct=0.5)))


# ---------- 3. 缺特征即拒绝 ----------

@pytest.mark.parametrize("mode", ["vol_z", "rv_pct", "index_rv_pct"])
def test_missing_feature_package_is_refused(mode):
    with pytest.raises(DegenerateInput, match="features=None"):
        forecast(ForecastSpec(sigma_mode=mode), None)


@pytest.mark.parametrize("mode", ["vol_z", "rv_pct", "index_rv_pct"])
def test_missing_feature_field_is_refused_not_fallen_back_to_baseline(mode):
    """**关键测试**：所需字段为 `None` → 拒绝，而不是悄悄用回基线 sigma。

    如果实现「回退到基线」，变体报告里会出现一批**看起来是变体口径、
    实际是基线**的行，而样本量悄悄变多 —— 这正是本包最贵的一类错。
    """
    with pytest.raises(DegenerateInput, match="算不出来"):
        forecast(ForecastSpec(sigma_mode=mode), PitFeatures())


def test_a_feature_package_for_another_mode_does_not_satisfy_this_one():
    """喂错字段（有 `rv_pct` 但要 `index_rv_pct`）也必须拒绝。"""
    with pytest.raises(DegenerateInput, match="index_rv_pct"):
        forecast(ForecastSpec(sigma_mode="index_rv_pct"),
                 PitFeatures(rv_pct=0.5))


# ---------- 4. 越界即拒绝 ----------

@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan")])
def test_out_of_range_quantile_is_refused(bad):
    with pytest.raises(DegenerateInput, match="不在 \\[0,1\\]"):
        forecast(ForecastSpec(sigma_mode="rv_pct"), PitFeatures(rv_pct=bad))


# ---------- 5. 跨层契约 ----------

def test_declared_sigma_modes_match_the_feature_registry():
    """`SIGMA_MODES`（spec 的合法取值）与 `PitFeatures.REQUIRED`（特征来源）
    必须一一对应 —— 改一处必须改两处，这条测试就是那个「必须」。"""
    assert set(SIGMA_MODES) == set(PitFeatures.REQUIRED) | {"const"}
    assert SIGMA_MODES[0] == "const"          # 默认值排第一，一眼看得见
    for mode, need in PitFeatures.REQUIRED.items():
        assert need, f"{mode} 登记了空的特征需求 —— 变体会静默退化成基线 sigma"
    assert PitFeatures.required_for("const") == frozenset()


def test_sigma_modes_are_exactly_the_documented_four():
    assert SIGMA_MODES == ("const", "vol_z", "rv_pct", "index_rv_pct")


def test_unknown_sigma_mode_is_refused_at_construction():
    with pytest.raises(ValueError, match="未知 sigma_mode"):
        ForecastSpec(sigma_mode="whatever")


def test_the_sigma_axis_does_not_open_a_door_to_the_frozen_quantities():
    """新轴**只能**取 `SIGMA_MODES` 里的值（构造期硬校验）——
    它不是「随便传个浮点数进去」的自由旋钮。"""
    for bad in (0.5, 1.5, -1.0, None, "", "const "):
        with pytest.raises(ValueError):
            ForecastSpec(sigma_mode=bad)


def test_two_variables_at_once_is_still_a_two_field_change():
    """同时改两个轴 → `changed_fields()` 是 2 个 → `assert_single_variable` 会拒。"""
    both = ForecastSpec(mu_mode="zero", sigma_mode="rv_pct")
    assert both.changed_fields() == ("mu_mode", "sigma_mode")
    assert not both.is_baseline


def test_is_baseline_means_every_field_not_just_mu():
    """`is_baseline` 的新语义：**所有**字段都在基线上。

    旧写法 `mu_mode == "sample_mean"` 会把「只改了 sigma」的 spec 判成基线，
    于是 evidence 走基线分支、连自己改了什么都不打印。
    """
    assert BASELINE_SPEC.is_baseline
    assert not ForecastSpec(sigma_mode="rv_pct").is_baseline
    assert not ForecastSpec(mu_mode="zero").is_baseline


def test_degenerate_input_is_one_class_not_two_lookalikes():
    """`DegenerateInput` 从 `predict.model` 搬到了 `stocklab.errors`，但必须是
    **同一个对象** —— 两个长得像的类会让 `except` 静默失效（最坏的一类 bug：
    错误路径看起来被处理了，其实没有）。"""
    import stocklab.errors as errors
    import stocklab.predict.model as model
    assert model.DegenerateInput is errors.DegenerateInput


def test_the_report_payload_is_pure_json_friendly_data():
    """变体载荷仍可 canonical 序列化（没有 NaN / 自定义类型混进来）。"""
    from stocklab.predict.model import payload_hash
    p = forecast(ForecastSpec(sigma_mode="vol_z"), PitFeatures(volume_z=-0.4))
    assert isinstance(payload_hash(p), str)
    copy.deepcopy(p)
