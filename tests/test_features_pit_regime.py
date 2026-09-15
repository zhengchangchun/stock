"""P9-a 特征层：量能 z / 已实现波动率分位（**PIT，先测后用**）。

四组必须被钉住的东西：

1. **拿不到未来**：把一堆「未来」bar（含极端值）塞进序列尾部，`asof` 处的特征值
   必须纹丝不动 —— 并且有一条**正向对照**（故意写一个不裁剪的实现）证明这些断言
   不是「恰好都通过」；
2. **不许碰 `amount` / `turnover`**：这两列在库里全为 NULL。一条 AST 扫描
   （结构上不可能）+ 一条行为测试（改了它们，特征值逐位不变）；
3. **缺数据硬拒绝**：`volume` 为 NULL / 非正 → `DegenerateInput`，**不填 0**；
   「历史不足」→ `None`（= 算不出，由调用方拒绝该行）；
4. **与既有 pandas 实现对齐**：`rv_percentile` 的窗口语义必须与
   `indicators.percentile_rank` 逐位相等（两处独立写，必须能被独立核对）。
"""

from __future__ import annotations

import ast
import math
import statistics
from pathlib import Path

import pandas as pd
import pytest

from stocklab.data.models import Bar
from stocklab.errors import DegenerateInput
from stocklab.features import indicators
from stocklab.features.pit_regime import (MIN_BARS, RV_LOOKBACK, RV_WINDOW,
                                          VOLUME_WINDOW, PitFeatures,
                                          UnknownSigmaMode, rv_percentile,
                                          rv_series, usable_until, volume_z)

from tests.test_predict_service import _to_date, _to_ord

CODE = "000333"
MODULE_PATH = Path(__file__).resolve().parents[1] / "stocklab" / "features" / "pit_regime.py"


def bars(n: int, *, start: str = "2020-01-01", base: float = 10.0,
         drift: float = 0.001, vol_base: int = 10_000, vol_step: int = 300):
    """造 `n` 根**确定性**日 K：价格几何漂移 + 正弦噪声，成交量线性递增。

    噪声用黄金角正弦（确定性、无随机种子），与 `test_experiments_variants._trend_bars`
    同一套路 —— 脚手架里的量必须能从实际数据反算，不能凭「看起来对」。
    """
    out = []
    d0 = _to_ord(start)
    for i in range(n):
        eps = 0.004 * math.sin(i * 2.399963)
        c = base * math.exp(drift * i + eps)
        out.append(Bar(code=CODE, date=_to_date(d0 + i), open=c, high=c * 1.01,
                       low=c * 0.99, close=c, volume=vol_base + vol_step * i,
                       amount=c * (vol_base + vol_step * i), turnover=1.0,
                       source="test", adj_mode="none"))
    return out


def future_spike(after: str, n: int = 60) -> list[Bar]:
    """`after` 之后的 `n` 根**极端**未来 bar：价格暴涨、成交量放大 5 个数量级。

    成交量刻意**逐根变化**（`10**9 + i`）而不是常数：常数会让对数成交量的标准差
    为 0，于是「不裁剪」的对照实现直接除零 —— 那样测的是崩溃，不是前视。
    """
    d0 = _to_ord(after)
    return [Bar(code=CODE, date=_to_date(d0 + i), open=1e6, high=1e6, low=1e-6,
                close=1e6, volume=10 ** 9 + i, amount=1e12, turnover=999.0,
                source="test", adj_mode="none") for i in range(1, n + 1)]


# ---------- 1. PIT：T 日特征只能由 T 及之前的数据决定 ----------

def test_volume_z_reads_only_bars_up_to_asof():
    past = bars(120)
    asof = past[-1].date
    before = volume_z(past, asof)
    assert before is not None
    assert volume_z(past + future_spike(asof), asof) == before


def test_rv_percentile_reads_only_bars_up_to_asof():
    past = bars(MIN_BARS + 200)
    asof = past[-1].date
    before = rv_percentile(past, asof)
    assert before is not None
    assert rv_percentile(past + future_spike(asof), asof) == before


def test_future_data_does_not_change_the_rv_series():
    past = bars(MIN_BARS + 200)
    asof = past[-1].date
    assert rv_series(past + future_spike(asof), asof) == rv_series(past, asof)


def test_the_pit_assertion_has_teeth():
    """**正向对照**：故意写一个不裁剪的实现，值必须真的变 —— 否则上面三条是空断言。

    这是 `tests/test_features_pit.py::test_pit_assertion_has_teeth` 的同款手法：
    「测试全绿」和「测试有鉴别力」是两件事，后者要单独证明。
    """
    past = bars(MIN_BARS + 200)
    asof = past[-1].date
    leaky = past + future_spike(asof)

    # 不裁剪 = 把未来行情当历史用。PIT 版本与它必须**不同**。
    def leaky_volume_z(series) -> float:
        xs = [math.log(b.volume) for b in series][-VOLUME_WINDOW:]
        return (xs[-1] - statistics.fmean(xs)) / statistics.stdev(xs)

    def leaky_rv_percentile(series) -> float:
        closes = [b.close for b in series]                    # MUTATION: 不裁 asof
        rets = [math.log(c / p) for p, c in zip(closes, closes[1:])]
        rv = [statistics.stdev(rets[i - RV_WINDOW + 1:i + 1])
              for i in range(RV_WINDOW - 1, len(rets))]
        tail = rv[-RV_LOOKBACK:]
        return sum(1 for v in tail if v <= tail[-1]) / len(tail)

    # 注意：这一条与 `test_rv_percentile_reads_only_bars_up_to_asof` 测的是**两件事**。
    # 那条测的是「喂进来的未来数据不影响输出」（裁剪在函数内部，所以它对输入免疫）；
    # 这条测的是「裁剪本身被拿掉时值会变」—— 对**结构性** PIT 来说，真正的照妖镜
    # 只能是「实现里没有裁剪」的对照，光喂未来数据是喂不出区别的。
    assert leaky_volume_z(leaky) != volume_z(leaky, asof)
    assert leaky_rv_percentile(leaky) != rv_percentile(past, asof)
    assert leaky_rv_percentile(past) == rv_percentile(past, asof)


def test_usable_until_keeps_order_and_drops_only_the_future():
    series = bars(10)
    cut = series[4].date
    assert usable_until(series, cut) == series[:5]


# ---------- 2. `amount` / `turnover` 结构上不可用 ----------

def test_the_module_never_touches_amount_or_turnover():
    """**AST 扫描**：源码里不出现 `.amount` / `.turnover` 属性访问。

    比「记得别用」强：这两列在 `bars_daily` 里全为 NULL（14164/14164），
    任何读到它们的特征值都是编出来的。扫 AST 而不是扫字符串 ——
    注释与 docstring 里出现这两个词是允许的（也不该让文档被迫绕开它们）。
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    touched = {n.attr for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and n.attr in ("amount", "turnover")}
    assert touched == set(), (
        f"pit_regime 里出现了 {sorted(touched)} 的属性访问 —— 这两列全为 NULL，"
        "用到它们的特征值是假的")


def test_changing_amount_and_turnover_changes_nothing():
    """行为证据：把两列改成 `None` / 天文数字，特征值**逐位不变**。"""
    series = bars(MIN_BARS + 200)
    asof = series[-1].date
    mutated = [Bar(**{**b.__dict__, "amount": None, "turnover": None})
               for b in series]
    huge = [Bar(**{**b.__dict__, "amount": 1e18, "turnover": 888.0})
            for b in series]
    assert volume_z(mutated, asof) == volume_z(series, asof)
    assert rv_percentile(huge, asof) == rv_percentile(series, asof)


# ---------- 3. 缺数据硬拒绝 ----------

def test_null_volume_is_refused_not_filled_with_zero():
    series = bars(120)
    asof = series[-1].date
    broken = series[:-1] + [Bar(**{**series[-1].__dict__, "volume": None})]
    with pytest.raises(DegenerateInput, match="volume 为 NULL"):
        volume_z(broken, asof)


@pytest.mark.parametrize("bad", [0, -1, float("nan")])
def test_non_positive_or_non_finite_volume_is_refused(bad):
    series = bars(120)
    asof = series[-1].date
    broken = series[:-1] + [Bar(**{**series[-1].__dict__, "volume": bad})]
    with pytest.raises(DegenerateInput, match="非正/非有限"):
        volume_z(broken, asof)


def test_non_positive_close_is_refused():
    series = bars(MIN_BARS + 10)
    asof = series[-1].date
    broken = series[:-1] + [Bar(**{**series[-1].__dict__, "close": 0.0})]
    with pytest.raises(DegenerateInput, match="close=0.0"):
        rv_percentile(broken, asof)


def test_insufficient_history_is_none_not_zero():
    """历史不足 → `None`（算不出），**不是** 0 / 0.5 这类「看起来正常」的值。"""
    assert volume_z(bars(VOLUME_WINDOW - 1), bars(VOLUME_WINDOW - 1)[-1].date) is None
    series = bars(MIN_BARS - 1)
    # 差一根：分位算不出，但 RV 序列本身**非空**（`RV_LOOKBACK - 1` 期）。
    # 所以这里要断言的是长度，不是 `== []` —— 「序列为空」与「窗口不够」是两件事，
    # 混在一起会让这条断言断言一个与实现无关的形状。
    assert len(rv_series(series, series[-1].date)) == RV_LOOKBACK - 1
    assert rv_percentile(series, series[-1].date) is None
    assert rv_series(bars(RV_WINDOW), bars(RV_WINDOW)[-1].date) == []


# ---------- 4. 数值语义 ----------

def test_volume_z_is_standardised_over_the_window():
    """递增量能 → z > 0；递减量能 → z < 0；且与手算一致。"""
    up = bars(120)
    asof = up[-1].date
    xs = [math.log(b.volume) for b in up[-VOLUME_WINDOW:]]
    assert volume_z(up, asof) == pytest.approx(
        (xs[-1] - statistics.fmean(xs)) / statistics.stdev(xs))
    assert volume_z(up, asof) > 0

    down = [Bar(**{**b.__dict__, "volume": 100_000 - 300 * i})
            for i, b in enumerate(bars(120))]
    assert volume_z(down, down[-1].date) < 0


def test_constant_volume_is_none_not_a_division_by_zero():
    flat = [Bar(**{**b.__dict__, "volume": 12_345}) for b in bars(120)]
    assert volume_z(flat, flat[-1].date) is None


def test_rv_percentile_matches_the_pandas_implementation():
    """两处独立实现（纯 Python vs `indicators.percentile_rank`）必须逐位相等。

    与 `experiments.metrics.daily_stats` 对 `verify.report._daily` 的处理同一套路：
    刻意各写一遍，再用一条测试把它们钉在一起 —— 否则「两处独立核对」是假的。
    """
    series = bars(MIN_BARS + 200)
    asof = series[-1].date
    rv = pd.Series(rv_series(series, asof))
    expected = indicators.percentile_rank(rv, RV_LOOKBACK).iloc[-1]
    assert rv_percentile(series, asof) == pytest.approx(float(expected))


def test_rv_percentile_is_in_the_unit_interval_and_hits_the_top_for_a_vol_spike():
    """`asof` 当天波动率骤升 → 分位 = 1.0（含当期的分位定义）。"""
    series = bars(MIN_BARS + 200)
    asof = series[-1].date
    p = rv_percentile(series, asof)
    assert 0.0 < p <= 1.0
    # 把最后 20 根换成一天暴涨一天暴跌 → 当期 RV 必然高于该窗内所有历史 RV
    spike = series[:-20] + [
        Bar(**{**b.__dict__, "close": b.close * (1.5 if i % 2 else 0.67)})
        for i, b in enumerate(series[-20:])]
    assert rv_percentile(spike, spike[-1].date) == 1.0


def test_indicators_and_pit_agree_on_short_history_too():
    """两处实现对「历史不足」的处理也必须一致（都是「算不出」）。"""
    series = bars(MIN_BARS - 1)
    asof = series[-1].date
    rv = pd.Series(rv_series(series, asof))
    assert rv.empty or math.isnan(
        indicators.percentile_rank(rv, RV_LOOKBACK).iloc[-1])
    assert rv_percentile(series, asof) is None


# ---------- 5. 特征包：取值查询与分位映射 ----------
# （`SIGMA_MODES` ←→ `PitFeatures.REQUIRED` 的**跨层**一致性断言在
#  `tests/test_predict_model_sigma.py`：那里才同时 import 两侧。）

def test_unknown_sigma_mode_is_refused_at_requirement_lookup():
    with pytest.raises(UnknownSigmaMode, match="未登记的 sigma_mode"):
        PitFeatures.required_for("whatever")


def test_quantile_mapping():
    assert PitFeatures().quantile_for("const") is None
    assert PitFeatures(volume_z=0.0).quantile_for("vol_z") == pytest.approx(0.5)
    assert PitFeatures(volume_z=1.96).quantile_for("vol_z") == pytest.approx(0.975, abs=1e-3)
    assert PitFeatures(rv_pct=0.31).quantile_for("rv_pct") == 0.31
    assert PitFeatures(index_rv_pct=0.77).quantile_for("index_rv_pct") == 0.77
    # 缺字段 → None（由 model 拒绝），**不是** 0 / 0.5
    assert PitFeatures().quantile_for("rv_pct") is None
    assert PitFeatures().quantile_for("index_rv_pct") is None
    with pytest.raises(UnknownSigmaMode, match="拒绝猜一个分位"):
        PitFeatures().quantile_for("whatever")


def test_rv_window_constants_are_the_documented_ones():
    """窗口是**变量定义**的一部分，不是可调参数 —— 改动必须是一次被看见的决定。"""
    assert (VOLUME_WINDOW, RV_WINDOW, RV_LOOKBACK) == (20, 20, 250)
    assert MIN_BARS == RV_LOOKBACK + RV_WINDOW
