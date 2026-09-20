"""派生指标（**不落库**，`build_ctx` 现算）。

## 累计 → 单季 → TTM

季报的流量项（营收、成本、净利、现金流）是**年内累计**：中报 = 上半年累计。
直接拿中报算净利率，与年报不可比，**算出来的因子是错的**。所以：

    单季(Qn) = 本累计 − 上一累计      （同一会计年度内）
    TTM     = 最近 4 个单季之和

存量类（总资产、权益、存货）用期末值；需要均值的用期初期末均值。

## 缺一期就给 None，不补 0

`None` 表示「算不出」，`0.0` 是「算出来是零」——两者混同会让下游把
「不知道」当成「最差」，正好违反设计 §6.3 的精神。
"""

from __future__ import annotations

import datetime as _dt

from stocklab.data.notice_date import report_type_of
from stocklab.data.models import FinancialReport

_QUARTER_BY_MONTH = {3: 1, 6: 2, 9: 3, 12: 4}


def quarter_of(report_date: str) -> tuple[int, int]:
    """`(年, 季)`。不是季末 → `ValueError`。"""
    d = _dt.date.fromisoformat(report_date)
    q = _QUARTER_BY_MONTH.get(d.month)
    if q is None:
        raise ValueError(f"{report_date!r} 不是季末日期")
    return d.year, q


def single_quarters(reports: list[FinancialReport],
                    field: str) -> dict[tuple[int, int], float]:
    """把累计值拆成单季。字段为 `None` 的期**直接跳过**（不产生 0）。"""
    cumulative: dict[tuple[int, int], float] = {}
    for r in reports:
        v = getattr(r, field, None)
        if v is None:
            continue
        cumulative[quarter_of(r.report_date)] = float(v)

    out: dict[tuple[int, int], float] = {}
    for (year, q), v in cumulative.items():
        if q == 1:
            out[(year, 1)] = v
            continue
        prev = cumulative.get((year, q - 1))
        if prev is None:
            continue                    # 上一期缺 → 这一期也算不出
        out[(year, q)] = v - prev
    return out


def ttm(quarters: dict[tuple[int, int], float], *, year: int, quarter: int,
        field: str) -> float | None:
    """滚动四季之和。任一季缺失 → `None`。

    `quarters` 是由 `single_quarters(reports, field)` 预先算好的单季字典，
    传入前字段已经解析完毕。因此 `field` **不影响计算结果**——它仅用于调用方
    可读性（让调用处 ``ttm(q, year=y, quarter=q, field="net_profit")`` 一眼看出
    ``q`` 对应哪个字段）。向 `field` 传错误名称**不会报错，也不会改变返回值**；
    如需字段名校验，请在 `single_quarters` 调用处检查。
    """
    need: list[tuple[int, int]] = []
    y, q = year, quarter
    for _ in range(4):
        need.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    if any(k not in quarters for k in need):
        return None
    return sum(quarters[k] for k in need)


def latest_period(reports: list[FinancialReport]) -> tuple[int, int] | None:
    """最新可用报告期 `(年, 季)`。"""
    qs = sorted({quarter_of(r.report_date) for r in reports})
    return qs[-1] if qs else None


_FACTOR_KEYS = ("roe", "gross_margin", "gm_yoy_pp", "inv_days",
                "fcf_margin", "dupont")


def _avg(reports, field, year, quarter) -> float | None:
    """期初期末均值：本期存量 与 上一年年末存量 的平均。

    上一期不存在 → `None`（不拿本期期末值充数）。
    """
    cur = _value_at(reports, field, year, quarter)
    prev = _value_at(reports, field, year - 1, 4)
    if cur is None or prev is None:
        return None
    return (prev + cur) / 2.0


def _value_at(reports, field, year, quarter) -> float | None:
    for r in reports:
        if quarter_of(r.report_date) == (year, quarter):
            v = getattr(r, field, None)
            return None if v is None else float(v)
    return None


def factors(reports: list[FinancialReport]) -> dict:
    """算出六个因子。**不落库**。

    返回的 dict **键永远齐全**：不可算的因子给 `None`，并在 `na_reasons`
    写明原因（设计 §6.3）。`dupont` 用**期末**口径，`roe` 用**均值**口径
    —— 两者不构成恒等，见设计 §7.1.1。
    """
    na: list[str] = []
    period = latest_period(reports)
    if period is None:
        return {"period": None,
                **{k: None for k in _FACTOR_KEYS},
                "na_reasons": ["没有任何财报期"],
                "dupont": None}
    year, quarter = period
    period_str = f"{year}Q{quarter}"

    def _need(field: str, name: str):
        qs = single_quarters(reports, field)
        v = ttm(qs, year=year, quarter=quarter, field=field)
        if v is None:
            na.append(f"{name}: {field} 的 TTM 四季不齐")
        return v

    income = _need("total_operate_income", "total_operate_income")
    cost = _need("operate_cost", "operate_cost")
    profit = _need("parent_netprofit", "parent_netprofit")
    ocf = _need("netcash_operate", "netcash_operate")
    capex = _need("construct_long_asset", "construct_long_asset")

    parent_eq_avg = _avg(reports, "parent_equity", year, quarter)
    inv_avg = _avg(reports, "inventory", year, quarter)
    assets_now = _value_at(reports, "total_assets", year, quarter)
    parent_eq_now = _value_at(reports, "parent_equity", year, quarter)

    roe = None
    if profit is not None and parent_eq_avg not in (None, 0):
        roe = profit / parent_eq_avg
    elif profit is not None:
        na.append("roe: 平均归母权益缺失或为零")

    gm = None
    if income not in (None, 0) and cost is not None:
        gm = (income - cost) / income
    else:
        na.append("gross_margin: operate_cost 或 total_operate_income 缺失"
                  "（金融股报表结构无营业成本，属预期 NA）")

    gm_yoy = None
    prev_year = year - 1
    if prev_year >= 1:
        inc_p = _value_at_ttm(reports, "total_operate_income", prev_year, quarter)
        cost_p = _value_at_ttm(reports, "operate_cost", prev_year, quarter)
        if gm is not None and inc_p not in (None, 0) and cost_p is not None:
            gm_yoy = (gm - (inc_p - cost_p) / inc_p) * 100.0
    if gm_yoy is None:
        na.append("gm_yoy_pp: 去年同期毛利率不可算（需 8 个单季）")

    inv_days = None
    if cost not in (None, 0) and inv_avg not in (None, 0):
        inv_days = 365.0 * inv_avg / cost
    else:
        na.append("inv_days: operate_cost 或 inventory 缺失"
                  "（金融股报表结构无存货，属预期 NA）")

    fcf_margin = None
    if ocf is not None and capex is not None and income not in (None, 0):
        fcf_margin = (ocf - capex) / income
    else:
        na.append("fcf_margin: 现金流或营收缺失")

    dupont = None
    if (profit is not None and income not in (None, 0) and assets_now
            and parent_eq_now not in (None, 0)):
        dupont = {
            "net_margin": profit / income,
            "asset_turnover": income / assets_now,
            "equity_multiplier": assets_now / parent_eq_now,
        }
    else:
        na.append("dupont: 归母净利/营收/总资产/期末归母权益不齐")

    return {"period": period_str, "roe": roe, "gross_margin": gm,
            "gm_yoy_pp": gm_yoy, "inv_days": inv_days,
            "fcf_margin": fcf_margin, "dupont": dupont, "na_reasons": na}


def _value_at_ttm(reports, field, year, quarter) -> float | None:
    qs = single_quarters(reports, field)
    return ttm(qs, year=year, quarter=quarter, field=field)
