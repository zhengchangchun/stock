"""Task 6：状态机（设计文档 §9.1）。状态由事件流推导，不存列。"""

import pytest

from stocklab.plugin import lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
SCORE_SCRIPT = (
    "def run(ctx):\n"
    "    return {'score': 50.0, 'pass_flag': True, 'reason': 'r',"
    " 'risk_list': []}\n"
)


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def _new(conn, version="1.0.0", plugin_id="3", text=SCORE_SCRIPT):
    return store.insert_script(conn, plugin_id=plugin_id, version=version,
                               source_text=text, note=None, now=NOW)


def test_fresh_script_is_draft(conn):
    assert lifecycle.script_state(conn, _new(conn)) == "draft"


def test_submit_then_sandbox_pass_is_pending_review(conn):
    sid = _new(conn)
    lifecycle.record_submit(conn, sid, actor="tester", now=NOW)
    assert lifecycle.script_state(conn, sid) == "draft"    # 沙盒还没跑
    lifecycle.record_sandbox(conn, sid, passed=True, reason="样本不足", now=NOW)
    assert lifecycle.script_state(conn, sid) == "pending_review"


def test_sandbox_fail_is_rejected(conn):
    sid = _new(conn)
    lifecycle.record_submit(conn, sid, actor="tester", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=False, reason="契约不符", now=NOW)
    assert lifecycle.script_state(conn, sid) == "rejected"


def test_approve_makes_it_active(conn):
    sid = _new(conn)
    lifecycle.record_submit(conn, sid, actor="tester", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=True, reason="ok", now=NOW)
    lifecycle.approve(conn, sid, actor="claude", reason="看过报告", now=NOW)
    assert lifecycle.script_state(conn, sid) == "active"
    assert lifecycle.active_script_id(conn, "3") == sid


def test_approving_new_version_archives_old(conn):
    a = _new(conn, version="1.0.0")
    lifecycle.record_submit(conn, a, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, a, passed=True, reason="ok", now=NOW)
    lifecycle.approve(conn, a, actor="t", reason="ok", now=NOW)

    b = _new(conn, version="1.1.0")
    lifecycle.record_submit(conn, b, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, b, passed=True, reason="ok", now=NOW)
    lifecycle.approve(conn, b, actor="t", reason="ok", now=NOW)

    assert lifecycle.script_state(conn, a) == "archived"
    assert lifecycle.script_state(conn, b) == "active"
    assert lifecycle.active_script_id(conn, "3") == b


def test_rollback_is_approve_on_archived(conn):
    """回滚不做独立命令 —— 对 archived 版本 approve 即可（设计文档 §9.1）。"""
    a = _new(conn, version="1.0.0")
    lifecycle.record_submit(conn, a, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, a, passed=True, reason="ok", now=NOW)
    lifecycle.approve(conn, a, actor="t", reason="ok", now=NOW)

    b = _new(conn, version="1.1.0")
    lifecycle.record_submit(conn, b, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, b, passed=True, reason="ok", now=NOW)
    lifecycle.approve(conn, b, actor="t", reason="ok", now=NOW)

    lifecycle.approve(conn, a, actor="t", reason="回滚：1.1.0 疑似过拟合", now=NOW)
    assert lifecycle.script_state(conn, a) == "active"
    assert lifecycle.script_state(conn, b) == "archived"
    assert lifecycle.active_script_id(conn, "3") == a


def test_illegal_transition_draft_to_active(conn):
    """跳过沙盒直接上线必须被拒。"""
    sid = _new(conn)
    with pytest.raises(lifecycle.PluginStateError) as e:
        lifecycle.approve(conn, sid, actor="t", reason="x", now=NOW)
    assert "draft" in str(e.value)


def test_illegal_transition_rejected_to_active(conn):
    sid = _new(conn)
    lifecycle.record_submit(conn, sid, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=False, reason="bad", now=NOW)
    with pytest.raises(lifecycle.PluginStateError):
        lifecycle.approve(conn, sid, actor="t", reason="x", now=NOW)


def test_reject_requires_pending_review(conn):
    sid = _new(conn)
    with pytest.raises(lifecycle.PluginStateError):
        lifecycle.reject(conn, sid, actor="t", reason="x", now=NOW)


def test_approve_requires_reason(conn):
    sid = _new(conn)
    lifecycle.record_submit(conn, sid, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=True, reason="ok", now=NOW)
    with pytest.raises(ValueError):
        lifecycle.approve(conn, sid, actor="t", reason="", now=NOW)


def test_reject_requires_reason(conn):
    """reject 同样要求 reason 非空；须先到达 pending_review 才能触发 reject 路径。"""
    sid = _new(conn)
    lifecycle.record_submit(conn, sid, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=True, reason="ok", now=NOW)
    with pytest.raises(ValueError):
        lifecycle.reject(conn, sid, actor="t", reason="", now=NOW)


def test_approve_on_active_raises(conn):
    """active → approve 必须拒绝（approve 的合法前置状态只有 pending_review / archived）。"""
    sid = _new(conn)
    lifecycle.record_submit(conn, sid, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=True, reason="ok", now=NOW)
    lifecycle.approve(conn, sid, actor="t", reason="ok", now=NOW)
    assert lifecycle.script_state(conn, sid) == "active"
    with pytest.raises(lifecycle.PluginStateError):
        lifecycle.approve(conn, sid, actor="t", reason="再批一次", now=NOW)


def test_no_active_plugin_raises(conn):
    with pytest.raises(lifecycle.NoActivePlugin):
        lifecycle.active_script_id(conn, "3", required=True)


def test_call_active_runs_the_active_version(conn):
    sid = _new(conn)
    lifecycle.record_submit(conn, sid, actor="t", now=NOW)
    lifecycle.record_sandbox(conn, sid, passed=True, reason="ok", now=NOW)
    lifecycle.approve(conn, sid, actor="t", reason="ok", now=NOW)
    assert lifecycle.call_active(conn, "3", {})["score"] == 50.0


def test_call_active_without_active_raises(conn):
    _new(conn)                                  # 只有 draft，没有 active
    with pytest.raises(lifecycle.NoActivePlugin):
        lifecycle.call_active(conn, "3", {})


def test_two_active_versions_is_a_bug(conn):
    """同一 plugin_id 下出现两个 active 是结构错误，必须炸而不是随便挑一个。"""
    a = _new(conn, version="1.0.0")
    for sid in (a,):
        lifecycle.record_submit(conn, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(conn, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(conn, sid, actor="t", reason="ok", now=NOW)
    # 手工伪造第二个 active：绕过 approve 直接写审计事件
    b = _new(conn, version="1.1.0")
    store.insert_audit(conn, script_id=b, action="approve", actor="t",
                       reason="伪造", now=NOW)
    with pytest.raises(lifecycle.PluginStateError) as e:
        lifecycle.active_script_id(conn, "3")
    assert "两个" in str(e.value) or "2" in str(e.value)


def test_approve_is_not_reachable_from_non_cli_code():
    """源码扫描：除 CLI 与 lifecycle 自身外，没有任何模块调用 approve/write。

    这条钉住设计文档 §9.3「approve 只能由人工 CLI 触发」。

    已知局限：扫描匹配字面串 `lifecycle.approve(` / `lifecycle.reject(`，
    因此无法捕获 `from stocklab.plugin.lifecycle import approve; approve(...)` 或
    模块别名（`lc = lifecycle; lc.approve(...)`）等间接调用形式。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "stocklab"
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        # 只允许 CLI 层与状态机自身：前者是人工入口，后者是状态机的定义处
        if rel.startswith("cli/") or rel == "plugin/lifecycle.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "lifecycle.approve(" in text or "lifecycle.reject(" in text:
            offenders.append(rel)
    assert not offenders, f"approve/reject 只允许从 CLI 调用，违规文件：{offenders}"
