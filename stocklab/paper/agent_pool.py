"""当日候选池（P52）：AI 操盘手**可投标的**的那个集合。

## 为什么只读 SQL、不 import `stocklab.candidate`

`paper/` 的源码护栏（`tests/test_paper_discipline_guard.py`）禁止模拟盘 import
选股链路 —— 那条纪律的**本意**是「静态臂只做纪律与分散，接选股就是换一个问题」。
P52 之后这句话对 `arm-agent` 不再成立（它的决策空间就是「池内自由选标的」，D-34），
但护栏本身**不因此作废**：静态臂照旧不许碰候选池。

所以这里用**裸 SQL 读那两张表**，而不是 `from stocklab.candidate import snapshot`：
- 守卫仍然是「`paper/` 不接选股链路」这一条，不需要开洞；
- 读的是**已落库的快照**（append-only 的历史行），不触发起评分内核 ——
  打分跑在别处，模拟盘只在事后读它的结论。

## 「当日候选池」= 每池各自最近一份快照

`candidate_snapshots` 的幂等键是 `(asof, run_kind)`，一天可以有 `light`/`weekly`/
`quarterly` 三份快照。于是「当日池」的定义必须是：**对每个池，取 `asof <= 决策日`
的最近一份含该池成员快照里的成员**（先按快照 asof 降序，再按 snapshot_id 降序）。

不用「必须恰好等于决策日」：候选池是**定期重算**的（模块1 的 light/weekly/quarterly），
要求每天都有快照就等于要求一个不存在的频率。用 `<=` 的含义是
「截至决策日、我方已知道的最新池」—— 与 PIT 守卫同一个方向（只看过去）。

## 空的池不许当成「投什么都可以」

池里没有快照 / 表还没前滚 → `codes` 为空集合，于是**任何标的都在池外被拒**。
这是 fail-closed：`available=False` 时 AI 臂一根单也下不了，而不是「池空了所以放行」。
"""

from __future__ import annotations

import sqlite3

TABLE_SNAPSHOTS = "candidate_snapshots"
TABLE_MEMBERS = "candidate_members"

#: 三个池。`candidate_members.pool` 的 CHECK 与这里同文（改一处须同步两处）。
POOLS: tuple[str, ...] = ("short", "mid", "long")

_SQL = (
    f"SELECT m.pool AS pool, m.snapshot_id AS snapshot_id, s.asof AS asof,"
    f" m.code AS code"
    f" FROM {TABLE_MEMBERS} m JOIN {TABLE_SNAPSHOTS} s"
    f"   ON s.snapshot_id = m.snapshot_id"
    f" WHERE s.asof <= ?"
    f" ORDER BY m.pool, s.asof DESC, s.snapshot_id DESC, m.code"
)


def _has_tables(conn: sqlite3.Connection) -> bool:
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
        (TABLE_SNAPSHOTS, TABLE_MEMBERS)).fetchone()[0]
    return int(n) == 2


def pool_snapshot(conn: sqlite3.Connection, asof: str) -> dict:
    """`asof` 当日可投的候选池（短/中/长**并集**），带每池各自的快照出处。

    返回：
    - `pools`：池 → `{snapshot_id, snapshot_asof, codes}`（**只有非空的池**）；
    - `codes`：三池并集（排序）；
    - `missing_pools`：`asof` 之前一份快照都没有的池；
    - `available`：至少有一个池有成员（否则任何决策都会被「池外」拒掉）。
    """
    if not _has_tables(conn):
        return {"asof": asof, "available": False, "pools": {}, "codes": [],
                "missing_pools": list(POOLS),
                "reason": (f"库里没有 `{TABLE_SNAPSHOTS}` / `{TABLE_MEMBERS}` —— "
                           f"候选池还没跑过；本页不编数")}
    pools: dict[str, dict] = {}
    chosen: dict[str, int] = {}       # 池 → 选中的 snapshot_id（**每池只取一份**）
    for row in conn.execute(_SQL, (asof,)):
        pool = str(row["pool"])
        if pool not in chosen:
            chosen[pool] = int(row["snapshot_id"])
            pools[pool] = {"snapshot_id": int(row["snapshot_id"]),
                           "snapshot_asof": str(row["asof"]), "codes": []}
        if int(row["snapshot_id"]) == chosen[pool]:
            pools[pool]["codes"].append(str(row["code"]))
    for info in pools.values():
        info["codes"].sort()
    codes = sorted({c for p in pools.values() for c in p["codes"]})
    return {
        "asof": asof,
        "available": bool(codes),
        "pools": pools,
        "codes": codes,
        "missing_pools": [p for p in POOLS if p not in pools],
        "reason": None if codes else
                  (f"{asof} 及之前没有任何候选池快照 → 池外一律拒绝（fail-closed）"),
    }


def pool_codes(conn: sqlite3.Connection, asof: str) -> set[str]:
    return set(pool_snapshot(conn, asof)["codes"])
