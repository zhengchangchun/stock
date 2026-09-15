"""收盘回填（P11）：把当日快照里的 `amount`/`turnover` 补进 `bars_daily` 对应行。

## 五条不许越过的线

1. **只补当日**：`WHERE code=? AND date=?` —— 语句层面够不到别的日期。
   现有 14164 行的 `amount`/`turnover` 全为 NULL，本模块**一行都不碰它们**。
2. **只补 NULL**：`COALESCE(amount, :amount)` —— 已有值的行原样不动。
   NULL 的语义是「当日未采集」，不是 0；填 0 / 插值 / 估算都是造数。
3. **只用当日最后一条快照**：`amount`/`turnover` 是**当日累计**量，收盘后的最后一条
   就是全天值。中途那条会被写成「半天成交额」——数值合法、含义全错。
4. **`adj_mode != 'none'` 硬拒绝**：与 `repo.insert_bars` 同一把闸（铁律①）。
   不复权表里混进复权价是最难发现的一类错，这里不开口子。
5. **缺数据不静默**：当日没有 bar 行 / 快照里该字段为空 → 计入摘要对应的
   `skipped_*` 列表并 `system_events` 留痕。一次「什么都没做」必须是**看得见**的。

## 为什么不在 `ingest bars` 里做

`ingest bars`（腾讯日K）源站**没有** `amount`/`turnover` 字段，它只能写 NULL；
快照是这两个字段的唯一来源。两件事分开，各自只做自己拿得到的那部分数据。
"""

from __future__ import annotations

import sqlite3

from stocklab.store import repo

#: 收盘时刻（Asia/Shanghai，本地时钟口径）。在此之后当日快照的最后一条即全天值。
CLOSE_HOUR = 15
CLOSE_MINUTE = 0

_LATEST_SQL = (
    "SELECT * FROM quote_snapshots q WHERE q.trade_date = ?"
    "  AND q.ts = (SELECT MAX(ts) FROM quote_snapshots"
    "              WHERE code = q.code AND trade_date = q.trade_date)"
    " ORDER BY q.code"
)

_UPDATE_SQL = (
    "UPDATE bars_daily"
    "   SET amount   = COALESCE(amount,   :amount),"
    "       turnover = COALESCE(turnover, :turnover)"
    " WHERE code = :code AND date = :date AND adj_mode = 'none'"
)


def latest_snapshots(conn: sqlite3.Connection, trade_date: str) -> dict[str, dict]:
    """该交易日每个标的的**最后一条**快照（`code` → 行）。"""
    return {r["code"]: dict(r)
            for r in conn.execute(_LATEST_SQL, (trade_date,))}


def backfill_close_amounts(conn: sqlite3.Connection, trade_date: str, *,
                           now: str) -> dict:
    """把 `trade_date` 收盘快照的 `amount`/`turnover` 补进当日 bar 行。

    幂等：第二次跑时 `amount` 已非 NULL → `skipped_already_filled`，一行不改。
    """
    out: dict = {
        "trade_date": trade_date,
        "codes_with_snapshots": [],
        "filled": [],
        "skipped_already_filled": [],
        "skipped_missing_bar": [],
        "skipped_no_value": [],
        "updated": 0,
    }
    snaps = latest_snapshots(conn, trade_date)
    out["codes_with_snapshots"] = sorted(snaps)
    if not snaps:
        out["reason"] = "no_snapshots"
        return out
    out["latest_ts"] = max(s["ts"] for s in snaps.values())

    for code, snap in sorted(snaps.items()):
        bars = conn.execute(
            "SELECT code, date, amount, turnover, adj_mode FROM bars_daily"
            " WHERE code=? AND date=?", (code, trade_date)).fetchall()
        if not bars:
            # 当日 bar 还没采到（`ingest bars` 未跑）：显式留痕，**不**去造一根 bar
            out["skipped_missing_bar"].append(code)
            repo.log_event(
                conn, "session", "warn",
                f"{code} {trade_date} 快照有数据但 bars_daily 无当日行，回填跳过",
                context={"code": code, "trade_date": trade_date,
                         "job": "session_backfill_close"}, now=now)
            continue
        bar = bars[0]
        if bar["adj_mode"] != "none":
            # 与 insert_bars 同一把闸：不复权是 bars_daily 的唯一合法口径（铁律①）
            raise ValueError(
                f"bars_daily 中 {code} {trade_date} 的 adj_mode="
                f"{bar['adj_mode']!r} ≠ 'none'，拒绝回填 —— bars_daily 只存不复权行"
            )
        if bar["amount"] is not None and bar["turnover"] is not None:
            out["skipped_already_filled"].append(code)
            continue
        if snap["amount"] is None and snap["turnover"] is None:
            # 源站没给这两个字段：不填 0、不插值，显式记下来
            out["skipped_no_value"].append(code)
            continue
        conn.execute(_UPDATE_SQL, {"amount": snap["amount"],
                                   "turnover": snap["turnover"],
                                   "code": code, "date": trade_date})
        out["updated"] += 1
        out["filled"].append({"code": code, "amount": snap["amount"],
                              "turnover": snap["turnover"],
                              "snapshot_id": int(snap["snapshot_id"]),
                              "ts": snap["ts"]})
        repo.log_event(
            conn, "session", "info",
            f"{code} {trade_date} 回填 amount/turnover（源：快照 {snap['ts']}）",
            context={"code": code, "trade_date": trade_date,
                     "snapshot_id": int(snap["snapshot_id"]),
                     "amount": snap["amount"], "turnover": snap["turnover"],
                     "job": "session_backfill_close"}, now=now)
    conn.commit()
    return out
