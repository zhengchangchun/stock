"""公告日三级回退（设计评审 A1）。

## 为什么不能直接用 DMSK 的 NOTICE_DATE

实测（2026-09-20，`000333.SZ` 全量 79 期）：DMSK 三表的 `NOTICE_DATE`
**只有「每种报告类型的最新一期」是对的**，历史行的取值指向**同类报告的下一次
公告日**：

    report_date   DMSK          F10           差
    2025-12-31    2026-08-29    2026-03-31    151 天
    2024-12-31    2026-03-31    2025-03-29    367 天

若照它做 PIT 锚，2025 年报的可读日会算成 2026-08-29 → 在 2026-04-30 至
2026-08-29 之间跑 `candidate run`，TTM 的四季永远凑不齐 → 中期/长期池**全部
`pass_flag=False`**，而报告只会说「数据不足」，看不出是源字段错。

## 三级回退

1. **F10 的 `NOTICE_DATE`**（实测与真实公告日吻合）
2. **合理性检查**：`report_date < notice_date <= report_date + 120 天`
3. 不过 → **法定披露截止日**，`source='statutory'`

第 3 级是**保守方向**的近似（最多晚约一个月），因此对 PIT 是安全的：
它只会让数据「晚一点可用」，绝不会让尚未公告的财报提前可见。
"""

from __future__ import annotations

import datetime as _dt

#: 报告期月份 → 报告类型。
_MONTH_TO_TYPE: dict[int, str] = {
    3: "一季报", 6: "中报", 9: "三季报", 12: "年报",
}

#: 报告类型 → 法定披露截止日的 (月, 日)。依据《证券法》与交易所定期报告
#: 披露规则：年报与一季报均为 4-30，中报 8-31，三季报 10-31。
_STATUTORY_MD: dict[str, tuple[int, int]] = {
    "年报": (4, 30), "一季报": (4, 30), "中报": (8, 31), "三季报": (10, 31),
}

#: 公告日合理性上限（自然日）。超过即认为该值不可信。
MAX_NOTICE_LAG_DAYS = 120


def report_type_of(report_date: str) -> str:
    """由报告期推出报告类型。不是季末日期 → `ValueError`（不静默兜底）。"""
    month = _dt.date.fromisoformat(report_date).month
    kind = _MONTH_TO_TYPE.get(month)
    if kind is None:
        raise ValueError(
            f"报告期 {report_date!r} 的月份是 {month}，不是季末 —— 无法判定报告类型")
    return kind


def statutory_deadline(report_date: str) -> str:
    """该报告期的法定披露截止日。"""
    d = _dt.date.fromisoformat(report_date)
    month, day = _STATUTORY_MD[report_type_of(report_date)]
    year = d.year + 1 if month < d.month else d.year
    return _dt.date(year, month, day).isoformat()


def resolve(original: str | None, *, report_date: str) -> tuple[str, str, bool]:
    """返回 `(notice_date, source, suspect)`。

    `suspect=True` 表示原始值不可信、已回退到法定截止日。
    """
    fallback = statutory_deadline(report_date)
    if not original:
        return fallback, "statutory", True

    start = _dt.date.fromisoformat(report_date)
    got = _dt.date.fromisoformat(original)
    lag = (got - start).days
    if 0 < lag <= MAX_NOTICE_LAG_DAYS:
        return original, "f10", False
    return fallback, "statutory", True
