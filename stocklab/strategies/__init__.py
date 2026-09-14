"""策略库（P5）：统一接口 + 注册表 + 内置策略 + walk-forward 评估。

分层（只允许**一个**方向的依赖）：

    strategies/  →  backtest/（复用 engine.Signal / portfolio / metrics / benchmark）
                 →  data/    （Bar 类型）

`backtest/` **不**反向依赖本包 —— 引擎不认识注册表，也不该认识。
「哪些策略可以出现在报告里」是策略层的白名单，不是引擎的职责。

**导入本包即完成内置策略注册**（下面的 import 是刻意的副作用）：
`TrendMA` / `BuyAndHoldStrategy` 在各自模块里用 `@strategy_registry.register` 登记。
"""

from stocklab.strategies.base import (FeatureView, ParamError, ParamSpec, Strategy,
                                      UndeclaredFeature, clip_features, clip_history,
                                      make_feature_views, missing_declared_features,
                                      resolve_params)
from stocklab.strategies.registry import (DuplicateStrategy, NotRegistered,
                                          StrategyRegistry, strategy_registry)
# 内置策略：import 即注册（顺序重要：先注册表，后策略）
from stocklab.strategies.buy_hold import BuyAndHoldStrategy  # noqa: E402
from stocklab.strategies.trend_ma import TrendMA  # noqa: E402

__all__ = [
    "BuyAndHoldStrategy", "DuplicateStrategy", "FeatureView", "NotRegistered",
    "ParamError", "ParamSpec", "Strategy", "StrategyRegistry", "TrendMA",
    "UndeclaredFeature", "clip_features", "clip_history", "make_feature_views",
    "missing_declared_features", "resolve_params", "strategy_registry",
]
