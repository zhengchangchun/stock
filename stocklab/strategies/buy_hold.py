"""`buy_and_hold`：躺平对照（Task 27，P5）。

**它是对照，不是策略结论**。存在的意义是回答「这个策略有没有跑赢什么都不做」。

## 三个 `buy_and_hold` 的口径差异（不许混为一谈）

| 实现 | 买入时点 | 复权口径 | 用途 |
|------|----------|----------|------|
| `metrics.buy_and_hold_nav` | 区间**首日开盘** | 调用方给的复权价 | 基准净值曲线（R5） |
| `backtest/strategies.py::BuyAndHold` | 首日收盘出信号 → **次日开盘**成交 | qfq（引擎强制） | 引擎端到端联通验证（单标的，非生产） |
| **本类**（注册表条目） | 每折运行窗首日收盘出信号 → 次日开盘成交，**每折重新建仓** | qfq | walk-forward **同尺对照** |

差异 1 是「滞后一个交易日」：引擎的时点约定是 T 日收盘出信号、T+1 开盘成交，
而 `buy_and_hold_nav` 直接从首日开盘买 —— 两者差一天的收益，**不是成本差异**。
差异 3 是「每折重新建仓」：walk-forward 的每折都从空仓 + `initial_cash` 开始，
所以 N 折就有 N 次买入，成本被折数放大。真实躺平只有一次买入。
报告必须把这两点写进披露，否则「策略跑赢了 buy_and_hold」可能只是因为
对照的建仓次数多、成本高。
"""

from __future__ import annotations

from stocklab.backtest.engine import Signal
from stocklab.strategies.base import Strategy
from stocklab.strategies.registry import strategy_registry


@strategy_registry.register
class BuyAndHoldStrategy(Strategy):
    strategy_id = "buy_and_hold"
    PARAMS: tuple = ()
    required_features: tuple[str, ...] = ()

    def __init__(self, **overrides):
        super().__init__(**overrides)
        self._bought: set[str] = set()

    def _generate(self, date, pit_history, pit_features):
        del pit_features
        out: dict[str, Signal] = {}
        for code in sorted(pit_history):
            bars = pit_history[code]
            if not bars or bars[-1].date != date:
                continue
            if code in self._bought:
                continue
            self._bought.add(code)
            out[code] = Signal("buy", 100.0, "buy_and_hold")
        return out
