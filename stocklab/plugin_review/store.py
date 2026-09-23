"""插桩5 复盘台账（P58）：`plugin_reviews` 的读写。

## 只提供 INSERT 与 SELECT

与 `plugin/store.py` 同纪律：不提供 UPDATE / DELETE —— 改错只能新增一行（换
`script_id` 重跑就是新的一行）。表上另有触发器兜底，但**接口层先不提供**是第一道
防线。

## 幂等键 `(asof, script_id)`

`ON CONFLICT (asof, script_id) DO NOTHING` —— **不用 `INSERT OR REPLACE`**：
后者解决唯一冲突的方式是隐式删行再插入，是 P6 那个坑（`recursive_triggers=OFF` 时
连 append-only 触发器都不跑）。这里要的是「同键第二次什么都不做」。

## 为什么 `analysis_json` 存原文

台账要能回答「这条结论是哪版脚本、在哪份输入上算出来的」。桩的输出也是**事实**
（事实是「脚本还没实现」，不是「复盘的结论是空」），所以原样落库、不许重写。
"""

from __future__ import annotations

import json
import sqlite3

TABLE = "plugin_reviews"


def insert_review(conn: sqlite3.Connection, *, asof: str, plugin_id: str,
                  script_id: int, script_version: str, source_sha256: str,
                  status: str, analysis: dict, bad_cases: list,
                  inputs: dict, report_path: str, report_sha256: str,
                  now: str) -> tuple[dict, bool]:
    """写一行复盘台账（幂等）。返回 `(该键上的行, 本次是否新插入)`。

    已存在同 `(asof, script_id)` 的行时：**一个字节都不改**，返回既有行与 `False`。
    """
    cur = conn.execute(
        f"INSERT INTO {TABLE} (asof, plugin_id, script_id, script_version,"
        " source_sha256, status, analysis_json, bad_case_json, inputs_json,"
        " report_path, report_sha256, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (asof, script_id) DO NOTHING",
        (asof, plugin_id, script_id, script_version, source_sha256, status,
         json.dumps(analysis, ensure_ascii=False, sort_keys=True),
         json.dumps(bad_cases, ensure_ascii=False, sort_keys=True),
         json.dumps(inputs, ensure_ascii=False, sort_keys=True),
         report_path, report_sha256, now))
    conn.commit()
    inserted = cur.rowcount == 1
    row = conn.execute(
        f"SELECT * FROM {TABLE} WHERE asof = ? AND script_id = ?",
        (asof, script_id)).fetchone()
    return (dict(row) if row is not None else {}), inserted


def load_reviews(conn: sqlite3.Connection, *, asof: str | None = None) -> list[dict]:
    """读台账（`analysis` / `bad_cases` / `inputs` 解成对象）。"""
    sql = f"SELECT * FROM {TABLE}"
    params: tuple = ()
    if asof is not None:
        sql += " WHERE asof = ?"
        params = (asof,)
    sql += " ORDER BY review_id"
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["analysis"] = json.loads(d.pop("analysis_json"))
        d["bad_cases"] = json.loads(d.pop("bad_case_json"))
        d["inputs"] = json.loads(d.pop("inputs_json"))
        out.append(d)
    return out
