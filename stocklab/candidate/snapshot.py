"""步骤9-10：候选池落库与快照（设计文档 §5.4-5.6）。

## 幂等键是 `(asof, run_kind)`，判据在写之前

`find_snapshot` 是**先决判据**：调用方在决定要不要跑主流程**之前**先问
「这个 asof 的快照存在吗」。顺序不能反 —— ERROR_DIARY #25 记过这个坑：
先做业务层查重再让幂等键兜底，会把**自己刚写下的那一行**当成重复。

`UNIQUE(asof, run_kind)` 是第二道结构性防线。

## 一天可以有多种快照

`run_kind` 分 `light`/`weekly`/`quarterly`——文档 07 的四层任务各自产出
自己的快照。它们不是互相覆盖的关系，所以幂等键必须带上 `run_kind`。

## 状态是快照时点的值

`candidate_members` 行不可变（触发器钉住）。标的的当前状态 = **最新快照
里的 status**，状态变更靠产生新快照表达，不靠改行。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

TABLE_SNAPSHOTS = "candidate_snapshots"
TABLE_MEMBERS = "candidate_members"
TABLE_REJECTS = "candidate_rejects"

#: 本轮只产出前两种；后两种依赖模块2 的持仓联动（设计文档 §5.5）。
STATUSES: tuple[str, ...] = ("观察中", "等待买点", "已建仓", "逻辑证伪移出")


@dataclass(frozen=True)
class MemberRow:
    code: str
    pool: str
    raw_score: float
    adj_score: float
    reason: str
    risk_json: str
    status: str = "观察中"


@dataclass(frozen=True)
class RejectRow:
    code: str
    stage: str
    reason: str
    plugin_id: str | None = None


def find_snapshot(conn: sqlite3.Connection, *, asof: str,
                  run_kind: str) -> int | None:
    row = conn.execute(
        f"SELECT snapshot_id FROM {TABLE_SNAPSHOTS}"
        " WHERE asof = ? AND run_kind = ?", (asof, run_kind)).fetchone()
    return None if row is None else int(row[0])


def write_snapshot(conn: sqlite3.Connection, *, asof: str, run_kind: str,
                   params: dict, members: list[MemberRow],
                   rejects: list[RejectRow], now: str) -> int:
    """写快照 + 成员 + 淘汰记录，返回 `snapshot_id`。

    已存在同 `(asof, run_kind)` 的快照时**直接返回既有 id，不再写**。
    """
    existing = find_snapshot(conn, asof=asof, run_kind=run_kind)
    if existing is not None:
        return existing

    cur = conn.execute(
        f"INSERT INTO {TABLE_SNAPSHOTS} (asof, run_kind, params_json, created_at)"
        " VALUES (?,?,?,?)",
        (asof, run_kind,
         json.dumps(params, ensure_ascii=False, sort_keys=True), now))
    snapshot_id = int(cur.lastrowid)

    for m in members:
        if m.status not in STATUSES:
            raise ValueError(
                f"非法状态 {m.status!r}；允许：{list(STATUSES)}")
        conn.execute(
            f"INSERT INTO {TABLE_MEMBERS} (snapshot_id, code, pool, raw_score,"
            " adj_score, reason, risk_json, status, entered_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (snapshot_id, m.code, m.pool, m.raw_score, m.adj_score, m.reason,
             m.risk_json, m.status, now))

    for r in rejects:
        conn.execute(
            f"INSERT INTO {TABLE_REJECTS} (snapshot_id, code, stage, reason,"
            " plugin_id) VALUES (?,?,?,?,?)",
            (snapshot_id, r.code, r.stage, r.reason, r.plugin_id))

    conn.commit()
    return snapshot_id


def load_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    snap = conn.execute(
        f"SELECT * FROM {TABLE_SNAPSHOTS} WHERE snapshot_id = ?",
        (snapshot_id,)).fetchone()
    if snap is None:
        raise LookupError(f"快照 {snapshot_id} 不存在")
    snapshot = dict(snap)
    snapshot["params"] = json.loads(snapshot.pop("params_json"))

    members = [dict(r) for r in conn.execute(
        f"SELECT * FROM {TABLE_MEMBERS} WHERE snapshot_id = ?"
        " ORDER BY pool, adj_score DESC, code", (snapshot_id,))]
    rejects = [dict(r) for r in conn.execute(
        f"SELECT * FROM {TABLE_REJECTS} WHERE snapshot_id = ?"
        " ORDER BY stage, code", (snapshot_id,))]
    return {"snapshot": snapshot, "members": members, "rejects": rejects}
