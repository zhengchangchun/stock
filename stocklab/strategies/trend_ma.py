"""`trend_ma`：均线趋势跟随 + ATR 止损（Task 27，P5）。

规则（**参数为文档默认值，本任务不做任何搜索** —— R7 反过拟合红线）：

    fast 上穿 slow（金叉）→ 满仓买入
    fast 下穿 slow（死叉）→ 清仓卖出
    持仓中收盘价 < 入场收盘价 − atr_mult × ATR14 → 止损清仓

**只用价量**：全部输入来自复权 K 线的 OHLC，`required_features = ()`。
好处是结构性的：`main_net_5d` / `pe_pct_3y` / `regime_label` 这三个当前全为 NULL
的字段，本策略**连读都读不到**（`FeatureView` 只放行声明过的字段），
所以「NULL 被当成 0」这一类错误在本策略里不可能发生。

**已知口径限制（必须披露，不许粉饰）**：`_in_market` 记的是**意图仓位**。
引擎在涨停买不到 / 跌停卖不掉 / 停盘 / 资金不足时会丢弃信号（记进 `metrics["rejected"]`），
而策略并不知道 —— 于是意图与实际持仓可能发散。报告里必须同时给出 `n_rejected`；
本策略不做「成交回报」式的状态同步（那需要引擎 → 策略的反向通道，属 P5 下半）。
"""

from __future__ import annotations

from statistics import fmean

from stocklab.backtest.engine import Signal
from stocklab.data.models import Bar
from stocklab.strategies.base import ParamSpec, ParamError, Strategy
from stocklab.strategies.registry import strategy_registry

#: ATR 窗口写死为 14（与 `features.registry` 的 atr14 同口径）。
#: 刻意**不做成参数**：每多一个旋钮就多一个可以被"调到跑赢"的自由度，
#: 而本任务的全部意义是拿到**无调参**的样本外数字。
ATR_WINDOW = 14


@strategy_registry.register
class TrendMA(Strategy):
    strategy_id = "trend_ma"
    PARAMS = (
        ParamSpec("fast", int, 20, 5, 250, "快线窗口（交易日）"),
        ParamSpec("slow", int, 60, 10, 500, "慢线窗口（交易日）"),
        ParamSpec("atr_mult", float, 2.0, 0.5, 10.0, "ATR 止损倍数"),
    )
    required_features: tuple[str, ...] = ()

    def __init__(self, **overrides):
        super().__init__(**overrides)
        if self.params["fast"] >= self.params["slow"]:
            # 「快线不快」不是越界，是**语义矛盾** —— 同样不许静默交换，
            # 否则调用方以为自己在测 20/60，实际测的是 60/20。
            raise ParamError(
                f"fast({self.params['fast']}) 必须 < slow({self.params['slow']})；"
                "静默交换参数会让实验台账与实际执行脱钩"
            )
        self._in_market = False
        self._entry_close: float | None = None

    # ---------- 主逻辑 ----------

    def _generate(self, date, pit_history, pit_features):
        del pit_features          # required_features = () → 本策略不消费任何特征字段
        fast, slow = self.params["fast"], self.params["slow"]
        out: dict[str, Signal] = {}
        for code in sorted(pit_history):
            bars = pit_history[code]
            # 当日无 K 线（停牌/非交易日/采集缺口）→ 不出信号。
            # 拿「最近一根」当成今日，等于用昨天的收盘价给今天做决定。
            if not bars or bars[-1].date != date:
                continue
            closes = [b.close for b in bars]
            if len(closes) < slow + 1:
                continue
            f_now = fmean(closes[-fast:])
            s_now = fmean(closes[-slow:])
            f_prev = fmean(closes[-fast - 1:-1])
            s_prev = fmean(closes[-slow - 1:-1])
            sig = self._decide(code, f_now, s_now, f_prev, s_prev,
                               _atr(bars, ATR_WINDOW), closes[-1])
            if sig is not None:
                out[code] = sig
        return out

    def _decide(self, code, f_now, s_now, f_prev, s_prev, atr, close) -> Signal | None:
        if not self._in_market:
            if f_prev <= s_prev and f_now > s_now:
                self._in_market = True
                self._entry_close = close
                return Signal("buy", 100.0, f"ma_golden_cross:{self.params['fast']}/"
                                            f"{self.params['slow']}")
            return None
        if f_prev >= s_prev and f_now < s_now:
            self._in_market = False
            self._entry_close = None
            return Signal("sell", 100.0, "ma_death_cross")
        mult = self.params["atr_mult"]
        if atr is not None and self._entry_close is not None \
                and close < self._entry_close - mult * atr:
            self._in_market = False
            self._entry_close = None
            return Signal("sell", 100.0, f"atr_stop:{mult}x")
        return None


def _atr(bars: list[Bar], window: int) -> float | None:
    """简单平均版 ATR（与 `features.indicators.atr` 同口径，此处不引 pandas）。

    不足 `window + 1` 根时返回 `None`（= 不可计算），**不是 0**：
    0 会让 `close < entry - mult*0` 退化成「只要跌破入场价就止损」，
    一个静默的口径变化。
    """
    if len(bars) < window + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-window - 1:-1], bars[-window:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    return fmean(trs)
