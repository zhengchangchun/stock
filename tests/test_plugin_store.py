"""Task 1：6 张新表存在、可写、且 append-only。"""

import hashlib

import pytest

from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"

NEW_TABLES = (
    "plugin_scripts", "plugin_audit", "plugin_backtests",
    "candidate_snapshots", "candidate_members", "candidate_rejects",
)


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


@pytest.mark.parametrize("table", NEW_TABLES)
def test_new_table_exists(conn, table):
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    assert row[0] == 1, f"{table} 未建出"


def test_plugin_scripts_accepts_insert(conn):
    conn.execute(
        "INSERT INTO plugin_scripts (plugin_id, version, source_text,"
        " source_sha256, created_at) VALUES ('3','1.0.0','def run(ctx): pass',"
        " 'abc', ?)", (NOW,))
    assert conn.execute("SELECT COUNT(*) FROM plugin_scripts").fetchone()[0] == 1


def test_plugin_scripts_is_append_only(conn):
    conn.execute(
        "INSERT INTO plugin_scripts (plugin_id, version, source_text,"
        " source_sha256, created_at) VALUES ('3','1.0.0','def run(ctx): pass',"
        " 'abc', ?)", (NOW,))
    with pytest.raises(Exception) as e:
        conn.execute("UPDATE plugin_scripts SET note='x' WHERE script_id=1")
    assert "append-only" in str(e.value)
    with pytest.raises(Exception) as e:
        conn.execute("DELETE FROM plugin_scripts WHERE script_id=1")
    assert "append-only" in str(e.value)


def test_plugin_scripts_unique_plugin_version(conn):
    conn.execute(
        "INSERT INTO plugin_scripts (plugin_id, version, source_text,"
        " source_sha256, created_at) VALUES ('3','1.0.0','a', 'x', ?)", (NOW,))
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO plugin_scripts (plugin_id, version, source_text,"
            " source_sha256, created_at) VALUES ('3','1.0.0','b', 'y', ?)", (NOW,))


def test_plugin_backtests_allows_null_baseline(conn):
    """首版插桩没有 baseline —— 该列必须可空（设计文档 §8.5）。"""
    conn.execute(
        "INSERT INTO plugin_scripts (plugin_id, version, source_text,"
        " source_sha256, created_at) VALUES ('3','1.0.0','a', 'x', ?)", (NOW,))
    conn.execute(
        "INSERT INTO plugin_backtests (candidate_script_id, baseline_script_id,"
        " pool, window_start, window_end, metrics_json, verdict, report_sha256,"
        " created_at) VALUES (1, NULL, 'short', '2023-01-01','2026-09-17',"
        " '{}', 'INCONCLUSIVE', 'sha', ?)", (NOW,))
    row = conn.execute("SELECT baseline_script_id FROM plugin_backtests").fetchone()
    assert row[0] is None


def test_plugin_audit_and_candidate_tables_append_only(conn):
    """每一张新表都要被 UPDATE / DELETE 触发器挡住。"""
    # plugin_audit.script_id=1 需要真实的 plugin_scripts 行作为前提
    conn.execute(
        "INSERT INTO plugin_scripts (plugin_id, version, source_text,"
        " source_sha256, created_at) VALUES ('3','1.0.0','def run(ctx): pass',"
        " 'abc', ?)", (NOW,))
    # candidate_rejects 无时间戳列，绑定参数为空元组
    inserts = {
        "plugin_audit": ("INSERT INTO plugin_audit (script_id, action, actor,"
                         " created_at) VALUES (1,'submit','tester',?)", (NOW,)),
        "candidate_snapshots": ("INSERT INTO candidate_snapshots (asof, run_kind,"
                                " params_json, created_at) VALUES ('2026-09-17',"
                                "'weekly','{}',?)", (NOW,)),
        "candidate_members": ("INSERT INTO candidate_members (snapshot_id, code,"
                              " pool, raw_score, adj_score, reason, risk_json,"
                              " status, entered_at) VALUES (1,'000333','short',"
                              "80.0,78.0,'r','[]','观察中',?)", (NOW,)),
        "candidate_rejects": ("INSERT INTO candidate_rejects (snapshot_id, code,"
                              " stage, reason) VALUES (1,'000333','pre_screen',"
                              "'st_flag')", ()),
    }
    for table, (sql, params) in inserts.items():
        conn.execute(sql, params)
        with pytest.raises(Exception) as e:
            conn.execute(f"UPDATE {table} SET rowid = rowid")
        assert "append-only" in str(e.value), table
        with pytest.raises(Exception) as e:
            conn.execute(f"DELETE FROM {table}")
        assert "append-only" in str(e.value), table


# ---------- Task 5：版本库读写 ----------

from stocklab.plugin import store


def test_source_sha256_is_stable():
    a = store.source_sha256("def run(ctx): pass")
    b = store.source_sha256("def run(ctx): pass")
    c = store.source_sha256("def run(ctx): pass  # 改一个字")
    assert a == b == hashlib.sha256("def run(ctx): pass".encode()).hexdigest()
    assert a != c


def test_insert_and_get_script(conn):
    sid = store.insert_script(conn, plugin_id="3", version="1.0.0",
                              source_text="def run(ctx): pass", note=None,
                              now=NOW)
    row = store.get_script(conn, sid)
    assert row["plugin_id"] == "3"
    assert row["version"] == "1.0.0"
    assert row["source_sha256"] == store.source_sha256("def run(ctx): pass")


def test_get_missing_script_returns_none(conn):
    assert store.get_script(conn, 999) is None


def test_list_scripts_filters_by_plugin_id(conn):
    store.insert_script(conn, plugin_id="3", version="1.0.0",
                        source_text="a", note=None, now=NOW)
    store.insert_script(conn, plugin_id="1", version="1.0.0",
                        source_text="b", note=None, now=NOW)
    assert len(store.list_scripts(conn)) == 2
    only3 = store.list_scripts(conn, plugin_id="3")
    assert [r["plugin_id"] for r in only3] == ["3"]


def test_audit_roundtrip(conn):
    sid = store.insert_script(conn, plugin_id="3", version="1.0.0",
                              source_text="a", note=None, now=NOW)
    store.insert_audit(conn, script_id=sid, action="submit", actor="tester",
                       reason=None, now=NOW)
    store.insert_audit(conn, script_id=sid, action="sandbox_pass",
                       actor="tester", reason="样本不足", now=NOW)
    rows = store.list_audit(conn, script_id=sid)
    assert [r["action"] for r in rows] == ["submit", "sandbox_pass"]
    assert rows[0]["actor"] == "tester"


def test_insert_backtest_and_load(conn):
    sid = store.insert_script(conn, plugin_id="3", version="1.0.0",
                              source_text="a", note=None, now=NOW)
    bid = store.insert_backtest(
        conn, candidate_script_id=sid, baseline_script_id=None, pool="short",
        window_start="2023-09-18", window_end="2026-09-17",
        metrics={"n_days": 0}, verdict="INCONCLUSIVE", overfit_flag=None,
        report_sha256="deadbeef", now=NOW)
    rows = store.load_backtests(conn, script_id=sid)
    assert len(rows) == 1
    assert rows[0]["backtest_id"] == bid
    assert rows[0]["verdict"] == "INCONCLUSIVE"
    assert rows[0]["metrics"]["n_days"] == 0        # JSON 已解回 dict


def test_insert_backtest_rejects_unknown_verdict(conn):
    sid = store.insert_script(conn, plugin_id="3", version="1.0.0",
                              source_text="a", note=None, now=NOW)
    with pytest.raises(Exception):
        store.insert_backtest(
            conn, candidate_script_id=sid, baseline_script_id=None, pool="short",
            window_start="2023-09-18", window_end="2026-09-17", metrics={},
            verdict="MAYBE", overfit_flag=None, report_sha256="x", now=NOW)


def test_insert_audit_rejects_empty_actor(conn):
    sid = store.insert_script(conn, plugin_id="3", version="1.0.0",
                              source_text="a", note=None, now=NOW)
    with pytest.raises(ValueError):
        store.insert_audit(conn, script_id=sid, action="submit",
                           actor="", reason=None, now=NOW)
