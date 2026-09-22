"""模块2 通路台账读写（`m2_channel_runs` / `m2_forecasts`）。

## 只提供 INSERT 与 SELECT

与 `plugin/store.py` / `store/validation.py` 同款：不给 UPDATE / DELETE ——
改错只能再追加一行。表上的 append-only 触发器是第二道防线，接口层先不提供是
第一道（「想改一行」的调用方在 IDE 里就找不到方法）。

## 幂等靠「零写入」，不靠「记一笔」

`ran_run()` 是通路 A/B 的**先决判据**：命中 `status='ran'` 就整个跳过，
连一行台账都不写。「记一笔『这次没做事』」不是幂等的表达方式 ——
它会让同一天重放两遍的库与只跑一遍的库**不一样**，而那正是 T1 要排除的东西。

`ran` 那一格另有**部分唯一索引**兜底（`uq_m2_channel_runs_ran`）：即便上层判据
将来被改坏，也不可能在同一天为同一账户写下两条 `ran`。
`skipped` / `rejected` 不设唯一 —— 它们可以重复出现（数据没补齐时每天都跳过）。
"""

from __future__ import annotations

import json
import sqlite3

from stocklab.m2.config import (
    CHANNELS,
    STATUS_RAN,
    TABLE_FORECASTS,
    TABLE_RUNS,
)

RUN_STATUSES: frozenset[str] = frozenset({"ran", "skipped", "rejected"})


def _rows(conn: sqlite3.Connection, sql: str, args: tuple) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


# ---------- 运行台账 ----------


def insert_run(conn: sqlite3.Connection, *, channel: str, account_id: str,
               asof: str, status: str, reason: str, plugins: dict,
               n_orders: int = 0, detail: dict | None = None, now: str,
               commit: bool = True) -> int:
    """记一次通路运行。`status` 不在白名单 / `reason` 为空 → 拒绝。

    `commit=False` 供通路把**整天的写入**放进一个事务（与
    `paper/engine.py::_step_all` 同一个理由：一次失败不得留下半截状态）。
    """
    if channel not in CHANNELS:
        raise ValueError(f"未知通路 {channel!r}；已知 {list(CHANNELS)}")
    if status not in RUN_STATUSES:
        raise ValueError(
            f"未知运行状态 {status!r} —— 必须在 m2/store.RUN_STATUSES 与 "
            "schema.sql 的 CHECK 里登记（不许静默忽略：状态与事件流一旦不一致，"
            "没有任何一层会发现）")
    if not str(reason).strip():
        raise ValueError(
            "reason 不许为空 —— 留痕的意义就是它。跳过/拒绝要说清是哪一条判据，"
            "`ran` 也要写摘要（空串等于「这天什么都不知道」）")
    cur = conn.execute(
        f"INSERT INTO {TABLE_RUNS} (channel, account_id, asof, status, reason,"
        " plugins_json, n_orders, detail_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (channel, account_id, asof, status, reason,
         json.dumps(dict(plugins), ensure_ascii=False, sort_keys=True),
         int(n_orders),
         json.dumps(dict(detail or {}), ensure_ascii=False, sort_keys=True), now))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def ran_run(conn: sqlite3.Connection, channel: str, account_id: str,
            asof: str) -> dict | None:
    """该 `(通路, 账户, 日)` 是否已经**成功跑过**（幂等判据）。

    只认 `ran`：`skipped` / `rejected` 那天并没有产出，重跑必须允许。
    """
    row = conn.execute(
        f"SELECT * FROM {TABLE_RUNS} WHERE channel = ? AND account_id = ?"
        " AND asof = ? AND status = ?", (channel, account_id, asof, STATUS_RAN)
    ).fetchone()
    return None if row is None else _decode_run(dict(row))


def list_runs(conn: sqlite3.Connection, *, channel: str | None = None,
              account_id: str | None = None, asof: str | None = None) -> list[dict]:
    sql = f"SELECT * FROM {TABLE_RUNS}"
    where, args = [], []
    for col, value in (("channel", channel), ("account_id", account_id),
                       ("asof", asof)):
        if value is not None:
            where.append(f"{col} = ?")
            args.append(value)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY run_id"
    return [_decode_run(r) for r in _rows(conn, sql, tuple(args))]


def _decode_run(row: dict) -> dict:
    row["plugins"] = json.loads(row.pop("plugins_json") or "{}")
    row["detail"] = json.loads(row.pop("detail_json") or "{}")
    return row


# ---------- 插桩预测（A3 / B1） ----------


def forecast_row(payload: dict) -> dict:
    """把**已校验**的插桩预测载荷摊成落库列（契约名 → 库列名）。

    `range_80` → `range_lo` / `range_hi`：**不重算、不归一、不夹紧** ——
    概率与区间已经在 `plugin/contract.py` 校验过（不归一即拒），这里只换名字。
    `None`（「不知道」）原样落 `NULL`，绝不落 0（`0.0` 会读成「最坏情况」）。
    """
    rng = payload.get("range_80")
    direction = payload.get("direction")
    return {
        "range_lo": None if rng is None else float(rng[0]),
        "range_hi": None if rng is None else float(rng[1]),
        "direction_up": None if direction is None else float(direction["up"]),
        "direction_flat": None if direction is None else float(direction["flat"]),
        "direction_down": None if direction is None else float(direction["down"]),
        "invalidate_if": payload.get("invalidate_if"),
        "na_reasons_json": json.dumps(list(payload.get("na_reasons") or []),
                                      ensure_ascii=False),
        "schema_version": str(payload["schema_version"]),
    }


def insert_forecast(conn: sqlite3.Connection, *, plugin_id: str, channel: str,
                    account_id: str, asof: str, code: str, payload: dict,
                    script_id: int, script_version: str, input_sha256: str,
                    now: str, commit: bool = True) -> int:
    """落一行插桩预测。同 `(账户, 日, 标的)` 已有行 → `sqlite3.IntegrityError`。

    **不先查再写**：这里的幂等单元与通路运行的幂等单元是**同一个**
    （通路已经用 `ran_run` 挡住了整天的重跑），所以走到这里还撞唯一键，
    说明调用方绕过了 `ran_run` —— 那是调用方的 bug，报出来比静默返回旧 id 好
    （ERROR_DIARY #25 的教训是「别让唯一键兜自己的重试」，不是「别用唯一键」）。
    """
    row = {"plugin_id": plugin_id, "channel": channel, "account_id": account_id,
           "asof_date": asof, "code": code, **forecast_row(payload),
           "script_id": int(script_id), "script_version": str(script_version),
           "input_sha256": str(input_sha256), "created_at": now}
    cols = ", ".join(row)
    cur = conn.execute(
        f"INSERT INTO {TABLE_FORECASTS} ({cols})"
        f" VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def list_forecasts(conn: sqlite3.Connection, *, account_id: str | None = None,
                   asof: str | None = None,
                   plugin_id: str | None = None) -> list[dict]:
    sql = f"SELECT * FROM {TABLE_FORECASTS}"
    where, args = [], []
    for col, value in (("account_id", account_id), ("asof_date", asof),
                       ("plugin_id", plugin_id)):
        if value is not None:
            where.append(f"{col} = ?")
            args.append(value)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY forecast_id"
    out = []
    for row in _rows(conn, sql, tuple(args)):
        row["na_reasons"] = json.loads(row.pop("na_reasons_json") or "[]")
        row["range_80"] = (None if row["range_lo"] is None
                           else [row["range_lo"], row["range_hi"]])
        row["direction"] = (None if row["direction_up"] is None else {
            "up": row["direction_up"], "flat": row["direction_flat"],
            "down": row["direction_down"]})
        out.append(row)
    return out
