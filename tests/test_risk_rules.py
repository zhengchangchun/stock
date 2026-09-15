"""P14 / Task 63：内置 PIT 规则的**信号口径**（纯函数，不碰数据库）。

回放的价格/成本/成交由 CLI 在真实数据上端到端验收；这里钉住最容易出错、
也最容易「悄悄变乐观」的两件事：① 信号只用 `≤ t` 的收盘价；② 成交在 `t+1`。
"""

from __future__ import annotations

import pytest

from stocklab.risk.rules import (LOT, MA_LONG, MA_SHORT, RULES, _qty, ma_pair,
                                 signals, trend_state)


def _series(flat: int, up: int, step: float = 0.20) -> list[float]:
    """`flat` 根走平 + `up` 根单边上涨（都是收盘价序列）。"""
    xs = [10.0] * flat
    for _ in range(up):
        xs.append(xs[-1] + step)
    return xs


def test_rules_tuple_is_the_documented_set():
    assert RULES == ("trend-state", "ma-cross", "buy-hold")


def test_trend_state_needs_enough_history():
    """不足 MA60 根 → None（**不许**用未来数据凑够窗口）。"""
    assert trend_state([10.0] * (MA_LONG - 1), MA_LONG - 2) is None
    assert trend_state([10.0] * MA_LONG, MA_LONG - 1) == "FLAT"


def test_trend_state_is_up_only_when_close_above_ma20_above_ma60():
    closes = _series(flat=60, up=40)
    i = len(closes) - 1
    ma_s, ma_l = ma_pair(closes, i)
    assert ma_s > ma_l and closes[i] > ma_s
    assert trend_state(closes, i) == "UP"
    # 走平段：close 不高于 ma20（等于）→ 不是 UP
    assert trend_state([10.0] * 80, 79) == "FLAT"


def test_trend_state_does_not_peek_forward():
    """同一根 i 的状态，与后面发生什么**无关**（截断序列结果一致）。"""
    closes = _series(flat=60, up=40)
    i = 70
    assert trend_state(closes, i) == trend_state(closes[:i + 1], i)


def test_trend_state_buys_on_the_transition_and_sells_on_the_exit():
    closes = _series(flat=60, up=60)
    sig = signals("trend-state", closes)
    assert sig[0][1] == "buy"
    assert sig[0][0] >= MA_LONG - 1          # 均线没成型前不可能有信号
    assert all(side in ("buy", "sell") for _, side in sig)
    # 信号交替出现，不会连着两次 buy
    sides = [s for _, s in sig]
    assert all(a != b for a, b in zip(sides, sides[1:]))


def test_ma_cross_buys_on_the_upward_cross():
    closes = _series(flat=60, up=40)
    sig = signals("ma-cross", closes)
    assert sig and sig[0][1] == "buy"
    assert sig[0][0] >= MA_LONG


def test_buy_hold_is_a_single_buy_at_the_first_bar():
    assert signals("buy-hold", [10.0, 11.0, 12.0]) == [(0, "buy")]
    assert signals("buy-hold", []) == []


def test_unknown_rule_is_rejected_loudly():
    with pytest.raises(ValueError, match="未知规则"):
        signals("magic", [10.0] * 100)


def test_qty_is_always_a_whole_lot():
    assert _qty(10.0, 10_000.0) == 1000
    assert _qty(86.80, 10_000.0) == 100          # 115 股 → 向下取整到 1 手
    assert _qty(10.0, 10_000.0) % LOT == 0


def test_qty_is_never_worse_than_one_lot_and_never_exceeds_the_notional():
    """回放固定名义本金：至少 1 手（否则算不出往返）；买得起时不得超过本金。"""
    assert _qty(200.0, 100.0) == LOT            # 本金买不起 1 手 → 仍按 1 手回放（口径统一）
    assert _qty(86.80, 10_000.0) * 86.80 <= 10_000.0
