"""Task 5：财报落库（幂等、首写保留、会计恒等式勾稽、单位量级）。"""

import json

import pytest

from stocklab.data.ingest import ingest_financial_reports
from stocklab.data.models import FinancialReport
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-20T16:00:00+08:00"


def _rep(**over) -> FinancialReport:
    base = dict(code="000333", report_date="2026-06-30", notice_date="2026-08-29",
                notice_date_source="f10", report_type="中报",
                total_assets=643101789000.0, parent_equity=212861055000.0,
                total_equity=225792941000.0, total_liabilities=417308848000.0,
                inventory=4e10, total_operate_income=2.5e11,
                operate_cost=1.8e11, parent_netprofit=2.6e10,
                netcash_operate=3e10, construct_long_asset=5e9,
                industry_name="白色家电")
    base.update(over)
    return FinancialReport(**base)


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def _refs():
    return [{"endpoint": "RPT_DMSK_FN_BALANCE", "resp_sha256": "abc",
             "cache_key": "financial:000333:2026-09-20:RPT_DMSK_FN_BALANCE:1",
             "page": 1}]


def test_writes_row(conn):
    r = ingest_financial_reports(conn, [_rep()], _refs(), now=NOW)
    assert r.ok and r.rows_written == 1
    row = conn.execute("SELECT * FROM financial_reports").fetchone()
    assert row["code"] == "000333"
    assert row["notice_date"] == "2026-08-29"
    assert row["parent_equity"] == 212861055000.0
    assert row["unit"] == "CNY"
    assert json.loads(row["raw_refs_json"])[0]["endpoint"] == "RPT_DMSK_FN_BALANCE"


def test_is_idempotent(conn):
    ingest_financial_reports(conn, [_rep()], _refs(), now=NOW)
    r2 = ingest_financial_reports(conn, [_rep()], _refs(), now=NOW)
    assert r2.rows_written == 0
    assert conn.execute("SELECT COUNT(*) FROM financial_reports").fetchone()[0] == 1


def test_first_write_wins_and_records_conflict(conn):
    """同键重采值变了 → 保留旧值 + 记 warn，不覆盖。"""
    ingest_financial_reports(conn, [_rep()], _refs(), now=NOW)
    r2 = ingest_financial_reports(conn, [_rep(total_assets=1.0)], _refs(), now=NOW)
    assert r2.rows_written == 0
    assert r2.conflicts == 1
    got = conn.execute("SELECT total_assets FROM financial_reports").fetchone()[0]
    assert got == 643101789000.0, "首写保留：旧值不许被覆盖"


def test_accounting_identity_violation_is_recorded(conn):
    """|资产 − 负债 − 权益| / 资产 >= 1e-6 → 记 issue，但仍落库（留痕不丢数据）。"""
    r = ingest_financial_reports(
        conn, [_rep(total_equity=1.0)], _refs(), now=NOW)
    assert r.rows_written == 1
    assert any("会计恒等式" in i for i in r.issues)


def test_implausible_magnitude_is_recorded(conn):
    """总资产不在 1e8~1e14 → 记 issue（量纲错 10000 倍是经典事故）。"""
    r = ingest_financial_reports(conn, [_rep(total_assets=1.0)], _refs(), now=NOW)
    assert any("量级" in i for i in r.issues)


def test_null_operate_cost_is_not_an_issue(conn):
    """金融股没有营业成本 —— 缺失是报表结构，不是脏数据。"""
    r = ingest_financial_reports(conn, [_rep(operate_cost=None)], _refs(), now=NOW)
    assert r.ok and r.rows_written == 1 and r.issues == ()


def test_seed_org_types_are_valid():
    """org_type 决定 F10 报表名（G/B/I），填错会静默拿不到财报。"""
    from stocklab.candidate.seeds import SEED_UNIVERSE

    allowed = {"通用", "银行", "保险"}
    got = {i.org_type for i in SEED_UNIVERSE}
    assert got <= allowed, f"出现未预期的 org_type: {got - allowed}"
    by_code = {i.code: i.org_type for i in SEED_UNIVERSE}
    assert by_code["600036"] == "银行"
    assert by_code["601398"] == "银行"
    assert by_code["601318"] == "保险"
    assert by_code["000333"] == "通用"
