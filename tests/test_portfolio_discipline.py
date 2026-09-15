"""P12 / Task 57：用户纪律的自动判定（边界一个一个钉住）。"""

import pytest

from stocklab.portfolio.discipline import (
    DISCIPLINE,
    check_cash_band,
    check_cash_per_trade,
    check_no_add_above,
    check_position_weight,
    check_stop_loss_close,
    check_stop_loss_weekly,
    run_checks,
)

#: 用户纪律的**数字**必须在库里只有一处（避免文档改了代码没改）
def test_limits_match_the_users_stated_rules():
    assert DISCIPLINE["single_position_max_pct"] == 40.0
    assert DISCIPLINE["cash_band_pct"] == (45.0, 60.0)
    assert DISCIPLINE["stop_loss_close"] == 85.00
    assert DISCIPLINE["stop_loss_weekly"] == 83.25
    assert DISCIPLINE["no_add_above"] == 87.00
    assert DISCIPLINE["cash_per_trade_max_pct"] == 5.0
    assert DISCIPLINE["trim_light_pct"] == 10.0
    assert DISCIPLINE["trim_on_break_pct"] == 20.0


# ---------- 单票 40% ----------

def test_position_weight_at_exactly_40_is_pass():
    """规则是「≤ 40%」—— 正好 40% 是合规的，不是越界。"""
    assert check_position_weight("000333", 40.0)["status"] == "PASS"


def test_position_weight_above_40_is_fail():
    out = check_position_weight("000333", 43.41)
    assert out["status"] == "FAIL"
    assert out["numbers"]["weight_pct"] == pytest.approx(43.41)
    assert out["numbers"]["limit_pct"] == 40.0
    assert out["numbers"]["excess_pct"] == pytest.approx(3.41)


def test_position_weight_just_above_40_is_fail():
    assert check_position_weight("000333", 40.01)["status"] == "FAIL"


def test_position_weight_well_below_is_pass():
    assert check_position_weight("000333", 12.0)["status"] == "PASS"


def test_missing_price_weight_is_undetermined_not_pass():
    """没有现价就没有市值，也就没有权重 —— 不许当成 0%（那会自动 PASS）。"""
    out = check_position_weight("000333", None)
    assert out["status"] == "UNDETERMINED"
    assert "数据不足" in out["detail"]


# ---------- 现金带 45–60% ----------

@pytest.mark.parametrize("pct", [45.0, 50.0, 56.59, 60.0])
def test_cash_band_inside_is_pass(pct):
    assert check_cash_band(pct)["status"] == "PASS"


@pytest.mark.parametrize("pct", [44.99, 60.01, 0.0, 100.0])
def test_cash_band_outside_is_warn(pct):
    """目标带是**目标**，不是硬约束 —— 带外是 WARN（有数字），不是 FAIL。"""
    assert check_cash_band(pct)["status"] == "WARN"


def test_cash_band_below_reports_the_gap():
    out = check_cash_band(30.0)
    assert out["numbers"]["low_pct"] == 45.0
    assert out["numbers"]["high_pct"] == 60.0
    assert out["numbers"]["gap_pct"] == pytest.approx(-15.0)


def test_cash_band_above_reports_the_gap():
    out = check_cash_band(80.0)
    assert out["numbers"]["gap_pct"] == pytest.approx(20.0)


# ---------- 止损：收盘 85.00 ----------

def test_close_at_stop_line_is_pass():
    """止损线是「跌破才动」—— 正好 85.00 还没跌破。"""
    assert check_stop_loss_close("000333", 85.00)["status"] == "PASS"


def test_close_below_stop_line_is_fail():
    out = check_stop_loss_close("000333", 84.99)
    assert out["status"] == "FAIL"
    assert out["numbers"]["line"] == 85.00
    assert out["numbers"]["distance"] == pytest.approx(-0.01)


def test_close_above_stop_line_is_pass_with_distance():
    out = check_stop_loss_close("000333", 86.80)
    assert out["status"] == "PASS"
    assert out["numbers"]["distance"] == pytest.approx(1.80)


def test_close_stop_line_undetermined_without_price():
    assert check_stop_loss_close("000333", None)["status"] == "UNDETERMINED"


# ---------- 止损：周线 83.25 ----------

def test_weekly_at_line_is_pass():
    assert check_stop_loss_weekly("000333", 83.25)["status"] == "PASS"


def test_weekly_below_line_is_fail():
    out = check_stop_loss_weekly("000333", 83.24)
    assert out["status"] == "FAIL"
    assert out["numbers"]["line"] == 83.25


def test_weekly_without_data_is_undetermined_with_reason():
    """周线取不到就明写「数据不足，未判定」，**不猜**。"""
    out = check_stop_loss_weekly("000333", None)
    assert out["status"] == "UNDETERMINED"
    assert "数据不足" in out["detail"]


# ---------- 不追高：87 元以上禁补仓 ----------

def test_below_87_allows_adding():
    assert check_no_add_above("000333", 86.99)["status"] == "PASS"


def test_exactly_87_forbids_adding():
    """「87 元以上不补仓」的 87 本身也算高位 —— 取 ≥，留安全边际。"""
    assert check_no_add_above("000333", 87.00)["status"] == "WARN"


def test_above_87_is_warn_not_fail():
    """它是**禁止动作**，不是**持仓违规** —— 现价高本身不构成错误。"""
    out = check_no_add_above("000333", 87.45)
    assert out["status"] == "WARN"
    assert out["numbers"]["threshold"] == 87.00
    assert out["numbers"]["over_by"] == pytest.approx(0.45)


def test_no_add_undetermined_without_price():
    assert check_no_add_above("000333", None)["status"] == "UNDETERMINED"


# ---------- 单次动用现金 ≤ 总资产 5% ----------

def test_cash_per_trade_gives_the_budget():
    out = check_cash_per_trade(20000.0)
    assert out["status"] == "PASS"
    assert out["numbers"]["limit_amount"] == pytest.approx(1000.0)


def test_cash_per_trade_undetermined_without_total():
    assert check_cash_per_trade(None)["status"] == "UNDETERMINED"
    assert check_cash_per_trade(0.0)["status"] == "UNDETERMINED"


# ---------- 汇总 ----------

def test_run_checks_covers_all_six_and_keeps_order():
    checks = run_checks(
        positions=[("000333", 43.41)],
        cash_pct=56.59,
        close=None, weekly_close=None, price=None,
        total_assets=19994.91)
    names = [c["check"] for c in checks]
    assert names == [
        "single_position_max_40pct",
        "cash_band_45_60pct",
        "stop_loss_close_85",
        "stop_loss_weekly_83_25",
        "no_add_above_87",
        "cash_per_trade_max_5pct",
    ]


def test_run_checks_flags_the_real_overweight_position():
    """真实持仓：000333 占 43.41% > 40% → 必须有 FAIL 被报出来。"""
    checks = run_checks(
        positions=[("000333", 43.41)], cash_pct=56.59,
        close=86.80, weekly_close=86.80, price=86.80, total_assets=19994.91)
    by = {c["check"]: c for c in checks}
    assert by["single_position_max_40pct"]["status"] == "FAIL"
    assert by["cash_band_45_60pct"]["status"] == "PASS"
    assert by["stop_loss_close_85"]["status"] == "PASS"
    assert by["stop_loss_weekly_83_25"]["status"] == "PASS"
    assert by["no_add_above_87"]["status"] == "PASS"


def test_run_checks_marks_every_position():
    checks = run_checks(positions=[("A", 10.0), ("B", 55.0)], cash_pct=50.0,
                        close=None, weekly_close=None, price=None, total_assets=100.0)
    weights = [c for c in checks if c["check"] == "single_position_max_40pct"]
    assert len(weights) == 2
    assert {c["subject"] for c in weights} == {"A", "B"}


def test_every_check_has_status_detail_and_numbers():
    checks = run_checks(positions=[("000333", 43.41)], cash_pct=56.59,
                        close=86.80, weekly_close=86.80, price=86.80,
                        total_assets=19994.91)
    for c in checks:
        assert c["status"] in ("PASS", "WARN", "FAIL", "UNDETERMINED"), c
        assert isinstance(c["detail"], str) and c["detail"], c
        assert isinstance(c["numbers"], dict), c
