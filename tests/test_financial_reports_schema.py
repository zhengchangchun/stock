"""Task 1：financial_reports 表结构 + append-only。"""

import pytest

from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-20T16:00:00+08:00"

ROW = ("INSERT INTO financial_reports (code, report_date, notice_date,"
       " notice_date_source, report_type, total_assets, parent_equity,"
       " total_equity, total_liabilities, inventory, total_operate_income,"
       " operate_cost, parent_netprofit, netcash_operate,"
       " construct_long_asset, industry_name, source, fetched_at, created_at,"
       " raw_refs_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

VALUES = ("000333", "2026-06-30", "2026-08-29", "f10", "中报",
          643101789000.0, 212861055000.0, 225792941000.0, 417308848000.0,
          40000000000.0, 250000000000.0, 180000000000.0, 26000000000.0,
          30000000000.0, 5000000000.0, "白色家电", "eastmoney-datacenter",
          NOW, NOW, '[{"endpoint":"DMSK_BALANCE","resp_sha256":"a","cache_key":"k","page":1}]')


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def test_table_exists(conn):
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name='financial_reports'").fetchone()
    assert row[0] == 1


def test_accepts_insert(conn):
    conn.execute(ROW, VALUES)
    assert conn.execute(
        "SELECT COUNT(*) FROM financial_reports").fetchone()[0] == 1


def test_is_append_only(conn):
    conn.execute(ROW, VALUES)
    with pytest.raises(Exception) as e:
        conn.execute("UPDATE financial_reports SET operate_cost = 1")
    assert "append-only" in str(e.value)
    with pytest.raises(Exception) as e:
        conn.execute("DELETE FROM financial_reports")
    assert "append-only" in str(e.value)


def test_primary_key_includes_notice_date(conn):
    """同一报告期在不同公告日下可以并存（财报重述）。"""
    conn.execute(ROW, VALUES)
    restated = list(VALUES)
    restated[2] = "2026-09-30"                      # 新的公告日
    restated[6] = 213000000000.0                    # 更正后的归母权益
    conn.execute(ROW, tuple(restated))
    n = conn.execute(
        "SELECT COUNT(*) FROM financial_reports"
        " WHERE code='000333' AND report_date='2026-06-30'").fetchone()[0]
    assert n == 2, "notice_date 必须参与主键，否则重述只能靠覆盖"


def test_same_triple_is_rejected(conn):
    conn.execute(ROW, VALUES)
    with pytest.raises(Exception):
        conn.execute(ROW, VALUES)


def test_notice_date_source_is_constrained(conn):
    bad = list(VALUES)
    bad[3] = "猜的"
    with pytest.raises(Exception):
        conn.execute(ROW, tuple(bad))
