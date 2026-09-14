"""Task 33：`verifications` 落库（四态 + 幂等 + 显式拒绝 + 结构性唯一索引）。

本文件的重点是**「不许静默覆盖」**：`verifications` 是这套系统的记账本，
一旦能被静默改写，「准确率」就变成了一个可以随手编辑的数字。
"""

from __future__ import annotations

import json

import pytest

from stocklab.store.db import connect
from stocklab.store.migrate import init_db
from stocklab.verify.score import VERIFICATION_FIELDS
from stocklab.verify.store import (VerificationConflict, find_verification,
                                   insert_verification, verification_from_row,
                                   verification_hash)


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    _seed_prediction(c, 7)
    _seed_prediction(c, 8)
    yield c
    c.close()


def _seed_prediction(conn, pred_id: int) -> None:
    """两条预测必须**同键不同值**：`predictions` 有 `(code, asof_date, model_version)`
    唯一索引，同键的第二条根本插不进去（这正是它的设计意图）。"""
    conn.execute(
        "INSERT INTO predictions (pred_id, code, asof_date, target_date,"
        " direction_up, direction_flat, direction_down, range_lo, range_hi,"
        " key_levels_json, action, size_pct, invalidate_if, strategy_mix_json,"
        " model_version, status, created_at)"
        " VALUES (?, '000333', ?, '2026-01-06', 0.5, 0.3, 0.2,"
        " 99.0, 103.0, '[]', 'add', 80.0, 'x', '{}', 'pit-rw-v1.0.1', 'ok', 't')",
        (pred_id, f"2026-01-{4 + pred_id:02d}"))
    conn.commit()


def _payload(pred_id: int = 7, **over) -> dict:
    base = {k: None for k in VERIFICATION_FIELDS}
    base.update({
        "pred_id": pred_id, "target_date": "2026-01-06", "scorable": True,
        "reason_code": None, "actual_close": 101.0, "actual_pct": 0.01,
        "benchmark_pct": 0.009, "hit_direction": 1, "hit_range": 1,
        "hit_levels": 1, "sim_pnl": 1234.56, "score_direction": 0.81,
        "score_range": 1.0, "score_level": 1.0, "score_action": 1.0,
        "total_score": 0.9, "invalidated": 0, "attribution_auto": "UNDETERMINED",
        "notes": {"scorable": True, "brier": 0.38},
    })
    base.update(over)
    return base


def _unscorable(pred_id: int = 7, code: str = "NO_BAR_TARGET") -> dict:
    p = _payload(pred_id, scorable=False, reason_code=code, actual_close=None,
                 actual_pct=None, hit_direction=None, total_score=None,
                 attribution_auto="DATA",
                 notes={"scorable": False, "reason_code": code, "reason": "no bar"})
    return p


def test_inserted_then_identical(conn):
    state, vid = insert_verification(conn, _payload(), now="t1")
    assert (state, vid) == ("inserted", 1)
    state2, vid2 = insert_verification(conn, _payload(), now="t2")
    assert (state2, vid2) == ("identical", 1)
    assert conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 1


def test_different_content_is_refused_not_overwritten(conn):
    insert_verification(conn, _payload(), now="t1")
    with pytest.raises(VerificationConflict) as exc:
        insert_verification(conn, _payload(actual_close=102.0), now="t2")
    assert "actual_close" in str(exc.value)
    # 库里那一行必须**原样**：没有新行、没有被改写
    rows = conn.execute("SELECT actual_close FROM verifications").fetchall()
    assert len(rows) == 1 and rows[0][0] == 101.0


def test_scorable_row_is_never_downgraded_to_unscorable(conn):
    """数据「消失」不该把一条已有成绩静默抹掉 —— 那是另一种覆盖。"""
    insert_verification(conn, _payload(), now="t1")
    with pytest.raises(VerificationConflict):
        insert_verification(conn, _unscorable(), now="t2")


def test_unscorable_row_is_rescored_when_data_arrives(conn):
    """`DATA` 缺口是**暂时**的：bar 到达后必须能补分（否则日循环永久卡死）。"""
    state, vid = insert_verification(conn, _unscorable(), now="t1")
    assert state == "inserted"
    before = dict(find_verification(conn, 7))

    state2, vid2 = insert_verification(conn, _payload(), now="t2")
    assert (state2, vid2) == ("rescored_after_data_gap", vid)
    assert conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 1

    row = find_verification(conn, 7)
    assert row["actual_close"] == 101.0
    # 身份列**一个都不许变**（ADR-002 的触发器也会拦，这里是第二道）
    for col in ("verification_id", "pred_id", "target_date", "created_at"):
        assert row[col] == before[col], col


def test_reason_code_is_part_of_the_content_identity(conn):
    """两条「不可评分」若原因不同，不许判成 identical（否则缺口会被掩盖）。"""
    insert_verification(conn, _unscorable(code="NO_BAR_TARGET"), now="t1")
    with pytest.raises(VerificationConflict):
        insert_verification(conn, _unscorable(code="SUSPENDED"), now="t2")


def test_unique_index_blocks_a_second_row_for_the_same_prediction(conn):
    """结构性防线：绕过代码直接 INSERT 第二行也不行。"""
    insert_verification(conn, _payload(), now="t1")
    sql = ("INSERT INTO verifications (pred_id, target_date, created_at)"
           " VALUES (7, '2026-01-06', 't')")
    with pytest.raises(Exception) as exc:
        conn.execute(sql)
        conn.commit()
    assert "UNIQUE" in str(exc.value).upper()
    conn.rollback()


def test_delete_is_still_rejected(conn):
    insert_verification(conn, _payload(), now="t1")
    with pytest.raises(Exception):
        conn.execute("DELETE FROM verifications")
    conn.rollback()


def test_round_trip_is_lossless(conn):
    """`verification_from_row` 必须能**逐字段**还原被 hash 的载荷，否则幂等是假的。"""
    payload = _payload()
    insert_verification(conn, payload, now="t1")
    rebuilt = verification_from_row(find_verification(conn, 7))
    assert verification_hash(rebuilt) == verification_hash(payload)
    assert rebuilt["notes"] == payload["notes"]


def test_unscorable_round_trip_keeps_the_reason(conn):
    payload = _unscorable(code="SUSPENDED")
    insert_verification(conn, payload, now="t1")
    rebuilt = verification_from_row(find_verification(conn, 7))
    assert rebuilt["scorable"] is False
    assert rebuilt["reason_code"] == "SUSPENDED"
    assert rebuilt["attribution_auto"] == "DATA"
    assert rebuilt["total_score"] is None


@pytest.mark.parametrize("payload_factory", [
    lambda: {**_unscorable(), "invalidated": None},     # 不可评分：整行没有分数
    lambda: _payload(invalidated=None),                 # 可评分但 invalidate_if 解析不出
], ids=["unscorable", "undetermined-invalidate"])
def test_undetermined_invalidated_is_idempotent_on_rerun(conn, payload_factory):
    """`invalidated=None`（判不了）入库时被列类型逼成 0 —— 幂等比较必须用**同一把尺子**。

    真实事故：全历史回放第二次跑，在第一条不可评分的记录（`pred_id=27`）上抛
    `VerificationConflict`，差异字段正是 `invalidated`。原因是拿**未规约**的新载荷
    （`None`）去比**已规约**的旧行（`0`），同一份结果被自己判成了「内容不同」。
    """
    payload = payload_factory()
    assert insert_verification(conn, payload, now="t1")[0] == "inserted"
    assert insert_verification(conn, payload, now="t2")[0] == "identical"
    assert conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 1
    # 但 `None` 与 0 的**语义**差别不能被这次规约抹掉：真值仍在 notes 里
    # （不可判定 vs 明确没失效），报告侧据此排除。


def test_hash_covers_every_contract_field(tmp_db):
    """逐个字段反转一次，hash 必须每次都变 —— 否则「内容全等」判据有洞。"""
    base = _payload()
    h0 = verification_hash(base)
    for field in VERIFICATION_FIELDS:
        mutated = dict(base)
        cur = mutated[field]
        if isinstance(cur, bool):
            mutated[field] = not cur
        elif isinstance(cur, (int, float)):
            mutated[field] = (cur or 0) + 1
        elif isinstance(cur, dict):
            mutated[field] = {**cur, "perturbed": 1}
        elif cur is None:
            mutated[field] = "perturbed"
        else:
            mutated[field] = f"{cur}-perturbed"
        assert verification_hash(mutated) != h0, field


def test_notes_are_stored_as_canonical_json(conn):
    insert_verification(conn, _payload(), now="t1")
    raw = conn.execute("SELECT notes FROM verifications").fetchone()[0]
    assert raw == json.dumps(json.loads(raw), sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False)
