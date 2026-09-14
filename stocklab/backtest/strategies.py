"""回测用的**非生产**策略（只为把引擎跑通与做对照）。

`BuyAndHold` 不是策略注册表（P5）里的条目，它只用于：
  - 引擎的端到端联通（真实数据跑出净值/回撤/成交笔数）；
  - 与 `buy_and_hold` 基准互相对照（两者应给出同一量级的收益，差在成本与成交时点）。

真正的策略必须在 `strategy_registry` 里注册、经过 walk-forward 评估才允许上报
绩效（见 CLAUDE.md「准确率优先」章）。
"""

from __future__ import annotations

from stocklab.backtest.engine import Signal


class BuyAndHold:
    """首个交易日收盘出「满仓买入」信号，此后不再动作。

    成交发生在**次日开盘**（引擎的时点约定），因此它不是「首日开盘买入」，
    与 `metrics.buy_and_hold_nav`（首日开盘买入）会有**一个交易日的差异** ——
    对照时应当知道这一点，不要把差异当成成本。
    """

    name = "buy_and_hold"

    def __init__(self, code: str, size_pct: float = 100.0):
        self.code = code
        self.size_pct = size_pct
        self._done = False

    def generate(self, date, history, features):
        del date, features
        if self._done or not history.get(self.code):
            return {}
        self._done = True
        return {self.code: Signal("buy", self.size_pct, "buy_and_hold")}
