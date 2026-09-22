"""收盘回填（P11）：把当日快照里的 `amount`/`turnover` 补进 `bars_daily` 对应行。

另有一个同源判据 `bars_finalized_on()`（P46）：**当天的 K 线定型了没** ——
它同样只认「收盘后采到的证据」，是 `predict run --asof 今天` 的闸门。

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
from datetime import datetime
from zoneinfo import ZoneInfo

from stocklab.store import repo

TZ = ZoneInfo("Asia/Shanghai")

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


def _fetched_time(fetched_at: str) -> tuple[int, int, int] | None:
    """`fetched_at`（ISO8601）→ 上海本地 `(hh, mm, ss)`；读不出 → `None`（fail-closed）。

    naive 值按 `Asia/Shanghai` 解释：本项目所有写入都用 `datetime.now(TZ)` 生成带
    `+08:00` 的 ISO 串，naive 只可能来自手改/老数据，按本地读是这里的正确答案。
    """
    if not isinstance(fetched_at, str) or not fetched_at:
        return None
    try:
        dt = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    dt = dt.astimezone(TZ)
    return dt.hour, dt.minute, dt.second


def bars_finalized_on(conn: sqlite3.Connection, trade_date: str) -> tuple[bool, str]:
    """`trade_date` 当天的日 K 是否已是**收盘后采到**的终值。→ `(定型?, 人话原因)`。

    ## 为什么需要它（P46 §T3）

    `predict run --asof 今天` 若在任何一次「收盘后」判定为真时跑，就能拿到一根
    **尚未定型**的 K 线：`is_trade_date_closed(今天, now)` 在 15:00 就为真，而当天
    K 线要到 15:30 收盘链的 `ingest bars` 才被刷成终值。此时算出的预测基于**半截
    bar**，而 `predictions` 是 append-only —— 15:30 收盘链重算必然条条冲突
    （2026-09-22 实测 17 条，收盘链 exit 1，见 ERROR_DIARY #60）。

    ## 判据：`bars_daily.fetched_at`（这一行**最后一次从源站采到的时刻**）

    `repo.insert_bars` 的 `ON CONFLICT DO UPDATE` 会把 `fetched_at` 刷成本次采集
    时刻，所以它天然就是「这根 K 线是什么时候拿到的」。早于收盘时刻 ⇒ 那时市场
    还没收盘 ⇒ 这根 K 线的 `close` 只能是盘中值。

    **不用**墙上时钟（节假日、临时休市、机器睡醒补跑都会骗过「现在几点」），
    **也不用**另外两个更顺手的判据 —— 它们都度量「快照/回填跑没跑」，而本问题是
    「**收盘价**是不是终值」：`backfill_close_amounts` 只补 `amount`/`turnover`，
    **一行都不碰 `close`**。两个反例都实测过（ERROR_DIARY #60）：
    `session backfill-close` 当天成功跑过、当天 `amount` 非 NULL，两者在事故当天
    都为真，闸门会恒放行。

    ## fail-closed（三条）

    当天**一行 bar 都没有** / `fetched_at` 读不出时刻 / **任一行**早于收盘时刻
    → 一律判「未定型」。最后一条是刻意的：整天只有一行旧数据，而它可能正是被预测
    的那个标的，容不得「多数行都新」这种多数表决。
    """
    rows = conn.execute(
        "SELECT code, fetched_at FROM bars_daily WHERE date=? AND adj_mode='none'"
        " ORDER BY code", (trade_date,)).fetchall()
    if not rows:
        return False, (f"{trade_date} 在 bars_daily 里一行都没有 —— "
                       "证不出当天的 K 线已定型")
    close_hm = (CLOSE_HOUR, CLOSE_MINUTE, 0)
    for row in rows:
        t = _fetched_time(row["fetched_at"])
        if t is None:
            return False, (f"{row['code']} {trade_date} 的 fetched_at="
                           f"{row['fetched_at']!r} 读不出时刻 → 按未定型处理")
        if t < close_hm:
            return False, (f"{row['code']} {trade_date} 的 K 线是 "
                           f"{t[0]:02d}:{t[1]:02d} 采到的（早于 "
                           f"{CLOSE_HOUR:02d}:{CLOSE_MINUTE:02d} 收盘时刻）"
                           "→ 收盘价还没定型")
    return True, ""


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
