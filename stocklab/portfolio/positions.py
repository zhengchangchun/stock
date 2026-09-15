"""持仓与盈亏推导（P12 / Task 54）：**纯函数**，不碰数据库。

方法：**加权平均成本法**（不是 FIFO）。
部分卖出按当时的均价结转已实现盈亏，**剩余持仓的均价不变** ——
这也是券商对账单的口径，模拟盘与实盘对比才有共同语言。

## 双口径

每一笔成本都同时维护两个数：

| 字段 | 含义 |
|---|---|
| `*_incl_fee` | **含费**：买入价×量 **+** 费用。账户里真正花掉的钱 |
| `*_excl_fee` | **不含费**：只有价×量。用来对用户口述的「成本 8,680」 |

字段名**必须带口径后缀**。默认口径是含费（ADR-006）；
不含费数字照样输出，因为用户报数时用的是它，两个口径对不上时必须能一眼看出差在哪
（差的就是费用），而不是让人怀疑账算错了。

## 已实现盈亏的两条式子

```
卖出时： a_incl = cost_incl / qty   （卖前均价）
        a_excl = cost_excl / qty
        realized_incl += (price*qty − fee) − a_incl*qty     # 卖出净额 − 结转成本(含费)
        realized_excl +=  price*qty         − a_excl*qty     # 卖出总额 − 结转成本(不含费)
```

两者**独立**、不是「一个减掉费用」的换算关系：只有全部清仓时
`realized_incl − realized_excl` 才恰好等于累计费用。混用会算错，所以两个都存。

## 精度

本层**不做四舍五入**，保留原始浮点；取整发生在输出层（`view.py`）。
理由：中间层取整会让「清仓后成本归零」这类恒等式不再成立（残渣无法消除）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


class LedgerError(ValueError):
    """账本层**可预期**的错误：校验不过、账不平、超卖。

    刻意用异常而不是返回码：调用方忘了检查返回值时，超卖会静默通过，
    写进账本的就是一份假持仓。写入口必须被异常拦住。
    """


@dataclass(frozen=True)
class Position:
    """某一只标的的持仓状态。`qty == 0`（已清仓）仍会返回，但不会进持仓表。"""

    code: str
    qty: int
    cost_incl_fee: float        # 剩余持仓总成本（含费）
    cost_excl_fee: float        # 剩余持仓总成本（不含费）
    realized_incl_fee: float    # 累计已实现盈亏（含费）
    realized_excl_fee: float    # 累计已实现盈亏（不含费）
    fees_paid: float            # 该标的累计付出的全部费用（买+卖）
    bought_qty: int
    sold_qty: int

    @property
    def avg_cost_incl_fee(self) -> float:
        """剩余持仓均价（含费）；已清仓时返回 0.0（不是 NaN，不是上一轮残值）。"""
        return self.cost_incl_fee / self.qty if self.qty else 0.0

    @property
    def avg_cost_excl_fee(self) -> float:
        return self.cost_excl_fee / self.qty if self.qty else 0.0

    def market_value(self, price: float) -> float:
        return price * self.qty

    def float_pnl_incl_fee(self, price: float) -> float:
        return price * self.qty - self.cost_incl_fee

    def float_pnl_excl_fee(self, price: float) -> float:
        return price * self.qty - self.cost_excl_fee


def _sort_key(row: Mapping):
    """回放顺序：`(date, trade_id)`。

    同一天内的先后由 `trade_id` 定 —— 账本里没有成交时刻，
    自增主键就是「录入顺序」这一唯一的时序信息。
    """
    return (str(row["date"]), int(row["trade_id"]))


def replay_trades(rows: Iterable[Mapping]) -> dict[str, Position]:
    """按 `(date, trade_id)` 回放全部成交，返回**出现过的每个标的**（含已清仓）。

    已清仓的标的也会返回（`qty == 0`），这样「清仓后成本归零、不是 NaN、
    也不留上一轮残渣」这条不变量是可观察、可测的。要渲染持仓表请用
    `open_positions()` —— 它把 `qty == 0` 滤掉。

    超卖（任一时点卖出量 > 持有量）抛 `LedgerError` —— 这是账本自身的不变量，
    违反说明账本已被写坏，不能继续算下去。
    """
    state: dict[str, dict] = {}
    for row in sorted(rows, key=_sort_key):
        code = str(row["code"])
        side = str(row["side"])
        qty = int(row["qty"])
        price = float(row["price"])
        fee = float(row["fee"])
        st = state.setdefault(code, {
            "code": code, "qty": 0, "cost_incl_fee": 0.0, "cost_excl_fee": 0.0,
            "realized_incl_fee": 0.0, "realized_excl_fee": 0.0,
            "fees_paid": 0.0, "bought_qty": 0, "sold_qty": 0,
        })
        st["fees_paid"] += fee
        if side == "buy":
            st["qty"] += qty
            st["bought_qty"] += qty
            st["cost_incl_fee"] += price * qty + fee
            st["cost_excl_fee"] += price * qty
        elif side == "sell":
            if qty > st["qty"]:
                raise LedgerError(
                    f"超卖：{code} 在 {row['date']} 卖出 {qty} 股，"
                    f"但当时只持有 {st['qty']} 股（trade_id={row['trade_id']}）"
                )
            a_incl = st["cost_incl_fee"] / st["qty"] if st["qty"] else 0.0
            a_excl = st["cost_excl_fee"] / st["qty"] if st["qty"] else 0.0
            st["realized_incl_fee"] += (price * qty - fee) - a_incl * qty
            st["realized_excl_fee"] += price * qty - a_excl * qty
            st["cost_incl_fee"] -= a_incl * qty
            st["cost_excl_fee"] -= a_excl * qty
            st["qty"] -= qty
            st["sold_qty"] += qty
            if st["qty"] == 0:
                # 清仓：成本归零。不做这一步，浮点残渣会渗进下一轮建仓。
                st["cost_incl_fee"] = 0.0
                st["cost_excl_fee"] = 0.0
        else:
            raise LedgerError(f"未知 side: {side!r}（只允许 buy/sell）")

    return {code: Position(**st) for code, st in state.items()}


def open_positions(rows: Iterable[Mapping]) -> dict[str, Position]:
    """只保留 `qty > 0` 的持仓 —— 组合视图渲染的就是这一组。"""
    return {c: p for c, p in replay_trades(rows).items() if p.qty > 0}


def qty_held_before(rows: Iterable[Mapping], code: str, date: str) -> int:
    """截至 `date`（含当日）该标的的持有量 —— 卖出校验用。

    新录入的卖单 `trade_id` 必然是最大值，所以「含当日、按 trade_id 排」等价于
    「排在当日已有成交之后」，正是它实际会发生的时序。
    """
    held = 0
    for row in sorted((r for r in rows if str(r["code"]) == code), key=_sort_key):
        if str(row["date"]) > date:
            break
        held += int(row["qty"]) if row["side"] == "buy" else -int(row["qty"])
    return held
