"""策略注册表（Task 26，P5）：**未注册的策略不允许进 walk-forward 评估**。

这条规则的价值在于：它把「评估口径」变成白名单。
任何能出现在绩效报告里的策略，都必须先在这里登记 `strategy_id`、
参数模式与必需特征 —— 于是「这个数字是用哪个策略、哪组参数跑出来的」
永远可回答，不会出现「临时脚本里改了一行就跑出来的成绩」。

两条显式失败（都不许静默）：
  - 重复 `strategy_id` 注册 → `DuplicateStrategy`（静默覆盖会让旧策略的成绩
    被新实现冒名顶替，而报告里的 id 看起来毫无变化）；
  - 取用未注册 id → `NotRegistered`（**不**返回默认策略、**不**自动兜底）。
"""

from __future__ import annotations

from stocklab.strategies.base import Strategy

#: 已注册的策略类
_REGISTRY: dict[str, type] = {}


class DuplicateStrategy(ValueError):
    """同一个 `strategy_id` 被注册两次。"""


class NotRegistered(LookupError):
    """取用了未注册的策略 id。"""


class StrategyRegistry:
    """策略白名单。`register` 是装饰器，`get` 是唯一的取用入口。"""

    def __init__(self) -> None:
        self._items: dict[str, type] = {}

    # ---------- 写 ----------

    def register(self, cls: type) -> type:
        sid = getattr(cls, "strategy_id", "")
        if not sid:
            raise ValueError(f"{cls.__name__} 未声明 strategy_id，无法注册")
        if not (isinstance(cls, type) and issubclass(cls, Strategy)):
            raise TypeError(f"{cls.__name__} 必须继承 strategies.base.Strategy")
        if sid in self._items:
            old = self._items[sid]
            raise DuplicateStrategy(
                f"策略 id {sid!r} 已注册（{old.__module__}.{old.__name__}），"
                f"不允许 {cls.__module__}.{cls.__name__} 覆盖它 —— "
                "静默覆盖会让报告里的同一个 id 指向两份不同的成绩"
            )
        self._items[sid] = cls
        return cls

    # ---------- 读 ----------

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

    def classes(self) -> dict[str, type]:
        return dict(self._items)

    def meta(self, strategy_id: str) -> dict:
        """参数模式描述；未注册时抛 `NotRegistered`。"""
        return self._resolve(strategy_id).describe()

    def get(self, strategy_id: str, **params) -> Strategy:
        """构造策略实例。**未注册 → `NotRegistered`**，绝不兜底。"""
        return self._resolve(strategy_id)(**params)

    def _resolve(self, strategy_id: str) -> type:
        cls = self._items.get(strategy_id)
        if cls is None:
            raise NotRegistered(
                f"策略 {strategy_id!r} 未注册，不允许进 walk-forward 评估。"
                f"已注册：{list(self.ids()) or '（空）'}。"
                "（本注册表刻意**不**提供默认兜底 —— 兜底会让报告里的策略名与实际"
                "执行的东西脱钩）"
            )
        return cls

    def __contains__(self, strategy_id: str) -> bool:
        return strategy_id in self._items

    def __len__(self) -> int:
        return len(self._items)


#: 进程内全局注册表（内置策略在 `stocklab/strategies/__init__.py` 里导入即注册）。
strategy_registry = StrategyRegistry()
