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
    BRANCHES,
    CHANNELS,
    STATUS_RAN,
    TABLE_FORECASTS,
    TABLE_JUDGEMENTS,
    TABLE_RUNS,
    TABLE_SCORES,
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


# ---------- 预测的事后校验分数（P48） ----------


#: `m2_forecast_scores` 的列（顺序即 INSERT 顺序）。`score_id` / `created_at` 由库或
#: 调用方给，不在这里 —— 分数的内容列与身份列分开，与 `verifications` 同款。
_SCORE_COLUMNS: tuple[str, ...] = (
    "forecast_id", "plugin_id", "account_id", "script_version", "asof_date",
    "target_date", "code", "scorable", "reason_code", "actual_close",
    "actual_pct", "range_lo", "range_hi", "range_hit", "dev_pct",
    "hit_direction", "pred_class", "actual_class", "bet_pct", "invalidated",
)


def find_score(conn: sqlite3.Connection, forecast_id: int) -> dict | None:
    """该预测的分数行（幂等判据）。"""
    row = conn.execute(
        f"SELECT * FROM {TABLE_SCORES} WHERE forecast_id = ?",
        (int(forecast_id),)).fetchone()
    return None if row is None else dict(row)


def insert_score(conn: sqlite3.Connection, *, score: dict, now: str,
                 commit: bool = True) -> int:
    """落一行校验分数。**幂等键 = `forecast_id` UNIQUE**。

    与 `insert_forecast` 同款：**不先查再写**（先查再写会让「幂等」变成一句
    由调用方保证的口头约定）。走到这里还撞唯一键，说明调用方没走 `find_score`
    那道判据 —— 那是调用方的 bug，报出来比静默返回旧 id 好。
    """
    row = {**{k: score.get(k) for k in _SCORE_COLUMNS}, "created_at": now}
    cols = ", ".join(row)
    cur = conn.execute(
        f"INSERT INTO {TABLE_SCORES} ({cols})"
        f" VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def list_scores(conn: sqlite3.Connection, *, plugin_id: str | None = None,
                asof: str | None = None) -> list[dict]:
    """分数行（含预测侧的 `code`/`asof_date` —— 它们是冗余列，不再 JOIN 回去）。

    默认按 `target_date, plugin_id, script_version, code` 排序：读数按
    `plugin_id` + `script_version` **分列**，聚合层自己再分组，这里只保证稳定序。
    """
    sql = f"SELECT * FROM {TABLE_SCORES}"
    where, args = [], []
    if plugin_id is not None:
        where.append("plugin_id = ?")
        args.append(plugin_id)
    if asof is not None:
        where.append("target_date <= ?")
        args.append(asof)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY target_date, plugin_id, script_version, account_id, code"
    return _rows(conn, sql, tuple(args))


# ---------- 自评估判定台账（P49） ----------


def find_judgement(conn: sqlite3.Connection, cycle_id: int,
                   asof: str) -> dict | None:
    """该 `(周期, 截止日)` 是否已经判定过（幂等判据，返回既有那一条）。"""
    row = conn.execute(
        f"SELECT * FROM {TABLE_JUDGEMENTS} WHERE cycle_id = ? AND asof_date = ?",
        (int(cycle_id), str(asof))).fetchone()
    return None if row is None else _decode_judgement(dict(row))


def insert_judgement(conn: sqlite3.Connection, *, cycle_id: int, script_id: int,
                     asof: str, branch: str, evidence: dict, criteria_text: str,
                     now: str, commit: bool = True) -> int:
    """落一条**建议**（不是执行）。`branch` 不在白名单 → 拒绝。

    幂等键 = `(cycle_id, asof_date)` UNIQUE。与 `insert_score` 同款：
    **不把「先查再写」当防线** —— 走到这里还撞唯一键，说明调用方没走
    `find_judgement` 那道判据，报出来比静默返回旧 id 好。
    """
    if branch not in BRANCHES:
        raise ValueError(
            f"未知判定分支 {branch!r} —— 必须在 m2/config.BRANCHES 与 schema.sql "
            "的 m2_judgements.branch CHECK 里登记（不许静默忽略：分支枚举一旦与"
            "库里的 CHECK 不一致，判定就写不进去，而调用方只看到一句外键似的错误）")
    cur = conn.execute(
        f"INSERT INTO {TABLE_JUDGEMENTS} (cycle_id, script_id, asof_date, branch,"
        " evidence_json, criteria_text, created_at) VALUES (?,?,?,?,?,?,?)",
        (int(cycle_id), int(script_id), str(asof), branch,
         json.dumps(dict(evidence), ensure_ascii=False, sort_keys=True),
         criteria_text, now))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def list_judgements(conn: sqlite3.Connection, *, cycle_id: int | None = None,
                    asof: str | None = None) -> list[dict]:
    """判定行（默认按 `asof_date` 升序 —— 同一周期的判定是一条时间线）。"""
    sql = f"SELECT * FROM {TABLE_JUDGEMENTS}"
    where, args = [], []
    if cycle_id is not None:
        where.append("cycle_id = ?")
        args.append(int(cycle_id))
    if asof is not None:
        where.append("asof_date <= ?")
        args.append(str(asof))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY asof_date, judgement_id"
    return [_decode_judgement(r) for r in _rows(conn, sql, tuple(args))]


def _decode_judgement(row: dict) -> dict:
    row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
    return row
