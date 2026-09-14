"""市场状态判定（Task 20）：规则简单、无前视、标签取值受控。"""

import pandas as pd
import pytest

from stocklab.features import regime

LABELS = {"trend_up", "trend_down", "range", "unknown"}


def test_uptrend_labeled():
    close = pd.Series([float(i) for i in range(1, 121)])   # 单调上升
    labels = regime.classify(close)
    assert labels.iloc[-1] == "trend_up"


def test_downtrend_labeled():
    close = pd.Series([float(i) for i in range(120, 0, -1)])
    labels = regime.classify(close)
    assert labels.iloc[-1] == "trend_down"


def test_flat_market_is_range():
    close = pd.Series([10.0 + (0.01 if i % 2 else -0.01) for i in range(120)])
    assert regime.classify(close).iloc[-1] == "range"


def test_insufficient_history_is_unknown():
    close = pd.Series([10.0] * 10)
    assert regime.classify(close).iloc[-1] == "unknown"


def test_no_lookahead():
    close = pd.Series([float(i) for i in range(1, 121)])
    before = regime.classify(close).iloc[80]
    after = regime.classify(pd.concat([close, pd.Series([999.0] * 10)],
                                      ignore_index=True)).iloc[80]
    assert before == after


def test_labels_are_restricted_to_known_values():
    """标签是下游的 join key，取值必须封闭 —— 不允许出现新字符串。"""
    close = pd.Series([float(i % 7) + 10 for i in range(200)])
    assert set(regime.classify(close)) <= LABELS


def test_all_ready_rows_are_labeled_not_unknown():
    """窗口够了以后不得再有 unknown（unknown 只代表「历史不足」）。"""
    close = pd.Series([float(i) for i in range(1, 121)])
    labels = regime.classify(close)
    assert labels.iloc[58] == "unknown"        # sma60 要到第 60 行（index 59）才有值
    assert labels.iloc[59] == "trend_up"       # 该行起必须给出真实标签
    assert "unknown" not in set(labels.iloc[59:])


def test_flat_but_rising_ma_is_not_trend_up():
    """均线朝上但快慢线贴合（spread < 阈值）→ range，不是 trend_up。

    这是「趋势」与「震荡」的分界，写错会让所有横盘日被当成上涨趋势。
    """
    close = pd.Series([10.0 + i * 0.001 for i in range(120)])
    labels = regime.classify(close)
    assert labels.iloc[-1] == "range"


def test_params_must_be_consistent():
    with pytest.raises(ValueError):
        regime.classify(pd.Series([1.0] * 120), ma_short=60, ma_long=20)
