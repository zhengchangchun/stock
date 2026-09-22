"""模块2 验证周期台账的读写（`validation_cycles` / `validation_rounds` /
`validation_events` 三表）。

## 只提供 INSERT 与 SELECT

与 `plugin/store.py` 同款：不给 UPDATE / DELETE —— 改错只能再追加一行。
表上另有 append-only 触发器兜底，但**接口层先不提供**是第一道防线，
这样「想改一行」的调用方在 IDE 里就找不到方法，而不是等到运行时撞触发器。

## 越界方案进不了台账

`insert_cycle` 在写库**之前**调 `config.limits.check_validation_plan`，
不满足主干边界就抛 `PlanOutOfBounds`（**不 clamp**、不落库）——
「AI 给的周期不合规」是调用方的错误，不该在台账里留下一条被悄悄改过的记录。

## 判据原文与实测值分列

`criteria_text` 存**判据原文**（D-31：不许事后换口径）；
`validation_events` 把触发时的 `at_value`（实测）与 `threshold`（阈值）**分开存** ——
只留一个数，读者无法判断到底有没有越界。
"""

from __future__ import annotations

import json
import sqlite3

from stocklab.config import limits

TABLE_CYCLES = "validation_cycles"
TABLE_ROUNDS = "validation_rounds"
TABLE_EVENTS = "validation_events"

#: `validation_events.kind` 白名单 —— 与 `schema.sql` 的 CHECK **必须逐字相同**。
EVENT_KINDS: frozenset[str] = frozenset({
    "circuit_breaker", "freeze", "unfreeze", "validation_end",
})


def insert_cycle(conn: sqlite3.Connection, *, script_id: int, account_id: str,
                 planned_rounds: int, planned_days: int, params: dict,
                 criteria_text: str, start_date: str, now: str) -> int:
    """开一轮验证周期。方案越界 → `PlanOutOfBounds`（写库前拒绝，不 clamp）。

    `criteria_text` 不许为空：台账要回答「判据原文是什么」，空白等于没记。
    """
    limits.check_validation_plan(rounds=planned_rounds, days=planned_days)
    if not criteria_text.strip():
        raise ValueError("criteria_text 不许为空 —— 台账必须留下判据原文")
    cur = conn.execute(
        f"INSERT INTO {TABLE_CYCLES} (script_id, account_id, planned_rounds,"
        " planned_days, params_json, criteria_text, start_date, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (script_id, account_id, planned_rounds, planned_days,
         json.dumps(params, ensure_ascii=False, sort_keys=True),
         criteria_text, start_date, now))
    conn.commit()
    return int(cur.lastrowid)


def insert_round(conn: sqlite3.Connection, *, cycle_id: int, round_no: int,
                 window_start: str, window_end: str, metrics: dict,
                 note: str | None, now: str) -> int:
    cur = conn.execute(
        f"INSERT INTO {TABLE_ROUNDS} (cycle_id, round_no, window_start,"
        " window_end, metrics_json, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (cycle_id, round_no, window_start, window_end,
         json.dumps(metrics, ensure_ascii=False, sort_keys=True), note, now))
    conn.commit()
    return int(cur.lastrowid)


def insert_event(conn: sqlite3.Connection, *, cycle_id: int, script_id: int,
                 kind: str, at_value: float | None, threshold: float | None,
                 criteria_text: str, reason: str, now: str) -> int:
    """记一条熔断/冻结/解冻事件。`kind` 不在 `EVENT_KINDS` 里直接拒绝。

    写入层先拦一道、schema 的 CHECK 再拦一道：前者给出**点名**的错误，
    后者保证绕过接口也写不进去（与 ERROR_DIARY #49「别让 CHECK 兜底」同向）。
    """
    if kind not in EVENT_KINDS:
        raise ValueError(
            f"未知台账事件 {kind!r} —— 必须在 validation.EVENT_KINDS 与 "
            "schema.sql 的 CHECK 里登记")
    cur = conn.execute(
        f"INSERT INTO {TABLE_EVENTS} (cycle_id, script_id, kind, at_value,"
        " threshold, criteria_text, reason, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (cycle_id, script_id, kind, at_value, threshold, criteria_text, reason,
         now))
    conn.commit()
    return int(cur.lastrowid)


def get_cycle(conn: sqlite3.Connection, cycle_id: int) -> dict | None:
    row = conn.execute(f"SELECT * FROM {TABLE_CYCLES} WHERE cycle_id = ?",
                       (cycle_id,)).fetchone()
    return None if row is None else dict(row)


def list_cycles(conn: sqlite3.Connection, *,
                script_id: int | None = None) -> list[dict]:
    if script_id is None:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_CYCLES} ORDER BY cycle_id").fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_CYCLES} WHERE script_id = ? ORDER BY cycle_id",
            (script_id,)).fetchall()
    return [dict(r) for r in rows]


def list_rounds(conn: sqlite3.Connection, cycle_id: int) -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM {TABLE_ROUNDS} WHERE cycle_id = ? ORDER BY round_no",
        (cycle_id,)).fetchall()
    return [dict(r) for r in rows]


def list_events(conn: sqlite3.Connection, cycle_id: int) -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM {TABLE_EVENTS} WHERE cycle_id = ? ORDER BY event_id",
        (cycle_id,)).fetchall()
    return [dict(r) for r in rows]
