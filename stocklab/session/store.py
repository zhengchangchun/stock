"""快照落库（P11）：三态 + 硬拒覆盖，与 `verify/store.py` 同一套纪律。

| 态 | 条件 | 行为 |
|----|------|------|
| `inserted` | 该 `(code, trade_date, ts)` 没有行 | 写入 |
| `identical` | 已有行，`_canonical()` 后内容 hash 相等 | **不写**，成功返回 |
| `conflict` | 同键不同内容 | 抛 `SnapshotConflict`，**不覆盖**，由调用方留痕 |

`SnapshotConflict` 抛而不是返回：调用方忘了检查返回值时，冲突就会变成静默通过。

## `_canonical()` 为什么必须有（ERROR_DIARY #10 的同一类坑）

幂等判定比的是「库里的行」而不是「调用方手里的对象」。内存里的 `87`（int）经 SQLite
REAL 列读回来是 `87.0`（float），而 `canonical_json` 会把它们序列化成 `87` / `87.0`
—— 两个**不同**的哈希。于是同一份数据第二次跑就会判成 `conflict`，
「幂等」变成「跑一次就自锁」。所以 hash 之前两侧都要过 `_canonical()` 归一数值类型。
"""

from __future__ import annotations

import sqlite3
from typing import Mapping

from stocklab.predict.model import canonical_json

#: 参与身份与内容的列（顺序即 INSERT 顺序）。
#: **`fetched_at` 刻意不在其中**：它是「我们什么时候拿到的」，不是「这份截面是什么」。
#: 让它进 hash 会让每次重抓都判成 `conflict`（时间戳必然不同）—— 见 ADR-005。
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "code", "trade_date", "ts", "price", "pre_close", "open", "high", "low",
    "volume", "amount", "turnover", "source",
)

#: 数值列（hash 前一律归一到 float / int，两侧同一把尺子）。
_REAL_FIELDS = ("price", "pre_close", "open", "high", "low", "amount", "turnover")
_INT_FIELDS = ("volume",)

_INSERT_SQL = (
    "INSERT INTO quote_snapshots (" + ", ".join(SNAPSHOT_FIELDS) + ", fetched_at)"
    " VALUES (" + ", ".join(f":{c}" for c in SNAPSHOT_FIELDS) + ", :fetched_at)"
)

_KEY_SQL = "SELECT * FROM quote_snapshots WHERE code=? AND trade_date=? AND ts=?"


class SnapshotConflict(RuntimeError):
    """同 `(code, trade_date, ts)` 已有一行**内容不同**的快照。

    刻意不做 upsert：`quote_snapshots` 是**盘中事实**的账本。源站事后修订某个 tick
    是可能的，但「修订」与「覆盖」是两件事 —— 覆盖会让「盘中当时看到的是什么」
    永久消失，而那正是本表存在的理由。冲突留痕、交给人处置。
    """


def canonical(row: Mapping) -> dict:
    """把一行规约成「入库后的样子」：数值列归一 + 只留 `SNAPSHOT_FIELDS`。"""
    out: dict = {}
    for k in SNAPSHOT_FIELDS:
        v = row.get(k)
        if k in _REAL_FIELDS:
            v = None if v is None else float(v)
        elif k in _INT_FIELDS:
            v = None if v is None else int(v)
        elif v is not None:
            v = str(v)
        out[k] = v
    return out


def snapshot_hash(row: Mapping) -> str:
    """内容 hash（只覆盖 `SNAPSHOT_FIELDS`，不含 `fetched_at`）。"""
    import hashlib

    blob = canonical_json(canonical(row))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def row_from_db(row: Mapping) -> dict:
    """由库里的行重建快照载荷（幂等判定基于它，不基于调用方手里的对象）。"""
    return {k: row[k] for k in SNAPSHOT_FIELDS}


def find_snapshot(conn: sqlite3.Connection, code: str, trade_date: str,
                  ts: str) -> sqlite3.Row | None:
    return conn.execute(_KEY_SQL, (code, trade_date, ts)).fetchone()


def insert_snapshot(conn: sqlite3.Connection, row: Mapping, *,
                    now: str) -> tuple[str, int]:
    """三态写入，返回 `(state, snapshot_id)`；`state ∈ {inserted, identical}`。

    同键不同内容抛 `SnapshotConflict`（**不覆盖**）。
    """
    key = (str(row["code"]), str(row["trade_date"]), str(row["ts"]))
    existing = find_snapshot(conn, *key)
    if existing is not None:
        old = row_from_db(existing)
        if snapshot_hash(old) == snapshot_hash(row):
            return "identical", int(existing["snapshot_id"])
        raise SnapshotConflict(
            f"快照 {key} 已存在且内容不同（库内 vs 新到："
            f"price {old['price']} vs {row['price']}，"
            f"amount {old['amount']} vs {row['amount']}，"
            f"volume {old['volume']} vs {row['volume']}）—— "
            "quote_snapshots 是盘中事实的账本，不覆盖。"
            "源站修订同一 tick 属于要查清并留痕的事，不属于要自动抹平的事。"
        )
    payload = canonical(row)
    payload["fetched_at"] = str(now)
    cur = conn.execute(_INSERT_SQL, payload)
    conn.commit()
    return "inserted", int(cur.lastrowid)


def insert_snapshots(conn: sqlite3.Connection, rows, *, now: str) -> tuple[dict, list[dict]]:
    """批量写入，返回 `(每代码状态, 异常列表)`。

    **一行失败不牵连其余**：冲突的进 `conflicts`，其余照常落库 ——
    否则一只票的源站修订会让整次采集颗粒无收（而本次采集对别的票仍然有效）。
    """
    states: dict[str, str] = {}
    conflicts: list[dict] = []
    for row in rows:
        try:
            state, sid = insert_snapshot(conn, row, now=now)
        except SnapshotConflict as exc:
            conflicts.append({"code": row["code"], "ts": row["ts"],
                              "reason": str(exc)})
            continue
        states[row["code"]] = f"{state}:{sid}"
    return states, conflicts
