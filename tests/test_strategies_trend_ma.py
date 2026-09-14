"""Task 27：`trend_ma`（P5）—— 金叉/死叉/ATR 止损 + 「只用价量」。

期望值全部由**实跑探针**确认过（不是从计划里抄的）：
构造 `[10]*10 + [20]*10 + [5]*10`、`fast=5, slow=10`，
实测金叉落在第 11 根（`2026-01-11`）、死叉落在第 21 根（`2026-01-21`）；
构造 `[10]*10 + [20]*5 + [12]*5` 时，第 16 根（`2026-01-16`）触发 ATR 止损
而非死叉（`_decide` 里死叉优先判定，此处 MA5 仍在 MA10 上方）。
"""

import pytest

from stocklab.data.models import Bar
from stocklab.strategies.base import ParamError
from stocklab.strategies.registry import strategy_registry
from stocklab.strategies.trend_ma import ATR_WINDOW, TrendMA


def mk(closes, code="000001", first_month=1):
    """构造一段复权日K（日期按 28 天/月推进，避免月份天数问题）。"""
    return [Bar(code=code, date=f"2026-{first_month + i // 28:02d}-{1 + i % 28:02d}",
                open=c, high=c * 1.01, low=c * 0.99, close=c, volume=10_000,
                amount=c * 10_000, turnover=1.0, source="test", adj_mode="qfq")
            for i, c in enumerate(closes)]


def signals(closes, **params):
    """逐日喂 K 线，返回 [(date, action, reason, close)]。"""
    s = strategy_registry.get("trend_ma", **params)
    hist = {"000001": []}
    out = []
    for b in mk(closes):
        hist["000001"].append(b)
        sig = s.generate(b.date, hist, {})
        if sig:
            out.append((b.date, sig["000001"].action, sig["000001"].reason, b.close))
    return out


# ---------- 规则正确性 ----------

def test_golden_cross_buys_then_death_cross_sells():
    out = signals([10.0] * 10 + [20.0] * 10 + [5.0] * 10, fast=5, slow=10)
    assert out == [("2026-01-11", "buy", "ma_golden_cross:5/10", 20.0),
                   ("2026-01-21", "sell", "ma_death_cross", 5.0)]


def test_atr_stop_fires_before_death_cross():
    # 第 16 根暴跌到 12：MA5 仍在 MA10 上方（无死叉），但跌破 入场价 − 2×ATR
    out = signals([10.0] * 10 + [20.0] * 5 + [12.0] * 5, fast=5, slow=10)
    assert out[0][1] == "buy"
    assert out[1][0] == "2026-01-16"
    assert out[1][1] == "sell"
    assert out[1][2] == "atr_stop:2.0x"


def test_atr_mult_is_respected():
    """把止损倍数放到极大 → 同一段行情不再触发止损，改为**死叉**离场。

    实测（探针）：默认 2.0 时第 16 根以 `atr_stop` 离场；
    改为 10.0 后离场推迟到第 18 根、原因变成 `ma_death_cross` ——
    这正是「止损倍数真的参与了决策」的证据。
    """
    out = signals([10.0] * 10 + [20.0] * 5 + [12.0] * 5, fast=5, slow=10,
                  atr_mult=10.0)
    assert [o[1] for o in out] == ["buy", "sell"]
    assert [o[2] for o in out] == ["ma_golden_cross:5/10", "ma_death_cross"]
    assert out[1][0] == "2026-01-18"


def test_no_signal_when_history_too_short():
    assert signals([10.0] * 9, fast=5, slow=10) == []      # 需要 slow + 1 根


def test_no_signal_when_no_bar_for_today():
    """当日无 K 线（停牌/缺口）→ 不出信号，绝不拿昨天当今天。"""
    s = strategy_registry.get("trend_ma", fast=5, slow=10)
    bars = mk([10.0] * 10 + [20.0] * 10)
    hist = {"000001": bars}
    assert s.generate("2026-02-01", hist, {}) == {}        # 该日无 K 线


def test_daily_flat_market_never_trades():
    """一条水平线既无金叉也无死叉 → 一次都不交易（防「静默满仓」）。"""
    assert signals([10.0] * 60, fast=5, slow=10) == []


# ---------- 参数纪律 ----------

def test_fast_must_be_less_than_slow():
    with pytest.raises(ParamError) as e:
        TrendMA(fast=60, slow=20)
    assert "必须 <" in str(e.value)


def test_out_of_range_param_raises():
    with pytest.raises(ParamError):
        TrendMA(fast=1)
    with pytest.raises(ParamError):
        TrendMA(atr_mult=99.0)


# ---------- 「只用价量」：结构上读不到缺失字段 ----------

def test_required_features_is_empty():
    assert TrendMA.required_features == ()
    assert "atr_window" not in [p.name for p in TrendMA.PARAMS]   # ATR 窗口不是旋钮


def test_null_feature_fields_change_nothing():
    """`regime_label` / `main_net_5d` / `pe_pct_3y` 全为 NULL：
    策略没有声明它们 → 读不到 → 信号与「完全不传 features」逐字相同。"""
    s1 = strategy_registry.get("trend_ma", fast=5, slow=10)
    s2 = strategy_registry.get("trend_ma", fast=5, slow=10)
    bars = mk([10.0] * 10 + [20.0] * 10 + [5.0] * 10)
    nulls = {"regime_label": None, "main_net_5d": None, "pe_pct_3y": None,
             "vol_ratio_5_20": None}
    a, b = [], []
    h1, h2 = {"000001": []}, {"000001": []}
    for bar in bars:
        h1["000001"].append(bar)
        h2["000001"].append(bar)
        a.append(s1.generate(bar.date, h1, {}))
        b.append(s2.generate(bar.date, h2, {bar.date: {"000001": dict(nulls)}}))
    assert a == b
    assert any(sig for sig in a)           # 确认真有信号，不是「两边都空」而巧合相等


def test_atr_none_when_history_insufficient():
    from stocklab.strategies.trend_ma import _atr

    assert _atr(mk([10.0] * ATR_WINDOW), ATR_WINDOW) is None   # 不足 window+1 根 → None
    assert _atr(mk([10.0] * (ATR_WINDOW + 1)), ATR_WINDOW) == pytest.approx(0.2)
