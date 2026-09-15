"""现价解析（P12 / Task 56）：快照 → 日线 → **没有**。

## 只有三条路，没有第四条

1. **同日** `quote_snapshots`（取 `ts` 最大的一条）→ `source="snapshot"`
2. 否则 `bars_daily` 最近一个 `date ≤ asof` 的收盘 → `source="bars"`
3. 都没有 → 返回 `None`，调用方标记 `missing_price`

**不许用成本价冒充现价，不许插值。** 用成本价冒充会把浮亏恒显示成 0，
是所有"看起来正常"的错误里最坏的一种：它让人以为没事。

## 两个容易写错的地方

- **快照必须同日**，不是「找最近一条」。拿 09-15 的快照去给 09-14 估值
  就是用了未来信息 —— 组合视图在 09-14 那天不可能看到 09-15 的价格。
- **只认 `adj_mode='none'`**。复权价是"为算收益而调整过的历史序列"，
  不是"今天这只票值多少钱"。拿 qfq 价当现价，市值会凭空缩水
  （本仓 000333 的 qfq 价与真实价差着历年分红送股）。

`price_asof` 是**价格实际所属的日期**，不是查询的 asof。asof 当天停牌、
用上一交易日收盘时，`price_asof` 会如实指向那个更早的日子 —— 否则
「这个数字是哪天的」就没法追了。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

#: 现价只认不复权（见模块 docstring）。
LIVE_ADJ_MODE = "none"

SNAPSHOT_SQL = (
    "SELECT ts, price FROM quote_snapshots"
    " WHERE code = ? AND trade_date = ?"
    " ORDER BY ts DESC LIMIT 1"
)

BARS_SQL = (
    "SELECT date, close FROM bars_daily"
    " WHERE code = ? AND date <= ? AND adj_mode = ?"
    " ORDER BY date DESC LIMIT 1"
)


@dataclass(frozen=True)
class Price:
    """一个可用于估值的价格，**自带出处**。"""

    code: str
    price: float
    source: str        # 'snapshot' | 'bars'
    price_asof: str    # 价格实际所属的日期（不一定等于查询的 asof）
    detail: str        # snapshot 的 ts / bars 的日期，供追溯

    @property
    def is_snapshot(self) -> bool:
        return self.source == "snapshot"


def resolve_price(conn: sqlite3.Connection, code: str, asof: str) -> Price | None:
    """解析 `code` 在 `asof` 的现价；拿不到返回 `None`（调用方必须显式处理）。

    返回 `None` 不是错误 —— 它是一种**要上报的状态**（`missing_price`），
    而不是一个可以拿别的东西填上的空位。
    """
    row = conn.execute(SNAPSHOT_SQL, (code, asof)).fetchone()
    if row is not None:
        return Price(code=code, price=float(row["price"]), source="snapshot",
                     price_asof=asof, detail=str(row["ts"]))

    row = conn.execute(BARS_SQL, (code, asof, LIVE_ADJ_MODE)).fetchone()
    if row is not None:
        return Price(code=code, price=float(row["close"]), source="bars",
                     price_asof=str(row["date"]), detail=str(row["date"]))

    return None


def resolve_prices(conn: sqlite3.Connection, codes, asof: str) -> dict[str, Price]:
    """批量解析；**拿不到的键直接不存在**（调用方用 `.get()` 得到 None）。"""
    out: dict[str, Price] = {}
    for code in codes:
        p = resolve_price(conn, code, asof)
        if p is not None:
            out[code] = p
    return out
