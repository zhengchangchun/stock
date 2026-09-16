"""收盘回填（P11）：把当日快照里的 `amount`/`turnover` 补进 `bars_daily` 对应行。

## 五条不许越过的线

1. **只补当日**：`WHERE code=? AND date=?` —— 语句层面够不到别的日期。
   现有 14164 行的 `amount`/`turnover` 全为 NULL，本模块**一行都不碰它们**。
2. **只补 NULL**：`COALESCE(amount, :amount)` —— 已有值的行原样不动。
   NULL 的语义是「当日未采集」，不是 0；填 0 / 插值 / 估算都是造数。
3. **只用当日最后一条快照，且必须是收盘后的**：`amount`/`turnover` 是**当日累计**量，
   收盘后的最后一条就是全天值。中途那条会被写成「半天成交额」——数值合法、含义全错。
   而当日只有盘中快照时（例如收盘 tick 没跑成），**取不到收盘值就不取** —— 见下。
4. **`adj_mode != 'none'` 硬拒绝**：与 `repo.insert_bars` 同一把闸（铁律①）。
   不复权表里混进复权价是最难发现的一类错，这里不开口子。
5. **缺数据不静默**：当日没有 bar 行 / 快照里该字段为空 → 计入摘要对应的
   `skipped_*` 列表并 `system_events` 留痕。一次「什么都没做」必须是**看得见**的。

## 为什么不在 `ingest bars` 里做

`ingest bars`（腾讯日K）源站**没有** `amount`/`turnover` 字段，它只能写 NULL；
快照是这两个字段的唯一来源。两件事分开，各自只做自己拿得到的那部分数据。

## 为什么还要校验快照时刻（P22）

第 3 条原本只取了 `MAX(ts)`，**不检查那条快照是不是收盘后的**。实测 2026-09-16：
15:05 的收盘 tick 排队未执行，当日最新快照只有 09:35 与 14:30，回填于是拿 14:30
那条盘中快照去填全天 `amount` —— **成交额被系统性低估，且没有任何告警**。
这是本项目最怕的那类错：数值合法、口径全错、静默。

判据是「快照**自身**的 `ts` 所表示的时刻 ≥ 15:00:00」（`trade_date` 当天本地时钟）。
不用 `now`（回填可能在事后补跑），也不看 `trade_date` 之外的东西 —— 逐标的判，
因为同一天不同标的的最后一条快照时刻可以不同。

**fail-closed**：`ts` 解不出时刻时按「未收盘」处理（跳过 + 留痕），
而不是当作已收盘去写。拿不准就不写。
"""

from __future__ import annotations

import sqlite3

from stocklab.store import repo

#: 收盘时刻（Asia/Shanghai，本地时钟口径）。在此**及之后**当日快照的最后一条即全天值。
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


def _ts_time(ts: str) -> tuple[int, int, int] | None:
    """`YYYYMMDDHHMMSS` → `(hh, mm, ss)`；解不出（长度/数字不对）→ `None`。

    刻意用「取位置 + 转 int」而不是 `datetime.strptime`：`ts` 是源站原样的字符串，
    本函数只关心**时刻**（日期部分由 `trade_date` 给定），解析失败必须能返回 `None`
    交给调用方 fail-closed，而不是抛出去把整轮回填炸掉。
    """
    if not isinstance(ts, str) or len(ts) < 14:
        return None
    try:
        return int(ts[8:10]), int(ts[10:12]), int(ts[12:14])
    except ValueError:
        return None


def is_closed_snapshot(ts: str) -> bool:
    """`ts` 的时刻是否已到收盘（`>= 15:00:00`）。解不出 → `False`（fail-closed）。"""
    parts = _ts_time(ts)
    if parts is None:
        return False
    return parts >= (CLOSE_HOUR, CLOSE_MINUTE, 0)


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
        "skipped_stale_snapshot": [],
        "updated": 0,
    }
    snaps = latest_snapshots(conn, trade_date)
    out["codes_with_snapshots"] = sorted(snaps)
    if not snaps:
        out["reason"] = "no_snapshots"
        return out
    out["latest_ts"] = max(s["ts"] for s in snaps.values())

    for code, snap in sorted(snaps.items()):
        if not is_closed_snapshot(snap["ts"]):
            # 当日只有盘中快照（收盘 tick 没跑成）→ 拿不到全天累计量。
            # **不**用盘中值凑数：那会把「半天成交额」写成全天值，且无人察觉。
            out["skipped_stale_snapshot"].append(code)
            repo.log_event(
                conn, "session", "warn",
                f"{code} {trade_date} 最新快照 ts={snap['ts']} 未到收盘时刻"
                f"（{CLOSE_HOUR:02d}:{CLOSE_MINUTE:02d}:00），回填跳过",
                context={"code": code, "trade_date": trade_date,
                         "ts": snap["ts"],
                         "job": "session_backfill_close"}, now=now)
            continue
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
    if (out["skipped_stale_snapshot"]
            and not out["filled"]
            and not out["skipped_already_filled"]):
        # 「一个正经值都没补上」是本次收尾的诚实结论，让调用方一眼看见（同 no_snapshots）。
        # 与 no_snapshots 同码：本来就没有可用的收盘数据，不是异常，所以不置退出 1。
        out["reason"] = "stale_snapshot"
    return out
