"""特征定义、参数与版本（Task 18）。

`feature_version` 变更规则（schema A1）：**任何会改变数值的改动都必须升版本号**。
`features_daily` 是 append-only 且 `UNIQUE(code, date, feature_version, feature_set)`，
同键重写会被写入口直接拒绝 —— 这是刻意的：同一个 snapshot_id 必须永远对应同一组
数值（`predictions.feature_snapshot_id` 会长期引用它）。
"""

from __future__ import annotations

import math

import pandas as pd

from stocklab.features import indicators as ind

#: v2（2026-09-15 / ADR-004）：特征改为在**复权价**上计算。
#: v1 的快照在**不复权价**上算 `ret_1d/ma/atr`，除权日含假跌幅（000333 的
#: `10派20元转15股` 那天不复权跌 ~63%），已失真且不得被覆盖 ——
#: `features_daily` 是 append-only + `UNIQUE(code,date,version,set)`，
#: 升版本号即可让新旧快照并存、各自可追溯。
FEATURE_VERSION = "v2"
FEATURE_SET = "core"

PARAMS: dict = {
    "ma_short": 20,
    "ma_long": 60,
    "atr_window": 14,
    "vol_short": 5,
    "vol_long": 20,
    "pe_pct_window": 750,
}

CORE_COLUMNS: tuple[str, ...] = (
    "close", "ma20", "ma60", "atr14", "vol_ratio_5_20",
    "ret_1d", "ret_5d", "main_net_5d", "pe_pct_3y", "regime_label",
)

# 默认参数下的最少历史根数（ma_long=60 起算 + 1 根余量）。
# 注意：真正生效的是 `required_history(params)`，改窗口参数时它会跟着变。
MIN_HISTORY = 61


def effective_params(params: dict | None = None) -> dict:
    """把覆盖参数合并进默认值。

    **未知参数名直接报错**：写错名字若静默忽略，会让一整轮单变量实验得出
    「改了参数但结果没变」的假结论 —— 这比崩溃危险得多。
    """
    if not params:
        return dict(PARAMS)
    unknown = sorted(set(params) - set(PARAMS))
    if unknown:
        raise ValueError(
            f"未知特征参数：{unknown}；合法参数：{sorted(PARAMS)}。"
            "要新增参数请先登记到 registry.PARAMS"
        )
    return {**PARAMS, **params}


def required_history(params: dict | None = None) -> int:
    """当前参数下算出一份完整快照所需的最少根数。"""
    p = effective_params(params)
    longest = max(p["ma_long"], p["ma_short"], p["atr_window"], p["vol_long"], 6)
    return longest + 1


def _clean(value):
    """NaN / ±inf → None。

    SQLite 存不了 NaN（会变 NULL），JSON 也不允许 NaN 字面量；
    若原样带出去，DB 列与 json_payload 会对同一事实给出两种表示。
    统一在源头归一为 None（= 不可计算），再由 `canonical_json` 兜底硬失败。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    f = float(value)
    return f if math.isfinite(f) else None


def compute_core(bars, *, params: dict | None = None) -> dict:
    """由日线序列计算核心特征。

    `bars` 必须是**按日期升序、且最后一行是 asof 日**的序列（R2）。
    调用方负责裁剪（见 `snapshot.usable_bars`）—— 本函数不做任何日期过滤，
    也绝不使用 `bars` 之外的任何数据。
    """
    p = effective_params(params)
    df = pd.DataFrame([
        {"date": b.date, "open": b.open, "high": b.high, "low": b.low,
         "close": b.close, "volume": float(b.volume), "amount": b.amount}
        for b in bars
    ])
    close = df["close"]
    raw = {
        "close": float(close.iloc[-1]),
        "ma20": float(ind.sma(close, p["ma_short"]).iloc[-1]),
        "ma60": float(ind.sma(close, p["ma_long"]).iloc[-1]),
        "atr14": float(ind.atr(df["high"], df["low"], close,
                               p["atr_window"]).iloc[-1]),
        "vol_ratio_5_20": float(ind.vol_ratio(df["volume"], p["vol_short"],
                                             p["vol_long"]).iloc[-1]),
        "ret_1d": float(ind.pct_change_n(close, 1).iloc[-1]),
        "ret_5d": float(ind.pct_change_n(close, 5).iloc[-1]),
        "main_net_5d": None,     # 资金流模块填充（P3 之后）
        "pe_pct_3y": None,       # 估值模块填充（P3 之后）
        "regime_label": None,    # 市场状态（无指数数据源，见 docs/tasks P3 记录）
    }
    return {k: _clean(v) for k, v in raw.items()}
