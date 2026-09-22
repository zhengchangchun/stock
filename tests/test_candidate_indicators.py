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


def test_latest_period_empty_returns_none():
    assert ind.latest_period([]) is None


def test_latest_period_returns_latest_tuple():
    reports = [rep("2026-03-31", income=10.0), rep("2026-06-30", income=25.0),
               rep("2025-12-31", income=8.0)]
    assert ind.latest_period(reports) == (2026, 2)


def test_quarter_of_non_quarter_end_raises():
    with pytest.raises(ValueError):
        ind.quarter_of("2026-05-15")


# ---------- Task 7：六个因子 ----------

def _full_year(year: int, *, income_q: float, cost_ratio: float,
               profit_q: float, assets: float, parent_eq: float,
               total_eq: float, liab: float, inv: float,
               ocf_q: float, capex_q: float,
               notice: str | None = None) -> list[FinancialReport]:
    """构造一年的四个季报（累计口径）。"""
    out, inc, cst, pro = [], 0.0, 0.0, 0.0
    for q, rd in enumerate(("03-31", "06-30", "09-30", "12-31"), start=1):
        inc += income_q
        cst += income_q * cost_ratio
        pro += profit_q
        out.append(FinancialReport(
            code="000333", report_date=f"{year}-{rd}",
            notice_date=notice or f"{year}-{rd[:2]}-28",
            notice_date_source="f10", report_type=ind.report_type_of(f"{year}-{rd}"),
            total_assets=assets, parent_equity=parent_eq, total_equity=total_eq,
            total_liabilities=liab, inventory=inv,
            total_operate_income=inc, operate_cost=cst, parent_netprofit=pro,
            netcash_operate=ocf_q * q, construct_long_asset=capex_q * q))
    return out


def _two_years(notice_2025: str | None = None) -> list[FinancialReport]:
    y25 = _full_year(2025, income_q=100.0, cost_ratio=0.7, profit_q=10.0,
                     assets=1000.0, parent_eq=400.0, total_eq=450.0,
                     liab=550.0, inv=200.0, ocf_q=20.0, capex_q=5.0,
                     notice=notice_2025)
    y26 = _full_year(2026, income_q=110.0, cost_ratio=0.6, profit_q=12.0,
                     assets=1100.0, parent_eq=460.0, total_eq=510.0,
                     liab=590.0, inv=210.0, ocf_q=22.0, capex_q=5.0)
    return y25 + y26


def test_roe_uses_ttm_profit_over_average_parent_equity():
    f = ind.factors(_two_years())
    # TTM 归母净利 = 4 × 12 = 48；平均归母权益 = (400 + 460) / 2 = 430
    assert f["roe"] == pytest.approx(48.0 / 430.0)


def test_gross_margin_uses_ttm():
    f = ind.factors(_two_years())
    # TTM 营收 = 440，TTM 成本 = 440 × 0.6 = 264 → 毛利率 0.4
    assert f["gross_margin"] == pytest.approx(0.4)


def test_gm_yoy_is_percentage_point_difference():
    """毛利率弹性 = 本期毛利率 − 去年同期毛利率（单位 pp）。"""
    f = ind.factors(_two_years())
    # 2026 毛利率 0.4；2025 = 1 − 0.7 = 0.3 → 差 10 pp
    assert f["gm_yoy_pp"] == pytest.approx(10.0)


def test_inv_days_uses_average_inventory():
    f = ind.factors(_two_years())
    # TTM 成本 264，平均存货 (200 + 210) / 2 = 205 → 365 × 205 / 264
    assert f["inv_days"] == pytest.approx(365.0 * 205.0 / 264.0)


def test_fcf_margin():
    f = ind.factors(_two_years())
    # TTM 经营现金流 = 88，TTM 资本开支 = 20 → FCF 68；营收 440
    assert f["fcf_margin"] == pytest.approx(68.0 / 440.0)


def test_dupont_components_present_and_internally_consistent():
    """三分项与 `roe` 同口径：都用期初期末均值。"""
    f = ind.factors(_two_years())
    d = f["dupont"]
    # 平均总资产 = (1000 + 1100) / 2 = 1050；平均归母权益 = (400 + 460) / 2 = 430
    assert d["net_margin"] == pytest.approx(48.0 / 440.0)
    assert d["asset_turnover"] == pytest.approx(440.0 / 1050.0)
    assert d["equity_multiplier"] == pytest.approx(1050.0 / 430.0)


def test_dupont_identity_holds_against_roe():
    """恒等式：净利率 × 总资产周转率 × 权益乘数 == roe（P53 T2 判据）。"""
    f = ind.factors(_two_years())
    d = f["dupont"]
    product = d["net_margin"] * d["asset_turnover"] * d["equity_multiplier"]
    assert abs(product - f["roe"]) < 1e-9
    assert product == pytest.approx(48.0 / 430.0)


def test_dupont_identity_negative_case_total_equity_breaks_it():
    """负例：**分子归母、分母合计**（P53 之前的历史 bug）→ 恒等式必须不成立。

    口径混用才是错：只把乘数的分母换成 `total_equity`（含少数股东），
    `roe` 仍按归母算，右边的乘积就变成一个**我们不上报的 ROE**。
    这条负例证明恒等式测试对口径敏感，不是恒真的橡皮图章。
    """
    f = ind.factors(_two_years())
    d = f["dupont"]
    # 夹具：平均总资产 1050、平均归母权益 430、平均合计权益 (450+510)/2 = 510
    mixed = d["net_margin"] * d["asset_turnover"] * (1050.0 / 510.0)
    assert abs(mixed - f["roe"]) >= 1e-9
    # 方向恒为**低估**（分母变大），幅度 = 1 − 平均归母/平均合计
    assert mixed < f["roe"]
    assert abs(mixed - f["roe"]) / f["roe"] == pytest.approx(1.0 - 430.0 / 510.0)


def test_swapping_parent_for_total_consistently_keeps_identity():
    """反面对照：**两边都**换成合计权益时恒等式仍成立 —— 错的是混用，不是取值。"""
    import dataclasses

    swapped = [dataclasses.replace(r, parent_equity=r.total_equity)
               for r in _two_years()]
    f = ind.factors(swapped)
    d = f["dupont"]
    product = d["net_margin"] * d["asset_turnover"] * d["equity_multiplier"]
    assert abs(product - f["roe"]) < 1e-9


def test_missing_operate_cost_marks_na_not_zero():
    """金融股没有营业成本 → 毛利率与存货周转 NA，其余仍可算。"""
    import dataclasses

    stripped = [dataclasses.replace(r, operate_cost=None, inventory=None)
                for r in _two_years()]
    f = ind.factors(stripped)
    assert f["gross_margin"] is None
    assert f["inv_days"] is None
    assert f["roe"] is not None
    assert any("gross_margin" in r for r in f["na_reasons"])
    assert any("inv_days" in r for r in f["na_reasons"])


def test_zero_equity_does_not_produce_inf():
    import dataclasses

    zeroed = [dataclasses.replace(r, parent_equity=0.0) for r in _two_years()]
    f = ind.factors(zeroed)
    assert f["roe"] is None
    assert any("roe" in r for r in f["na_reasons"])


def test_period_is_reported():
    f = ind.factors(_two_years())
    assert f["period"] == "2026Q4"


# ---------- Fix round 1: NA deduplication ----------

def test_missing_profit_na_no_duplicate_root_cause():
    """缺 parent_netprofit 时，na_reasons 里只出现一条关于它的条目（dupont 不重复）。"""
    import dataclasses

    stripped = [dataclasses.replace(r, parent_netprofit=None)
                for r in _two_years()]
    f = ind.factors(stripped)
    assert f["dupont"] is None
    # dupont 的根因是 profit（flow 字段），已被 _need 汇报；不应再出现第二条
    profit_entries = [r for r in f["na_reasons"] if "parent_netprofit" in r]
    assert len(profit_entries) <= 1, (
        f"parent_netprofit 根因出现了 {len(profit_entries)} 次: {f['na_reasons']}")
    dupont_entries = [r for r in f["na_reasons"] if "dupont" in r]
    assert len(dupont_entries) == 0, (
        f"dupont 条目应被压制，但仍出现: {dupont_entries}")


def test_dupont_na_fires_for_own_missing_stock_fields():
    """缺 total_assets 时（flow 字段均齐），na_reasons 中应有一条 dupont 专属条目。"""
    import dataclasses

    stripped = [dataclasses.replace(r, total_assets=None)
                for r in _two_years()]
    f = ind.factors(stripped)
    assert f["dupont"] is None
    dupont_entries = [r for r in f["na_reasons"] if "dupont" in r]
    assert len(dupont_entries) == 1, (
        f"期望恰好一条 dupont NA 条目，实际: {dupont_entries}")
    # 确保没有因 profit/income 缺失而导致虚假重复
    assert len(dupont_entries) <= 1


def test_every_none_factor_has_an_explaining_na_entry():
    """对任意 None 因子，na_reasons 里必须有至少一条可解释条目。"""
    import dataclasses

    stripped = [dataclasses.replace(r, operate_cost=None, inventory=None,
                                    total_assets=None)
                for r in _two_years()]
    f = ind.factors(stripped)
    for key in ("roe", "gross_margin", "gm_yoy_pp", "inv_days",
                "fcf_margin", "dupont"):
        if f[key] is None:
            has_entry = any(key in r for r in f["na_reasons"])
            # gross_margin covers gm_yoy_pp indirectly; check either
            if key == "gm_yoy_pp":
                has_entry = any("gm_yoy" in r or "gross_margin" in r
                                for r in f["na_reasons"])
            assert has_entry, (
                f"因子 {key!r} 是 None 但 na_reasons 里找不到解释条目: "
                f"{f['na_reasons']}")


def test_gm_yoy_na_message_when_gm_is_none():
    """当 gm 本身为 None 时，gm_yoy 的 NA 消息应说明真正根因，不说「去年同期」。"""
    import dataclasses

    stripped = [dataclasses.replace(r, operate_cost=None)
                for r in _two_years()]
    f = ind.factors(stripped)
    assert f["gm_yoy_pp"] is None
    gm_yoy_entries = [r for r in f["na_reasons"] if "gm_yoy" in r]
    assert len(gm_yoy_entries) == 1
    # 消息不应把责任推给「去年同期」（真正的根因是当期 gm 缺失）
    assert "去年同期" not in gm_yoy_entries[0], (
        f"消息误称「去年同期」，实际根因是当期 gm 缺失: {gm_yoy_entries[0]}")
