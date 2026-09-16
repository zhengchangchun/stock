"""P28 估值 / 资金流落库：append-only 触发器 + 首写保留幂等（无网络）。

纪律（P28 计划 §4）：
  - `valuation_daily` / `money_flow_daily` 是 append-only：禁 UPDATE / DELETE；
  - 同 (code,date) **首写保留**：重采值一致 → 幂等跳过（重跑 0 新行）；
    重采值不同（源站重算历史）→ 计入 `restated`，**不覆盖**（非 PIT 对策）；
  - 每行带 `resp_sha256` / `cache_key`，可复现、可溯源。
"""

import sqlite3

import pytest

from stocklab.data.models import MoneyFlowDaily, ValuationDaily
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-16T22:00:00+08:00"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def val(date="2026-09-16", pe=14.61, pb=3.06):
    return ValuationDaily(code="000333", date=date, pe_ttm=pe, pb=pb,
                          ps_ttm=1.39, total_mv=6.48e11, total_shares=7.63e9,
                          close_price=85.0, change_rate=-2.56)


def flow(date="2026-09-16", close=85.0):
    return MoneyFlowDaily(code="000333", date=date, close=close, change_ratio=-0.03,
                          turnover=43.346, main_net=-2.75e8, xl_net=-1.62e8,
                          ratio_amount=-0.11)


SHA = "a" * 64
KEY = "valuation:000333:2026-09-16:1:500"


def test_insert_valuation_writes_fingerprint(conn):
    written, restated = repo.insert_valuation(
        conn, [(val(), SHA, KEY)], now=NOW)
    assert (written, restated) == (1, 0)
    row = conn.execute(
        "SELECT pe_ttm, source, fetched_at, created_at, resp_sha256, cache_key"
        " FROM valuation_daily WHERE code='000333' AND date='2026-09-16'"
    ).fetchone()
    assert row["pe_ttm"] == pytest.approx(14.61)
    assert row["source"] == "eastmoney-datacenter"
    assert row["fetched_at"] == NOW and row["created_at"] == NOW
    assert row["resp_sha256"] == SHA                    # 可溯源
    assert row["cache_key"] == KEY


def test_insert_valuation_idempotent_rerun_zero(conn):
    repo.insert_valuation(conn, [(val(), SHA, KEY)], now=NOW)
    written, restated = repo.insert_valuation(conn, [(val(), SHA, KEY)], now=NOW)
    assert (written, restated) == (0, 0)                # 验收 d：重跑 0 新行
    n = conn.execute("SELECT COUNT(*) FROM valuation_daily").fetchone()[0]
    assert n == 1


def test_insert_valuation_restated_not_overwritten(conn):
    repo.insert_valuation(conn, [(val(pe=14.61), SHA, KEY)], now=NOW)
    written, restated = repo.insert_valuation(
        conn, [(val(pe=13.00), SHA, KEY)], now=NOW)
    assert (written, restated) == (0, 1)                # 源站重算：只留痕不覆盖
    row = conn.execute("SELECT pe_ttm FROM valuation_daily").fetchone()
    assert row["pe_ttm"] == pytest.approx(14.61)        # 首写保留


def test_insert_money_flow_idempotent_and_fingerprint(conn):
    repo.insert_money_flow(conn, [(flow(), SHA, KEY)], now=NOW)
    written, restated = repo.insert_money_flow(conn, [(flow(), SHA, KEY)], now=NOW)
    assert (written, restated) == (0, 0)
    row = conn.execute(
        "SELECT turnover, resp_sha256 FROM money_flow_daily").fetchone()
    assert row["turnover"] == pytest.approx(43.346)
    assert row["resp_sha256"] == SHA


def test_empty_rows_are_noop(conn):
    assert repo.insert_valuation(conn, [], now=NOW) == (0, 0)
    assert repo.insert_money_flow(conn, [], now=NOW) == (0, 0)


def test_valuation_append_only_update_delete_rejected(conn):
    repo.insert_valuation(conn, [(val(), SHA, KEY)], now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE valuation_daily SET pe_ttm=1 WHERE code='000333'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM valuation_daily WHERE code='000333'")


def test_money_flow_append_only_update_delete_rejected(conn):
    repo.insert_money_flow(conn, [(flow(), SHA, KEY)], now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE money_flow_daily SET turnover=1 WHERE code='000333'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM money_flow_daily WHERE code='000333'")


def test_p28_triggers_exist(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    expected = {
        "trg_valuation_daily_no_update", "trg_valuation_daily_no_delete",
        "trg_money_flow_daily_no_update", "trg_money_flow_daily_no_delete",
    }
    assert expected - names == set(), f"缺少触发器: {sorted(expected - names)}"
