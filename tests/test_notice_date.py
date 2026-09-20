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
    got, src, suspect = nd.resolve("2026-03-31", report_date="2025-12-31")
    assert (got, src, suspect) == ("2026-03-31", "f10", False)


def test_missing_date_falls_back_to_statutory():
    got, src, suspect = nd.resolve(None, report_date="2025-12-31")
    assert (got, src) == ("2026-04-30", "statutory")
    assert suspect is True


def test_same_day_as_report_date_is_suspect():
    """公告日不能早于或等于报告期。"""
    got, src, suspect = nd.resolve("2025-12-31", report_date="2025-12-31")
    assert src == "statutory" and suspect is True


def test_beyond_120_days_is_suspect_and_falls_back():
    """实测：美的 2004-12-31 的 F10 公告日写成 2008-02-19（晚 1146 天）。"""
    got, src, suspect = nd.resolve("2008-02-19", report_date="2004-12-31")
    assert (got, src) == ("2005-04-30", "statutory")
    assert suspect is True


def test_exactly_120_days_is_kept():
    """边界：<= 120 天保留。报告期 2025-12-31 的 4-30 是 120 天。"""
    got, src, _ = nd.resolve("2026-04-30", report_date="2025-12-31")
    assert (got, src) == ("2026-04-30", "f10")


def test_121_days_is_not_kept():
    got, src, suspect = nd.resolve("2026-05-01", report_date="2025-12-31")
    assert src == "statutory" and suspect is True
