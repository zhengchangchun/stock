"""P93 T4：`plugin_resource_events` 表 ＋ 触发器 ＋ 唯一写入口 ＋ submit 预检接线。

要钉死的六件事：

1. **append-only**：UPDATE / DELETE 一律被触发器拒（照 `plugin_*` 同族写法）；
2. **写入口只有一个**：`stocklab/plugin/store.py::record_resource_event` ——
   全仓 `INSERT INTO plugin_resource_events` 只准出现在那一个文件里；
3. **前滚幂等**：`ensure_schema` 建表；重入不产变更；doctor marker 登记；
4. **submit 预检接线**：唯一会执行未审脚本的路径要真的落一行
   （`ok` 与 `resource` 两种结局都有用例）；
5. **既有精确 except 点同时接住两者**：`cli/candidate.py` 那处按
   `PluginTimeout` 精确匹配的 handler 必须也列上 `PluginResourceError`，
   并且行为上真的走同一条通道（exit 1 + 点名）；
6. 表结构逐列对（D5 的列名与 NOT NULL 语义）。
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
from types import SimpleNamespace

import pytest

from stocklab.cli import candidate as cand_cli
from stocklab.cli import plugin as plugin_cli
from stocklab.config import limits
from stocklab.plugin import resources, runtime, sandbox, store
from stocklab.store.db import connect
from stocklab.store.migrate import ensure_schema, init_db
from stocklab.store import migrate

ROOT = pathlib.Path(__file__).resolve().parents[1] / "stocklab"
TABLE = "plugin_resource_events"

COUNTING = (
    "def run(ctx):\n"
    "    return {'score': 1.0, 'pass_flag': True, 'reason': 'r', 'risk_list': []}\n"
)


@pytest.fixture
def db(tmp_path) -> pathlib.Path:
    path = tmp_path / "p93.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db):
    c = connect(db)
    yield c
    c.close()


def _events(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM {TABLE} ORDER BY event_id")]


# ---------- ① append-only ----------

def test_update_is_rejected(conn):
    store.record_resource_event(
        conn, plugin_id="1", outcome="ok", rss_delta_bytes=10,
        rss_peak_bytes=20, duration_ms=1.0, detail=None, now="2026-09-27T00:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE {TABLE} SET outcome = 'resource'")


def test_delete_is_rejected(conn):
    store.record_resource_event(
        conn, plugin_id="1", outcome="ok", rss_delta_bytes=10,
        rss_peak_bytes=20, duration_ms=1.0, detail=None, now="2026-09-27T00:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {TABLE}")


# ---------- ② 唯一写入口 ----------

def test_insert_into_the_table_appears_in_exactly_one_module():
    hits = []
    for path in sorted((ROOT).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "INSERT INTO plugin_resource_events" in text:
            hits.append(path.relative_to(ROOT).as_posix())
    assert hits == ["plugin/store.py"], f"写入口不唯一：{hits}"


# ---------- ③ 表结构与前滚 ----------

def test_columns_match_the_locked_shape(conn):
    cols = {r[1]: r for r in conn.execute(f"PRAGMA table_info({TABLE})")}
    assert set(cols) == {"event_id", "plugin_id", "at", "outcome",
                         "rss_delta_bytes", "rss_peak_bytes", "duration_ms",
                         "detail"}
    for name in ("plugin_id", "at", "outcome", "rss_delta_bytes",
                 "rss_peak_bytes"):
        assert cols[name][3] == 1, f"{name} 应当是 NOT NULL"
    assert cols["duration_ms"][3] == 0 and cols["detail"][3] == 0


def test_outcome_check_rejects_an_unknown_word(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO {TABLE} (plugin_id, at, outcome, rss_delta_bytes,"
            " rss_peak_bytes) VALUES ('1','t','exploded',0,0)")


def test_ensure_schema_is_idempotent_and_returns_empty_on_reentry(db):
    assert ensure_schema(db) == []
    c = connect(db)
    try:
        assert c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,)).fetchone()[0] == 1
    finally:
        c.close()


def test_doctor_marker_is_registered(conn):
    status = migrate.schema_status(conn)
    assert "p93_plugin_resource_events" in status["markers"]
    assert status["markers"]["p93_plugin_resource_events"]["present"] is True


def test_migrate_p93_is_reentrant(conn):
    assert migrate.migrate_p93_resource_events(conn) == []


def test_migrate_p93_restores_missing_triggers(conn):
    conn.execute("DROP TRIGGER trg_plugin_resource_no_update")
    conn.commit()
    assert migrate.resource_events_need_p93(conn) is True
    assert migrate.migrate_p93_resource_events(conn) == [
        "plugin_resource_events.triggers"]
    assert migrate.resource_events_need_p93(conn) is False


def test_migrate_p93_is_noop_without_the_table():
    """新表：表不存在时 `executescript` 会建出，不算 pending（P80 同款）。"""
    c = sqlite3.connect(":memory:")
    try:
        assert migrate.resource_events_need_p93(c) is False
        assert migrate.migrate_p93_resource_events(c) == []
    finally:
        c.close()


# ---------- ④ 唯一写入口的行为 ----------

def test_record_resource_event_writes_a_row(conn):
    rid = store.record_resource_event(
        conn, plugin_id="m2_a1", outcome="resource", rss_delta_bytes=999,
        rss_peak_bytes=1234, duration_ms=0.5, detail="越界",
        now="2026-09-27T10:00:00Z")
    rows = _events(conn)
    assert len(rows) == 1
    assert rows[0]["event_id"] == rid
    assert rows[0]["plugin_id"] == "m2_a1"
    assert rows[0]["outcome"] == "resource"
    assert rows[0]["rss_delta_bytes"] == 999
    assert rows[0]["rss_peak_bytes"] == 1234
    assert rows[0]["duration_ms"] == 0.5
    assert rows[0]["at"] == "2026-09-27T10:00:00Z"
    assert rows[0]["detail"] == "越界"


def test_record_resource_event_rejects_an_unknown_outcome(conn):
    with pytest.raises(ValueError) as e:
        store.record_resource_event(
            conn, plugin_id="1", outcome="exploded", rss_delta_bytes=0,
            rss_peak_bytes=0, duration_ms=None, detail=None, now="t")
    assert "exploded" in str(e.value)


def test_record_resource_event_accepts_the_three_known_outcomes(conn):
    for outcome in ("ok", "timeout", "resource"):
        store.record_resource_event(
            conn, plugin_id="1", outcome=outcome, rss_delta_bytes=0,
            rss_peak_bytes=0, duration_ms=None, detail=None, now="t")
    assert [r["outcome"] for r in _events(conn)] == ["ok", "timeout", "resource"]


# ---------- ⑤ submit 预检接线 ----------

def _boom(*_a, **_k):
    raise RuntimeError("沙盒桩：本用例只验预检那一段")


def test_submit_precheck_records_an_ok_row(conn, monkeypatch):
    monkeypatch.setattr(sandbox, "run_sandbox", _boom)
    passed, reason = plugin_cli._run_sandbox(
        conn, script_id=1, plugin_id="1", source_text=COUNTING,
        now="2026-09-27T00:00:00Z")
    assert passed is False and "沙盒" in reason        # 桩把它挡住了
    rows = _events(conn)                              # 但预检那一行已经落了
    assert [r["outcome"] for r in rows] == ["ok"]
    assert rows[0]["plugin_id"] == "1"
    assert rows[0]["at"]


def test_submit_precheck_records_a_resource_row_when_the_gate_trips(conn,
                                                                   monkeypatch):
    monkeypatch.setattr(resources, "peak_rss_bytes",
                        lambda **kw: limits.PLUGIN_PROCESS_RSS_LIMIT_BYTES + 1)
    passed, reason = plugin_cli._run_sandbox(
        conn, script_id=1, plugin_id="1", source_text=COUNTING,
        now="2026-09-27T00:00:00Z")
    assert passed is False
    assert "PluginResourceError" in reason
    rows = _events(conn)
    assert [r["outcome"] for r in rows] == ["resource"]


# ---------- ⑥ 既有精确 except 点同时接住两者 ----------

def _timeout_except_tuple() -> ast.Tuple:
    tree = ast.parse((ROOT / "cli" / "candidate.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if isinstance(node.type, ast.Tuple) and any(
                isinstance(e, ast.Attribute) and e.attr == "PluginTimeout"
                for e in node.type.elts):
            return node.type
    raise AssertionError("cli/candidate.py 里找不到按 PluginTimeout 精确匹配的 handler")


def test_candidate_except_point_catches_both_siblings():
    attrs = {e.attr for e in _timeout_except_tuple().elts
             if isinstance(e, ast.Attribute)}
    assert "PluginTimeout" in attrs
    assert "PluginResourceError" in attrs, (
        "PluginResourceError 是 PluginTimeout 的兄弟：精确匹配的那一处必须同时接住它，"
        "否则资源越界会从「脚本跑不出来」这条通道漏出去")


def test_candidate_review_maps_resource_error_to_exit_one(tmp_path, monkeypatch,
                                                          capsys):
    """行为证明：同一个 handler 接住资源越界 ⇒ exit 1 ＋ 点名，与超时同一条路。"""
    path = tmp_path / "review.db"
    init_db(path)

    def _raise(*_a, **_k):
        raise runtime.PluginResourceError("插桩 5 的这一次调用越界（测试桩）")

    monkeypatch.setattr(cand_cli, "run_review", _raise)
    rc = cand_cli.cmd_candidate_review(SimpleNamespace(
        db=str(path), asof="2026-09-21", now="2026-09-27T00:00:00Z",
        report_dir=None))
    err = capsys.readouterr().err
    assert rc == 1
    assert "PluginResourceError" in err


def test_resource_error_is_not_silently_swallowed_by_plugin_submit(conn,
                                                                  monkeypatch):
    """`_run_sandbox` 的兜底 `except Exception` 也必须点名，不许静默吞。"""
    monkeypatch.setattr(resources, "peak_rss_bytes",
                        lambda **kw: limits.PLUGIN_PROCESS_RSS_LIMIT_BYTES + 1)
    passed, reason = plugin_cli._run_sandbox(
        conn, script_id=1, plugin_id="1", source_text=COUNTING, now="t")
    assert passed is False
    assert "资源" in reason or "PluginResourceError" in reason
