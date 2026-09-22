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


def test_insert_bind_tuple_order_matches_f_cols(conn):
    """INSERT 绑定元组必须与 _F_COLS 顺序一一对应，否则列值错位。

    做法：写入一行后从数据库按 _F_COLS 顺序逐列读出，校验关键列的值。
    若将来 _F_COLS 被重排但 dict 建值逻辑未跟上，tuple(d[col] for col in _F_COLS)
    会因 KeyError 立即报错；若 dict 键与 _F_COLS 存在遗漏，同样会 KeyError。
    本测试额外断言实际落库的 parent_equity / total_equity 与入参一致，
    确保「同一 dict 派生顺序」没有在某列悄悄写错值。
    """
    from stocklab.data.ingest import _F_COLS

    rep = _rep()
    ingest_financial_reports(conn, [rep], _refs(), now=NOW)

    row = conn.execute(
        f"SELECT {', '.join(_F_COLS)} FROM financial_reports"
    ).fetchone()

    # 验证绑定元组长度等于 _F_COLS
    assert len(_F_COLS) == len(row), (
        f"SELECT 列数({len(row)}) ≠ _F_COLS 长度({len(_F_COLS)})"
    )

    # 按列名定位索引，断言关键财务字段值正确
    idx_pe = _F_COLS.index("parent_equity")
    idx_te = _F_COLS.index("total_equity")

    assert row[idx_pe] == rep.parent_equity, (
        f"parent_equity 列值错位：期望 {rep.parent_equity}, 得到 {row[idx_pe]}"
    )
    assert row[idx_te] == rep.total_equity, (
        f"total_equity 列值错位：期望 {rep.total_equity}, 得到 {row[idx_te]}"
    )


# ---------- P53 T1：公告日质检 notice_date_suspect ----------

def test_implausible_source_notice_date_raises_warn(conn):
    """源站给过公告日但不合理（已回退）→ 记 `notice_date_suspect`。"""
    r = ingest_financial_reports(
        conn, [_rep(notice_date="2026-04-30", notice_date_source="statutory",
                    notice_date_suspect=True, report_type="中报")],
        _refs(), now=NOW)
    assert r.rows_written == 1, "只记不拒：可疑也要留痕落库"
    assert any("notice_date_suspect" in i for i in r.issues)


def test_statutory_from_missing_is_not_a_suspect(conn):
    """源站**没给**值 → `missing` 推定，不是可疑，不许发 warn。

    真库 175 行 statutory 里绝大多数是这一类（美的 2004–2007 那 17 期），
    把它们全报成 suspect 等于噪声淹没信号。
    """
    r = ingest_financial_reports(
        conn, [_rep(notice_date="2026-08-31", notice_date_source="statutory")],
        _refs(), now=NOW)
    assert r.rows_written == 1
    assert not any("notice_date_suspect" in i for i in r.issues)


def test_leap_year_statutory_121_days_is_not_a_suspect(conn):
    """真库 41 行：年报 12-31 → 次年 4-30 在闰年是 **121** 天，合法。

    120 天上界只约束源站原始值，**不套 statutory 回退值** —— 否则年年误报。
    """
    r = ingest_financial_reports(
        conn, [_rep(report_date="2019-12-31", report_type="年报",
                    notice_date="2020-04-30", notice_date_source="statutory")],
        _refs(), now=NOW)
    assert r.rows_written == 1
    assert not any("notice_date_suspect" in i for i in r.issues), r.issues


def test_f10_row_violating_range_is_caught(conn):
    """标 f10 却越过 120 天上界 → 回归护栏命中（当前 resolve 不会产出这种行）。"""
    r = ingest_financial_reports(
        conn, [_rep(notice_date="2027-06-30", notice_date_source="f10")],
        _refs(), now=NOW)
    assert r.rows_written == 1
    assert any("notice_date_suspect" in i for i in r.issues)


def test_statutory_row_off_the_deadline_is_caught(conn):
    """标 statutory 却不等法定披露截止日 → 回归护栏命中。"""
    r = ingest_financial_reports(
        conn, [_rep(notice_date="2026-08-30", notice_date_source="statutory")],
        _refs(), now=NOW)
    assert any("notice_date_suspect" in i for i in r.issues)


# ---------- P53 T3：早期期数 NOTICE_DATE 为 NULL 也能入库 ----------

def test_pre_2007_period_with_empty_notice_date_is_storable(conn):
    """真库 244 行 `report_date < 2007`；其中美的 17 期源站公告日为 NULL。

    主键 `notice_date` 是 `NOT NULL` —— 若没有回退，这批历史期根本进不去。
    本用例钉死：回退到法定截止日后**插入成功**，且来源如实标 `statutory`。
    """
    from stocklab.data import notice_date as nd

    got, src, kind = nd.resolve(None, report_date="2004-12-31")
    assert (got, src, kind) == ("2005-04-30", "statutory", "missing")

    rep = _rep(report_date="2004-12-31", report_type="年报",
               notice_date=got, notice_date_source=src,
               total_assets=2.0e10, parent_equity=1.0e10,
               total_equity=1.2e10, total_liabilities=8.0e9)
    r = ingest_financial_reports(conn, [rep], _refs(), now=NOW)
    assert r.ok and r.rows_written == 1
    row = conn.execute(
        "SELECT report_date, notice_date, notice_date_source FROM financial_reports"
    ).fetchone()
    assert tuple(row) == ("2004-12-31", "2005-04-30", "statutory")
