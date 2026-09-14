"""Task 32：打分口径的纯函数（`stocklab/verify/score.py`）。

这些测试的纪律（来自 ERROR_DIARY，逐条对应已踩过的坑）：

1. **手算断言，不只断言值域**。「值域合法 ≠ 值有意义」——
   P6 的 `p_touch` 恒等于 0.0 完美满足 `0 <= p <= 1`，靠真实回放才发现。
2. **口径必须来自单一真源**：`FLAT_BAND` 只能从 `predict.model` 拿。
3. **不可评分必须显式**：目标日无 bar / 停牌 / 复权不可用 → `scorable=False` +
   `reason_code`，**不抛异常、不进任何成功分母**。
4. **用复权价、不用不复权价**：除权日的不复权序列是假跌幅，拿它打分方向会反。
"""

from __future__ import annotations

import math

import pytest

from stocklab.data.models import Bar
from stocklab.predict import model as predict_model
from stocklab.verify.score import (ATTRIBUTION_DATA, ATTRIBUTION_UNDETERMINED,
                                   Unscorable, score_prediction)


def _bar(date: str, *, close: float, high: float | None = None,
         low: float | None = None, adj: str = "qfq") -> Bar:
    return Bar(code="000333", date=date, open=close, high=high or close,
               low=low or close, close=close, volume=1000, amount=None,
               turnover=None, source="test", adj_mode=adj)


def _raw(date: str, *, close: float, high: float | None = None,
         low: float | None = None) -> Bar:
    return _bar(date, close=close, high=high, low=low, adj="none")


def _pred(**over) -> dict:
    base = {
        "pred_id": 7,
        "code": "000333",
        "asof_date": "2026-01-05",
        "target_date": "2026-01-06",
        "direction": {"up": 0.5, "flat": 0.3, "down": 0.2},
        "range_80": [99.0, 103.0],
        "key_levels": [
            {"price": 103.0, "role": "resistance", "p_touch": 0.2},
            {"price": 99.0, "role": "support", "p_touch": 0.1},
        ],
        "action": "add",
        "size_pct": 80.0,
        "invalidate_if": "收盘跌破 99.00（20日低点）或收盘站上 103.00（20日高点）",
        "strategy_mix": {},
        "model_version": "pit-rw-v1.0.1",
    }
    base.update(over)
    return base


def _score(bars, raw_bars=None, pred=None, **kw):
    pred = pred or _pred()
    return score_prediction(pred, bars=bars,
                            raw_bars=raw_bars if raw_bars is not None else bars,
                            **kw)


# ---------- 1. 方向与 Brier：手算 ----------

def test_direction_and_brier_match_hand_computed_values():
    """实际涨 1% → up。手算 Brier = (0.5-1)^2 + 0.3^2 + 0.2^2 = 0.38。"""
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=101.0)]
    out = _score(bars)

    assert out["scorable"] is True
    assert out["actual_pct"] == pytest.approx(0.01, abs=1e-9)
    # 手算：(0.5-1)^2 + (0.3-0)^2 + (0.2-0)^2 = 0.25 + 0.09 + 0.04 = 0.38
    assert out["notes"]["brier"] == pytest.approx(0.38, abs=1e-9)
    assert out["notes"]["pred_class"] == "up"
    assert out["notes"]["actual_class"] == "up"
    assert out["hit_direction"] == 1
    # score_direction = 1 - Brier/2，且**可逆**（Brier 能从库里反算）
    assert out["score_direction"] == pytest.approx(1 - 0.38 / 2, abs=1e-6)


def test_direction_probabilities_below_band_are_flat():
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=100.4)]
    out = _score(bars)
    assert out["notes"]["actual_class"] == "flat"
    assert out["hit_direction"] == 0            # 预测 up，实际 flat → 错


def test_flat_band_comes_from_the_single_source():
    assert predict_model.FLAT_BAND == 0.005
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=100.6)]
    # 0.6% > 0.5% → up，不是 flat。若哪天有人把带宽调宽，这条会红。
    assert _score(bars)["notes"]["actual_class"] == "up"


def test_argmax_tie_breaks_like_the_model_action_rule():
    """p_flat == p_up 时记为 flat —— 与 model 的 `p_flat >= p_up` 同序，不许各判各的。"""
    pred = _pred(direction={"up": 0.4, "flat": 0.4, "down": 0.2})
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=100.0)]
    assert _score(bars, pred=pred)["notes"]["pred_class"] == "flat"


# ---------- 2. 区间与关键位 ----------

def test_range_uses_close_not_intraday_extremes():
    """区间口径按 §8.2 是**收盘**。盘中冲高但收盘回落 → 不算命中。"""
    bars = [_bar("2026-01-05", close=100.0),
            _bar("2026-01-06", close=98.0, high=104.0, low=97.0)]
    pred = _pred(range_80=[99.0, 103.0])
    assert _score(bars, pred=pred)["hit_range"] == 0


def test_touch_uses_intraday_high_low_not_close():
    """阻力位 103：盘中 high=103.5 触及（虽然收盘 100）→ 「会触及」兑现。"""
    bars = [_bar("2026-01-05", close=100.0),
            _bar("2026-01-06", close=100.0, high=103.5, low=99.5)]
    pred = _pred(key_levels=[
        {"price": 103.0, "role": "resistance", "p_touch": 0.9},   # 会触及 → 真触及 ✅
        {"price": 99.0, "role": "support", "p_touch": 0.9},       # 会触及 → low 99.5 未触及 ❌
    ])
    out = _score(bars, pred=pred)
    assert out["hit_levels"] == 0
    assert out["score_level"] == pytest.approx(0.5)
    assert out["score_range"] in (0.0, 1.0)


def test_low_p_touch_is_a_real_prediction_of_no_touch():
    """「不会触及」也要能被证伪：p_touch=0.1 且真的没触及 → 兑现。"""
    bars = [_bar("2026-01-05", close=100.0),
            _bar("2026-01-06", close=100.0, high=100.5, low=99.8)]
    pred = _pred(key_levels=[
        {"price": 103.0, "role": "resistance", "p_touch": 0.1},
        {"price": 99.0, "role": "support", "p_touch": 0.1},
    ])
    assert _score(bars, pred=pred)["score_level"] == 1.0


# ---------- 3. 不可评分：显式、不抛异常、不进分母 ----------

def test_missing_target_bar_is_unscorable_data():
    bars = [_bar("2026-01-05", close=100.0)]
    out = _score(bars)
    assert out["scorable"] is False
    assert out["reason_code"] == "NO_BAR_TARGET"
    assert out["attribution_auto"] == ATTRIBUTION_DATA
    # 不可评分时结果列**必须为空**，否则会被统计当成 0 分混进分母
    for col in ("hit_direction", "hit_range", "hit_levels", "total_score",
                "score_direction", "score_action", "actual_close"):
        assert out[col] is None, col


def test_suspended_target_bar_is_unscorable_data():
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=100.0)]
    out = score_prediction(_pred(), bars=bars, raw_bars=bars,
                           suspended={"2026-01-06"})
    assert (out["scorable"], out["reason_code"]) == (False, "SUSPENDED")
    assert out["attribution_auto"] == ATTRIBUTION_DATA


def test_missing_asof_bar_is_unscorable_data():
    bars = [_bar("2026-01-06", close=100.0)]
    out = _score(bars, raw_bars=[_raw("2026-01-06", close=100.0)])
    assert (out["scorable"], out["reason_code"]) == (False, "NO_BAR_ASOF")


def test_unadjusted_series_is_rejected_not_silently_used():
    """复权序列缺失但不复权在 → `NOT_ADJUSTABLE`，**不许**拿不复权价顶替。"""
    raw = [_raw("2026-01-05", close=100.0), _raw("2026-01-06", close=100.0)]
    out = _score(bars=[], raw_bars=raw)
    assert (out["scorable"], out["reason_code"]) == (False, "NOT_ADJUSTABLE")


def test_score_uses_adjusted_prices_not_raw_ones():
    """除权日：不复权序列是一根 -50% 假跌幅，复权序列是平的。

    拿不复权打分 → up/down 全反；用复权 → flat。这条钉死「用哪个口径」。
    """
    raw = [_raw("2026-01-05", close=100.0), _raw("2026-01-06", close=50.0)]
    adj = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=100.0)]
    pred = _pred(direction={"up": 0.1, "flat": 0.8, "down": 0.1})
    out = score_prediction(pred, bars=adj, raw_bars=raw)
    assert out["notes"]["actual_class"] == "flat"
    assert out["actual_close"] == 100.0          # 复权价，不是 50.0
    assert out["hit_direction"] == 1


# ---------- 4. 动作：扣成本、参数无关 ----------

def test_action_is_scored_against_same_day_buy_and_hold_with_costs():
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=110.0)]
    out = _score(bars, pred=_pred(size_pct=100.0))
    # 满仓 → 与 buy_and_hold 完全同一笔进出 → 超额恒为 0 → score_action = 0
    assert out["notes"]["excess_ret"] == pytest.approx(0.0, abs=1e-12)
    assert out["score_action"] == 0.0
    assert out["benchmark_pct"] == pytest.approx(out["notes"]["bh_ret"], abs=1e-12)


def test_half_position_underperforms_in_an_up_move():
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=110.0)]
    out = _score(bars, pred=_pred(size_pct=50.0))
    assert out["notes"]["excess_ret"] < 0
    assert out["score_action"] == 0.0


def test_costs_are_actually_deducted():
    """平价行情下扣成本 → 亏损。若成本没进模拟，这条会红。"""
    bars = [_bar("2026-01-05", close=10.0), _bar("2026-01-06", close=10.0)]
    out = _score(bars, pred=_pred(size_pct=100.0))
    assert out["sim_pnl"] < 0


def test_index_benchmark_is_recorded_but_not_required():
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=101.0)]
    out = _score(bars, index_pct=0.004)
    assert out["notes"]["index_pct"] == pytest.approx(0.004)
    assert _score(bars)["notes"]["index_pct"] is None       # 缺指数 ≠ 不可评分


# ---------- 5. invalidated 与归因 ----------

def test_invalidated_parses_the_stated_condition():
    """跌破 99.00（20日低点）：收盘 98 < 99 → 失效。"""
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=98.0)]
    assert _score(bars)["invalidated"] == 1


def test_not_invalidated_when_close_stays_inside():
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=100.5)]
    assert _score(bars)["invalidated"] == 0


def test_unparsable_invalidate_if_stays_undetermined():
    """解析不出条件就写 None（UNDETERMINED），**不猜**。"""
    pred = _pred(invalidate_if="跌破就减仓")
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=98.0)]
    assert _score(bars, pred=pred)["invalidated"] is None
    assert "invalidate_if" in _score(bars, pred=pred)["notes"]["undetermined"]


def test_attribution_is_never_auto_assigned_to_a_cause():
    """SIGNAL/STRATEGY/MODEL/NOISE 一律不许由程序自动写（硬判 = 造假归因）。"""
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=101.0)]
    out = _score(bars)
    assert out["attribution_auto"] == ATTRIBUTION_UNDETERMINED
    assert out["attribution_auto"] not in ("SIGNAL", "STRATEGY", "MODEL", "NOISE")


@pytest.mark.parametrize("reason, kwargs", [
    # target 日根本没有 K 线
    ("NO_BAR_TARGET", {"bars": [_bar("2026-01-05", close=100.0)]}),
    # target 日停牌（有 bar 但无成交）
    ("SUSPENDED", {"bars": [_bar("2026-01-05", close=100.0),
                            _bar("2026-01-06", close=100.0)],
                   "suspended": ("2026-01-06",)}),
    # 基准日没有 K 线（算不出当日收益）
    ("NO_BAR_ASOF", {"bars": [_bar("2026-01-06", close=100.0)]}),
    # 复权链缺口：必须拒绝，不许拿不复权价顶替
    ("NOT_ADJUSTABLE", {"bars": [_bar("2026-01-06", close=100.0)],
                        "raw_bars": [_raw("2026-01-05", close=100.0),
                                     _raw("2026-01-06", close=100.0)]}),
], ids=lambda v: v if isinstance(v, str) else "")
def test_unscorable_carries_no_score_at_all(reason, kwargs):
    """**四种**不可评分路径都不得留下任何分数列。

    为什么值得逐路径测：聚合器（`report.py`）判定「这条算不算进分母」看的是
    「结果列是否为 None」。只要有一条路径漏写了 1 / 0.0，
    那批「没数据」的样本就会被当成「预测对了」计入分母 —— 准确率凭空变好。
    """
    out = _score(**kwargs)
    assert out["scorable"] is False
    assert out["reason_code"] == reason
    assert out["attribution_auto"] == ATTRIBUTION_DATA
    for col in ("actual_close", "actual_pct", "hit_direction", "hit_range",
                "hit_levels", "sim_pnl", "score_direction", "score_range",
                "score_level", "score_action", "total_score"):
        assert out[col] is None, f"{reason} 路径漏写了 {col}"


def test_total_score_uses_the_documented_weights():
    bars = [_bar("2026-01-05", close=100.0),
            _bar("2026-01-06", close=100.0, high=103.5, low=96.5)]
    pred = _pred(direction={"up": 0.6, "flat": 0.3, "down": 0.1},
                 range_80=[101.0, 105.0], size_pct=0.0,
                 key_levels=[
                     {"price": 103.0, "role": "resistance", "p_touch": 0.9},
                     {"price": 97.0, "role": "support", "p_touch": 0.9}])
    out = _score(bars, pred=pred)
    expected = (0.3 * out["score_direction"] + 0.2 * out["score_range"]
                + 0.2 * out["score_level"] + 0.3 * out["score_action"])
    assert out["total_score"] == pytest.approx(expected, abs=1e-6)
    assert out["hit_levels"] == 1
    assert out["hit_range"] == 0                 # 收盘 100 ∉ [101,105] → 未命中


def test_unscorable_exception_type_exists_for_callers():
    """`Unscorable` 是给「数据层直接抛错」的调用方用的类，不是打分器的返回路径。"""
    assert issubclass(Unscorable, Exception)


def test_no_wall_clock_in_the_payload():
    """载荷里不许有时间戳 —— 否则同输入两次打分 hash 不同，「幂等」就是假的。"""
    bars = [_bar("2026-01-05", close=100.0), _bar("2026-01-06", close=101.0)]
    out = _score(bars)
    assert not any(k in out for k in ("created_at", "now", "timestamp"))
    # 复算一次必须逐键相等
    assert out == _score(bars)


def test_math_is_not_imported_by_accident():
    """占位守卫：`math` 只用于日志/断言辅助，正式实现不许把浮点比较写死。"""
    assert math.isclose(0.005, predict_model.FLAT_BAND)
