import sqlite3

import pytest

from stocklab.store.db import connect, transaction
from stocklab.store.migrate import init_db

NOW = "2026-09-14T19:00:00+08:00"

# ADR-001 §5：这 5 张表 append-only；`experiment_decisions` 是 P8 Task 43 新增的同族表
APPEND_ONLY_TABLES = (
    "features_daily",
    "predictions",
    "verifications",
    "sim_trades",
    "decisions",
    "experiment_decisions",
    "quote_snapshots",                 # P11：盘中截面是历史事实，不许改写
    "real_trades",                     # P12：实盘账本（改错只能冲正）
    "cash_flows",                      # P12：本金/现金流
    "ledger_idem",                     # P12：幂等键台账
)


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def _seed_feature(conn):
    conn.execute(
        "INSERT INTO features_daily (code, date, feature_version, json_payload,"
        " payload_hash, params_hash, data_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("000333", "2026-09-14", "v1", "{}", "h", "p", "d1", NOW),
    )


def _seed_prediction(conn, *, model_version="v0.1.0"):
    """写一条预测。

    `model_version` 可覆盖：P6 给 `predictions` 加了身份唯一键
    `(code, asof_date, model_version)`（`uq_predictions_identity`），
    同键第二条会被 UNIQUE 挡下 —— 需要「两条各不相同」的测试必须换版本号，
    这本身就是该唯一键的设计意图（要改预测就升 `model_version`）。
    """
    conn.execute(
        "INSERT INTO predictions (code, asof_date, target_date, direction_up,"
        " direction_flat, direction_down, action, size_pct, invalidate_if,"
        " strategy_mix_json, model_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("000333", "2026-09-14", "2026-09-15", 0.4, 0.3, 0.3, "hold", 50,
         "跌破 82.14", "{}", model_version, NOW),
    )


def _seed_sim_trade(conn):
    conn.execute(
        "INSERT INTO sim_trades (date, code, side, price, qty, fee, strategy_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("2026-09-14", "000333", "buy", 10.0, 100, 5.01, "trend_ma", NOW),
    )


def _seed_verification(conn):
    conn.execute(
        "INSERT INTO verifications (pred_id, target_date, created_at) VALUES (1,?,?)",
        ("2026-09-15", NOW),
    )


def _seed_decision(conn):
    conn.execute(
        "INSERT INTO decisions (date, scope, change, created_at) VALUES (?,?,?,?)",
        ("2026-09-14", "score_weights", "冻结参数集", NOW),
    )


# ---------- 触发器存在性（元测试：防止触发器被误删而测试假绿） ----------

def test_all_append_only_triggers_exist(conn):
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    expected = {
        "trg_features_no_update", "trg_features_no_delete",
        "trg_predictions_no_update", "trg_predictions_no_delete",
        "trg_sim_trades_no_update", "trg_sim_trades_no_delete",
        "trg_decisions_no_update", "trg_decisions_no_delete",
        "trg_experiment_decisions_no_update", "trg_experiment_decisions_no_delete",
        # verifications 不做全表 UPDATE 保护，改用「身份列不可变」触发器（ADR-002）
        "trg_verifications_no_update_identity", "trg_verifications_no_delete",
        # P12：实盘账本三表
        "trg_real_trades_no_update", "trg_real_trades_no_delete",
        "trg_cash_flows_no_update", "trg_cash_flows_no_delete",
        "trg_ledger_idem_no_update", "trg_ledger_idem_no_delete",
        # P11：quote_snapshots
        "trg_quote_snapshots_no_update", "trg_quote_snapshots_no_delete",
    }
    assert expected - names == set(), f"缺少触发器: {sorted(expected - names)}"


# ---------- features_daily ----------

def test_features_update_rejected(conn):
    with transaction(conn):
        _seed_feature(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE features_daily SET close = 1.0")


def test_features_delete_rejected(conn):
    with transaction(conn):
        _seed_feature(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM features_daily")


# ---------- predictions ----------

def test_predictions_update_rejected(conn):
    """R8 的核心：事后改预测 = 自我欺骗。"""
    with transaction(conn):
        _seed_prediction(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE predictions SET direction_up = 0.99")


def test_predictions_delete_rejected(conn):
    with transaction(conn):
        _seed_prediction(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM predictions")


def test_insert_still_works_after_failed_update(conn):
    """触发器必须是 ABORT 而非静默忽略。"""
    with transaction(conn):
        _seed_prediction(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE predictions SET size_pct = 0")
    with transaction(conn):
        # 换 model_version：身份键 (code, asof_date, model_version) 是 P6 新增的，
        # 同键第二条本就该被挡（见 _seed_prediction docstring）。
        # 本断言要的是「ABORT 之后连接仍可用」，不是「同键能写两条」。
        _seed_prediction(conn, model_version="v0.1.1")
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 2


# ---------- sim_trades / decisions（计划未单测，ADR-001 §5 要求） ----------

def test_sim_trades_update_rejected(conn):
    with transaction(conn):
        _seed_sim_trade(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE sim_trades SET price = 1.0")


def test_sim_trades_delete_rejected(conn):
    with transaction(conn):
        _seed_sim_trade(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM sim_trades")


def test_decisions_update_rejected(conn):
    with transaction(conn):
        _seed_decision(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE decisions SET change = '事后改写'")


def test_decisions_delete_rejected(conn):
    with transaction(conn):
        _seed_decision(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM decisions")


# ---------- verifications：刻意例外（ADR-002） ----------

def test_verifications_are_updatable_for_manual_attribution(conn):
    """刻意例外：attribution_manual 需要人工回填，故不加全表 append-only 触发器。"""
    with transaction(conn):
        _seed_prediction(conn)
        _seed_verification(conn)
        conn.execute("UPDATE verifications SET attribution_manual = 'STRATEGY'")
    row = conn.execute("SELECT attribution_manual FROM verifications").fetchone()
    assert row["attribution_manual"] == "STRATEGY"


def test_verifications_delete_rejected(conn):
    with transaction(conn):
        _seed_prediction(conn)
        _seed_verification(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM verifications")


def test_verifications_identity_columns_immutable(conn):
    """例外只放开「回填」列；pred_id / target_date / created_at 仍不可改。"""
    with transaction(conn):
        _seed_prediction(conn)
        _seed_verification(conn)
    with pytest.raises(sqlite3.IntegrityError, match="identity"):
        conn.execute("UPDATE verifications SET target_date = '2026-09-16'")
    with pytest.raises(sqlite3.IntegrityError, match="identity"):
        conn.execute("UPDATE verifications SET pred_id = 999")


# ---------- P12 实盘账本三表 ----------

def _seed_real_trade(conn):
    conn.execute(
        "INSERT INTO real_trades (date, code, side, price, qty, fee, note, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("2026-09-14", "000333", "buy", 86.80, 100, 5.09, None, NOW),
    )


def _seed_cash_flow(conn):
    conn.execute(
        "INSERT INTO cash_flows (date, kind, amount, note, created_at)"
        " VALUES (?,?,?,?,?)",
        ("2026-09-14", "deposit", 20000.0, None, NOW),
    )


def test_real_trades_update_rejected(conn):
    """账本不许改：改错的唯一合法路径是冲正（写反向记录），不是 UPDATE。"""
    with transaction(conn):
        _seed_real_trade(conn)
    with pytest.raises(sqlite3.IntegrityError, match="real_trades is append-only"):
        conn.execute("UPDATE real_trades SET qty = 200 WHERE trade_id = 1")


def test_real_trades_delete_rejected(conn):
    with transaction(conn):
        _seed_real_trade(conn)
    with pytest.raises(sqlite3.IntegrityError, match="real_trades is append-only"):
        conn.execute("DELETE FROM real_trades")


def test_real_trades_insert_or_replace_cannot_bypass_trigger(conn):
    """`INSERT OR REPLACE` 的隐式 DELETE 也必须被拦下（ERROR_DIARY #6）。

    SQLite 默认 `recursive_triggers=OFF` 时隐式 DELETE 不触发 DELETE 触发器，
    覆盖会静默成功 —— `db.connect()` 打开该 PRAGMA 正是为了这一刻。
    """
    with transaction(conn):
        _seed_real_trade(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "INSERT OR REPLACE INTO real_trades"
            " (trade_id, date, code, side, price, qty, fee, note, created_at)"
            " VALUES (1, '2026-09-14', '000333', 'buy', 1.0, 999, 0, '覆盖', ?)",
            (NOW,),
        )
    row = conn.execute("SELECT price, qty FROM real_trades WHERE trade_id = 1").fetchone()
    assert (row["price"], row["qty"]) == (86.80, 100), "原行必须原封不动"


def test_real_trades_allows_same_price_qty_fee_twice(conn):
    """同价同量同费的两笔**必须**都能入库 —— 那就是两张真单。

    这条测试钉住「不加 UNIQUE 元组约束」这个决策：真实成交会重复，
    唯一约束会静默吃掉真单（ADR-006）。
    """
    with transaction(conn):
        _seed_real_trade(conn)
        _seed_real_trade(conn)
    n = conn.execute("SELECT COUNT(*) FROM real_trades").fetchone()[0]
    assert n == 2


def test_cash_flows_update_and_delete_rejected(conn):
    with transaction(conn):
        _seed_cash_flow(conn)
    with pytest.raises(sqlite3.IntegrityError, match="cash_flows is append-only"):
        conn.execute("UPDATE cash_flows SET amount = 1.0")
    with pytest.raises(sqlite3.IntegrityError, match="cash_flows is append-only"):
        conn.execute("DELETE FROM cash_flows")


def test_cash_flows_kind_check(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cash_flows (date, kind, amount, note, created_at)"
            " VALUES ('2026-09-14','transfer',1.0,NULL,?)", (NOW,))


def test_ledger_idem_update_and_delete_rejected(conn):
    with transaction(conn):
        conn.execute(
            "INSERT INTO ledger_idem (idem_key, scope, row_id, created_at)"
            " VALUES ('k1','trade',1,?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError, match="ledger_idem is append-only"):
        conn.execute("UPDATE ledger_idem SET row_id = 2")
    with pytest.raises(sqlite3.IntegrityError, match="ledger_idem is append-only"):
        conn.execute("DELETE FROM ledger_idem")


def test_ledger_idem_key_is_primary(conn):
    conn.execute(
        "INSERT INTO ledger_idem (idem_key, scope, row_id, created_at) VALUES ('k1','trade',1,?)",
        (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ledger_idem (idem_key, scope, row_id, created_at)"
            " VALUES ('k1','trade',2,?)", (NOW,))
