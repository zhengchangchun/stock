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
