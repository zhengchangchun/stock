"""Task 2/4：东财财报适配器（离线，JSON 直接喂）。"""

from stocklab.data.errors import FetchError
from stocklab.data.models import FinancialReport
from stocklab.data.sources import eastmoney


def _payload(rows, pages=1, count=None):
    return {"result": {"pages": pages, "count": count or len(rows), "data": rows}}


def test_f10_report_name_selects_by_org_type():
    """银行/保险的资产负债表存在，只是报表名不同 —— 实测 BBALANCE/IBALANCE。"""
    assert eastmoney.f10_report_name("银行", "BALANCE") == "RPT_F10_FINANCE_BBALANCE"
    assert eastmoney.f10_report_name("保险", "BALANCE") == "RPT_F10_FINANCE_IBALANCE"
    assert eastmoney.f10_report_name("通用", "BALANCE") == "RPT_F10_FINANCE_GBALANCE"
    assert eastmoney.f10_report_name("", "BALANCE") == "RPT_F10_FINANCE_GBALANCE"
    assert eastmoney.f10_report_name("银行", "INCOME") == "RPT_F10_FINANCE_BINCOME"


def test_datacenter_url_forces_columns_all():
    """显式列清单在缺该列的标的上会让整个请求返回 code 9501 —— 必须 columns=ALL。"""
    url = eastmoney.datacenter_url("RPT_DMSK_FN_BALANCE", secucode="000333.SZ",
                                   page=1, page_size=500)
    assert "columns=ALL" in url
    assert "pageSize=500" in url
    assert "RPT_DMSK_FN_BALANCE" in url
    assert "000333.SZ" in url


def test_parse_rows_returns_raw_dicts():
    rows = eastmoney.parse_datacenter_rows(_payload([{"REPORT_DATE": "2026-06-30 00:00:00"}]))
    assert rows[0]["REPORT_DATE"] == "2026-06-30 00:00:00"


def test_parse_rows_tolerates_null_result():
    """ETF 的三表返回 result=null —— 合法空，不是抓取失败。"""
    assert eastmoney.parse_datacenter_rows({"result": None}) == []
    assert eastmoney.parse_datacenter_rows({}) == []


def test_financial_report_is_frozen_and_has_units_documented():
    import dataclasses

    assert dataclasses.is_dataclass(FinancialReport)
    assert FinancialReport.__dataclass_params__.frozen is True
    assert "元" in (FinancialReport.__doc__ or "")
