"""持仓推进内核（Task 21）。

回测（历史批处理）与模拟盘（每日增量）共用本模块 —— 两套成交逻辑必然发散，
而发散的成交逻辑会让「模拟盘对不上回测」这类问题永远查不清（评审 D1）。

三条 A 股约束在这里落地，**一处定义、全局生效**：
  ① 成本含**最低 5 元/笔佣金**（R4）—— 走 `CostModel.fees`，本模块不自算费率；
  ② **T+1**：当日买入的股数当日不可卖（按股数锁定，不是按标的整体锁定）；
  ③ 涨跌停 / 停牌不可成交，涨跌停幅度**取自 `instruments.board`**
     （main 10% / gem 20% / star 20% / bse 30%），本模块**不设默认板**：
     板别未知时直接报错，避免「悄悄按主板 10% 判」这种静默降级。

价格一律是**复权价**（引擎侧强制 `adj_mode == "qfq"`）。本模块只管推进，
不读库、不抓数、不知道复权口径 —— 口径在边界上把关。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stocklab.config.costs import CostModel

#: 各板块涨跌停幅度（上交所/深交所/北交所现行规则）。
#: **不要**给它加 `dict.get(board, 0.10)` 式的默认值：未知板别必须报错，
#: 而不是按主板处理（计划里点名的坑）。
LIMIT_BY_BOARD: dict[str, float] = {
    "main": 0.10,   # 主板 ±10%
    "gem": 0.20,    # 创业板 ±20%
    "star": 0.20,   # 科创板 ±20%
    "bse": 0.30,    # 北交所 ±30%
}

#: 浮点比较容差：涨停价 = pre_close * 1.1 会出现 10.999999999999998 这类表示误差。
LIMIT_TOLERANCE = 1e-6


class BoardUnknown(ValueError):
    """板别不在 `LIMIT_BY_BOARD` 里 —— 无法判定涨跌停，拒绝猜。"""


@dataclass
class Position:
    code: str
    qty: int
    cost_price: float      # 含费成本价（元/股）


@dataclass(frozen=True)
class Trade:
    date: str
    code: str
    side: str              # "buy" | "sell"
    price: float           # 实际成交价（已含滑点）
    qty: int               # 股
    fee: float             # 元
    reason: str = ""


@dataclass(frozen=True)
class NavPoint:
    date: str
    nav: float


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    costs: CostModel = field(default_factory=CostModel)
    #: 当日买入的股数（T+1 锁定），`settle()` 时清空
    _locked_today: dict[str, int] = field(default_factory=dict, repr=False)

    # ---------- 交易约束 ----------

    def sellable_qty(self, code: str) -> int:
        """可卖股数 = 持仓 − 当日买入锁定（T+1）。"""
        pos = self.positions.get(code)
        if pos is None:
            return 0
        return max(0, pos.qty - self._locked_today.get(code, 0))

    def can_trade(self, code: str, bar, *, board: str, pre_close: float | None = None,
                  side: str = "buy") -> bool:
        """停牌 / 涨跌停检查。

        - 停牌：`bar.volume <= 0` → 不可成交（无量即无价）。
        - 涨跌停：`limit = LIMIT_BY_BOARD[board]`；买在涨停、卖在跌停时不可成交。
          **`board` 必传且必须已知**（不设默认板，见 `BoardUnknown`）。
        - `pre_close is None` 表示「窗口内第一根 K 线，没有前收盘可判」：
          此时只做停牌检查（新股首日本无涨跌幅限制）。这是**唯一**合法的 None
          场景 —— 引擎在有前一日行情时一律传入前收盘。

        价格口径：`bar` 必须是复权价（引擎已强制），否则除权日的假跌幅会把
        一次正常成交误判成跌停。
        """
        if board not in LIMIT_BY_BOARD:
            raise BoardUnknown(
                f"{code} 的板别 {board!r} 不在 {sorted(LIMIT_BY_BOARD)} 中，"
                "无法判定涨跌停（禁止按主板默认值处理）"
            )
        if bar.volume <= 0:
            return False
        if pre_close is None or pre_close <= 0:
            return True
        limit = LIMIT_BY_BOARD[board]
        if side == "buy":
            return bar.close < pre_close * (1.0 + limit) - LIMIT_TOLERANCE
        return bar.close > pre_close * (1.0 - limit) + LIMIT_TOLERANCE

    # ---------- 成交 ----------

    def max_buy_qty(self, ref_price: float, budget: float) -> int:
        """`budget` 元最多能买多少股（整手，**含费用**）。

        必须把费用算进去：按「金额 ≤ 现金」取整手会让「刚好满仓」的委托
        因为几元佣金而整笔失败（回测里表现为「策略从不满仓」这类幽灵问题）。
        """
        if ref_price <= 0 or budget <= 0:
            return 0
        qty = int(budget / ref_price / 100) * 100
        while qty > 0:
            price = self.costs.fill_price("buy", ref_price)
            if price * qty + self.costs.fees("buy", price, qty) <= budget:
                return qty
            qty -= 100
        return 0

    def buy(self, date: str, code: str, ref_price: float, qty: int,
            reason: str = "") -> Trade | None:
        """买入。现金不足（含费用）返回 `None`（调用方留痕），不改动任何状态。"""
        if qty <= 0:
            return None
        price = self.costs.fill_price("buy", ref_price)
        fee = self.costs.fees("buy", price, qty)
        total = price * qty + fee
        if total > self.cash + LIMIT_TOLERANCE:
            return None
        self.cash -= total
        pos = self.positions.get(code)
        if pos is None:
            self.positions[code] = Position(code, qty, total / qty)
        else:
            new_qty = pos.qty + qty
            pos.cost_price = (pos.cost_price * pos.qty + total) / new_qty
            pos.qty = new_qty
        self._locked_today[code] = self._locked_today.get(code, 0) + qty
        return Trade(date, code, "buy", price, qty, fee, reason)

    def sell(self, date: str, code: str, ref_price: float, qty: int,
             reason: str = "") -> Trade | None:
        """卖出。持仓不足 / 违反 T+1 返回 `None`，不改动任何状态。"""
        if qty <= 0 or qty > self.sellable_qty(code):
            return None
        price = self.costs.fill_price("sell", ref_price)
        fee = self.costs.fees("sell", price, qty)
        self.cash += price * qty - fee
        pos = self.positions[code]
        pos.qty -= qty
        if pos.qty == 0:
            del self.positions[code]
            self._locked_today.pop(code, None)
        return Trade(date, code, "sell", price, qty, fee, reason)

    def settle(self, date: str) -> None:
        """收盘结算：解除 T+1 锁定（每个交易日结束**必须**调用一次）。"""
        _ = date
        self._locked_today.clear()

    # ---------- 估值 ----------

    def market_value(self, prices: dict[str, float]) -> float:
        """持仓市值。缺失价格的标的按 0 计 —— 由 `missing_prices()` 留痕。"""
        return sum(p.qty * prices.get(c, 0.0) for c, p in self.positions.items())

    def missing_prices(self, prices: dict[str, float]) -> list[str]:
        """估值时缺价的标的（停牌/未采集）—— 调用方必须留痕，不许静默当 0。"""
        return sorted(c for c in self.positions if c not in prices)

    def nav(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)


def replay(initial_cash: float, trades, prices_by_date: dict[str, dict[str, float]],
           costs: CostModel) -> list[NavPoint]:
    """由**成交流水**重放净值曲线（评审 D1 的可执行形式）。

    用途：模拟盘的成交流水落库后，回测净值必须能从流水重放出来 ——
    重放结果与 `Portfolio` 逐日推进的结果不一致，说明两条路径已经发散。

    与 `Portfolio.buy/sell` 用**同一套**现金/成本算式（含费用计入成本价），
    但不重复施加 T+1 与涨跌停约束：流水是既成事实，重放只负责还原。
    """
    p = Portfolio(cash=initial_cash, positions={}, costs=costs)
    out: list[NavPoint] = []
    for t in trades:
        if t is None:
            continue
        if t.side == "buy":
            total = t.price * t.qty + t.fee
            p.cash -= total
            pos = p.positions.get(t.code)
            if pos is None:
                p.positions[t.code] = Position(t.code, t.qty, total / t.qty)
            else:
                new_qty = pos.qty + t.qty
                pos.cost_price = (pos.cost_price * pos.qty + total) / new_qty
                pos.qty = new_qty
        elif t.side == "sell":
            p.cash += t.price * t.qty - t.fee
            pos = p.positions.get(t.code)
            if pos is None:
                raise ValueError(
                    f"流水不自洽：{t.date} 卖出 {t.code} {t.qty} 股，但无持仓"
                )
            if t.qty > pos.qty:
                raise ValueError(
                    f"流水不自洽：{t.date} 卖出 {t.code} {t.qty} 股 > 持仓 {pos.qty}"
                )
            pos.qty -= t.qty
            if pos.qty == 0:
                del p.positions[t.code]
        else:
            raise ValueError(f"未知方向 {t.side!r}（只接受 buy/sell）")
        out.append(NavPoint(t.date, p.nav(prices_by_date.get(t.date, {}))))
    return out
