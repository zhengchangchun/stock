"""数据质量校验（Task 13）：脏数据的唯一防线。

设计原则（对应铁律③「失败留痕」与 ERROR_DIARY「禁止静默降级」）：
  - 这里的规则**只报告、不修复**。修复必须在抓取层显式完成并留痕，
    否则「悄悄改过的数据」比「明显错的数据」更危险。
  - `Issue` 是 frozen dataclass：问题一旦产生就不可被下游改写。
  - 严重度语义：`error` = 硬错误（调用方**不得落库**）、`warn` = 可疑、
    `info` = 需知悉的正常现象（如停牌日成交量为 0）。

**C3 量额一致性**是本单位错误（手/股、万/元）的唯一探针：
`amount ≈ close × volume`。腾讯给「手 + 万元」，东财给「手 + 元」，
换算错一步偏差就是 100 倍 —— 该规则专门抓这个。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from stocklab.data.models import Bar, Quote

AMOUNT_TOLERANCE = 0.01     # ±1%
VALID_SEVERITIES = ("info", "warn", "error")


@dataclass(frozen=True)
class Issue:
    severity: str       # info | warn | error
    issue_type: str
    code: str
    date: str
    detail: str


def _issue(severity: str, issue_type: str, code: str, date: str,
           detail: str = "") -> Issue:
    return Issue(severity=severity, issue_type=issue_type, code=code, date=date,
                 detail=detail)


def _check_ohlc(b: Bar) -> list[Issue]:
    out = []
    if min(b.open, b.high, b.low, b.close) <= 0:
        out.append(_issue("error", "non_positive_price", b.code, b.date,
                          f"O={b.open} H={b.high} L={b.low} C={b.close}"))
    if b.high < max(b.open, b.close) or b.low > min(b.open, b.close):
        out.append(_issue("error", "ohlc_inconsistent", b.code, b.date,
                          f"O={b.open} H={b.high} L={b.low} C={b.close}"))
    return out


def _check_amount(b: Bar) -> list[Issue]:
    """量额一致性（C3）。停牌/零量时跳过，避免误报。"""
    if b.amount is None or b.volume <= 0 or b.close <= 0:
        return []
    expected = b.close * b.volume
    if expected <= 0:
        return []
    dev = abs(b.amount - expected) / expected
    if dev <= AMOUNT_TOLERANCE:
        return []
    return [_issue("error", "amount_volume_mismatch", b.code, b.date,
                   f"amount={b.amount:.0f} vs close*volume={expected:.0f} "
                   f"偏差 {dev:.1%}（疑似单位错误）")]


def check_bars(bars: Sequence[Bar], *, calendar=None) -> list[Issue]:
    """逐根校验日K，返回全部问题（不修改输入，不落库）。"""
    issues: list[Issue] = []
    seen: set[str] = set()
    prev: str | None = None

    for b in bars:
        issues.extend(_check_ohlc(b))

        if b.volume < 0:
            issues.append(_issue("error", "negative_volume", b.code, b.date,
                                 f"volume={b.volume}"))
        elif b.volume == 0:
            issues.append(_issue("info", "zero_volume", b.code, b.date,
                                 "成交量为 0，疑似停牌"))

        # 重复 / 乱序是**互斥**的判定：重复优先（重复必然也 <= prev）
        if b.date in seen:
            issues.append(_issue("error", "duplicate_date", b.code, b.date,
                                 f"{b.date} 重复出现"))
        elif prev is not None and b.date <= prev:
            issues.append(_issue("error", "dates_not_increasing", b.code, b.date,
                                 f"{b.date} <= {prev}"))
        seen.add(b.date)
        prev = b.date

        issues.extend(_check_amount(b))

    if calendar is not None and bars:
        issues.extend(_check_calendar_gaps(bars, calendar))
    return issues


def _check_calendar_gaps(bars: Sequence[Bar], calendar) -> list[Issue]:
    """交易日历有、行情没有 → 缺口（warn：可能是停牌，需人工确认）。"""
    start, end = bars[0].date, bars[-1].date
    have = {b.date for b in bars}
    code = bars[0].code
    return [_issue("warn", "calendar_gap", code, d, "交易日历有该日但无行情")
            for d in calendar.sessions(start, end) if d not in have]


def check_quote(q: Quote) -> list[Issue]:
    """校验快照。快照是**实时**数据，不做「与自身一致」之外的推断。"""
    issues: list[Issue] = []
    if min(q.price, q.pre_close) <= 0:
        issues.append(_issue("error", "non_positive_price", q.code, q.ts,
                             f"price={q.price} pre_close={q.pre_close}"))
    if q.volume < 0:
        issues.append(_issue("error", "negative_volume", q.code, q.ts,
                             f"volume={q.volume}"))
    if q.high and q.low and q.high < q.low:
        issues.append(_issue("error", "ohlc_inconsistent", q.code, q.ts,
                             f"high={q.high} < low={q.low}"))
    return issues
