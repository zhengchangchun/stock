"""T1：趋势状态标签纯函数（`stocklab/trend/state.py`）。

预注册口径（**照抄，不许改**）：`UP`：`close_t > MA20_t` 且 `MA20_t > MA60_t`；
`DOWN`：`close_t < MA20_t` 且 `MA20_t < MA60_t`；`FLAT`：其余。
`MA20/MA60` 含当日收盘，无平滑无迟滞，`N=5`。

本文件要钉住四件事：

1. **等号边界**：三条不等式全是**严格**的 —— `close == MA20` 不是 UP 也不是 DOWN；
   `MA20 == MA60` 一定是 FLAT。差一个等号，FLAT 的占比就会整体挪位。
2. **PIT**：`i` 日的状态只用 `closes[:i+1]`。这不是靠自觉，而是
   `assert_pit_state_fn` 用两种错位输入去**证伪**：截断自证 + 未来扰动不变。
3. **未来函数必须被这套检查抓住** —— 故意写一个偷看未来的实现，
   检查器不吭声就等于没检查（ERROR_DIARY：「守卫只拦一半 = 没拦」）。
4. **与既有实现一致**：`risk.rules.trend_state` 已经按同一口径实现了这条标签，
   两份实现必须在随机数据上逐点相等。
"""

from __future__ import annotations

import random

import pytest

from stocklab.risk.rules import trend_state as risk_trend_state
from stocklab.trend.state import (MA_LONG, MA_SHORT, HORIZON, FutureLeakError,
                                  assert_pit_state_fn, forward_hit,
                                  forward_return, sma, state_series,
                                  trend_state)

CLOSES = [10.0 + 0.1 * i for i in range(200)]


def _pad(n: int, value: float) -> list[float]:
    """n 个常数收盘 —— 用来把某条均线关系钉在等号上。"""
    return [value] * n


# ---------- 1. 等号边界 ----------


def test_definition_constants_are_frozen_by_the_prereg():
    assert (MA_SHORT, MA_LONG, HORIZON) == (20, 60, 5)


def test_close_equal_to_ma20_is_not_up():
    """`close == MA20`：UP 要求**严格**大于 → 只能是 FLAT（而 MA20 确实 > MA60）。

    构造：前 40 根 10、后 40 根 11 → 第 79 根处 `MA20 = 11.0 = close`，
    同时 `MA60 = 10.667 < MA20`。差一个等号，这一天就会从 FLAT 挪进 UP。
    """
    closes = _pad(40, 10.0) + _pad(40, 11.0)
    assert sma(closes, 79, MA_SHORT) == pytest.approx(11.0)
    assert sma(closes, 79, MA_LONG) == pytest.approx(640 / 60)
    assert closes[79] == pytest.approx(sma(closes, 79, MA_SHORT))
    assert trend_state(closes, 79) == "FLAT"


def test_strictly_greater_close_with_ma20_above_ma60_is_up():
    """沿上升序列：close > MA20 > MA60 → UP（同一根序列再涨一天就翻成 UP）。"""
    closes = _pad(40, 10.0) + _pad(40, 11.0)
    assert trend_state(CLOSES, 100) == "UP"
    assert trend_state(closes + [11.5], 80) == "UP"


def test_descending_series_is_down():
    """`close < MA20 < MA60` → DOWN。"""
    closes = _pad(40, 11.0) + _pad(40, 10.0)
    assert trend_state(closes + [9.5], 80) == "DOWN"


def test_equal_moving_averages_are_flat_even_if_close_is_above():
    """`MA20 == MA60` → FLAT：第二条不等式也是严格的。

    构造：前 40 根 11、接着 10 根 10、最后 10 根 12 ——
    两条均线都等于 11，而当日收盘 12 **严格大于** MA20。
    此时第一条不等式成立、第二条是等号 → 仍是 FLAT。
    """
    flat = _pad(60, 10.0)
    assert trend_state(flat, 59) == "FLAT"          # close == MA20 == MA60
    closes = _pad(40, 11.0) + _pad(10, 10.0) + _pad(10, 12.0)
    ma20 = sma(closes, 59, MA_SHORT)
    ma60 = sma(closes, 59, MA_LONG)
    assert closes[59] > ma20 and ma20 == pytest.approx(ma60) == 11.0
    assert trend_state(closes, 59) == "FLAT"


def test_short_history_returns_none():
    """不足 MA60 根 → `None`（不是 FLAT：没算出来 ≠ 算出「横盘」）。"""
    assert trend_state(CLOSES[: MA_LONG - 1], MA_LONG - 2) is None
    assert trend_state(CLOSES[:MA_LONG], MA_LONG - 1) is not None
    assert all(s is None for s in state_series(CLOSES[: MA_LONG - 1]))


def test_flat_is_the_residue_not_a_threshold():
    """FLAT 只能由两条均线关系定义 —— 不引入任何分位阈值。"""
    # close 在 MA20 下方但 MA20 > MA60 → FLAT
    closes = [10.0] * 40 + [12.0] * 20
    closes[-1] = 10.5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    assert closes[-1] < ma20 and ma20 > ma60
    assert trend_state(closes, 59) == "FLAT"


# ---------- 2. sma ----------


def test_sma_is_the_trailing_mean_including_today():
    closes = [float(i) for i in range(100)]
    assert sma(closes, 19, 20) == pytest.approx(sum(range(0, 20)) / 20)
    assert sma(closes, 99, 60) == pytest.approx(sum(range(40, 100)) / 60)
    assert sma(closes, 18, 20) is None


# ---------- 3. PIT ----------


def test_state_at_i_equals_state_computed_from_the_prefix_only():
    """截断自证：只喂 `closes[:i+1]`，第 `i` 个状态必须逐字相同。"""
    for i in (59, 60, 101, 199):
        assert state_series(CLOSES[: i + 1])[i] == trend_state(CLOSES, i)


def test_future_prices_do_not_change_past_states():
    spiked = list(CLOSES)
    for i in range(150, 200):
        spiked[i] = 999.0
    a = state_series(CLOSES)[:150]
    b = state_series(spiked)[:150]
    assert a == b


def test_assert_pit_state_fn_accepts_the_real_implementation():
    assert_pit_state_fn(trend_state, CLOSES, at=(59, 60, 121))


def test_assert_pit_state_fn_catches_a_deliberate_future_leak():
    """偷看未来的实现必须**当场被抓** —— 否则这套检查等于没检查。"""

    def leaky(closes: list[float], i: int) -> str | None:
        if i < MA_LONG - 1:
            return None
        future = closes[-1]                     # 用了 t 之后的价格
        ma_s = sma(closes, i, MA_SHORT)
        ma_l = sma(closes, i, MA_LONG)
        if future > ma_s and ma_s > ma_l:
            return "UP"
        if future < ma_s and ma_s < ma_l:
            return "DOWN"
        return "FLAT"

    with pytest.raises(FutureLeakError):
        assert_pit_state_fn(leaky, CLOSES, at=(60, 121))


def test_assert_pit_state_fn_catches_a_one_day_lookahead():
    """把「明天的标签」当成「今天的标签」—— 一天的前视也必须被抓。"""

    def tomorrow(closes: list[float], i: int) -> str | None:
        if i < MA_LONG:
            return None
        return trend_state(closes, i + 1)

    with pytest.raises(FutureLeakError):
        assert_pit_state_fn(tomorrow, CLOSES, at=(80, 121))


def test_assert_pit_state_fn_catches_a_whole_series_mean():
    """用**全样本**均值当均线（最经典的前视）：必须被抓。"""

    def full_sample(closes: list[float], i: int) -> str | None:
        if i < MA_LONG - 1:
            return None
        ma_all = sum(closes) / len(closes)   # 读了 i 之后的价格（全样本均值）
        return "UP" if closes[i] > ma_all else "FLAT"

    with pytest.raises(FutureLeakError):
        assert_pit_state_fn(full_sample, CLOSES, at=(70, 130))


def test_assert_pit_state_fn_catches_a_centered_window():
    """错位的中心窗口（`rolling(center=True)` 那类）也必须被抓。

    在截断输入上它**根本求不了值**（窗口越界）—— 那本身就是「读了 i 之后」的
    直接证据，检查器必须报 `FutureLeakError` 而不是把异常吞掉。
    """

    def centered(closes: list[float], i: int) -> str | None:
        if i < MA_LONG - 1:
            return None
        ma_s = sma(closes, i + 10, MA_SHORT)     # 窗口中心错位到未来
        ma_l = sma(closes, i + 10, MA_LONG)
        if ma_s is None or ma_l is None:
            return "FLAT"
        if closes[i] > ma_s and ma_s > ma_l:
            return "UP"
        return "FLAT"

    with pytest.raises(FutureLeakError):
        assert_pit_state_fn(centered, CLOSES, at=(120,))


# ---------- 4. 与既有实现一致 ----------


def test_matches_the_existing_risk_rule_implementation():
    rng = random.Random(20260916)
    closes = [10.0]
    for _ in range(300):
        closes.append(max(0.1, closes[-1] * (1 + rng.gauss(0, 0.02))))
    for i in range(len(closes)):
        assert trend_state(closes, i) == risk_trend_state(closes, i)


# ---------- 5. 前向收益 / 命中 ----------


def test_forward_return_and_hit_use_close_at_t_plus_n():
    closes = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 9.0]
    assert forward_return(closes, 0, 5) == pytest.approx(0.05)
    assert forward_hit(closes, 0, 5) == 1
    assert forward_hit(closes, 1, 5) == 0        # 10.1 → 9.0 下跌
    assert forward_return(closes, 2, 5) is None  # 越过序列末端
    assert forward_hit(closes, 2, 5) is None


def test_flat_forward_price_counts_as_a_miss_and_is_separately_countable():
    """价格完全相等 → 计 0（=非命中），且调用方能把它数出来单独上报。"""
    closes = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    assert forward_hit(closes, 0, 5) == 0
    assert forward_return(closes, 0, 5) == pytest.approx(0.0)


def test_state_series_length_matches_input():
    assert len(state_series(CLOSES)) == len(CLOSES)
