"""Task 13：快照落库（设计文档 §5.4-5.6）。幂等键 (asof, run_kind)。"""

import pytest

from stocklab.candidate import snapshot
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
MEMBER = snapshot.MemberRow(code="000333", pool="short", raw_score=80.0,
                            adj_score=75.0, reason="量价好", risk_json="[]",
                            status="观察中")
REJECT = snapshot.RejectRow(code="600690", stage="pre_screen",
                            reason="st_flag", plugin_id=None)


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def test_write_then_load(conn):
    sid = snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                  params={"note": "x"}, members=[MEMBER],
                                  rejects=[REJECT], now=NOW)
    got = snapshot.load_snapshot(conn, sid)
    assert [m["code"] for m in got["members"]] == ["000333"]
    assert got["members"][0]["adj_score"] == 75.0
    assert [r["code"] for r in got["rejects"]] == ["600690"]


def test_same_asof_and_kind_returns_existing_id(conn):
    """幂等：同日同类型重跑不产生第二条快照。"""
    a = snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                params={}, members=[MEMBER], rejects=[],
                                now=NOW)
    b = snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                params={}, members=[MEMBER], rejects=[],
                                now=NOW)
    assert a == b
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate_members").fetchone()[0] == 1


def test_different_kind_makes_new_snapshot(conn):
    a = snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                params={}, members=[MEMBER], rejects=[],
                                now=NOW)
    b = snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="light",
                                params={}, members=[MEMBER], rejects=[],
                                now=NOW)
    assert a != b


def test_find_snapshot_returns_none_when_absent(conn):
    assert snapshot.find_snapshot(conn, asof="1999-01-01",
                                  run_kind="weekly") is None


def test_params_roundtrip_as_json(conn):
    sid = snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                  params={"seed": 20, "topn": {"short": 6}},
                                  members=[], rejects=[], now=NOW)
    got = snapshot.load_snapshot(conn, sid)
    assert got["snapshot"]["params"]["seed"] == 20


def test_member_status_is_validated(conn):
    bad = snapshot.MemberRow(code="000333", pool="short", raw_score=1.0,
                             adj_score=1.0, reason="r", risk_json="[]",
                             status="随便写的")
    with pytest.raises(Exception):
        snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                params={}, members=[bad], rejects=[], now=NOW)
