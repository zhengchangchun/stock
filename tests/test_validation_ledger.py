"""P44 T3：验证周期台账 —— 前滚迁移、写入、append-only 与可追溯性。

本文件按 Task 顺序累积：
  · Task 4：`plugin_audit` 老 shape 前滚（幂等 + 历史行不动 + doctor 可见）
  · Task 6：三张台账表的读写与触发器
"""

import json
import sqlite3

import pytest

from stocklab.config import limits
from stocklab.plugin import store as plstore
from stocklab.store import migrate, validation
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-22T16:00:00+08:00"
SCRIPT = ("def run(ctx):\n"
          "    return {'score': 50.0, 'pass_flag': True, 'reason': 'r',"
          " 'risk_list': []}\n")


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


# ===========================================================================
# Task 4：plugin_audit 前滚迁移
# ===========================================================================

#: P44 之前 `plugin_audit` 的样子（CHECK 只有 6 个事件、无新触发器）。
#: 用它把新库**退化**回老 shape —— 这是 ERROR_DIARY #41 的反向构造：
#: 只在新 shape 库上测，证明不了「真库（老 shape）能前滚」。
_OLD_AUDIT_DDL = (
    "CREATE TABLE plugin_audit ("
    " audit_id INTEGER PRIMARY KEY AUTOINCREMENT, script_id INTEGER NOT NULL,"
    " action TEXT NOT NULL CHECK (action IN"
    "   ('submit','sandbox_pass','sandbox_fail','approve','reject','archive')),"
    " actor TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL)"
)


def _demote_to_old_shape(conn):
    conn.executescript(
        "DROP TRIGGER trg_plugin_audit_no_update;"
        "DROP TRIGGER trg_plugin_audit_no_delete;"
        "DROP TABLE plugin_audit;"
        + _OLD_AUDIT_DDL + ";"
        "INSERT INTO plugin_audit (script_id, action, actor, reason, created_at)"
        " VALUES (1,'approve','human','看过报告','2026-09-22T10:00:00+08:00');")
    conn.commit()


def test_old_shape_plugin_audit_is_forward_migrated(tmp_path):
    """老 shape 库跑 ensure_schema 后：新事件可写、历史行一字不改、触发器回来。"""
    db = tmp_path / "old.db"
    init_db(db)
    conn = connect(db)
    _demote_to_old_shape(conn)
    assert migrate.plugin_audit_needs_module2_events(conn) is True
    conn.close()

    migrate.ensure_schema(db)

    conn = connect(db)
    assert migrate.plugin_audit_needs_module2_events(conn) is False
    rows = [dict(r) for r in conn.execute(
        "SELECT audit_id, script_id, action, actor, reason, created_at"
        " FROM plugin_audit")]
    assert rows == [{"audit_id": 1, "script_id": 1, "action": "approve",
                     "actor": "human", "reason": "看过报告",
                     "created_at": "2026-09-22T10:00:00+08:00"}], "历史行必须原封不动"
    # 触发器随 DROP 一起没了，迁移必须把它们重建回来
    trigs = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='plugin_audit'")}
    assert trigs == {"trg_plugin_audit_no_update", "trg_plugin_audit_no_delete"}
    # 前滚的意义：新事件从此写得进去
    conn.execute("INSERT INTO plugin_audit (script_id, action, actor, reason,"
                 " created_at) VALUES (1,'start_validation','t','x',?)", (NOW,))
    conn.commit()
    conn.close()


def test_ensure_schema_is_idempotent_for_p44(tmp_path):
    """连跑三次 `ensure_schema`：台账行数不变、触发器不重复、无备份堆积。"""
    db = tmp_path / "x.db"
    migrate.ensure_schema(db)
    conn = connect(db)
    tables = ("validation_cycles", "validation_rounds", "validation_events",
              "plugin_audit")
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in tables}
    trig_before = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]
    conn.close()

    for _ in range(2):
        migrate.ensure_schema(db)

    conn = connect(db)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in tables}
    trig_after = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]
    assert after == before
    assert trig_after == trig_before, "重跑不得重复建触发器"
    assert not (tmp_path / "backups").exists(), "无结构变更时不该产备份（P33）"
    conn.close()


def test_p44_marker_is_registered_for_doctor(tmp_path):
    """新迁移必须登记进 `_KNOWN_MARKERS`，否则 doctor 看不见（ERROR_DIARY #41）。"""
    db = tmp_path / "d.db"
    init_db(db)
    conn = connect(db)
    status = migrate.schema_status(conn)
    assert status["markers"]["p44_plugin_audit_events"]["present"] is True
    assert status["ok"] is True
    conn.close()


# ===========================================================================
# Task 6：台账读写层
# ===========================================================================

def _script(conn):
    return plstore.insert_script(conn, plugin_id="m2_a1", version="1.0.0",
                                 source_text=SCRIPT, note=None, now=NOW)


def _cycle(conn, **over):
    # 注意：默认脚本只在**没显式给 script_id 时**才建。写成
    # `dict(script_id=_script(conn), ...)` 或 `kw.setdefault("script_id", _script(conn))`
    # 都会**先求值**再判断，于是对同一个 (plugin_id, version) 建两次、撞 UNIQUE
    # （本文件初次就是这么红的 —— 惰性默认值必须写成显式分支）。
    kw = dict(account_id="m2-a1-v1", planned_rounds=2,
              planned_days=10, params={}, criteria_text="方向准确率 ≥ 55%",
              start_date="2026-09-23", now=NOW)
    kw.update(over)
    if "script_id" not in kw:
        kw["script_id"] = _script(conn)
    return validation.insert_cycle(conn, **kw)


def test_cycle_records_version_params_and_criteria(conn):
    """台账必须能回答「哪个策略版本 / 哪组参数 / 判据原文」（任务书 T3）。"""
    sid = _script(conn)
    cid = validation.insert_cycle(
        conn, script_id=sid, account_id="m2-a1-v1", planned_rounds=3,
        planned_days=30, params={"top_n": 5, "max_weight": 0.2},
        criteria_text="方向准确率 ≥ 55% 且超额 > 0（预注册 2026-09-22）",
        start_date="2026-09-23", now=NOW)
    row = validation.get_cycle(conn, cid)
    assert row["script_id"] == sid
    assert (row["planned_rounds"], row["planned_days"]) == (3, 30)
    assert json.loads(row["params_json"]) == {"top_n": 5, "max_weight": 0.2}
    assert "方向准确率" in row["criteria_text"]
    assert validation.list_cycles(conn, script_id=sid)[0]["cycle_id"] == cid


def test_out_of_bounds_plan_is_refused_by_the_store(conn):
    """越界方案**进不了台账**（拒绝，不 clamp）—— 与 T2 同一条边界。"""
    sid = _script(conn)
    with pytest.raises(limits.PlanOutOfBounds):
        validation.insert_cycle(conn, script_id=sid, account_id="a",
                                planned_rounds=9, planned_days=365, params={},
                                criteria_text="x", start_date="2026-09-23",
                                now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM validation_cycles").fetchone()[0] == 0


def test_cycle_requires_criteria_text(conn):
    """判据原文空白 = 没记 —— 直接拒绝（D-31：不许事后换口径）。"""
    sid = _script(conn)
    with pytest.raises(ValueError):
        validation.insert_cycle(conn, script_id=sid, account_id="a",
                                planned_rounds=2, planned_days=10, params={},
                                criteria_text="   ", start_date="2026-09-23",
                                now=NOW)


def test_rounds_are_unique_per_cycle(conn):
    cid = _cycle(conn)
    kw = dict(cycle_id=cid, round_no=1, window_start="2026-09-23",
              window_end="2026-10-02", metrics={"hit": 0.5}, note=None, now=NOW)
    validation.insert_round(conn, **kw)
    with pytest.raises(sqlite3.IntegrityError):
        validation.insert_round(conn, **kw)   # 同一轮补跑 = 唯一键挡下，不许静默覆盖
    assert len(validation.list_rounds(conn, cid)) == 1


def test_events_keep_value_and_threshold_separate(conn):
    """触发时的**实测值**与**判据阈值**分列 —— 只留一个数，读者无法判断是否越界。"""
    sid = _script(conn)
    cid = _cycle(conn, script_id=sid)
    validation.insert_event(
        conn, cycle_id=cid, script_id=sid, kind="circuit_breaker",
        at_value=-0.137, threshold=limits.CIRCUIT_BREAKER_DRAWDOWN,
        criteria_text="验证期内自峰值回撤 ≥ 10%", reason="第 3 轮触发", now=NOW)
    ev = validation.list_events(conn, cid)[0]
    assert (ev["at_value"], ev["threshold"]) == (-0.137, 0.10)
    assert ev["kind"] == "circuit_breaker"


def test_unknown_event_kind_is_refused_before_the_db(conn):
    sid = _script(conn)
    cid = _cycle(conn, script_id=sid)
    with pytest.raises(ValueError) as e:
        validation.insert_event(conn, cycle_id=cid, script_id=sid, kind="made_up",
                                at_value=0.0, threshold=0.0, criteria_text="x",
                                reason="y", now=NOW)
    assert "made_up" in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM validation_events").fetchone()[0] == 0


def test_schema_check_also_refuses_unknown_event_kind(conn):
    """写入层拦一道，schema 的 CHECK 再拦一道（纵深防御；也钉住两处允许集合一致）。"""
    sid = _script(conn)
    cid = _cycle(conn, script_id=sid)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO validation_events (cycle_id, script_id, kind,"
            " criteria_text, reason, created_at) VALUES (?,?,?,?,?,?)",
            (cid, sid, "made_up", "x", "y", NOW))


@pytest.mark.parametrize("table", ["validation_cycles", "validation_rounds",
                                   "validation_events"])
def test_ledger_tables_are_append_only(conn, table):
    """ERROR_DIARY #40：空表上的触发器是空转 —— 先写一行证明有东西可拦，再验拦截。"""
    sid = _script(conn)
    cid = _cycle(conn, script_id=sid)
    if table == "validation_rounds":
        validation.insert_round(conn, cycle_id=cid, round_no=1,
                                window_start="2026-09-23", window_end="2026-10-02",
                                metrics={}, note=None, now=NOW)
    elif table == "validation_events":
        validation.insert_event(conn, cycle_id=cid, script_id=sid, kind="freeze",
                                at_value=1.0, threshold=1.0, criteria_text="x",
                                reason="y", now=NOW)
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert n > 0, f"{table} 是空的 —— 触发器测的是空气"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE {table} SET created_at = created_at")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")


def test_ledger_triggers_exist(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    expected = {f"trg_{t}_no_{op}"
                for t in ("validation_cycles", "validation_rounds",
                          "validation_events")
                for op in ("update", "delete")}
    assert expected - names == set()


def test_event_kinds_match_schema_check(conn):
    """`validation.EVENT_KINDS` 与 `validation_events` 的 CHECK 必须逐字相同。

    与 `store.ALLOWED_ACTIONS` ↔ `plugin_audit` 同款（ERROR_DIARY #22 家族）：
    两处真源迟早漂移，钉一条测试让漂移变成读得懂的失败。
    """
    import re
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='validation_events'"
    ).fetchone()[0]
    m = re.search(r"kind\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(kind\s+IN\s*\(([^)]*)\)",
                  sql, re.S)
    assert m, f"没在 schema 里找到 kind 的 CHECK：{sql}"
    in_schema = {s.strip().strip("'") for s in m.group(1).split(",")}
    assert in_schema == set(validation.EVENT_KINDS)
