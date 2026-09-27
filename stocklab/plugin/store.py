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
TABLE_RESOURCE_EVENTS = "plugin_resource_events"

#: `plugin_audit.action` 的白名单 —— 与 `schema.sql` 的 CHECK **必须逐字相同**
#: （`tests/test_plugin_lifecycle_module2.py::test_action_whitelist_matches_schema_check`
#: 钉住两者一致）。放在这里而不是状态机里：写入侧的校验不该依赖读取侧。
ALLOWED_ACTIONS: frozenset[str] = frozenset({
    "submit", "sandbox_pass", "sandbox_fail", "approve", "reject", "archive",
    "start_validation", "finish_validation", "freeze", "unfreeze",
})


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
    if action not in ALLOWED_ACTIONS:
        raise ValueError(
            f"未知审计事件 {action!r} —— 事件类型必须先在 "
            f"store.ALLOWED_ACTIONS 与 schema.sql 的 CHECK 里登记"
            "（不许静默忽略：状态与事件流一旦不一致，没有任何一层会发现）")
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
    """返回回测记录列表。

    ``script_id`` 过滤的是 ``candidate_script_id`` 列，**不包含**该脚本作为
    ``baseline_script_id`` 的回测记录。Task 7 调用时请注意此语义。
    """
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


# ---------- 资源事件（P93） ----------

#: `plugin_resource_events.outcome` 的白名单 —— 与 `schema.sql` 的 CHECK **必须
#: 逐字相同**，也与 `plugin/runtime.py::load_script(on_call=...)` 的取值域相同
#: （`ALLOWED_ACTIONS` 的同一条纪律：写入侧的校验不该依赖读取侧）。
ALLOWED_OUTCOMES: frozenset[str] = frozenset({"ok", "timeout", "resource"})


#: INSERT 语句写成**裸字面量**（不用 `TABLE_RESOURCE_EVENTS` 插件）：D5 要求
#: 「写入口只有一个」，而 `tests/test_plugin_resource_store.py` 用源码扫描钉住
#: 「`INSERT INTO plugin_resource_events` 只出现在本文件」—— 拼成 f-string 会让
#: 那条扫描扫不到，等于把唯一的护栏变成空转。
_INSERT_RESOURCE_EVENT_SQL = (
    "INSERT INTO plugin_resource_events (plugin_id, at, outcome,"
    " rss_delta_bytes, rss_peak_bytes, duration_ms, detail)"
    " VALUES (?,?,?,?,?,?,?)")


def record_resource_event(conn: sqlite3.Connection, *, plugin_id: str,
                          outcome: str, rss_delta_bytes: int,
                          rss_peak_bytes: int, duration_ms: float | None,
                          detail: str | None, now: str) -> int:
    """**唯一**写入口：插桩调用结束的一条资源读数（P93 / D5）。

    `outcome` 走 `ALLOWED_OUTCOMES` 白名单（未知词直接抛，不静默忽略）——
    schema 的 CHECK 是最后一道，不是第一道。
    """
    if outcome not in ALLOWED_OUTCOMES:
        raise ValueError(
            f"未知资源结局 {outcome!r} —— 必须先在 store.ALLOWED_OUTCOMES 与 "
            "schema.sql 的 CHECK 里登记（不许静默忽略：这张表是资源越界唯一的事后证据）")
    cur = conn.execute(
        _INSERT_RESOURCE_EVENT_SQL,
        (plugin_id, now, outcome, int(rss_delta_bytes), int(rss_peak_bytes),
         duration_ms, detail))
    conn.commit()
    return int(cur.lastrowid)


def list_resource_events(conn: sqlite3.Connection,
                         *, limit: int | None = None) -> list[dict]:
    """最近的事件（`event_id` **降序** = 最新在前）。供巡检的 `sandbox_guard` 用。"""
    sql = f"SELECT * FROM {TABLE_RESOURCE_EVENTS} ORDER BY event_id DESC"
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (int(limit),)
    return [dict(r) for r in conn.execute(sql, params)]
