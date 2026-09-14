"""技术指标（Task 17）：纯函数集合。

铁律 R2（point-in-time）：任一 t 时刻的值**只能**依赖 t 及之前的数据。
本模块不使用 `shift(-n)`、`center=True`、`rolling(...).apply` 里的未来窗口，
也不用任何全样本统计量（如全序列均值/标准差）—— 那是最隐蔽的前视来源。

约定：输入是**按日期升序**的序列，输出与输入等长同索引；
窗口不足时返回 NaN（由调用方决定「缺失」如何表达，见 `registry.compute_core`）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(values: pd.Series, window: int) -> pd.Series:
    """简单移动平均。前 `window-1` 期为 NaN。"""
    return values.rolling(window=window, min_periods=window).mean()


def rolling_max(values: pd.Series, window: int) -> pd.Series:
    return values.rolling(window=window, min_periods=window).max()


def rolling_min(values: pd.Series, window: int) -> pd.Series:
    return values.rolling(window=window, min_periods=window).min()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """真实波幅 TR = max(H-L, |H-前收|, |L-前收|)。

    首行没有前收，两个含 NaN 的项被 `max(skipna=True)` 忽略，自然退化为 H-L
    （无需再手工赋值 —— 那会是一行永不生效的死代码）。
    序列中间若缺前收（数据缺口），同样退化为 H-L；缺失本身由 quality 层拦截，
    不在这里静默编造。
    """
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        window: int = 14) -> pd.Series:
    """平均真实波幅（简单平均版 ATR，非 Wilder 平滑）。"""
    return true_range(high, low, close).rolling(
        window=window, min_periods=window).mean()


def vol_ratio(volume: pd.Series, short: int = 5, long: int = 20) -> pd.Series:
    """量比 = 短期均量 / 长期均量。

    长期均量为 0（如长期停牌）时返回 NaN 而**不是** inf：
    inf 会一路污染 JSON 与下游比较，且不是可序列化的数值。
    """
    long_ma = sma(volume, long)
    return sma(volume, short) / long_ma.where(long_ma != 0)


def pct_change_n(close: pd.Series, n: int) -> pd.Series:
    """n 期涨跌幅（小数，非百分数）。"""
    return close / close.shift(n) - 1.0


def percentile_rank(values: pd.Series, window: int) -> pd.Series:
    """当前值在过去 `window` 期（含当期）中的分位，取值 (0, 1]。

    只回看历史；窗口不足返回 NaN。
    """
    return values.rolling(window=window, min_periods=window).apply(
        lambda w: float((w <= w[-1]).sum()) / len(w), raw=True)


def is_finite(value) -> bool:
    """浮点有限性判定（NaN / ±inf 都算「不可用」）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and bool(np.isfinite(value))
