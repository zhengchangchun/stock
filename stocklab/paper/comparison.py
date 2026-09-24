"""对照臂同轴表（P52 / D-36）：5 条对照臂 ＋ 归因用的随机臂。

## 五条臂各是什么，以及它们**不是**什么

| # | 臂 | 数据来源 | 有成本吗 |
|---|---|---|---|
| ① | `arm-agent` | `paper_nav_daily`（AI 操盘手） | 有 |
| ② | `arm-now` | `paper_nav_daily`（实盘账本镜像，ADR-006） | 有 |
| ③ | 真实主动权益基金等权平均 | `fund_nav_daily`（**净值曲线**） | 无（**不可比**的部分如实标） |
| ④ | `arm-hold` | `paper_nav_daily`（冻结快照） | 无成交 ⇒ 无成本 |
| ⑤ | `sh000300` | `bars_daily` 指数收盘 | **无**（指数不可直接交易） |
| ＋ | `arm-agent-random` | `paper_nav_daily`（同护栏同成本的随机臂） | 有 |

## 三条不许破的规矩

1. **缺随机臂不得出结论**：`delta_vs_random` 无数据时是 `null`，并写明
   「**不存在，不是 0**」—— 写 `+0.00%` 就是把「还没接线」读成一个结论。
2. **不可比的行写「不可比」，不填 0**：基金臂没有持仓与成本、指数不可交易。
   把「没有数据」画成 0，等于把「不知道」显示成「没涨没跌」。
3. **样本 < 120 交易日只给读数、不给结论**（`CLAUDE.md` 度量纪律第 3 条，
   措辞与 P41 的 `insufficient` 同源）。

本模块**只做并列**：不算「谁赢」，不排序，不挑「推荐」。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Mapping

from stocklab.fund import nav as fund_nav
from stocklab.paper import store
from stocklab.paper.config import (
    ARM_AGENT,
    ARM_AGENT_RANDOM,
    ARM_HOLD,
    ARM_KIND_AGENT,
    ARM_KIND_AGENT_RANDOM,
    ARM_KIND_DISCIPLINE,
    ARM_KIND_HOLD,
    ARM_KIND_NOW,
    ARM_NOW,
    FUND_EQUAL_WEIGHT_ID,
    FUND_EQUAL_WEIGHT_LABEL,
    FUND_NAV_APPROX_NOTE,
    FUND_NAV_SOURCE_URL,
    HALTED_LABEL,
    LIVE_KEY,
    NOT_COMPARABLE,
    PAPER_START_DATE,
    SAMPLE_THRESHOLD,
)
from stocklab.paper.engine import INDEX_300_SYMBOL, arm_target_label

#: 门禁没过时的**唯一**措辞（与 P41 的 `PERFORMANCE_INSUFFICIENT` 同源）。
INSUFFICIENT_NOTE = "样本不足，仅供观察；不得据此选策略或改口径"

INDEX_LABEL = "沪深300 指数"
_INDEX_NOTE = "指数不可直接交易，故不含成本 —— 与各臂对照时口径偏乐观"


def _nav_series(conn: sqlite3.Connection, asof: str) -> dict[str, dict[str, float]]:
    """各账户 `date <= asof` 的**累计收益**序列（`cum_return`，扣成本后的口径）。"""
    out: dict[str, dict[str, float]] = {}
    for r in conn.execute(
            "SELECT account_id, date, cum_return, nav FROM paper_nav_daily"
            " WHERE date <= ? ORDER BY date", (asof,)):
        out.setdefault(str(r["account_id"]), {})[str(r["date"])] = float(r["cum_return"])
    return out


def _index_series(conn: sqlite3.Connection, start: str, dates: list[str]) -> dict:
    """指数自起跑日的累计收益（**只按精确日期取**，不做「取最近前一个交易日」）。"""
    marks = ",".join("?" * len(dates)) if dates else "''"
    sql = ("SELECT date, close FROM bars_daily WHERE code = ? AND adj_mode = 'none'"
           f" AND date IN ({marks})")
    levels = {str(r["date"]): float(r["close"])
              for r in conn.execute(sql, (INDEX_300_SYMBOL, *dates))}
    base = levels.get(start)
    if base is None:
        return {"points": [None for _ in dates], "base_level": None,
                "comparable": False,
                "note": f"起跑日 {start} 没有 {INDEX_300_SYMBOL} 的收盘价 → 基期缺失，"
                        f"整条线不可比（**不拿别的日期顶替**）"}
    return {"points": [None if d not in levels else round(levels[d] / base - 1.0, 6)
                       for d in dates],
            "base_level": base, "comparable": True, "note": _INDEX_NOTE}


def _account_rows(conn: sqlite3.Connection, asof: str) -> list[dict]:
    rows = []
    for account in store.load_accounts(conn):
        params = json.loads(account["params_json"] or "{}")
        rows.append({
            "account_id": str(account["account_id"]),
            "arm_kind": str(account["arm"]),
            "etf_target_pct": (None if account["etf_target_pct"] is None
                               else float(account["etf_target_pct"])),
            "params_json": str(account["params_json"] or "{}"),
            # P69 / T2：停飞位（缺省在飞）。**逐字读库里的键**，不在这里猜。
            "live": params.get(LIVE_KEY) is not False,
            "executor": params.get("executor"),
        })
    return rows


#: 「今日无决策」与「有决策但不动手」在页面上**必须不同形** —— 前者是这一格
#: 没有决定，后者是决定了但没成交。合成一句话会把「没决定」读成「决定不动手」。
def _decision_state(conn: sqlite3.Connection, account: Mapping, asof: str) -> dict | None:
    """AI 操盘手（含随机对照与版本账户）在 `asof` 的决策状态；别的臂 → `None`。

    数字全部来自台账与账户行（`agent_decide.portfolio_decision_on`），本函数不重算。

    P69 / T2：**停飞臂（`params.live=false`）不是「今日无决策」** —— 它是「不再接受
    考核」。两者在页面上必须不同形，否则「这条臂已经停了」会被读成「它今天没决定」，
    而后者会让人以为明天还会有。
    """
    kind = str(account["arm_kind"])
    if kind not in (ARM_KIND_AGENT, ARM_KIND_AGENT_RANDOM):
        return None
    from stocklab.paper import agent_decide, engine    # 懒 import：避免模块成环
    aid = str(account["account_id"])
    live = engine.live_of(json.loads(account["params_json"] or "{}"))
    if not live:
        return {
            "present": False, "asof": asof, "decision_id": None, "model_id": None,
            "n_codes": 0, "cash_pct": None, "halted": True,
            "note": (f"**{HALTED_LABEL}**：`{aid}` 已停飞"
                     f"（`params.{LIVE_KEY}=false`）⇒ 日终不由 `paper agent run` "
                     f"认领，**不判它缺决策**。历史台账与净值行一行未动"
                     f"（D-48：旧账户保留不删）"),
        }
    present = agent_decide.portfolio_decision_on(conn, aid, asof)
    if present is not None:
        payload = present.get("payload") or {}
        n_codes = len(payload.get("decisions") or [])
        return {
            "present": True, "asof": asof, "halted": False,
            "decision_id": int(present["decision_id"]),
            "model_id": str(present["model_id"]),
            "n_codes": n_codes, "cash_pct": float(payload.get("cash_pct") or 0.0),
            "note": (f"**今日有决策**：台账第 {int(present['decision_id'])} 条"
                     f"（`{present['model_id']}`），{n_codes} 个标的、"
                     f"现金 {float(payload.get('cash_pct') or 0.0):g}%。"
                     f"**有没有动手看成交列** —— 「有决策但不动手」与"
                     f"「今日无决策」不是一件事"),
        }
    return {
        "present": False, "asof": asof, "halted": False,
        "decision_id": None, "model_id": None,
        "n_codes": 0, "cash_pct": None,
        "note": (f"**今日无决策**：`{aid}` 在 {asof} 的 `paper_agent_decisions` 里"
                 f"没有这一行 ⇒ 那天**没决定**（不是「决定不动手」）。"
                 f"本页不给它编一个默认决策"),
    }


def build(conn: sqlite3.Connection, asof: str) -> dict:
    """`asof` 的对照表（同轴、同口径、同成本说明；不含生成时刻 ⇒ 可重放）。"""
    accounts = _account_rows(conn, asof)
    start = PAPER_START_DATE
    session_dates = [str(r["date"]) for r in conn.execute(
        "SELECT DISTINCT date FROM paper_nav_daily WHERE date <= ? ORDER BY date",
        (asof,))]
    if session_dates:
        start = str(conn.execute(
            "SELECT MIN(start_date) AS s FROM paper_accounts").fetchone()["s"] or start)
    dates = sorted({start, *session_dates}) if session_dates else [start]
    series = _nav_series(conn, asof)

    arms: list[dict] = []
    for a in accounts:
        aid = a["account_id"]
        pts = [series.get(aid, {}).get(d) for d in dates]
        latest = next((v for v in reversed(pts) if v is not None), None)
        # 停飞臂：口径那句**不再按 `arm_kind` 拼**（`arm-agent` 与 `arm-agent-ds-v2`
        # 的 kind 都是 `agent`，拼出来是同一句 —— 正是 P56 §8.4 的错标签）。
        label = (f"`{aid}` · {HALTED_LABEL}" if not a["live"]
                 else f"`{aid}` · {arm_target_label(a['arm_kind'], a['etf_target_pct'])}")
        arms.append({
            "id": aid, "label": label,
            "kind": "account", "tradable": True, "comparable": True,
            "has_cost": True, "is_index": False, "approximate": False,
            "points": pts, "latest": latest,
            "live": a["live"], "executor": a["executor"],
            "note": None,
            "decision": _decision_state(conn, a, asof),
            "n_sessions": sum(1 for v in pts if v is not None),
        })

    fund = fund_nav.equal_weight_curve(conn, dates=dates, start=start)
    arms.append({
        "id": FUND_EQUAL_WEIGHT_ID, "label": FUND_EQUAL_WEIGHT_LABEL,
        "kind": "fund_basket", "tradable": False,
        "comparable": bool(fund["comparable"]),
        "has_cost": False, "is_index": False, "approximate": True,
        "points": fund["points"], "latest": fund["latest"],
        "note": FUND_NAV_APPROX_NOTE,
        "source_url": FUND_NAV_SOURCE_URL,
        "n_funds_used": fund["n_funds_used"], "codes": fund["codes"],
        "codes_missing": fund["codes_missing"],
        "n_sessions": sum(1 for v in fund["points"] if v is not None),
    })

    idx = _index_series(conn, start, dates)
    arms.append({
        "id": INDEX_300_SYMBOL, "label": INDEX_LABEL,
        "kind": "index", "tradable": False, "comparable": bool(idx["comparable"]),
        "has_cost": False, "is_index": True, "approximate": False,
        "points": idx["points"],
        "latest": next((v for v in reversed(idx["points"]) if v is not None), None),
        "note": idx["note"],
        "n_sessions": sum(1 for v in idx["points"] if v is not None),
    })

    by_id = {a["id"]: a for a in arms}
    agent = by_id.get(ARM_AGENT)
    random_arm = by_id.get(ARM_AGENT_RANDOM)
    n_decisions = int(conn.execute(
        "SELECT COUNT(*) FROM paper_agent_decisions WHERE arm = ? AND asof <= ?",
        (ARM_AGENT, asof)).fetchone()[0]) if _has_ledger(conn) else 0
    n_random = int(conn.execute(
        "SELECT COUNT(*) FROM paper_agent_decisions WHERE arm = ? AND asof <= ?",
        (ARM_AGENT_RANDOM, asof)).fetchone()[0]) if _has_ledger(conn) else 0

    delta, available = None, n_random > 0
    if available and agent and random_arm:
        if agent["latest"] is not None and random_arm["latest"] is not None:
            delta = round(agent["latest"] - random_arm["latest"], 6)
        else:
            available = False
    gate_ok = len(session_dates) >= SAMPLE_THRESHOLD
    return {
        "asof": asof, "start_date": start, "dates": dates,
        "n_sessions": len(session_dates),
        "sample_gate": {
            "threshold": SAMPLE_THRESHOLD, "n_sessions": len(session_dates),
            "status": "ok" if gate_ok else "insufficient",
            "note": None if gate_ok else INSUFFICIENT_NOTE,
        },
        "arms": arms,
        "n_decisions_agent": n_decisions,
        "n_decisions_random": n_random,
        "delta_vs_random": delta,
        "delta_vs_random_available": available,
        "delta_vs_random_note": None if available else (
            f"`{ARM_AGENT_RANDOM}` 还没有任何一条决策 ⇒ 差分**不存在**，不是 0。"
            f"缺它的时候，「AI 选对了」与「同预算下多试了几次」分不开（D-19）"),
        "random_arm_note": None if n_random else (
            f"`{ARM_AGENT_RANDOM}` 未出决策时，AI 臂的读数一律**不可归因**"),
        "not_comparable_note": (
            f"基金等权臂与指数**不扣成本、不可直接交易**，标「{NOT_COMPARABLE}」"
            f"而不是 0；基金持仓不公开 ⇒ 只能比净值曲线"),
        "disclaimer_extra": FUND_NAV_APPROX_NOTE,
    }


def _has_ledger(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name='paper_agent_decisions'").fetchone()[0] > 0
