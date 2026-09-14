"""数据质量校验（Task 13）。

这些规则是「脏数据不进库」的唯一防线（铁律③）。
C3（量额一致性）尤其关键：它是**单位错误**（手/股、万/元）的唯一探针。
"""

from stocklab.calendar.trading_calendar import Calendar
from stocklab.data.models import Bar, Quote
from stocklab.quality.checks import check_bars, check_quote

CAL = Calendar.from_dates(["2026-09-10", "2026-09-11", "2026-09-14"])


def bar(date="2026-09-11", o=10.0, h=10.8, low=9.9, c=10.5, v=1000, amt=None):
    return Bar(code="000333", date=date, open=o, high=h, low=low, close=c,
               volume=v, amount=amt, turnover=None, source="test")


def types(issues):
    return {i.issue_type for i in issues}


def test_valid_bar_has_no_issues():
    assert check_bars([bar()]) == []


def test_high_below_open_is_error():
    issues = check_bars([bar(o=10.0, h=9.5, low=9.0, c=9.8)])
    assert any(i.issue_type == "ohlc_inconsistent" and i.severity == "error"
               for i in issues)


def test_low_above_close_is_error():
    issues = check_bars([bar(o=10.0, h=10.8, low=10.6, c=10.5)])
    assert any(i.issue_type == "ohlc_inconsistent" for i in issues)


def test_non_positive_price_is_error():
    issues = check_bars([bar(o=0.0, h=0.0, low=0.0, c=0.0)])
    assert any(i.issue_type == "non_positive_price" for i in issues)


def test_negative_volume_is_error():
    issues = check_bars([bar(v=-1)])
    assert any(i.issue_type == "negative_volume" for i in issues)


def test_duplicate_date_is_error():
    issues = check_bars([bar("2026-09-11"), bar("2026-09-11")])
    assert any(i.issue_type == "duplicate_date" for i in issues)


def test_out_of_order_dates_is_error():
    issues = check_bars([bar("2026-09-11"), bar("2026-09-10")])
    assert any(i.issue_type == "dates_not_increasing" for i in issues)


def test_amount_volume_consistency_within_tolerance():
    """C3：amount ≈ close × volume（±1%）。这是单位错误的唯一防线。"""
    b = bar(o=10.0, h=10.5, low=9.9, c=10.0, v=1000, amt=10_000.0)
    assert [i for i in check_bars([b])
            if i.issue_type == "amount_volume_mismatch"] == []


def test_amount_volume_mismatch_detected():
    # 若把「手」当「股」或把「万」当「元」，偏差是 100 倍
    b = bar(o=10.0, h=10.5, low=9.9, c=10.0, v=1000, amt=1_000_000.0)
    issues = check_bars([b])
    assert any(i.issue_type == "amount_volume_mismatch" for i in issues)


def test_amount_none_skips_consistency_check():
    b = bar(amt=None)
    assert [i for i in check_bars([b])
            if i.issue_type == "amount_volume_mismatch"] == []


def test_calendar_gap_detected():
    bars = [bar("2026-09-10"), bar("2026-09-14")]     # 缺 09-11
    issues = check_bars(bars, calendar=CAL)
    assert any(i.issue_type == "calendar_gap" for i in issues)


def test_no_gap_when_complete():
    bars = [bar("2026-09-10"), bar("2026-09-11"), bar("2026-09-14")]
    assert [i for i in check_bars(bars, calendar=CAL)
            if i.issue_type == "calendar_gap"] == []


def test_zero_volume_flagged_as_possible_suspension():
    b = bar(v=0, amt=0.0)
    issues = check_bars([b])
    assert any(i.issue_type == "zero_volume" and i.severity == "info"
               for i in issues)


# ---------- 边界与错误路径（计划外补充） ----------

def test_empty_bars_is_not_an_error():
    assert check_bars([]) == []


def test_issue_is_frozen_and_carries_code_and_date():
    issues = check_bars([bar(v=-1)])
    issue = issues[0]
    assert issue.code == "000333"
    assert issue.date == "2026-09-11"
    assert issue.detail                       # 必须能定位到具体数值
    try:
        issue.severity = "info"               # type: ignore[misc]
    except Exception as exc:                  # noqa: BLE001 — frozen dataclass
        assert "frozen" in str(exc).lower() or "cannot assign" in str(exc).lower()
    else:
        raise AssertionError("Issue 必须是 frozen dataclass")


def test_wrong_scale_volume_detected_as_mismatch():
    """把「手」当「股」写进 volume：量额比偏差 100 倍，必须被抓到。"""
    b = bar(o=10.0, h=10.5, low=9.9, c=10.0, v=1000, amt=10_000.0 * 100)
    assert "amount_volume_mismatch" in types(check_bars([b]))


def test_zero_volume_skips_amount_check_to_avoid_false_positive():
    """停牌日 amount=0 不该被报成单位错误。"""
    b = bar(v=0, amt=0.0)
    assert "amount_volume_mismatch" not in types(check_bars([b]))


def test_gap_uses_first_and_last_bar_code():
    bars = [bar("2026-09-10"), bar("2026-09-14")]
    gap = [i for i in check_bars(bars, calendar=CAL)
           if i.issue_type == "calendar_gap"]
    assert len(gap) == 1
    assert gap[0].date == "2026-09-11"
    assert gap[0].severity == "warn"


def test_no_calendar_makes_no_gap_claims():
    bars = [bar("2026-09-10"), bar("2026-09-14")]
    assert "calendar_gap" not in types(check_bars(bars))


def test_check_quote_valid():
    q = Quote(code="000333", name="美的集团", price=86.80, pre_close=86.26,
              open=86.29, high=87.40, low=86.26, volume=18_095_100,
              amount=1_572_360_000.0, turnover=0.26, pe_ttm=14.92,
              float_mv=None, total_mv=None, pb=3.13, ts="20260914161421")
    assert check_quote(q) == []


def test_check_quote_non_positive_price_is_error():
    q = Quote(code="000333", name="美的集团", price=0.0, pre_close=0.0,
              open=0.0, high=0.0, low=0.0, volume=0, amount=0.0, turnover=None,
              pe_ttm=None, float_mv=None, total_mv=None, pb=None,
              ts="20260914161421")
    assert any(i.issue_type == "non_positive_price" for i in check_quote(q))


def test_check_quote_high_below_low_is_error():
    q = Quote(code="000333", name="美的集团", price=86.80, pre_close=86.26,
              open=86.29, high=80.0, low=90.0, volume=100, amount=1000.0,
              turnover=None, pe_ttm=None, float_mv=None, total_mv=None, pb=None,
              ts="20260914161421")
    assert any(i.issue_type == "ohlc_inconsistent" for i in check_quote(q))
