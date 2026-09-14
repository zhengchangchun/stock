"""市场状态判定（Task 20）。

v1 只用最简单的规则：MA20/MA60 的相对位置 + 快线斜率。
不要一上来就上机器学习（总纲第 7 节）—— 先用可解释、可归因的规则，
让「预测错了」这件事能被复盘成「哪一天的 regime 判错了」。

无前视（R2）：每个 t 的标签只依赖 t 及之前的收盘价。
"""

from __future__ import annotations

import pandas as pd

from stocklab.features.indicators import sma

SLOPE_LOOKBACK = 5
FLAT_THRESHOLD = 0.005      # |MA20 相对 MA60 偏离| < 0.5% 视为区间

LABELS: frozenset[str] = frozenset({"trend_up", "trend_down", "range", "unknown"})


def classify(close: pd.Series, *, ma_short: int = 20, ma_long: int = 60,
             vol_window: int = 20) -> pd.Series:
    """逐行给出市场状态标签。

    规则（全部只用历史）：
    - `trend_up`：MA_short 高于 MA_long 超过阈值 **且** 快线向上
    - `trend_down`：MA_short 低于 MA_long 超过阈值 **且** 快线向下
    - `range`：两者贴合，或方向与该侧不一致（均线走平时不给趋势标签）
    - `unknown`：历史不足（长均线还没形成）

    `vol_window` 预留（v1 不用波动率分支），保留参数是为了以后加「高波动区间」
    时不必改调用方签名。
    """
    if ma_short >= ma_long:
        raise ValueError(
            f"ma_short({ma_short}) 必须小于 ma_long({ma_long})；"
            "写反会静默产出完全相反的标签"
        )
    _ = vol_window
    fast = sma(close, ma_short)
    slow = sma(close, ma_long)
    spread = (fast - slow) / slow
    slope = fast / fast.shift(SLOPE_LOOKBACK) - 1.0

    labels = pd.Series("unknown", index=close.index, dtype=object)
    ready = spread.notna() & slope.notna()
    up = ready & (spread > FLAT_THRESHOLD) & (slope > 0)
    down = ready & (spread < -FLAT_THRESHOLD) & (slope < 0)
    flat = ready & ~up & ~down
    labels[up] = "trend_up"
    labels[down] = "trend_down"
    labels[flat] = "range"
    return labels
