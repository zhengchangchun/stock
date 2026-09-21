"""模块1 页面取数（候选池）：**只读** `candidate_*` 三表 + `instruments` 取名。

## 为什么是独立的类，不是 `Lab` 的一个方法

两者的「当前」不是同一个东西：

| | `Lab`（模块2） | `CandLab`（本模块） |
|---|---|---|
| `asof` | **页面口径的今天**（`--asof` 可固定） | **快照自己的日期**（历史某天） |
| 由谁决定 | 启动参数 | 页面上的选择器 |

把两套 asof 塞进一个对象，迟早在某处用错 —— 用快照 asof 去签 `_token`
就是**永久 403**（设计文档 §3.1，实测 `mint("2026-09-18")` 配
`verify(today=今日)` 恒 `False`）。所以这里**不接收** `asof` 参数。

## 不产生新口径

- 快照一律走 `candidate.snapshot.load_snapshot` / `find_snapshot`；
- 排序沿用 `load_snapshot` 的 `(pool, adj_score DESC, code)` / `(stage, code)`，
  这里**不重排**（重排就是第二个口径）；
- 唯一新写的 SQL 是「有快照的 `(asof, run_kind)` 列表」，它落在
  `candidate/snapshot.py::list_snapshot_keys`，不在页面里。

## 名称来自 `instruments`，取不到就不编

成员表没有 `name` 列。名称只从 `instruments` 取 `code → name`，
**查不到就返回 `None`，由渲染层显示代码本身**（不猜、不补）。

## 时间戳

`candidate_snapshots.created_at` 与 `candidate_members.entered_at` 是 **UTC**
（CLI 用 `datetime.now(timezone.utc)` 写）。这里统一转 `Asia/Shanghai`，
转换用的 `TZ` 与 `data.now_iso()` **同一处** —— 否则同一屏里会出现差 8 小时的两个时间。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from stocklab.candidate import snapshot
from stocklab.candidate.pools import ALL_POOLS
from stocklab.candidate.report import report_path
from stocklab.candidate.seeds import SEED_CODES
from stocklab.config import paths
from stocklab.labweb.data import TZ
from stocklab.store.db import connect


@dataclass(frozen=True)
class SnapshotKey:
    """一条快照的**唯一键**：`(asof, run_kind)`（schema 里的 UNIQUE 就是这个）。"""

    asof: str
    run_kind: str

    @property
    def label(self) -> str:
        return f"{self.asof} · {self.run_kind}"


def to_local(ts: str | None) -> str | None:
    """UTC ISO 字符串 → `YYYY-MM-DD HH:MM`（Asia/Shanghai）。

    认不出的字符串**原样返回**（宁可按原样显示，也不静默丢掉一个时间）。
    没有时区信息的按 UTC 解释 —— 写库的调用方就是用 UTC 写的。
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def risk_items(raw: str | None) -> tuple[list | None, str]:
    """`risk_json` → `(条目列表 | None, 渲染口径)`。

    `"[]"` 是**已知的「无风险项」**（走插桩4 时 `risk_out` 为空），
    渲染成「无」而不是「0 条」—— 与既有「没有数的地方不写 0」的规矩一致。
    解不开（或不是列表）→ `None`，渲染层明说「解析失败」，**不静默吞**。
    """
    if raw is None or raw == "":
        return [], "empty"
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return None, "bad"
    if not isinstance(items, list):
        return None, "bad"
    return items, ("empty" if not items else "items")


class CandLab:
    """模块1 的只读门面。线程安全（每次请求各自开连接）。"""

    def __init__(self, db_path: Path | str, *,
                 out_dir: Path | str | None = None) -> None:
        self.db_path = Path(db_path)
        #: 报告落盘目录（与 CLI 默认值同一处定义，见 `candidate/report.report_path`）
        self.out_dir = Path(out_dir) if out_dir else paths.REPORT_DIR / "candidate"

    # ---------- 基础设施 ----------

    def exists(self) -> bool:
        return self.db_path.exists()

    @contextmanager
    def conn(self):
        c = connect(self.db_path)
        try:
            yield c
        finally:
            c.close()

    def report_path(self, key: SnapshotKey) -> Path:
        return report_path(self.out_dir, key.asof, key.run_kind)

    # ---------- 页面数据 ----------

    def keys(self) -> list[SnapshotKey]:
        with self.conn() as c:
            return [SnapshotKey(a, k) for a, k in snapshot.list_snapshot_keys(c)]

    def view(self, *, asof: str | None = None,
             run_kind: str | None = None) -> dict:
        """页面要的全部数据。**请求的组合不存在时回落到最新一条并置 `stale`**。

        回落而不是报错：库里没有这条快照是「你选的组合没了」，不是「系统坏了」；
        但**必须让页面说出来**（`stale=True`），否则用户看的是另一条快照却不自知。
        """
        out: dict = {
            "db_path": str(self.db_path),
            "db_exists": self.exists(),
            "keys": [],
            "chosen": None,
            "requested": None,
            "stale": False,
            "snapshot": None,
            "pools": [],
            "rejects": [],
            "n_members": 0,
            "n_rejects": 0,
            "n_seed": len(SEED_CODES),
            "seed_from": "SEED_CODES（当前常量）",
            "missing": [],
            "report_path": None,
        }
        if not out["db_exists"]:
            return out

        with self.conn() as c:
            keys = [SnapshotKey(a, k) for a, k in snapshot.list_snapshot_keys(c)]
            out["keys"] = keys
            if not keys:
                return out

            want = SnapshotKey(asof, run_kind) if (asof and run_kind) else None
            if want is None and asof:
                # 只给了 asof（手编 URL）：取该 asof 下**最新**的一条
                want = next((k for k in keys if k.asof == asof), None)
            out["requested"] = want

            chosen = None
            if want is not None:
                chosen = next((k for k in keys if k == want), None)
            if chosen is None:
                chosen = keys[0]
                out["stale"] = want is not None
            out["chosen"] = chosen

            snapshot_id = snapshot.find_snapshot(
                c, asof=chosen.asof, run_kind=chosen.run_kind)
            loaded = snapshot.load_snapshot(c, snapshot_id)
            snap = dict(loaded["snapshot"])
            snap["created_local"] = to_local(snap.get("created_at"))
            out["snapshot"] = snap
            out["report_path"] = str(self.report_path(chosen))

            names = {r["code"]: r["name"]
                     for r in c.execute("SELECT code, name FROM instruments")}

            members = []
            for m in loaded["members"]:
                items, state = risk_items(m["risk_json"])
                members.append({
                    **m,
                    "name": names.get(m["code"]),
                    "risk_items": items,
                    "risk_state": state,
                    "entered_local": to_local(m.get("entered_at")),
                })
            out["pools"] = [
                {"pool": p, "rows": [m for m in members if m["pool"] == p]}
                for p in ALL_POOLS]
            out["n_members"] = len(members)

            out["rejects"] = [dict(r) for r in loaded["rejects"]]
            out["n_rejects"] = len(out["rejects"])

            seen = {m["code"] for m in members} | {r["code"] for r in out["rejects"]}
            out["missing"] = [{"code": code, "name": names.get(code)}
                              for code in sorted(set(SEED_CODES) - seen)]
        return out


__all__ = ["CandLab", "SnapshotKey", "risk_items", "to_local"]
