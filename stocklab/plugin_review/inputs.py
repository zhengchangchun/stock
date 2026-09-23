"""插桩5 的输入装配（P58 §1.3/§1.4）：**全部只读、全部 `<= asof`**。

三样输入，各有一个既有的真源（一个都不重算）：

| 输入 | 真源 | 本模块做的事 |
|---|---|---|
| 历史快照 | `candidate_snapshots` / `candidate_members` | 挑出 `asof <= 目标日` 的候选 |
| 前视收益 | `data/adjust.py::load_bars_adjusted`（ADR-004 复权链） | 取 t0 / t0+N 的**复权**收盘价 |
| 回测台账 | `plugin_backtests`（已落库的回放读数） | 原样搬运，**不重跑回放** |
| 模块2 回流 | `m2/cases.py::miss_cases`（唯一口径） | 原样搬运 |

## 前视窗口：只算**已经走完**的窗口

`FORWARD_DAYS = 5`（常量，改它要改代码）。t1 = t0 之后第 5 个**交易日**
（交易日历 = `trading_calendar`，它由指数日线前滚，是这里唯一的交易日真源）。
t1 必须 `<= asof` —— 走不完的窗口整条**剔除并逐条记原因**，绝不「拿半个窗口
算个数」。这条是 PIT 的硬边界：半个窗口的收益里含未来。

## 复权口径走 `load_bars_adjusted`，不拿不复权价冒充

ADR-004/ADR-008：复权链算不出来的标的（ETF、链有缺口）**拒绝服务**。这里把它
转成「该样本剔除 + 原因留痕」，而不是退回 `bars_daily.close` —— 后者会在一段
跨除权的窗口上凭空多出一截假跌幅，且数值看着完全正常。

## 剔除是可见的

每一条剔除都带 `(code, pool, asof, reason)` 进 `ctx["sample_drops"]`，报告里
按原因聚合后如实印出来。**不许静默丢样本** —— 静默会同时造假分子与分母。
"""

from __future__ import annotations

import sqlite3

from stocklab.data import adjust
from stocklab.m2 import cases as m2_cases
from stocklab.plugin import store as plugin_store
from stocklab.session.tick import load_calendar

#: 前视窗口长度（个交易日）。**常量**：改它要改代码，不是配置项（P58 §1.4）。
FORWARD_DAYS: int = 5

#: 剔除原因里截断到多少字符（ADR-008 的拒绝文案很长，台账里不需要全文）。
REASON_CHARS: int = 160


def _snapshots(conn: sqlite3.Connection, asof: str) -> list[dict]:
    """`asof <= 目标日` 的历史快照（含池别与评分的那张表按 snapshot_id 取）。"""
    rows = conn.execute(
        "SELECT snapshot_id, asof, run_kind FROM candidate_snapshots"
        " WHERE asof <= ? ORDER BY asof, snapshot_id", (asof,)).fetchall()
    return [dict(r) for r in rows]


def _members(conn: sqlite3.Connection, snapshot_ids: list[int]) -> list[dict]:
    if not snapshot_ids:
        return []
    holders = ",".join("?" * len(snapshot_ids))
    rows = conn.execute(
        "SELECT snapshot_id, code, pool, adj_score, status FROM candidate_members"
        f" WHERE snapshot_id IN ({holders}) ORDER BY pool, code", snapshot_ids).fetchall()
    return [dict(r) for r in rows]


def _adjusted_closes(conn: sqlite3.Connection, code: str, t0: str, asof: str,
                     cache: dict) -> tuple[dict[str, float] | None, str | None]:
    """`code` 在 `[t0, asof]` 上的复权收盘序列，或 `(None, 拒绝原因)`。

    `start=t0` 是**显式缩窗口**：复权链的可用性取决于窗口起点（`adjust_bars`
    的判据是「`(t0, asof]` 里有没有不可定价事件」），所以按样本自己的 t0 取
    —— 同一个标的在不同 t0 上可用与否可以不同，这正是 ADR-004 要表达的东西。
    """
    key = (code, t0)
    if key not in cache:
        try:
            bars = adjust.load_bars_adjusted(conn, code, asof, start=t0)
            cache[key] = ({b.date: b.close for b in bars}, None)
        except adjust.AdjustError as exc:
            why = str(exc).splitlines()[0].strip()[:REASON_CHARS]
            cache[key] = (None, f"{type(exc).__name__}: {why}")
    return cache[key]


def build_ctx(conn: sqlite3.Connection, asof: str, *, script_id: int,
              script_version: str) -> dict:
    """装配插桩5 的 `ctx`（**只读**：本函数不写库）。"""
    cal, cal_error = load_calendar(conn)
    snaps = _snapshots(conn, asof)
    members = _members(conn, [s["snapshot_id"] for s in snaps])
    t0_of = {s["snapshot_id"]: s["asof"] for s in snaps}

    samples: list[dict] = []
    drops: list[dict] = []
    cache: dict = {}

    def drop(m: dict, t0: str, why: str) -> None:
        drops.append({"code": m["code"], "pool": m["pool"], "asof": t0,
                      "reason": why})

    for m in members:
        t0 = t0_of[m["snapshot_id"]]
        horizon = [d for d in cal.sessions(t0, asof) if d > t0]
        if len(horizon) < FORWARD_DAYS:
            drop(m, t0, f"窗口未走完：{t0} 之后到 {asof} 只有 {len(horizon)} 个交易日"
                        f"（需要 {FORWARD_DAYS} 个）")
            continue
        t1 = horizon[FORWARD_DAYS - 1]
        closes, why = _adjusted_closes(conn, m["code"], t0, asof, cache)
        if closes is None:
            drop(m, t0, f"复权链拒绝服务（{why}）")
            continue
        px0, px1 = closes.get(t0), closes.get(t1)
        if px0 is None or px1 is None:
            missing = t0 if px0 is None else t1
            drop(m, t0, f"{missing} 没有收盘价（停牌 / 当日无 K 线）")
            continue
        samples.append({
            "code": m["code"], "pool": m["pool"],
            "adj_score": float(m["adj_score"]),
            "asof": t0, "target": t1,
            "ret_pct": round((px1 / px0 - 1.0) * 100.0, 6),
        })

    return {
        "asof": asof,
        "plugin_id": "5",
        "script_id": script_id,
        "script_version": script_version,
        "n_days": FORWARD_DAYS,
        "samples": samples,
        "n_candidates": len(members),
        "sample_drops": drops,
        "calendar_error": cal_error,
        "backtests": _backtests(conn),
        "bad_cases": m2_cases.miss_cases(conn, asof),
    }


def _backtests(conn: sqlite3.Connection) -> list[dict]:
    """回测台账的已落库读数（**不重跑回放**）。"""
    return [{"pool": r["pool"], "verdict": r["verdict"],
             "overfit_flag": r["overfit_flag"],
             "window_start": r["window_start"], "window_end": r["window_end"],
             "metrics": r["metrics"]}
            for r in plugin_store.load_backtests(conn)]


def summarize(ctx: dict) -> dict:
    """`ctx` → **台账用的紧凑摘要**（台账存不下、也不该存整份样本序列）。"""
    snaps = sorted({s["asof"] for s in ctx["samples"]})
    by_reason: dict[str, int] = {}
    for d in ctx["sample_drops"]:
        by_reason[d["reason"]] = by_reason.get(d["reason"], 0) + 1
    bad = ctx["bad_cases"]
    return {
        "n_days": ctx["n_days"],
        "n_candidates": ctx["n_candidates"],
        "n_samples": len(ctx["samples"]),
        "n_dropped": len(ctx["sample_drops"]),
        "sample_asof_range": ([snaps[0], snaps[-1]] if snaps else None),
        "drop_reasons": [{"reason": k, "n": by_reason[k]} for k in sorted(by_reason)],
        "n_backtests": len(ctx["backtests"]),
        "bad_cases": {"n_cases": bad["n_cases"], "n_miss_total": bad["n_miss_total"],
                      "n_scored": bad["n_scored"], "limit": bad["limit"]},
        "calendar_error": ctx["calendar_error"],
    }
