"""Task 2/4：东财财报适配器（离线，JSON 直接喂）。"""

import pytest

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


# ---------- Task 4：fetch_financial_reports ----------

from stocklab.data import fetch

PAGE_SIZE = eastmoney.FINANCIAL_PAGE_SIZE


class _FakeClient:
    """按 URL 里的 reportName / pageNumber 回放，不联网。"""

    def __init__(self, by_report):
        self.by_report = by_report
        self.calls = []

    def get_text(self, url, *, headers, source, cache_key):
        import json
        self.calls.append((url, cache_key))
        name = url.split("reportName=")[1].split("&")[0]
        page = int(url.split("pageNumber=")[1].split("&")[0])
        rows = self.by_report.get(name, {})
        return json.dumps(_payload(rows.get(page, []),
                                   pages=max(rows) if rows else 0))


DMSK_ROWS = {
    "RPT_DMSK_FN_BALANCE": {1: [
        {"REPORT_DATE": "2026-06-30 00:00:00", "NOTICE_DATE": "2026-08-29 00:00:00",
         "TOTAL_ASSETS": 643101789000.0, "TOTAL_EQUITY": 225792941000.0,
         "TOTAL_LIABILITIES": 417308848000.0, "INVENTORY": 4e10,
         "INDUSTRY_NAME": "白色家电"},
        {"REPORT_DATE": "2025-12-31 00:00:00", "NOTICE_DATE": "2026-08-29 00:00:00",
         "TOTAL_ASSETS": 6e11, "TOTAL_EQUITY": 2.1e11,
         "TOTAL_LIABILITIES": 3.9e11, "INVENTORY": 3.9e10,
         "INDUSTRY_NAME": "白色家电"},
    ]},
    "RPT_DMSK_FN_INCOME": {1: [
        {"REPORT_DATE": "2026-06-30 00:00:00", "TOTAL_OPERATE_INCOME": 2.5e11,
         "OPERATE_COST": 1.8e11, "PARENT_NETPROFIT": 2.6e10},
        {"REPORT_DATE": "2025-12-31 00:00:00", "TOTAL_OPERATE_INCOME": 4e11,
         "OPERATE_COST": 2.9e11, "PARENT_NETPROFIT": 4e10},
    ]},
    "RPT_DMSK_FN_CASHFLOW": {1: [
        {"REPORT_DATE": "2026-06-30 00:00:00", "NETCASH_OPERATE": 3e10,
         "CONSTRUCT_LONG_ASSET": 5e9},
    ]},
}

F10_G = {
    "RPT_F10_FINANCE_GBALANCE": {1: [
        {"REPORT_DATE": "2026-06-30 00:00:00", "NOTICE_DATE": "2026-08-29 00:00:00",
         "TOTAL_PARENT_EQUITY": 212861055000.0},
        {"REPORT_DATE": "2025-12-31 00:00:00", "NOTICE_DATE": "2026-03-31 00:00:00",
         "TOTAL_PARENT_EQUITY": 2.0e11},
    ]},
    # GINCOME: 提供一行，NOTICE_DATE 故意与 GBALANCE 不同（晚 1 天）；
    # 用于验证合并顺序：F10 循环按 BALANCE→INCOME→CASHFLOW 顺序、首见优先，
    # 故 GBALANCE 的日期应赢，GINCOME 的日期被忽略。
    "RPT_F10_FINANCE_GINCOME": {1: [
        {"REPORT_DATE": "2026-06-30 00:00:00", "NOTICE_DATE": "2026-08-30 00:00:00"},
    ]},
    # GCASHFLOW: 提供一行（无 NOTICE_DATE），覆盖现金流合并路径。
    "RPT_F10_FINANCE_GCASHFLOW": {1: [
        {"REPORT_DATE": "2026-06-30 00:00:00"},
    ]},
}


def test_fetch_merges_dmsk_and_f10():
    reports, refs = fetch.fetch_financial_reports(
        _FakeClient({**DMSK_ROWS, **F10_G}), code="000333", org_type="通用",
        fetched_date="2026-09-20")
    by_date = {r.report_date: r for r in reports}
    assert set(by_date) == {"2026-06-30", "2025-12-31"}

    june = by_date["2026-06-30"]
    assert june.notice_date == "2026-08-29"          # GBALANCE 优先（BALANCE→INCOME→CASHFLOW 首见）
    assert june.notice_date_source == "f10"
    # GINCOME 的 NOTICE_DATE（2026-08-30）不得覆盖 GBALANCE 的（2026-08-29）：
    # 合并顺序是 BALANCE→INCOME→CASHFLOW，首见优先（`rd not in f10_notice` 守卫）。
    assert june.notice_date != "2026-08-30", "GINCOME 的日期不应覆盖 GBALANCE 的日期"
    assert june.parent_equity == 212861055000.0            # 来自 F10
    assert june.total_assets == 643101789000.0             # 来自 DMSK
    assert june.parent_netprofit == 2.6e10
    assert june.industry_name == "白色家电"
    assert june.report_type == "中报"
    assert reports == sorted(reports, key=lambda r: r.report_date)


def test_bad_f10_notice_date_falls_back_to_statutory():
    """DMSK 的年报公告日指向下一年 —— 由 F10 的三级回退兜住。"""
    f10 = dict(F10_G)
    f10["RPT_F10_FINANCE_GBALANCE"] = {1: [
        {"REPORT_DATE": "2025-12-31 00:00:00",
         "NOTICE_DATE": "2026-08-29 00:00:00",        # 不合理：晚 241 天
         "TOTAL_PARENT_EQUITY": 2.0e11}]}
    reports, _ = fetch.fetch_financial_reports(
        _FakeClient({**DMSK_ROWS, **f10}), code="000333", org_type="通用",
        fetched_date="2026-09-20")
    y = {r.report_date: r for r in reports}["2025-12-31"]
    assert y.notice_date == "2026-04-30"
    assert y.notice_date_source == "statutory"


def test_etf_empty_response_returns_empty_not_error():
    """三表全空 = 合法无数据，不许当抓取失败。"""
    reports, refs = fetch.fetch_financial_reports(
        _FakeClient({}), code="510300", org_type="通用", fetched_date="2026-09-20")
    assert reports == []
    assert refs != []          # 但仍要留痕：空响应也记溯源


def test_short_page_mid_pagination_raises():
    """非末页行数 != pageSize → FetchError（半截历史不许当完整）。"""
    rows = {**DMSK_ROWS, **F10_G}
    # 让资产负债表声称有 2 页，但第一页只给 3 行
    rows["RPT_DMSK_FN_BALANCE"] = {
        1: [{"REPORT_DATE": "2026-06-30 00:00:00"}] * 3,
        2: [{"REPORT_DATE": "2025-12-31 00:00:00"}],
    }
    with pytest.raises(FetchError) as e:
        fetch.fetch_financial_reports(_FakeClient(rows), code="000333",
                                      org_type="通用", fetched_date="2026-09-20")
    assert "截断" in str(e.value)


def test_cache_key_carries_fetch_date():
    """缓存键必须带采集日，否则同键重采永远命中旧 body。"""
    client = _FakeClient({**DMSK_ROWS, **F10_G})
    fetch.fetch_financial_reports(client, code="000333", org_type="通用",
                                  fetched_date="2026-09-20")
    assert client.calls, "没有发起任何请求"
    for _url, key in client.calls:
        assert key.startswith("financial:000333:2026-09-20:")
        assert key.split(":")[2] == "2026-09-20", "第 3 段必须是 ISO 日期（ADR-009）"


# ---------- P53 T4：分页取全（正向） ----------

_QUARTER_DAY = {3: "31", 6: "30", 9: "30", 12: "31"}


def _quarter_ends(n: int) -> list[str]:
    """从 2026-06-30 往回生成 n 个季末日期（升序不要求，覆盖足够即可）。"""
    out, y, m = [], 2026, 6
    for _ in range(n):
        out.append(f"{y}-{m:02d}-{_QUARTER_DAY[m]}")
        m -= 3
        if m < 3:
            y, m = y - 1, 12
    return out


def test_multi_page_response_is_fully_collected():
    """>pageSize 的响应必须**翻全**，不许只拿第一页（P53 T4 判据）。

    造 700 行（pageSize=500 → 2 页：500 + 200）。若分页缺失，只会入库 500 行，
    且「半截当完整」——正是 valuation_daily 踩过的坑。
    """
    dates = _quarter_ends(700)
    assert len(dates) == 700
    rows = {1: [{"REPORT_DATE": f"{d} 00:00:00"} for d in dates[:PAGE_SIZE]],
            2: [{"REPORT_DATE": f"{d} 00:00:00"} for d in dates[PAGE_SIZE:]]}
    reports, refs = fetch.fetch_financial_reports(
        _FakeClient({"RPT_DMSK_FN_BALANCE": rows}), code="000333",
        org_type="通用", fetched_date="2026-09-20")

    assert len(reports) == 700, f"只收到 {len(reports)} 行 —— 分页没翻全"
    assert {r.report_date for r in reports} == set(dates)
    pages = [r["page"] for r in refs if r["endpoint"] == "RPT_DMSK_FN_BALANCE"]
    assert pages == [1, 2], f"溯源必须记录两页，实际 {pages}"
