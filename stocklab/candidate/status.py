"""P91（B7）：候选池「标的状态」的**读侧 overlay** —— 纯只读，零写入口。

## 状态是**事件**，不是列

`candidate_members.status` 是**快照时点**的值，行不可变（触发器钉住）；而状态的
变更时刻与快照的 `asof` **无关**（人可以周三改、快照是上周的）。所以真源是
`candidate_status_events` 这张 append-only 事件表，`candidate_members.status`
只是「那一刻的记录」。

## 口径（任务书 §0.5 D2）

    status(code, D) = 事件里 asof_date <= D 且 (asof_date, event_id) 最大的那行的 status
    无事件          ⇒ ("观察中", None)      ← 与 `MemberRow.status` 的默认值逐位相同

`asof_date` 是 **PIT 锚**：写 `2026-09-25` 就是「从 09-25 起生效」，
09-24 的查询看不到它（`tests/test_candidate_status.py` 钉住）。

同日两条 ⇒ `event_id` 大者赢（后写覆盖**读侧**，历史两行都在）——
这正是「写错了只能再追加一行」能成立的原因。

## 本模块**只读**

写入口在 CLI（`cli/candidate.py::cmd_candidate_status_set`）。这里没有、也不许有
INSERT/UPDATE/DELETE —— 有一条用例按字面扫这个文件（D2）。
"""

from __future__ import annotations

import sqlite3

TABLE = "candidate_status_events"

#: 无事件时的状态。**必须**与 `candidate/snapshot.py::MemberRow.status` 的默认值
#: 同一份值 —— 靠一条用例钉住（`test_default_status_matches_member_row`），不靠人记。
DEFAULT_STATUS = "观察中"

#: 每 code 取最新一行（窗口函数；SQLite ≥ 3.25）。
#: `asof_date` 优先于 `event_id`：新一天的事件赢过旧一天后写的。
_LATEST_SQL = (
    "SELECT code, status, event_id, asof_date FROM ("
    " SELECT code, status, event_id, asof_date,"
    " ROW_NUMBER() OVER (PARTITION BY code"
    " ORDER BY asof_date DESC, event_id DESC) AS rn"
    f" FROM {TABLE} WHERE asof_date <= ?"
    ") WHERE rn = 1"
)


def _rows(cur) -> list[dict]:
    """游标 → dict 列表（不依赖连接的 `row_factory`）。"""
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def latest_status(conn: sqlite3.Connection, code: str,
                  asof: str) -> tuple[str, dict | None]:
    """`code` 在 `asof` 这一天的状态。返回 `(status, 事件行 | None)`。

    无事件 ⇒ `(DEFAULT_STATUS, None)`。
    """
    cur = conn.execute(_LATEST_SQL + " AND code = ?", (asof, code))
    rows = _rows(cur)
    if not rows:
        return DEFAULT_STATUS, None
    return str(rows[0]["status"]), rows[0]


def status_map(conn: sqlite3.Connection, codes, asof: str) -> dict[str, str]:
    """批量版（页面用，**一次查完**，不做 N+1）。

    返回的键集合**恒等于**请求的 code 集合 —— 无事件的 code 给 `DEFAULT_STATUS`，
    调用方不必自己补默认值（少一处「忘了补」的机会）。
    """
    want = list(dict.fromkeys(codes))          # 去重、保序
    if not want:
        return {}
    marks = ",".join("?" * len(want))
    cur = conn.execute(_LATEST_SQL + f" AND code IN ({marks})", (asof, *want))
    got = {str(r["code"]): str(r["status"]) for r in _rows(cur)}
    return {code: got.get(code, DEFAULT_STATUS) for code in want}


def history(conn: sqlite3.Connection, *, code: str | None = None,
            limit: int = 10, asof: str | None = None) -> list[dict]:
    """事件流水，按 `(asof_date, event_id)` **降序**。

    `code` / `asof` 为 `None` ⇒ 不过滤（页面要的是全局最近 N 条；
    `asof` 给值时是 PIT 过滤：只看 `asof_date <= asof` 的）。
    """
    where, params = [], []
    if code is not None:
        where.append("code = ?")
        params.append(code)
    if asof is not None:
        where.append("asof_date <= ?")
        params.append(asof)
    sql = f"SELECT * FROM {TABLE}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY asof_date DESC, event_id DESC LIMIT ?"
    return _rows(conn.execute(sql, (*params, int(limit))))


__all__ = ["DEFAULT_STATUS", "TABLE", "history", "latest_status", "status_map"]
