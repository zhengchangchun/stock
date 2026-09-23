"""A3 / B1 预测的事后校验：目标日 → 复用 `verify/` 的打分器 → 落 `m2_forecast_scores`。

## 打分器**一份都不新写**（P48 §2 表格第 1/2 行的硬判据）

方向命中（涨/平/跌 + `FLAT_BAND`）、区间命中、`invalidate_if` 失效判定、
不可评分的四种原因码 —— 全部走 `verify/score.py::score_prediction` 与
`verify/service.py::load_scoring_bars`。本模块只做三件它不做的事：

1. **推出目标日**（`m2_forecasts` 契约里没有 `target_date`，见下）；
2. 把 `m2_forecasts` 的行**摊成打分器要的预测形状**；
3. 从打分结果里**取出**属于模块2 读数的那几列（而不是把 `verifications` 的
   全套列搬过来 —— `size_pct` / `key_levels` 不在 m2 契约里）。

### 目标日怎么来（为什么不是日历）

`plugin/contract.py::_M2_FORECAST_SHAPE` 只有 `range_80` / `direction` /
`invalidate_if` / `na_reasons` / `schema_version` —— **没有 `target_date`**，
而 `m2_forecasts` 的列也不许加（P48 §5 反目标：只加新表）。预测的语义是
「下一个交易日的持仓收益」，所以目标日 = 决策日之后**第一个市场交易日**，
取自 `predict/service.py::market_axis`（与 `predict` 管线**同一条**轴，
不另造一个交易日历：两套日历必然在某个节假日上分歧）。

窗口里还没有下一个交易日（例如决策日就是库里最后一天）⇒ 这一条**不落行**：
它是「还没到能判的时候」，不是「判出来不可评分」。两条必须分得开 ——
把「尚未到期」写成一行的 `scorable=0`，会让「今天有没有预测到期」永远数不清。

### 打分器要的两个字段是**填出来的**，而且不消费它们派生的列

`score_prediction` 的入参里，`key_levels` 与 `size_pct` 是它算
区间外的分项（`hit_levels` / `score_level` / `total_score` / `sim_pnl` /
`benchmark_pct`）用的；m2 的预测契约里这两样**不存在**。
本层填 `[]` / `0.0`，并**只取**由 `direction` + `range_80` + `invalidate_if`
决定的那些列（`hit_direction` / `range_hit` / `invalidated` / `actual_*`），
由填出来的字段派生的六列**一列都不落库** —— 否则报告里那列 `sim_pnl=0.00`
会被读成「这笔没赚没亏」，而事实是「没有这个数」。
"""

from __future__ import annotations

import bisect
import sqlite3
from collections.abc import Sequence

from stocklab.m2 import config
from stocklab.m2 import store as m2_store
from stocklab.predict.service import market_axis
from stocklab.verify.score import score_prediction
from stocklab.verify.service import load_scoring_bars


def next_session(sessions: Sequence[str], after: str) -> str | None:
    """`after` 之后的下一个交易日（`sessions` 必须是**已排序**的交易日集合）。

    严格大于：决策日自己**不是**它的目标日 —— `>=` 会让「今天预测今天」看起来
    成立（那是一次无滞后的自我验证，测出来的是零）。
    """
    i = bisect.bisect_right(sessions, after)
    return sessions[i] if i < len(sessions) else None


def _bet_pct(pred_class: str, actual_pct: float) -> float:
    """按预测方向下注一单位的收益：预测涨 `+actual` / 预测跌 `−actual` / 预测平 `0`。

    它是「盈亏比」与「最大回撤」的**序列元素**（P48 §2 第 3/4 行点名复用既有实现，
    不许另写算法）。平的那一条给 0：`backtest/metrics` 的盈亏比口径里
    **零收益日两边都不计入**，所以「预测平」既不冒充赢、也不冒充输。
    方向本身不是新算法 —— 取反不改变任何比率的定义，只把「这条预测是对的还是错的」
    变成一条收益序列。
    """
    if pred_class == "up":
        return actual_pct
    if pred_class == "down":
        return -actual_pct
    return 0.0


def _dev_pct(close_t: float, close_asof: float, lo: float, hi: float) -> float:
    """实际收盘**超出**预测区间的那一段收益（落在区间内 = 0）。

    用 `clamp` 而不是「到区间中点的距离」：中点是一个**本层编出来的位置**，
    而「出了区间多少」是区间自己定义的事 —— 预测说 80% 落在 [lo, hi]，
    实际出了界，「出了多少」才是这份预测的偏差。分母取决策日收盘，
    使不同价位的标的可比（价格单位的偏差在跨标的汇总时没有意义）。
    """
    outside = close_t - min(max(close_t, lo), hi)
    return outside / close_asof if close_asof else 0.0


def _forecast_pred(row: dict, target: str) -> dict:
    """`m2_forecasts` 的行 → `score_prediction` 要的预测形状（只换名字，不造数）。"""
    return {
        "pred_id": int(row["forecast_id"]),
        "asof_date": str(row["asof_date"]),
        "target_date": target,
        "code": str(row["code"]),
        "direction": row["direction"],
        "range_80": row["range_80"],
        "invalidate_if": row["invalidate_if"],
        # 见模块 docstring：契约里没有这两样，填的是「空」，且不消费它们派生的列
        "key_levels": [],
        "size_pct": 0.0,
    }


def _uninformative(row: dict, target: str) -> dict:
    """插桩明说「不知道」（`direction=None`）⇒ 不可评分，结果列全空。"""
    return {
        "forecast_id": int(row["forecast_id"]), "plugin_id": str(row["plugin_id"]),
        "account_id": str(row["account_id"]),
        "script_version": str(row["script_version"]),
        "asof_date": str(row["asof_date"]), "target_date": target,
        "code": str(row["code"]), "scorable": 0,
        "reason_code": config.REASON_NO_DIRECTION,
        "actual_close": None, "actual_pct": None,
        "range_lo": row["range_lo"], "range_hi": row["range_hi"],
        "range_hit": None, "dev_pct": None, "hit_direction": None,
        "pred_class": None, "actual_class": None, "bet_pct": None,
        "invalidated": None,
    }


def score_forecast(conn: sqlite3.Connection, row: dict, *, target: str,
                   cache=None, costs=None) -> dict:
    """一条预测 → 一行分数载荷。**打分判据全部来自 `verify/`**（见模块 docstring）。

    `direction=None` 或 `range_80=None` 的预测不喂给打分器：它会 `KeyError` /
    拿 `None` 当数算。这种预测按 `REASON_NO_DIRECTION` 记一行空结果 ——
    它是「这条预测本身没话说」，与「行情没到」是两件事（原因码必须分开）。
    """
    if row["direction"] is None or row["range_80"] is None:
        return _uninformative(row, target)
    adjusted, raw, suspended, err = load_scoring_bars(
        conn, str(row["code"]), target, cache=cache)
    verdict = score_prediction(
        _forecast_pred(row, target), bars=adjusted, raw_bars=raw,
        suspended=suspended, costs=costs, adjust_error=err)

    scorable = bool(verdict["scorable"])
    lo = None if row["range_lo"] is None else float(row["range_lo"])
    hi = None if row["range_hi"] is None else float(row["range_hi"])
    notes = verdict["notes"] if scorable else {}
    close_asof = notes.get("close_asof")
    dev = None
    if scorable and lo is not None and hi is not None and close_asof:
        dev = _dev_pct(float(verdict["actual_close"]), float(close_asof), lo, hi)
    return {
        "forecast_id": int(row["forecast_id"]), "plugin_id": str(row["plugin_id"]),
        "account_id": str(row["account_id"]),
        "script_version": str(row["script_version"]),
        "asof_date": str(row["asof_date"]), "target_date": target,
        "code": str(row["code"]), "scorable": 1 if scorable else 0,
        "reason_code": verdict["reason_code"],
        "actual_close": verdict["actual_close"], "actual_pct": verdict["actual_pct"],
        "range_lo": lo, "range_hi": hi,
        "range_hit": (int(verdict["hit_range"]) if scorable else None),
        "dev_pct": dev,
        "hit_direction": (int(verdict["hit_direction"]) if scorable else None),
        "pred_class": notes.get("pred_class"),
        "actual_class": notes.get("actual_class"),
        "bet_pct": ((_bet_pct(str(notes["pred_class"]), float(verdict["actual_pct"])))
                    if scorable else None),
        "invalidated": (verdict["invalidated"] if scorable else None),
    }


def score_all(conn: sqlite3.Connection, *, asof: str, now: str, cache=None,
              costs=None) -> dict:
    """把 `target_date <= asof` 且**还没打过分**的预测全部打分并落库。

    幂等单元 = `forecast_id`：`find_score` 命中就跳过（**一个字节都不写**，
    连一行台账都不追加）。与通路的幂等同一个理由 —— 「记一笔『这次没做事』」
    会让同一天跑两遍的库与只跑一遍的库不一样。

    返回的计数里 `pending` 是「目标日还没到 / 目标日不在库里」的那些：
    **故意不落行**，等实际行情到位后下一次再打（见模块 docstring）。
    """
    sessions = sorted(market_axis(conn))
    counts = {"scanned": 0, "scored": 0, "unscorable": 0, "already": 0,
              "pending": 0}
    for row in m2_store.list_forecasts(conn):
        counts["scanned"] += 1
        if m2_store.find_score(conn, int(row["forecast_id"])) is not None:
            counts["already"] += 1
            continue
        target = next_session(sessions, str(row["asof_date"]))
        if target is None or target > asof:
            counts["pending"] += 1
            continue
        score = score_forecast(conn, row, target=target, cache=cache, costs=costs)
        m2_store.insert_score(conn, score=score, now=now, commit=False)
        counts["scored" if score["scorable"] else "unscorable"] += 1
    conn.commit()
    return {"asof": asof, "n_sessions": len(sessions), **counts}
