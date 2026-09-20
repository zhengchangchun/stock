"""Task 6：累计→单季→TTM（口径错了不会报错，只会静默给错数）。"""

import pytest

from stocklab.candidate import indicators as ind
from stocklab.data.models import FinancialReport

NOW = "2026-09-20T16:00:00+08:00"


def rep(rd: str, *, income: float | None = None, cost: float | None = None,
        profit: float | None = None, notice: str = "2026-08-29",
        **extra) -> FinancialReport:
    return FinancialReport(
        code="000333", report_date=rd, notice_date=notice,
        notice_date_source="f10", report_type=ind.report_type_of(rd),
        total_operate_income=income, operate_cost=cost,
        parent_netprofit=profit, **extra)


def test_quarter_of():
    assert ind.quarter_of("2026-03-31") == (2026, 1)
    assert ind.quarter_of("2026-06-30") == (2026, 2)
    assert ind.quarter_of("2026-09-30") == (2026, 3)
    assert ind.quarter_of("2026-12-31") == (2026, 4)


def test_cumulative_to_single_quarter():
    """评审 B1 给的定值：Q1=10, H1=25, Q1-Q3=42, FY=60 → 单季 [10,15,17,18]。"""
    reports = [rep("2026-03-31", income=10.0), rep("2026-06-30", income=25.0),
               rep("2026-09-30", income=42.0), rep("2026-12-31", income=60.0)]
    q = ind.single_quarters(reports, "total_operate_income")
    assert q[(2026, 1)] == 10.0
    assert q[(2026, 2)] == 15.0
    assert q[(2026, 3)] == 17.0
    assert q[(2026, 4)] == 18.0


def test_ttm_rolls_four_quarters():
    reports = [rep("2026-03-31", income=10.0), rep("2026-06-30", income=25.0),
               rep("2026-09-30", income=42.0), rep("2026-12-31", income=60.0)]
    q = ind.single_quarters(reports, "total_operate_income")
    assert ind.ttm(q, year=2026, quarter=4, field="total_operate_income") == 60.0


def test_ttm_across_year_boundary():
    """跨年滚动：2027Q1 的 TTM = 2026Q2+Q3+Q4 + 2027Q1。"""
    reports = [rep("2026-03-31", income=10.0), rep("2026-06-30", income=25.0),
               rep("2026-09-30", income=42.0), rep("2026-12-31", income=60.0),
               rep("2027-03-31", income=18.0)]
    q = ind.single_quarters(reports, "total_operate_income")
    assert ind.ttm(q, year=2027, quarter=1,
                   field="total_operate_income") == 15.0 + 17.0 + 18.0 + 18.0


def test_ttm_returns_none_when_a_quarter_is_missing():
    reports = [rep("2026-03-31", income=10.0), rep("2026-06-30", income=25.0),
               rep("2026-12-31", income=60.0)]           # 缺 Q3
    q = ind.single_quarters(reports, "total_operate_income")
    assert ind.ttm(q, year=2026, quarter=4,
                   field="total_operate_income") is None


def test_missing_field_is_skipped_not_zero():
    """字段为 None 的期不产生单季值，也不当成 0。"""
    reports = [rep("2026-03-31", income=10.0), rep("2026-06-30", income=None)]
    q = ind.single_quarters(reports, "total_operate_income")
    assert q[(2026, 1)] == 10.0
    assert (2026, 2) not in q
