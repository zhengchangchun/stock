"""技术指标（Task 17）：纯函数 + 手算一致性 + 无前视。

数值断言全部按定义**手算**后写死（ERROR_DIARY 2026-09-14「计划里的断言量级写错」）。
"""

import numpy as np
import pandas as pd
import pytest

from stocklab.features import indicators as ind


def test_sma_matches_hand_calculation():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ind.sma(s, 3)
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)   # (1+2+3)/3
    assert out.iloc[4] == pytest.approx(4.0)   # (3+4+5)/3


def test_true_range_first_row_uses_high_low():
    high = pd.Series([10.0, 11.0])
    low = pd.Series([9.0, 9.5])
    close = pd.Series([9.5, 10.5])
    tr = ind.true_range(high, low, close)
    assert tr.iloc[0] == pytest.approx(1.0)              # 首行无前收
    assert tr.iloc[1] == pytest.approx(max(11.0 - 9.5,   # H-L
                                           abs(11.0 - 9.5),
                                           abs(9.5 - 9.5)))


def test_atr_simple_average_of_tr():
    high = pd.Series([10.0] * 5)
    low = pd.Series([9.0] * 5)
    close = pd.Series([9.5] * 5)
    out = ind.atr(high, low, close, window=3)
    assert out.iloc[3] == pytest.approx(1.0)


def test_atr_nan_before_window():
    high = pd.Series([10.0] * 3)
    low = pd.Series([9.0] * 3)
    close = pd.Series([9.5] * 3)
    out = ind.atr(high, low, close, window=14)
    assert out.isna().all()


def test_vol_ratio():
    vol = pd.Series([100.0] * 20 + [200.0])
    out = ind.vol_ratio(vol, short=5, long=20)
    # 近 5 日均量 = (100*4 + 200)/5 = 120；20 日均量 = (100*19+200)/20 = 105
    assert out.iloc[20] == pytest.approx(120.0 / 105.0)


def test_vol_ratio_zero_long_average_is_nan_not_inf():
    """长期均量为 0 时不得返回 inf（会污染下游与 JSON）。"""
    vol = pd.Series([0.0] * 25)
    assert ind.vol_ratio(vol, short=5, long=20).isna().all()


def test_pct_change_n():
    close = pd.Series([10.0, 11.0, 12.0])
    out = ind.pct_change_n(close, 1)
    assert out.iloc[1] == pytest.approx(0.1)
    assert out.iloc[2] == pytest.approx(12.0 / 11.0 - 1)


def test_percentile_rank():
    """窗口不足 → NaN；窗口内的最大值 = 100 分位、最小值 = 20 分位。

    计划原文断言 `out.iloc[0] == 0.2`（5 元素序列、窗口 5）：不成立 —— iloc[0]
    只有 1 个观测，`min_periods=window` 下必为 NaN；若要让 iloc[0] 得到 0.2，
    只能是**全样本**分位，即前视（R2 违规）。故改断言而非改实现。
    """
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ind.percentile_rank(s, 5)
    assert np.isnan(out.iloc[3])                 # 窗口不足
    assert out.iloc[4] == pytest.approx(1.0)     # 最大值 → 100 分位
    down = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])
    assert ind.percentile_rank(down, 5).iloc[4] == pytest.approx(0.2)  # 最小值 → 20 分位


def test_rolling_max_min():
    s = pd.Series([3.0, 1.0, 4.0, 1.0, 5.0])
    assert ind.rolling_max(s, 3).iloc[2] == pytest.approx(4.0)
    assert ind.rolling_min(s, 3).iloc[2] == pytest.approx(1.0)


def test_no_lookahead_sma():
    """SMA 在 t 时刻的值只依赖 t 及之前 —— 追加未来值不改变历史。"""
    s = pd.Series([1.0, 2.0, 3.0])
    before = ind.sma(s, 2).iloc[2]
    after = ind.sma(pd.Series([1.0, 2.0, 3.0, 100.0, 100.0]), 2).iloc[2]
    assert before == pytest.approx(after)
