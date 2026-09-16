"""预测落库（Task 30）：四态写入 + **结构性**防覆盖。

`predictions` 是 append-only（UPDATE/DELETE 皆被触发器 ABORT），
本任务再加一条 `UNIQUE (code, asof_date, model_version)`（schema）。
两条防线叠起来，「静默覆盖」在 SQLite 层面不可能 —— 下面的
`test_silent_upsert_is_structurally_impossible` 就是钉这件事的。
"""

import json
import sqlite3

import pytest

from stocklab.predict import model as M
from stocklab.predict.store import (PredictionConflict, find_prediction,
                                    insert_prediction, payload_from_row)
from stocklab.store.db import connect
from stocklab.store.migrate import init_db


def payload(**over):
    base = {
        "code": "000333",
        "asof_date": "2026-09-14",
        "target_date": "2026-09-15",
        "direction": {"up": 0.371, "flat": 0.258, "down": 0.371},
        "range_80": [70.11, 74.03],
        "key_levels": [{"price": 74.5, "role": "resistance", "p_touch": 0.41},
                       {"price": 70.8, "role": "support", "p_touch": 0.38}],
        "action": "wait",
        "size_pct": 0.0,
        "invalidate_if": "收盘跌破 70.80（20日低点）或收盘站上 74.50（20日高点）",
        "strategy_mix": M.degenerate_strategy_mix(excluded={"trend_ma": "被否证"},
                                                  benchmark_only=["buy_and_hold"]),
        "model_version": M.MODEL_VERSION,
    }
    base.update(over)
    return base


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def test_insert_reports_inserted_and_row_is_readable(conn):
    state, pred_id = insert_prediction(conn, payload(), now="2026-09-14T19:00:00+08:00", origin="live")
    assert state == "inserted" and pred_id > 0
    row = find_prediction(conn, "000333", "2026-09-14", M.MODEL_VERSION)
    assert row["target_date"] == "2026-09-15"
    assert row["action"] == "wait"
    assert json.loads(row["key_levels_json"])[0]["role"] == "resistance"
    # 特征快照**刻意**留 NULL：本模型不消费 features_daily（见 version.MODEL_SPEC）
    assert row["feature_snapshot_id"] is None
    assert row["regime_label"] is None


def test_same_payload_is_identical_not_duplicated(conn):
    insert_prediction(conn, payload(), now="t1", origin="live")
    state, pred_id = insert_prediction(conn, payload(), now="t2", origin="live")
    assert state == "identical"
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    assert pred_id == find_prediction(conn, "000333", "2026-09-14",
                                      M.MODEL_VERSION)["pred_id"]


def test_conflicting_payload_for_same_key_is_rejected(conn):
    """同 (code, asof, model_version) 但载荷不同 → **拒绝**，不覆盖、不留痕。"""
    insert_prediction(conn, payload(), now="t1", origin="live")
    with pytest.raises(PredictionConflict) as exc:
        insert_prediction(conn, payload(action="trim", size_pct=62.9), now="t2", origin="live")
    assert "append-only" in str(exc.value) or "已被占用" in str(exc.value)
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    assert find_prediction(conn, "000333", "2026-09-14",
                           M.MODEL_VERSION)["action"] == "wait"


def test_silent_upsert_is_structurally_impossible(conn):
    """`INSERT OR REPLACE` 也救不了静默覆盖：REPLACE 的隐式 DELETE 撞 append-only 触发器。

    这条测试是「结构性防线」的证据 —— 就算将来有人绕开 `insert_prediction`
    直接写 SQL，覆盖仍然做不到。
    """
    insert_prediction(conn, payload(), now="t1", origin="live")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT OR REPLACE INTO predictions (code, asof_date, target_date,"
            " direction_up, direction_flat, direction_down, range_lo, range_hi,"
            " key_levels_json, action, size_pct, invalidate_if, strategy_mix_json,"
            " model_version, status, created_at)"
            " VALUES ('000333','2026-09-14','2026-09-15',0.1,0.1,0.8,NULL,NULL,"
            " '[]','trim',10.0,'x','{}','" + M.MODEL_VERSION + "','ok','t2')")
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1


def test_row_roundtrip_hash_matches_payload(conn):
    """库里读回来重建的载荷，hash 必须与写进去的**逐位相同**。

    这是「可复现」在存储侧的落点：如果 JSON 列的序列化方式与 hash 用的
    规范化方式不一致，这里就会红。
    """
    p = payload()
    insert_prediction(conn, p, now="t1", origin="live")
    row = find_prediction(conn, "000333", "2026-09-14", M.MODEL_VERSION)
    assert M.payload_hash(payload_from_row(row)) == M.payload_hash(p)


def test_insert_writes_origin_and_origin_is_not_part_of_payload_hash(conn):
    """P32：origin 是**来源元数据**，不是载荷 —— 落库、可读、且不进 hash。

    hash 不含 origin 意味着「回放/实时」与「预测内容」正交：同一份载荷，
    用 live 或 replay 写进去，载荷 hash 都一样（判据见 `payload_from_row`）。
    """
    p = payload()
    state, _ = insert_prediction(conn, p, now="t1", origin="replay")
    assert state == "inserted"
    row = find_prediction(conn, "000333", "2026-09-14", M.MODEL_VERSION)
    assert row["origin"] == "replay"
    assert M.payload_hash(payload_from_row(row)) == M.payload_hash(p)


def test_insert_requires_origin(conn):
    """origin 是必填关键字参数：漏传必须 TypeError，而不是静默写 NULL。"""
    with pytest.raises(TypeError):
        insert_prediction(conn, payload(), now="t1")  # type: ignore[call-arg]


def test_different_model_version_is_a_different_record(conn):
    insert_prediction(conn, payload(), now="t1", origin="live")
    state, _ = insert_prediction(conn, payload(model_version=M.MODEL_VERSION + "-next"),
                                 now="t2", origin="live")
    assert state == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 2
