"""P14 / Task 64：`f_final` → 股数，以及**谁把数字压下来了**。

红线的落点在这里：0 股必须是**算出来的结论**并说清理由，
不能是「悄悄买一手」或「静默 0」。
"""

from __future__ import annotations

import pytest

from stocklab.portfolio.discipline import lines_for
from stocklab.risk.sizing import (C_CASH_FLOOR, C_LOT, C_NO_ADD, C_PER_TRADE,
                                  C_SINGLE, LOT, size_position)


def test_zero_kelly_gives_zero_shares_and_says_it_is_a_conclusion():
    s = size_position(code="000333", f_final=0.0, total_assets=20_000.0,
                      cash=11_314.91, price=86.80, qty_held=100)
    assert s["suggested_shares"] == 0
    assert s["binding_constraints"] == []
    assert any("结论" in n and "不下注" in n for n in s["notes"])


def test_target_below_one_lot_gives_zero_and_explains_the_lot():
    """¥500 的目标在 ¥86.80 上买不到 1 手 → 0 股，且**不许**四舍五入成 1 手。"""
    s = size_position(code="000333", f_final=0.0005, total_assets=1_000_000.0,
                      cash=1_000_000.0, price=86.80, qty_held=0)
    assert s["kelly_target_shares"] == 0
    assert s["suggested_shares"] == 0
    assert any("不足 1 手" in n and "四舍五入" in n for n in s["notes"])


def test_per_trade_cash_cap_is_the_binding_constraint():
    """单次动用现金 ≤5% 总资产：凯利要 1000 股，纪律只给 900 股。"""
    s = size_position(code="000333", f_final=0.05, total_assets=200_000.0,
                      cash=200_000.0, price=10.0, qty_held=0)
    assert s["kelly_target_shares"] == 1000
    assert s["suggested_shares"] == 900
    assert s["binding_constraints"] == [C_PER_TRADE]
    assert s["cash_outflow"] <= 0.05 * 200_000.0
    assert any("压到" in n for n in s["notes"])


def test_cash_floor_can_push_the_size_to_zero():
    """现金已在 45% 下限 → 一股都不能买（凯利有仓位也不行）。"""
    s = size_position(code="000333", f_final=0.30, total_assets=100_000.0,
                      cash=45_000.0, price=10.0, qty_held=0)
    assert s["kelly_target_shares"] == 3000
    assert s["suggested_shares"] == 0
    assert set(s["binding_constraints"]) == {C_CASH_FLOOR, C_PER_TRADE}
    assert any("被纪律约束压到 0 股" in n for n in s["notes"])


def test_no_add_line_is_judged_on_market_price_not_limit_price():
    """「现价 ≥ 87.00 禁加仓」看的是**现价**，不是你的挂单价。

    挂个 86.80 的低价单不该把这条纪律绕过去。
    """
    lines = lines_for("000333")
    assert lines and lines["no_add_above"] == 87.00
    s = size_position(code="000333", f_final=0.25, total_assets=200_000.0,
                      cash=190_000.0, price=86.80, qty_held=100,
                      market_price=87.23)
    assert s["kelly_target_shares"] == 500          # 50000 / 86.80 → 576 → 5 手
    assert s["suggested_shares"] == 0
    assert C_NO_ADD in s["binding_constraints"]
    assert s["no_add_judged_on"] == 87.23
    assert s["price"] == 86.80 and s["market_price"] == 87.23


def test_no_add_line_does_not_bind_when_only_the_limit_price_is_low():
    """不给现价时按拟成交价判 —— 86.80 < 87.00 → 这条**不**触发（其余纪律照旧）。"""
    s = size_position(code="000333", f_final=0.25, total_assets=200_000.0,
                      cash=190_000.0, price=86.80, qty_held=100)
    assert s["suggested_shares"] == 100             # 被单次动用现金 ≤5% 压到 1 手
    assert C_NO_ADD not in s["binding_constraints"]
    assert C_PER_TRADE in s["binding_constraints"]
    assert s["no_add_judged_on"] == 86.80


def test_single_position_cap_binds_with_an_existing_holding():
    s = size_position(code="000333", f_final=0.5, total_assets=100_000.0,
                      cash=100_000.0, price=10.0, qty_held=3_900)
    assert C_SINGLE in s["binding_constraints"]
    assert (s["qty_held"] + s["suggested_shares"]) * 10.0 <= 0.40 * 100_000.0


def test_lot_rounding_is_reported_not_hidden():
    """目标 210 股 → 200 股；差额是整手造成的，必须报出来。"""
    s = size_position(code="000333", f_final=0.021, total_assets=100_000.0,
                      cash=100_000.0, price=10.0, qty_held=0)
    assert s["kelly_target_shares"] == 200          # floor(210/100)*100
    assert s["suggested_shares"] % LOT == 0
    assert s["suggested_shares"] == 200
    assert C_LOT in s["binding_constraints"]


def test_fill_price_is_reported_even_at_zero_shares():
    """含滑点价是成本模型的性质，与买多少股无关 —— 0 股时也必须给。"""
    s = size_position(code="000333", f_final=0.0, total_assets=20_000.0,
                      cash=11_314.91, price=86.80, qty_held=100)
    assert s["suggested_shares"] == 0
    assert s["fill_price"] == pytest.approx(86.80 * 1.0005, abs=1e-4)


def test_out_of_range_inputs_are_rejected():
    kw = dict(code="000333", total_assets=100_000.0, cash=50_000.0, price=10.0)
    with pytest.raises(ValueError):
        size_position(f_final=1.2, **kw)
    with pytest.raises(ValueError):
        size_position(f_final=-0.1, **kw)
    with pytest.raises(ValueError):
        size_position(f_final=0.1, **dict(kw, price=0.0))
    with pytest.raises(ValueError):
        size_position(f_final=0.1, **dict(kw, total_assets=0.0))
