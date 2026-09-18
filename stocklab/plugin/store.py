"""插桩版本库读写：`plugin_scripts` / `plugin_audit` / `plugin_backtests` 三表。

## 只提供 INSERT 与 SELECT

不提供 UPDATE / DELETE —— 改错只能新增一行。表上另有触发器兜底
（`schema.sql` 的 `trg_plugin_*`），但**接口层先不提供**是第一道防线，
这样「想改一行」的调用方在 IDE 里就找不到方法，而不是等到运行时
撞触发器。

## `metrics` 以 JSON 落库，读回时解成 dict

报告里要用到明细，但列会随实验演进；JSON 是这里唯一不过度设计的选择。

## `source_sha256` 是版本指纹

同一个 `plugin_id` 下，同一份文本必须得到同一个 hash。`(plugin_id, version)`
有 UNIQUE 约束管「版本号不重复」，hash 管「内容对不对得上」—— 两者是不同的
问题，都要有。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

TABLE_SCRIPTS = "plugin_scripts"
TABLE_AUDIT = "plugin_audit"
TABLE_BACKTESTS = "plugin_backtests"


def source_sha256(source_text: str) -> str:
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


# ---------- 脚本 ----------

def insert_script(conn: sqlite3.Connection, *, plugin_id: str, version: str,
                  source_text: str, note: str | None, now: str) -> int:
    cur = conn.execute(
        f"INSERT INTO {TABLE_SCRIPTS} (plugin_id, version, source_text,"
        " source_sha256, created_at, note) VALUES (?,?,?,?,?,?)",
        (plugin_id, version, source_text, source_sha256(source_text), now, note))
    conn.commit()
    return int(cur.lastrowid)


def get_script(conn: sqlite3.Connection, script_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT * FROM {TABLE_SCRIPTS} WHERE script_id = ?",
        (script_id,)).fetchone()
    return None if row is None else dict(row)


def list_scripts(conn: sqlite3.Connection,
                 *, plugin_id: str | None = None) -> list[dict]:
    if plugin_id is None:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_SCRIPTS} ORDER BY script_id").fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_SCRIPTS} WHERE plugin_id = ?"
            " ORDER BY script_id", (plugin_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------- 审核事件 ----------

def insert_audit(conn: sqlite3.Connection, *, script_id: int, action: str,
                 actor: str, reason: str | None, now: str) -> int:
    if not actor:
        raise ValueError("actor 不许为空 —— 审计链上「谁批的」不能是空的")
    cur = conn.execute(
        f"INSERT INTO {TABLE_AUDIT} (script_id, action, actor, reason,"
        " created_at) VALUES (?,?,?,?,?)",
        (script_id, action, actor, reason, now))
    conn.commit()
    return int(cur.lastrowid)


def list_audit(conn: sqlite3.Connection,
               *, script_id: int | None = None) -> list[dict]:
    if script_id is None:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_AUDIT} ORDER BY audit_id").fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_AUDIT} WHERE script_id = ?"
            " ORDER BY audit_id", (script_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------- 回测报告 ----------

def insert_backtest(conn: sqlite3.Connection, *, candidate_script_id: int,
                    baseline_script_id: int | None, pool: str,
                    window_start: str, window_end: str, metrics: dict,
                    verdict: str, overfit_flag: str | None,
                    report_sha256: str, now: str) -> int:
    cur = conn.execute(
        f"INSERT INTO {TABLE_BACKTESTS} (candidate_script_id,"
        " baseline_script_id, pool, window_start, window_end, metrics_json,"
        " verdict, overfit_flag, report_sha256, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (candidate_script_id, baseline_script_id, pool, window_start,
         window_end, json.dumps(metrics, ensure_ascii=False, sort_keys=True),
         verdict, overfit_flag, report_sha256, now))
    conn.commit()
    return int(cur.lastrowid)


def load_backtests(conn: sqlite3.Connection,
                   *, script_id: int | None = None) -> list[dict]:
    if script_id is None:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_BACKTESTS} ORDER BY backtest_id").fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_BACKTESTS} WHERE candidate_script_id = ?"
            " ORDER BY backtest_id", (script_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["metrics"] = json.loads(d.pop("metrics_json"))
        out.append(d)
    return out
