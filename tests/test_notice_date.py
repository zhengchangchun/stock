"""Task 3：公告日三级回退（设计评审 A1）。"""

import pytest

from stocklab.data import notice_date as nd


def test_report_type_from_report_date():
    assert nd.report_type_of("2026-03-31") == "一季报"
    assert nd.report_type_of("2026-06-30") == "中报"
    assert nd.report_type_of("2026-09-30") == "三季报"
    assert nd.report_type_of("2026-12-31") == "年报"


def test_report_type_rejects_unknown():
    with pytest.raises(ValueError):
        nd.report_type_of("2026-05-15")


def test_statutory_deadline():
    """法定披露截止日：年报/一季报 4-30、中报 8-31、三季报 10-31。"""
    assert nd.statutory_deadline("2025-12-31") == "2026-04-30"
    assert nd.statutory_deadline("2026-03-31") == "2026-04-30"
    assert nd.statutory_deadline("2026-06-30") == "2026-08-31"
    assert nd.statutory_deadline("2026-09-30") == "2026-10-31"


def test_plausible_date_is_kept():
    got, src, kind = nd.resolve("2026-03-31", report_date="2025-12-31")
    assert (got, src, kind) == ("2026-03-31", "f10", None)


def test_missing_date_falls_back_to_statutory_as_missing():
    """源站没给值 → 回退，但回退原因是 `missing`（推定），**不是**可疑。"""
    got, src, kind = nd.resolve(None, report_date="2025-12-31")
    assert (got, src) == ("2026-04-30", "statutory")
    assert kind == "missing"


def test_same_day_as_report_date_is_suspect():
    """公告日不能早于或等于报告期。"""
    got, src, kind = nd.resolve("2025-12-31", report_date="2025-12-31")
    assert src == "statutory" and kind == "implausible"


def test_beyond_120_days_is_suspect_and_falls_back():
    """实测：美的 2004-12-31 的 F10 公告日写成 2008-02-19（晚 1146 天）。"""
    got, src, kind = nd.resolve("2008-02-19", report_date="2004-12-31")
    assert (got, src) == ("2005-04-30", "statutory")
    assert kind == "implausible"


def test_exactly_120_days_is_kept():
    """边界：<= 120 天保留。报告期 2025-12-31 的 4-30 是 120 天。"""
    got, src, _ = nd.resolve("2026-04-30", report_date="2025-12-31")
    assert (got, src) == ("2026-04-30", "f10")


def test_121_days_is_not_kept():
    got, src, kind = nd.resolve("2026-05-01", report_date="2025-12-31")
    assert src == "statutory" and kind == "implausible"


def test_notice_date_before_report_date_falls_back():
    """公告日早于报告期 = 数据错位，必须回退法定截止日（不许把它当有效日期）。"""
    got, src, kind = nd.resolve("2025-06-30", report_date="2025-12-31")
    assert (got, src) == ("2026-04-30", "statutory")
    assert kind == "implausible"


# ---------- P53 T1：plausible() 与闰年 121 天 ----------

def test_plausible_boundaries():
    assert nd.plausible("2025-12-31", "2026-04-30") is True     # 正好 120 天
    assert nd.plausible("2025-12-31", "2026-05-01") is False    # 121 天
    assert nd.plausible("2025-12-31", "2025-12-31") is False    # 等于报告期
    assert nd.plausible("2025-12-31", "2025-06-30") is False    # 早于报告期


def test_statutory_leap_year_is_121_days_and_that_is_legitimate():
    """真库 41 行如此：年报 12-31 → 次年 4-30，闰年跨 2 月 = 121 天。

    所以 `plausible()` **不许**拿去检查 `statutory` 回退值 —— 那会年年误报。
    回退值只受「等于法定截止日」约束，不受 120 天上界约束。
    """
    for rd, deadline in (("2019-12-31", "2020-04-30"),   # 2020 闰年 → 121 天
                         ("2020-12-31", "2021-04-30"),   # 2021 平年 → 120 天
                         ("2007-12-31", "2008-04-30")):  # 2008 闰年 → 121 天
        got, src, kind = nd.resolve(None, report_date=rd)
        assert (got, src, kind) == (deadline, "statutory", "missing")
    assert nd.plausible("2019-12-31", "2020-04-30") is False   # 121 天 → 越过 120 上界
    assert nd.statutory_deadline("2019-12-31") == "2020-04-30"  # 但它就是法定期限
