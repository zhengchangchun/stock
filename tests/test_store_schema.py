import sqlite3

import pytest

from stocklab.config.paths import SCHEMA_SQL

EXPECTED_TABLES = {
    "instruments", "bars_daily", "adj_factors", "corp_actions", "money_flow_daily",
    "valuation_daily", "sector_daily", "market_state", "trading_calendar",
    "features_daily", "predictions", "verifications",
    "strategy_registry", "strategy_daily", "sim_portfolio", "sim_trades",
    "real_trades", "decisions", "data_quality", "system_events",
    "raw_fetch_cache", "job_runs", "adj_factor_blackout",
    "experiment_decisions",
    "quote_snapshots",                 # P11：盘中快照（append-only）
    "cash_flows",                      # P12：本金/现金流（append-only）
    "ledger_idem",                     # P12：幂等键台账（append-only）
    "paper_accounts",                  # P19：模拟盘三臂（append-only）
    "paper_trades",                    # P19：模拟成交（append-only）
    "paper_nav_daily",                 # P19：每日净值（append-only）
    "market_holidays",                 # P30：已公告休市（append-only，前瞻；非 trading_calendar）
    "plugin_scripts",                  # P插桩：脚本版本库（append-only）
    "plugin_audit",                    # P插桩：审计事件流（append-only）
    "plugin_backtests",                # P插桩：回测结果（append-only）
    "candidate_snapshots",             # P插桩：候选池快照（append-only）
    "candidate_members",               # P插桩：候选池成员（append-only）
    "candidate_rejects",               # P插桩：候选池淘汰记录（append-only）
}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    yield c
    c.close()


def test_schema_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    assert {r[0] for r in rows} == EXPECTED_TABLES


def test_schema_is_idempotent(conn):
    """重跑 DDL 不得报错（迁移幂等的前提）。"""
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))


NOW = "2026-09-14T19:00:00+08:00"

FEATURE_SQL = (
    "INSERT INTO features_daily (code, date, feature_version, json_payload, payload_hash,"
    " params_hash, data_version, created_at) VALUES (?,?,?,?,?,?,?,?)"
)
FEATURE_ROW = ("000333", "2026-09-14", "v1", "{}", "h1", "p1", "d1", NOW)


def test_features_daily_allows_multiple_versions(conn):
    """A1：append-only 与'重算追加'必须共存。"""
    conn.execute(FEATURE_SQL, FEATURE_ROW)
    conn.execute(FEATURE_SQL, ("000333", "2026-09-14", "v2", "{}", "h2", "p1", "d1", NOW))
    n = conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0]
    assert n == 2


def test_features_daily_rejects_duplicate_version(conn):
    conn.execute(FEATURE_SQL, FEATURE_ROW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(FEATURE_SQL, FEATURE_ROW)


def test_bars_daily_primary_key(conn):
    sql = ("INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
           " source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
    row = ("000333", "2026-09-14", 1, 2, 0.5, 1.5, 100, "none", "tencent", NOW)
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)


def test_real_portfolio_is_gone(conn):
    """A2：实盘只留流水，不留每日快照表。"""
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT 1 FROM real_portfolio")


def test_trading_calendar_unique(conn):
    sql = "INSERT INTO trading_calendar (date, source, created_at) VALUES (?, ?, ?)"
    row = ("2026-09-14", "index_bars", NOW)
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)
