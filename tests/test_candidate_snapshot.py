"""Task 13：快照落库（设计文档 §5.4-5.6）。幂等键 (asof, run_kind)。"""

import sqlite3

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
MEMBER2 = snapshot.MemberRow(code="600519", pool="short", raw_score=70.0,
                             adj_score=65.0, reason="量价好", risk_json="[]",
                             status="观察中")


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
    with pytest.raises(ValueError):
        snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                params={}, members=[bad], rejects=[], now=NOW)


def test_invalid_status_mid_list_leaves_no_orphan(conn):
    """回归：第一个成员合法、第二个非法时，不能留下孤儿快照行。"""
    good = MEMBER
    bad = snapshot.MemberRow(code="600036", pool="short", raw_score=1.0,
                             adj_score=1.0, reason="r", risk_json="[]",
                             status="非法状态")
    with pytest.raises(ValueError):
        snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                params={}, members=[good, bad], rejects=[],
                                now=NOW)
    count = conn.execute(
        "SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0]
    assert count == 0, f"期望0行，实际{count}行（孤儿快照）"


# ---------- P90：写入原子化（根因 B） ----------
#
# `store/db.py::connect()` 是 `isolation_level=None`（autocommit），原来
# `write_snapshot` 末尾的 `conn.commit()` 是**空操作**、抛错也**没有 ROLLBACK**
# ⇒ 留下「半截快照」，再跑还会被 `find_snapshot` 当「已做过」静默返回残缺快照。
# 修法（D3）：用既有的 `stocklab.store.db.transaction(conn)` 包住全部写入。

#: 故意用 **pool 非法**（撞 `candidate_members.pool` 的 CHECK）而不是 status 非法 ——
#: status 会被 `write_snapshot` 的 pre-flight 在**开事务之前**拦掉，那就只证明了
#: pre-flight、证不到回滚。pool 不在 pre-flight 校验范围内，异常发生在写 members
#: 的中途（快照行 + 第 1 个成员已 insert），正是要测的那一刻。
BAD_POOL = snapshot.MemberRow(code="600036", pool="不存在的池", raw_score=1.0,
                              adj_score=1.0, reason="r", risk_json="[]",
                              status="观察中")


def _counts(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("candidate_snapshots", "candidate_members",
                      "candidate_rejects")}


def test_mid_write_failure_rolls_back_everything(conn):
    """写 members 中途抛错 ⇒ 快照行、成员、淘汰记录**零残留**。"""
    with pytest.raises(sqlite3.IntegrityError):
        snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                params={}, members=[MEMBER, BAD_POOL],
                                rejects=[REJECT], now=NOW)
    assert _counts(conn) == {"candidate_snapshots": 0,
                             "candidate_members": 0,
                             "candidate_rejects": 0}


def test_failure_leaves_no_fake_completed_snapshot(conn):
    """失败后不留「假已完成」：`find_snapshot` 为空，正常路径再跑能成功。"""
    with pytest.raises(sqlite3.IntegrityError):
        snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                params={}, members=[MEMBER, BAD_POOL],
                                rejects=[REJECT], now=NOW)
    assert snapshot.find_snapshot(conn, asof="2026-09-17",
                                  run_kind="weekly") is None

    sid = snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                                  params={"note": "x"},
                                  members=[MEMBER, MEMBER2], rejects=[REJECT],
                                  now=NOW)
    got = snapshot.load_snapshot(conn, sid)
    assert len(got["members"]) == 2
    assert [r["code"] for r in got["rejects"]] == ["600690"]


def test_no_transaction_left_open_after_success_and_failure(conn):
    """`transaction()` 必须是最外层且不留悬挂事务（autocommit 下的隐式 BEGIN）。"""
    snapshot.write_snapshot(conn, asof="2026-09-17", run_kind="weekly",
                            params={}, members=[MEMBER], rejects=[REJECT],
                            now=NOW)
    assert conn.in_transaction is False, "成功后仍有未结束的事务"

    with pytest.raises(sqlite3.IntegrityError):
        snapshot.write_snapshot(conn, asof="2026-09-18", run_kind="weekly",
                                params={}, members=[BAD_POOL], rejects=[],
                                now=NOW)
    assert conn.in_transaction is False, "失败后仍有未结束的事务（未 rollback）"


def test_three_run_kinds_coexist_at_same_asof(conn):
    """D7：同 `asof` 的 light/weekly/quarterly 是三条独立快照，互不覆盖。"""
    ids = {k: snapshot.write_snapshot(conn, asof="2026-09-17", run_kind=k,
                                      params={"kind": k}, members=[MEMBER],
                                      rejects=[REJECT], now=NOW)
           for k in ("light", "weekly", "quarterly")}
    assert len(set(ids.values())) == 3
    assert _counts(conn) == {"candidate_snapshots": 3,
                             "candidate_members": 3,
                             "candidate_rejects": 3}
