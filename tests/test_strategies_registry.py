"""Task 26：策略注册表（P5）—— 白名单语义必须**显式失败**。

注册表存在的唯一理由：让「哪些策略可以出现在绩效报告里」成为可回答的问题。
所以它的两条失败路径必须比成功路径更硬：
  - 重复 id → `DuplicateStrategy`（静默覆盖会让同一个 id 指向两份成绩）；
  - 未注册 id → `NotRegistered`（**不**兜底、**不**返回默认策略）。
"""

import pytest

from stocklab.strategies.base import Strategy
from stocklab.strategies.buy_hold import BuyAndHoldStrategy
from stocklab.strategies.registry import (DuplicateStrategy, NotRegistered,
                                          StrategyRegistry, strategy_registry)
from stocklab.strategies.trend_ma import TrendMA


def test_builtins_are_registered():
    assert "trend_ma" in strategy_registry
    assert "buy_and_hold" in strategy_registry
    # 只断言内置策略**在**注册表里：别的测试模块会注册自己的探针策略，
    # 那不影响本测试的意图（内置策略必须注册），却会让精确相等变成脆弱断言。
    assert {"buy_and_hold", "trend_ma"} <= set(strategy_registry.ids())


def test_get_builds_instance_with_params():
    s = strategy_registry.get("trend_ma")
    assert isinstance(s, TrendMA)
    assert s.params == {"fast": 20, "slow": 60, "atr_mult": 2.0}
    assert strategy_registry.get("trend_ma", fast=30).params["fast"] == 30
    assert isinstance(strategy_registry.get("buy_and_hold"), BuyAndHoldStrategy)


def test_duplicate_id_raises_explicitly():
    reg = StrategyRegistry()
    reg.register(TrendMA)
    with pytest.raises(DuplicateStrategy) as e:

        @reg.register
        class Another(Strategy):
            strategy_id = "trend_ma"

            def _generate(self, date, h, f):
                return {}

    msg = str(e.value)
    assert "trend_ma" in msg and "不允许" in msg


def test_unregistered_raises_and_lists_known():
    reg = StrategyRegistry()
    with pytest.raises(NotRegistered) as e:
        reg.get("does_not_exist")
    assert "does_not_exist" in str(e.value)
    assert "已注册" in str(e.value)


def test_meta_requires_registration():
    reg = StrategyRegistry()
    with pytest.raises(NotRegistered):
        reg.meta("does_not_exist")


def test_register_rejects_non_strategy():
    reg = StrategyRegistry()

    class NotAStrategy:
        strategy_id = "nope"

    with pytest.raises(TypeError):
        reg.register(NotAStrategy)


def test_register_rejects_missing_id():
    reg = StrategyRegistry()

    class NoId(Strategy):
        def _generate(self, date, h, f):
            return {}

    with pytest.raises(ValueError):
        reg.register(NoId)


def test_meta_shape():
    m = strategy_registry.meta("trend_ma")
    assert m["strategy_id"] == "trend_ma"
    names = [p["name"] for p in m["params"]]
    assert names == ["fast", "slow", "atr_mult"]
    assert m["required_features"] == []


def test_registries_are_independent():
    """局部注册表不得污染全局注册表（否则单测之间会互相看不见地串味）。"""
    reg = StrategyRegistry()
    assert len(reg) == 0
    assert len(strategy_registry) >= 2
