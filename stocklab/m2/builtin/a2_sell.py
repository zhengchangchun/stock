"""`m2_a2`（AI 模拟卖出条件）**源文本** —— 首版 v1.0.0。

## 只看 `ctx["holdings"]`，只给 `side='sell'`

A2 的允许集合是**账户当前持仓**（`plugin_hooks.sell_orders` 的 `held_codes`），
它能卖的只有手里有的 —— 给池子会允许它「卖掉一个没持有的标的」，那在 A 股是融券。
买入侧不归它管（那是 A1 的职责），所以 `side` 只可能是 `sell`（契约层已钉死）。

## 一条持仓最多一条指令

规则按 ① 止损 → ② 止盈 → ③ 调仓退出 的顺序**先命中先算**（`if/elif`）。
同一支同时命中「止损」与「掉出池子」时给两条指令，会让下游按目标权重 0 结算两次
—— 一笔清仓意图变成两条腿，报告里看不出是谁决定的。

## 「不知道成本」⇒ 一条都不卖（含规则 ③）

任务书 §1.4 的原文是「`cost_price is None`（账户里没记成本）⇒ 不卖
（『不知道』≠『该卖』）」。这里的落地是**字面读法**：没记成本的持仓连
「掉出池子 ⇒ 调仓退出」也不做。代价写在明处：这类持仓会一直躺在账户里
（现状它们本来就存在 —— 成交表里没有成本列，后续买入的标的一律 `cost_price=None`），
换到的是「绝不在不知道盈亏的情况下动别人的仓位」。若将来要放开，得**单开一版**
（改的是口径，走 `plugin submit`，不就地改）。
"""

#: 止损线（%）。相对 `cost_price` 浮亏达到它就卖。
STOP_LOSS_PCT: float = -8.0

#: 止盈线（%）。
TAKE_PROFIT_PCT: float = 20.0

SOURCE: str = '''# m2_a2 —— AI 模拟卖出条件：止盈 / 止损 / 调仓退出（D-24 / D-33）
#
# 纯函数：只读 ctx，不 import、不做 IO、不取时间、不用随机。
# 允许集合 = 当前持仓（账户里没有的标的**不允许**出现在 orders 里）。
STOP_LOSS_PCT = -8.0     # 相对 cost_price 浮亏 <= -8% ⇒ 止损
TAKE_PROFIT_PCT = 20.0   # 相对 cost_price 浮盈 >= +20% ⇒ 止盈


def run(ctx):
    orders = []
    holdings = sorted(ctx["holdings"], key=lambda h: str(h["code"]))
    for h in holdings:
        code = str(h["code"])
        cost = h.get("cost_price")
        close = h.get("close")
        # 「不知道成本」≠「该卖」：没记成本的持仓一律不动（连③调仓退出也不做）。
        if cost is None:
            continue
        if close is None or float(cost) <= 0.0:
            # 「不知道现价」同理：算不出盈亏就不下结论（也绝不拿成本价顶现价）。
            continue
        pnl_pct = (float(close) / float(cost) - 1.0) * 100.0
        reason = None
        if pnl_pct <= STOP_LOSS_PCT:
            reason = ("止损：浮亏 %.2f%%（成本 %.4g → 现价 %.4g，阈值 %g%%）"
                      % (pnl_pct, float(cost), float(close), STOP_LOSS_PCT))
        elif pnl_pct >= TAKE_PROFIT_PCT:
            reason = ("止盈：浮盈 +%.2f%%（成本 %.4g → 现价 %.4g，阈值 +%g%%）"
                      % (pnl_pct, float(cost), float(close), TAKE_PROFIT_PCT))
        elif h.get("pool") is None:
            reason = ("调仓退出：该标的已不在任何候选池（short/mid/long 均无），"
                      "当前浮盈 %.2f%% 未触及止盈止损线" % pnl_pct)
        if reason is not None:
            orders.append({"code": code, "side": "sell", "reason": reason})
    return {"orders": orders, "schema_version": "1.0.0"}
'''
