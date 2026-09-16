"""「能不能动」结论（P1b）：把纪律判据翻译成**一条人话结论 + 可选项**。

## 为什么单独一层

`portfolio/discipline.py` 判的是**状态**（合规吗），页面说人话要看的是
**动作**（能做什么、不能做什么）。两者不是一回事：同一条纪律
「跌破止损线」在状态层是 `FAIL`，在动作层是「全清或不动，二选一」。
本模块只做这件事：**把已有判据拼成结论，不产生新的纪律数字**。

纪律数字的唯一来源仍是 `discipline.DISCIPLINE` / `discipline.lines_for()`；
价格口径仍是 `portfolio.view.daily_close()`（`bars_daily` 收盘价）。
这里没有任何新阈值 —— 出现第二个 87.00 就等于出现第二套纪律。

## 三条线（都是**收盘价**口径）

| 收盘价 | 结论 | 动作 |
|---|---|---|
| `< 85.00` | 跌破止损线 | 全清 / 不动，二选一（**只摆选项，不推荐**） |
| `85.00 ≤ 收盘价 < 87.00` | 持有不动 | 权重超限也拆不开（见 `whole_lot_reason`） |
| `≥ 87.00` | 涨到禁补仓线 | 只禁「补仓」这一件事；持有的照旧 |

判「跌破」用 `收盘价 < 线`，正好落在线上**不算破** —— 与
`discipline.check_stop_loss_close` 逐字同口径（那边是 `close >= line → PASS`）。

## 只摆选项、不推荐

跌破止损线时，本模块**不给出「建议卖」或「建议拿」**。理由不是骑墙：
系统的预测在没有样本外 edge 之前没有资格替人做这个决定，
推荐会让人以为是模型说的。两个选项各自的钱、各自的口径都摊开，
选择权在人。这条有测试钉住（`options` 里不许出现「建议/推荐/应该」）。

## 一种情形只闭环一次

`state` 三选一（`hold` / `no_add` / `stop`），另加 `unknown`（判断不了）。
`unknown` 不是「没事」：拿不到收盘价、没有持仓、没有纪律线都会落到这里，
并且必须带上**为什么判不了**。
"""

from __future__ import annotations

from stocklab.config.costs import ASSET_STOCK, CostModel
from stocklab.portfolio.discipline import DISCIPLINE, lines_for

#: A 股整手规模。深交所 3.3.8：卖出申报数量应当为 100 股的整数倍
#: （余额不足 100 股的部分应当一次性申报卖出）。
LOT_SIZE = 100

STATE_HOLD = "hold"
STATE_NO_ADD = "no_add"
STATE_STOP = "stop"
STATE_UNKNOWN = "unknown"

#: 结论句（页面上首屏那句话）。**人话**：不出现 PASS/FAIL/UNDETERMINED。
HEADLINE = {
    STATE_HOLD: "持有不动。",
    STATE_NO_ADD: "可以继续持有，但不能补仓。",
    STATE_STOP: "收盘价跌破了止损线：全清或不动，二选一。",
    STATE_UNKNOWN: "现在判不了能不能动。",
}


def sell_to_cash(close: float, qty: int, *,
                 model: CostModel | None = None) -> dict:
    """按**收盘价**全清 `qty` 股，到手多少钱 —— 逐项拆开，钱取到分。

    口径**全程走 `CostModel("stock")`**（佣金 0.025% 且最低 5 元、印花税 0.05%
    仅卖出、过户费 0.001% 双边、滑点 5 个基点卖出下调）。
    费用总额取 `model.fees()` 这个**权威入口**，逐项明细只是把它拆开展示；
    两者不一致就是本模块的 bug，`fees_priced_against` 把它暴露出来
    （正常情况下恒为 0.00，有测试钉住 0.01 以内）。
    """
    m = model or CostModel(asset_class=ASSET_STOCK)
    fill = m.fill_price("sell", close)          # 卖出：下调滑点后的成交价
    gross = fill * qty
    commission = max(gross * m.commission_rate, m.min_commission)
    transfer_fee = gross * m.transfer_fee_rate
    stamp_tax = gross * m.stamp_tax_rate        # 仅卖出
    fee_total = m.fees("sell", fill, qty)       # 权威口径

    def cents(x: float) -> float:
        return round(float(x), 2)

    parts_sum = cents(commission) + cents(transfer_fee) + cents(stamp_tax)
    return {
        "qty": int(qty),
        "close": cents(close),
        "fill_price": fill,
        "gross": gross,
        "commission": commission,
        "transfer_fee": transfer_fee,
        "stamp_tax": stamp_tax,
        "fee_total": fee_total,
        "proceeds": cents(gross - fee_total),
        "slippage_per_share": cents(close - fill),
        "fees_priced_against": round(parts_sum - fee_total, 2),
        "policy": ("按收盘价卖出：成交价 = 收盘价 − 滑点；费用按股票口径"
                   "（佣金 0.025%、最低 5 元，印花税 0.05% 仅卖出，"
                   "过户费 0.001%）"),
    }


def whole_lot_reason(qty: int) -> str | None:
    """`qty` 为什么拆不开？拆得开（不是整手整数倍 / 超过一手）返回 `None`。

    「拆不开」只有一种情况：**手里正好一手或不足一手** ——
    卖出的申报数量必须是 100 股的整数倍，手里就 100 股时，
    小于 100 股的减仓申报是非法的。
    """
    if qty <= LOT_SIZE:
        return (f"手里就 {qty} 股，而卖出必须是 100 股的整数倍"
                f"（深交所 3.3.8）—— 一次卖 100 股就是全部，"
                f"小于 100 股的减仓申报不合法")
    return None


def _money(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def _pct(x: float | None) -> float | None:
    return None if x is None else round(float(x), 4)


def position_decision(code: str, *, close: float | None,
                      close_asof: str | None = None, qty: int = 0,
                      cost_basis: float | None = None,
                      avg_cost: float | None = None,
                      price: float | None = None,
                      price_asof: str | None = None,
                      price_source: str | None = None,
                      total_assets: float | None = None,
                      cash: float | None = None,
                      name: str | None = None,
                      sell_model: CostModel | None = None) -> dict:
    """把一只持仓拼成「能不能动」结论。**纯函数**，不读库。

    入参全部来自 `portfolio.view.build_portfolio` 的结果，本函数不自己取数、
    不自己定价。`close` 是 `bars_daily` 的**收盘价**（可能比 `price` 早一天，
    页面必须如实显示是哪天的）。
    """
    lines = lines_for(code)
    money_block = {
        "qty": int(qty),
        "avg_cost": (round(float(avg_cost), 4) if avg_cost is not None
                     else (round(cost_basis / qty, 4) if cost_basis and qty else None)),
        "cost_basis": _money(cost_basis),
        "close": _money(close),
        "close_asof": close_asof,
        "price": _money(price),
        "price_asof": price_asof,
        "price_source": price_source,
        "market_value": _money(close * qty if close is not None else None),
        "float_pnl": (_money(close * qty - cost_basis)
                      if close is not None and cost_basis is not None else None),
    }

    weight: dict = {"pct": None, "limit_pct": DISCIPLINE["single_position_max_pct"],
                    "over_pct": None, "total_assets": _money(total_assets)}
    if close is not None and qty and total_assets:
        pct = close * qty / total_assets * 100.0
        weight["pct"] = _pct(pct)
        weight["over_pct"] = _pct(pct - DISCIPLINE["single_position_max_pct"])

    out: dict = {
        "code": code,
        "name": name,
        "state": STATE_UNKNOWN,
        "headline": HEADLINE[STATE_UNKNOWN],
        "because": [],
        "options": [],
        "money": money_block,
        "weight": weight,
        "sell_all": None,
        "lines": {
            "stop_loss_close": None if lines is None else lines["stop_loss_close"],
            "stop_loss_weekly": None if lines is None else lines["stop_loss_weekly"],
            "no_add_above": None if lines is None else lines["no_add_above"],
            "cash_per_trade_max_pct": DISCIPLINE["cash_per_trade_max_pct"],
        },
        "rules": [],
        "disclosure": [],
    }

    if not qty:
        out["because"] = ["账本里没有这只标的的持仓（或已清仓）—— 没有可动的仓。"]
        return out
    if lines is None:
        out["because"] = [
            f"{code} 没有配过纪律线：止损线是从买入价推出来的，不是全市场通用的数，"
            f"所以不拿别的股票的数来套它。"]
        return out
    if close is None:
        out["because"] = [
            f"{code} 取不到收盘价（日线里没有 ≤ 查询日的收盘记录）—— 判不了。"
            f"没有价格就不猜：不用买入价冒充，也不插值。"]
        return out

    if close < lines["stop_loss_close"]:
        out["state"] = STATE_STOP
    elif close >= lines["no_add_above"]:
        out["state"] = STATE_NO_ADD
    else:
        out["state"] = STATE_HOLD
    out["headline"] = HEADLINE[out["state"]]

    if total_assets:
        out["sell_all"] = sell_to_cash(close, qty, model=sell_model)
        sell = out["sell_all"]
        sell["cash_after"] = _money((cash or 0.0) + sell["proceeds"])
        sell["total_after"] = _money(total_assets - sell["fee_total"])
        sell["cash_before"] = _money(cash)

    over = weight["over_pct"]
    breach = (f"这 {qty} 股占你总资产 {weight['pct']:.2f}%，"
              f"比你自己定的 {weight['limit_pct']:.0f}% 上限多出 {over:.2f} 个百分点。"
              if weight["pct"] is not None and over is not None and over > 0
              else f"这 {qty} 股的占比没有超过你自己定的 "
                   f"{weight['limit_pct']:.0f}% 上限。")
    reason = whole_lot_reason(qty)

    if out["state"] == STATE_HOLD:
        out["because"] = [
            f"今天收盘 {close:.2f} 元，在止损线 {lines['stop_loss_close']:.2f} 元上方，"
            f"也没到 {lines['no_add_above']:.2f} 元的补仓上限 —— 不用动。",
            breach + (f"但拆不开：{reason}。" if reason else ""),
            ("减仓的建议股数（按纪律 10% / 20% 折算）小于一手，"
             "在这个持仓量下执行不了 —— 要降权重只能全清，那是另一个决定。"
             if reason else "想降权重可以先减一部分仓。"),
        ]
        out["options"] = ["不动（继续持有这 100 股）。",
                          "想降权重只能考虑全清（见下面的全清试算）。"]
    elif out["state"] == STATE_NO_ADD:
        out["because"] = [
            f"今天收盘 {close:.2f} 元，已经到你定的 {lines['no_add_above']:.2f} 元"
            f"补仓上限（高出 {close - lines['no_add_above']:.2f} 元）—— "
            f"这个价不许再买入这只票，是**只禁补仓这一件事**。",
            "已经拿着的那部分不受影响：价格高不会让手里的持仓变成错的，"
            "所以要卖还是继续拿，按止损线那套判。",
            breach,
        ]
        out["options"] = ["继续持有这 100 股（只是不能补仓）。",
                          "不买。"]
    else:
        out["because"] = [
            f"今天收盘 {close:.2f} 元，跌破了你自己定的止损线 "
            f"{lines['stop_loss_close']:.2f} 元（低 {lines['stop_loss_close'] - close:.2f} 元）。",
            "这只是**触发了你之前定的规则**，系统不替你选：下面两个选项的钱都算好了，"
            "只摆选项，不推荐。",
            breach + (f"另外拆不开：{reason}。" if reason else ""),
        ]
        sell = out["sell_all"]
        out["options"] = [
            "① 不动：继续拿这 100 股，等你自己重新定一条线。",
            ("② 全清：一次卖掉 100 股，"
             + (f"到手 {sell['proceeds']:,.2f} 元。" if sell else "到手多少要等有价格才能算。")),
        ]

    # 「查看详细」里的判据与公式：写在**结论之外**，首屏不放术语。
    per_trade = (f'单次动用现金不得超过总资产的 '
                 f'{DISCIPLINE["cash_per_trade_max_pct"]:.0f}%'
                 + (f'（当前约 {total_assets * DISCIPLINE["cash_per_trade_max_pct"] / 100:,.2f} 元）'
                    if total_assets else '（总资产不可用，算不出额度）'))
    out["rules"] = [
        f'止损线（收盘价口径）：{lines["stop_loss_close"]:.2f} —— 收盘价**低于**它才算跌破，'
        f'正好落在线上不算。',
        f'止损线（周线口径）：{lines["stop_loss_weekly"]:.2f} —— 按本周最后一个收盘看，'
        f'这是第二条线，两条都看。',
        f'补仓上限：{lines["no_add_above"]:.2f} —— 现价到这条线以上就禁止再买，'
        f'只禁买入，不影响已有持仓。',
        per_trade + '。',
        f'整手规则：卖出必须是 {LOT_SIZE} 股的整数倍（深交所 3.3.8）——'
        f'手里 {qty} 股时，只有「全卖」和「不动」两种执行方式。',
    ]
    out["disclosure"] = [
        f"价格口径：本页结论用的是**日收盘价**（bars_daily，"
        f"{close_asof or '无日期'}），不是盘中报价 —— 盘中价每分钟都在变，"
        f"拿它判「收盘破没破线」会每分钟给出不同答案。",
        ("页面另显示现价"
         + (f"（{price:.2f} 元，{price_source or '来源未知'}，{price_asof or '日期未知'}）"
            if price is not None else "（当前取不到）")
         + "，那只是参考，结论不看它。"),
        "权重 = 这 100 股的市值 ÷ 总资产。总资产 = 现金 + 已定价持仓市值。",
        "全清试算已扣佣金、印花税、过户费，并按卖出下调滑点估算成交价。",
    ]
    return out
