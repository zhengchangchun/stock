"""P44 T1：模块2 状态枚举（D-29）。

主干 5 态语义一个字节不改；新增 `validating` / `frozen` + 4 个事件；
**未知事件拒绝**（不许静默忽略 —— 静默忽略会让状态与事件流不一致，
而且没有任何一层会发现）。
"""

import re
import sqlite3

import pytest

from stocklab.plugin import lifecycle, store
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


def _active(conn, plugin_id="3", version="1.0.0"):
    sid = store.insert_script(conn, plugin_id=plugin_id, version=version,
                              source_text=SCRIPT, note=None, now=NOW)
    lifecycle.record_submit(conn, sid, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=True, reason="ok", now=NOW)
    lifecycle.approve(conn, sid, actor="human", reason="看过报告", now=NOW)
    return sid


# ---------- 主干 5 态一个字节不改 ----------

def test_original_five_states_are_untouched():
    assert lifecycle.STATES[:5] == ("draft", "pending_review", "rejected",
                                    "active", "archived")


def test_two_new_states_are_declared():
    assert set(lifecycle.STATES) == {
        "draft", "pending_review", "rejected", "active", "archived",
        "validating", "frozen"}


# ---------- 新事件折叠 ----------

def test_start_validation_moves_active_to_validating(conn):
    sid = _active(conn)
    assert lifecycle.start_validation(conn, sid, actor="t", reason="进入验证",
                                      now=NOW) == "validating"
    assert lifecycle.script_state(conn, sid) == "validating"


def test_finish_validation_returns_to_active(conn):
    sid = _active(conn)
    lifecycle.start_validation(conn, sid, actor="t", reason="x", now=NOW)
    assert lifecycle.finish_validation(conn, sid, actor="t", reason="验证结束",
                                       now=NOW) == "active"


def test_freeze_then_unfreeze(conn):
    sid = _active(conn)
    lifecycle.start_validation(conn, sid, actor="t", reason="x", now=NOW)
    assert lifecycle.freeze(conn, sid, actor="t", reason="熔断", now=NOW) == "frozen"
    assert lifecycle.unfreeze(conn, sid, actor="human", reason="人工解冻",
                              now=NOW) == "active"


def test_validating_version_is_not_resolved_as_active(conn):
    """钉住**当前**口径（P48 再议）：`active_script_id` 只认 `active`。

    进入 validating 后不再被解析为在役版本 —— P44 无生产调用者，主干链不受影响。
    """
    sid = _active(conn)
    assert lifecycle.active_script_id(conn, "3") == sid
    lifecycle.start_validation(conn, sid, actor="t", reason="x", now=NOW)
    assert lifecycle.active_script_id(conn, "3") is None


# ---------- 前置状态守卫 ----------

def test_start_validation_requires_active(conn):
    sid = store.insert_script(conn, plugin_id="3", version="1.0.0",
                              source_text=SCRIPT, note=None, now=NOW)  # draft
    with pytest.raises(lifecycle.PluginStateError) as e:
        lifecycle.start_validation(conn, sid, actor="t", reason="x", now=NOW)
    assert "draft" in str(e.value)


def test_unfreeze_requires_frozen(conn):
    sid = _active(conn)
    with pytest.raises(lifecycle.PluginStateError):
        lifecycle.unfreeze(conn, sid, actor="t", reason="x", now=NOW)


def test_new_events_require_reason(conn):
    sid = _active(conn)
    with pytest.raises(ValueError):
        lifecycle.start_validation(conn, sid, actor="t", reason="", now=NOW)


# ---------- 未知事件一律拒绝（不许静默忽略） ----------

def test_unknown_event_is_rejected_not_ignored():
    with pytest.raises(lifecycle.PluginStateError) as e:
        lifecycle.apply_event("promote_to_king")
    assert "promote_to_king" in str(e.value)


def test_corrupted_audit_row_makes_folding_raise(conn, monkeypatch):
    """折叠层对未知事件的拒绝必须**有区分力**。

    真实的坏行在本 schema 下造不出来 —— `plugin_audit` 的 append-only 触发器拦
    UPDATE、CHECK 拦写未知 action（这是纵深的第一层，另一条测试已钉住）。
    所以第二层用桩行直接打：**不这么做的话，「折叠会拒绝未知事件」这句话就
    没有任何可失败的证据**（初版这条测试就是想 `UPDATE ... SET action='bogus'`
    而被 CHECK 挡下，实测报 `CHECK constraint failed: action IN (...)`）。
    """
    sid = _active(conn)
    bogus = dict(store.list_audit(conn, script_id=sid)[-1])
    bogus["action"] = "bogus"
    monkeypatch.setattr(store, "list_audit",
                        lambda conn, *, script_id=None: [bogus])
    with pytest.raises(lifecycle.PluginStateError) as e:
        lifecycle.script_state(conn, sid)
    assert "bogus" in str(e.value)


def test_write_layer_refuses_unknown_action_even_bypassing_the_state_machine(conn):
    """纵深：`store.insert_audit` 自己就拦未知 action（不依赖 CHECK 兜底，
    报的是点名错误而不是 sqlite 的约束名 —— 同 ERROR_DIARY #49）。"""
    sid = _active(conn)
    with pytest.raises(ValueError) as e:
        store.insert_audit(conn, script_id=sid, action="bogus", actor="t",
                           reason="x", now=NOW)
    assert "bogus" in str(e.value)


# ---------- 事件白名单与 schema CHECK 同源 ----------

def test_action_whitelist_matches_schema_check(conn):
    """`store.ALLOWED_ACTIONS` 与 schema 里的 CHECK 必须逐字相同（ERROR_DIARY #22 家族：
    两处真源迟早漂移，所以钉一条测试让漂移变成读得懂的失败）。"""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='plugin_audit'"
    ).fetchone()[0]
    m = re.search(
        r"action\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(action\s+IN\s*\(([^)]*)\)", sql, re.S)
    assert m, f"没在 schema 里找到 action 的 CHECK：{sql}"
    in_schema = {s.strip().strip("'") for s in m.group(1).split(",")}
    assert in_schema == set(store.ALLOWED_ACTIONS)


# ---------- approve 闸门不许放宽 ----------

def test_approve_still_refuses_validating_and_frozen(conn):
    sid = _active(conn)
    lifecycle.start_validation(conn, sid, actor="t", reason="x", now=NOW)
    with pytest.raises(lifecycle.PluginStateError):
        lifecycle.approve(conn, sid, actor="t", reason="x", now=NOW)
    lifecycle.freeze(conn, sid, actor="t", reason="x", now=NOW)
    with pytest.raises(lifecycle.PluginStateError):
        lifecycle.approve(conn, sid, actor="t", reason="x", now=NOW)


def test_audit_table_still_append_only(conn):
    """重建迁移不许把触发器弄丢（ERROR_DIARY #40：先写一行再验拦截）。"""
    sid = _active(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE plugin_audit SET actor = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM plugin_audit")
