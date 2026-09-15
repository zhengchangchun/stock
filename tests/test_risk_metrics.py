"""P14 / Task 65：风险预算指标 + 止损位。

钉住三件容易骗人的事：① 样本不足给 `None` 而不是 0；
② 破产风险 `A ≤ 0` 必须给 `1.0` 并标「长期必败」；③ 先触发的止损是哪条。
"""

from __future__ import annotations

from statistics import pstdev

import pytest

from stocklab.risk.metrics import (TRADING_DAYS, build_metrics, calmar,
                                   log_returns, max_drawdown, realized_vol,
                                   ruin_risk, sortino, var_cvar,
                                   vol_target_position)
from stocklab.risk.stops import ATR_WINDOW, atr, render_stops, stop_levels


class FakeBar:
    """只需 stop_levels/atr 用到的字段。"""

    def __init__(self, date, close, high=None, low=None):
        self.date, self.close = date, close
        self.high = close if high is None else high
        self.low = close if low is None else low


def _bars(closes, spread=0.0):
    return [FakeBar(f"2026-01-{i + 1:02d}", c, c + spread, c - spread)
            for i, c in enumerate(closes)]


LINES = {"stop_loss_close": 85.00, "stop_loss_weekly": 83.25, "no_add_above": 87.00}


# ---------- RV ----------

def test_realized_vol_is_annualized():
    closes, x = [100.0], 100.0
    for i in range(40):
        x *= 1.01 if i % 2 == 0 else 1 / 1.01
        closes.append(round(x, 6))
    rv = realized_vol(closes)
    assert rv == pytest.approx(pstdev(log_returns(closes[-21:])) * TRADING_DAYS ** 0.5)
    assert 0.15 < rv < 0.17                     # 每日 ±1% → 年化约 16%


def test_realized_vol_is_none_when_the_window_is_not_there():
    assert realized_vol([100.0] * 20) is None   # 需要 21 根才有 20 个收益
    assert realized_vol([]) is None


# ---------- VaR / CVaR ----------

def test_var_and_cvar_are_positive_losses_with_declared_sample():
    # 收益要**有分布**：等比例下跌会让所有 5 日收益完全相同，
    # 尾部只剩一个值 → CVaR 与 VaR 相等，等于没测出区别
    closes = [100.0]
    for i in range(49):
        closes.append(closes[-1] * (1 - 0.001 * (i + 1)))
    vc = var_cvar(closes, [f"d{i}" for i in range(50)])
    assert vc["n_obs"] == 45                    # 重叠 5 日窗口个数
    assert vc["horizon_days"] == 5 and vc["method"] == "historical"
    assert vc["window"] == {"start": "d0", "end": "d49"}
    assert vc["var"] > 0 and vc["cvar"] > vc["var"]   # 尾部均值一定比分为点更差


def test_var_declares_insufficient_sample_instead_of_guessing():
    vc = var_cvar([100.0, 101.0, 102.0], None)
    assert vc["var"] is None and vc["cvar"] is None
    assert vc["label"] == "样本不足，仅供观察"


# ---------- MDD / 索提诺 / Calmar ----------

def test_max_drawdown_uses_the_peak_before_the_trough():
    m = max_drawdown([100.0, 120.0, 60.0, 90.0], ["a", "b", "c", "d"])
    assert m["mdd_pct"] == pytest.approx(50.0)
    assert (m["peak"], m["trough"]) == ("b", "c")


def test_sortino_is_none_without_downside_but_calmar_is_not():
    up = [0.01] * 30
    assert sortino(up) is None                            # 没有下行 → 不是无穷大
    assert calmar(up, None) is None                        # 没有回撤 → 不给比值
    assert calmar(up, 20.0) == pytest.approx(0.01 * TRADING_DAYS / 0.20)


# ---------- 波动率目标 ----------

def test_vol_target_clips_at_full_position_and_says_why():
    assert vol_target_position(0.30)["w"] == pytest.approx(0.5)
    hi = vol_target_position(0.10)                         # 公式要 1.5 倍杠杆
    assert hi["w"] == 1.0 and hi["clipped"] is True
    none = vol_target_position(None)
    assert none["w"] is None and none["clipped"] is None    # 不是 0，也不是满仓


# ---------- 破产风险 ----------

def test_ruin_risk_is_certainty_when_edge_is_not_positive():
    for a in (0.0, -0.05, -0.5):
        r = ruin_risk(a, 10)
        assert r["ruin_risk"] == 1.0
        assert "长期必败" in r["note"]


def test_ruin_risk_matches_the_formula_and_suspects_a_ge_1():
    assert ruin_risk(0.2, 10)["ruin_risk"] == pytest.approx(((0.8 / 1.2) ** 10), abs=1e-8)
    assert ruin_risk(1.0, 10)["ruin_risk"] == 0.0
    assert "先怀疑输入" in ruin_risk(1.0, 10)["note"]
    with pytest.raises(ValueError):
        ruin_risk(0.2, 0)


# ---------- 汇总 ----------

def test_build_metrics_reports_the_sample_gate():
    m = build_metrics(_bars([100.0 + i * 0.5 for i in range(130)]), asof="2026-01-30")
    assert m["sample"]["meets"] is True and m["sample"]["label"] == "样本充足"
    assert m["rv20_annual"] is not None and "var_cvar" in m and "vol_target" in m
    short = build_metrics(_bars([100.0 + i for i in range(30)]), asof="2026-01-30")
    assert short["sample"]["meets"] is False
    assert short["sample"]["label"] == "样本不足，仅供观察"


# ---------- 止损位 ----------

def test_atr_uses_true_range_and_refuses_a_short_window():
    assert atr(_bars([100.0] * 20, spread=1.0)) == pytest.approx(2.0)
    assert atr(_bars([100.0] * 5, spread=1.0)) is None


def test_first_trigger_is_the_closest_line_below_the_price():
    s = stop_levels(_bars([86.0] * 70), lines=LINES, price=87.0)
    assert s["atr"] == 0.0                                  # 全平的 K 线：TR=0
    assert s["first_trigger"]["name"].startswith("MA60")
    assert s["first_trigger"]["distance_pct"] == pytest.approx(-1.1494, abs=1e-3)
    assert s["n_below_price"] == 3


def test_atr_stop_is_computed_from_the_current_price_not_the_entry():
    s = stop_levels(_bars([100.0] * 20, spread=1.0), lines=None, price=100.0)
    atr_lv = [lv for lv in s["levels"] if lv["name"].startswith("ATR")][0]
    assert atr_lv["level"] == pytest.approx(96.0)           # 100 − 2×2
    assert "从现价" in atr_lv["source"]


def test_overlapping_ma60_and_weekly_stop_is_flagged():
    s = stop_levels(_bars([83.4] * 70), lines=LINES, price=87.0)
    w = s["overlap_warning"]
    assert w and w["gap_pct"] < 1.0 and "数了两遍" in w["note"]
    plain = stop_levels(_bars([70.0] * 70), lines=LINES, price=87.0)
    assert plain["overlap_warning"] is None                  # MA60 远离周线 → 不报


def test_renders_do_not_lie_about_missing_numbers():
    txt = render_stops(stop_levels(_bars([86.0] * 70), lines=LINES, price=87.0))
    assert "最先" in txt and f"ATR({ATR_WINDOW})" in txt
