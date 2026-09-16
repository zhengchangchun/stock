"""「已到期却未评分」的判定（P27）—— 为什么需要它、以及为什么它必须是这个形状。

## 缺口是怎么来的（实测，不是推测）

2026-09-16 只读查库：

| 事实 | 值 |
|------|----|
| `trading_calendar` 的 `2026-09-16` 行落库时刻 | `2026-09-16T15:32:56+08:00` |
| 当天最后一次 `session_tick` | `2026-09-16T15:23:35+08:00` |
| 那天 tick 的 `verify_inserted` | `0` |

`session_tick` 步骤③ **有**验证逻辑，但它的 cutoff 只来自日历
（`closed_through(cal, now)`）。而日历行由 `ingest index` 在 **15:30+** 才写 ——
**比当天最后一次 tick 晚 9 分钟**。于是那次 tick 看到的 cutoff 只能是昨天，
「今天到期」的预测**永远轮不到它**。15:30 的收盘链又只跑 `session backfill-close`
（只回填 amount/turnover），不跑 `session tick`。

结论：缺口不是「模型没产出」，而是**判据的证据源单一且天生滞后一天**。

## 修法：把证据源扩到 `trading_calendar ∪ bars_daily`

日 K 只在收盘后才有，所以「`bars_daily` 里有这一天的行」本身就是**收盘已完成**的
正面证据，且它的到达时刻与预测要打分的时刻是同一个环节（`ingest bars` 之后）。
日历侧保留（它覆盖指数日线、也是既有实现），两侧取**最大**。

## fail-closed 的三个落点

1. **未来日不算已收盘**：两侧都过 `session.tick.is_trade_date_closed`
   （`d < today` 或「`d == today` 且已过收盘时刻」），所以库里混进未来日期的 bar
   也不会被当成「已到期」；
2. **日历为空不报错**：复用 `load_calendar` 的既有降级语义（返回空日历 + 原因），
   判据自动落到 bars 一侧；
3. **两侧都判不出来 → `None`**：`pending_predictions` 此时返回 `n = None` + `reason`，
   **绝不返回 0** —— 0 会被读成「没有缺口」，而真相是「判不了」
   （ERROR_DIARY #36：每一个 0 旁边必须写出为什么是 0）。

## 「待验证」的选取条件必须与 `verify_target` 逐字一致

`verify_target` 取 `WHERE target_date=? AND status='ok'`。如果本模块的清单把
`status != 'ok'` 的行也算进来，就会出现「清单里有、打分器永远不碰」的行 →
**永久假报警**。所以 `status='ok'` 在这里是**不变量**，不是碰巧。
（实测库里 6059 行全是 `ok`，所以它今天是空操作 —— 正因为是空操作才更要钉住。）
"""

from __future__ import annotations

import sqlite3

from stocklab.session.tick import (closed_through, is_trade_date_closed,
                                   load_calendar)

#: 判据原文（进报告/JSON，供审计者逐字复核）。
CLOSED_RULE = (
    "「最新已收盘交易日」= max(日历侧, 行情侧)："
    "① 日历侧 = `session.tick.closed_through(trading_calendar, now)`；"
    "② 行情侧 = `MAX(bars_daily.date)`，且该日必须满足 "
    "`session.tick.is_trade_date_closed(该日, now)`（日 K 只在收盘后入库，"
    "故「有 bar」本身就是收盘已完成的正面证据）。"
    "两侧都取不到 → None（判不了，**不是**「没有缺口」）。"
)

#: 判不出来时的原因码（进 JSON）。
REASON_UNDETERMINED = "cannot_determine_latest_closed_session"

#: 补缺口的可操作提示（`review daily` / `verify pending` 共用一份，避免两处走样）。
HINT = ("跑 `stocklab verify pending`（幂等；只补「已到期且没有验证行」的预测，"
        "不改不删任何已有行）")


def latest_closed_session(conn: sqlite3.Connection,
                          now: str) -> tuple[str | None, dict]:
    """`(最新已收盘交易日, 证据)`；判不出来时 `(None, 证据)`。

    `now` 是 ISO8601 时刻字符串（与 `session.tick` 的 `now` 同格式），
    **本函数不读时钟** —— 同一个 `now` 一定得到同一个结果。
    """
    cal, cal_error = load_calendar(conn)
    calendar_side = closed_through(cal, now)

    bars_max = conn.execute("SELECT MAX(date) AS d FROM bars_daily").fetchone()["d"]
    # 行情侧只认「已收盘」的那一天：未来日期 / 当天但未到收盘时刻一律不算
    bars_side = (bars_max if bars_max and is_trade_date_closed(bars_max, now)
                 else None)

    candidates = [d for d in (calendar_side, bars_side) if d]
    latest = max(candidates) if candidates else None
    return latest, {
        "rule": CLOSED_RULE,
        "now": now,
        "calendar_side": calendar_side,
        "bars_side": bars_side,
        "bars_max_date": bars_max,
        "calendar_error": cal_error,
        "calendar_range": ({"first": min(cal.all_dates), "last": max(cal.all_dates)}
                           if cal.all_dates else None),
    }


def pending_predictions(conn: sqlite3.Connection, now: str) -> dict:
    """列出「已到期且没有验证行」的预测（**只读**，一行都不写）。

    `n` 为 `None` 表示**判不了**（不是「没有缺口」）；`rows` 恒为列表。
    """
    latest, evidence = latest_closed_session(conn, now)
    if latest is None:
        return {
            "latest_closed_session": None,
            "evidence": evidence,
            "n": None,
            "rows": [],
            "by_target_date": {},
            "reason": REASON_UNDETERMINED,
            "hint": None,
            "note": ("日历与行情两侧都判不出「已收盘交易日」—— 这是**判不了**，"
                     "不是「没有缺口」，所以不给 0（ERROR_DIARY #36）"),
        }

    # 选取条件与 `verify_target` 逐字一致（含 `status='ok'`）：清单里不许有
    # 「打分器永远不碰」的行，否则每天都报同一个假警。
    rows = [dict(r) for r in conn.execute(
        "SELECT p.pred_id, p.code, p.asof_date, p.target_date, p.model_version,"
        " p.created_at FROM predictions p"
        " LEFT JOIN verifications v ON v.pred_id = p.pred_id"
        " WHERE v.verification_id IS NULL AND p.status = 'ok'"
        "   AND p.target_date <= ?"
        " ORDER BY p.target_date, p.code", (latest,))]

    by_target: dict[str, int] = {}
    for r in rows:
        by_target[r["target_date"]] = by_target.get(r["target_date"], 0) + 1

    return {
        "latest_closed_session": latest,
        "evidence": evidence,
        "n": len(rows),
        "rows": rows,
        "by_target_date": by_target,
        "reason": None,
        "hint": HINT if rows else None,
        "note": ("只统计 `target_date <= 最新已收盘交易日` 且**没有验证行**的预测；"
                 "目标日还没到的预测**不在其中**（它的 bar 本来就不该存在，"
                 "记一笔「数据缺口」是假的）"),
    }
