"""主干常量的值与边界（D-27 / D-28）。

这些数字是**主干硬约束**：AI 不可改，越界一律拒绝、**不许 clamp**
（clamp 会让实验无法归因，见 ERROR_DIARY「参数静默夹紧」同源）。
"""

import pytest

from stocklab.config import limits


def test_constants_have_the_documented_values():
    assert limits.CIRCUIT_BREAKER_DRAWDOWN == 0.10
    assert (limits.VALIDATION_ROUNDS_MIN, limits.VALIDATION_ROUNDS_MAX) == (2, 3)
    assert limits.VALIDATION_MAX_DAYS == 30
    assert limits.FREEZE_MAX_DAYS == 90


def test_constants_are_primitives_not_mutable_containers():
    """常量必须是不可变标量 —— 可变容器等于开了个「就地改值」的后门。"""
    for name in ("CIRCUIT_BREAKER_DRAWDOWN", "VALIDATION_ROUNDS_MIN",
                 "VALIDATION_ROUNDS_MAX", "VALIDATION_MAX_DAYS", "FREEZE_MAX_DAYS"):
        assert isinstance(getattr(limits, name), (int, float))


def test_in_bounds_plan_is_accepted():
    limits.check_validation_plan(rounds=2, days=30)
    limits.check_validation_plan(rounds=3, days=30)
    limits.check_validation_plan(rounds=2, days=1)


@pytest.mark.parametrize("rounds,days", [
    (1, 30),      # 轮次 < 下界
    (4, 30),      # 轮次 > 上界
    (2, 31),      # 天数 > 上界
    (0, 30),      # 0 轮
    (2, 0),       # 0 天
])
def test_out_of_bounds_plan_is_rejected_not_clamped(rounds, days):
    with pytest.raises(limits.PlanOutOfBounds) as e:
        limits.check_validation_plan(rounds=rounds, days=days)
    # 报错要说清「收到了什么」，且**不许**出现「已调整/已截断」这类「我替你改好了」的措辞。
    # （断言改过一次：初版写 `"clamp" not in msg`，被实现里那句「不做 clamp」判红 ——
    #  正确消息**应当**声明它不 clamp，所以禁的是「悄悄改」的措辞，不是「clamp」这个词。）
    msg = str(e.value)
    assert "拒绝" in msg
    for word in ("已调整", "已截断", "已夹紧", "已限制为", "已修正"):
        assert word not in msg, f"越界消息里出现了「替你改好」的措辞：{word}"


def test_freeze_days_boundary():
    limits.check_freeze_days(days=90)
    with pytest.raises(limits.PlanOutOfBounds):
        limits.check_freeze_days(days=91)
