"""仓位裁剪（P14 / Task 64）：`f_final` → **具体股数**，并逐条报谁把数字压下来了。

## 三条纪律

1. **凯利的 f 只是起点**，用户纪律是**硬约束**（单票 ≤40%、买后现金 ≥45%、
   单次动用现金 ≤5% 总资产、≥87.00 禁加仓、100 股整手）。
2. **逐条报 `binding_constraints`**：只给一个最终数字，人就无法知道
   「为什么不是凯利算出来的那个数」，下次他会自己把纪律调松。
   报清楚是哪条在起作用，纪律才是可讨论的。
3. **不足 1 手 → 0 并说明**：不许「四舍五入买一手」——
   那等于悄悄把仓位放大到超过 f 允许的风险。

## 数字的唯一来源

阈值全部来自 `stocklab.portfolio.discipline`（`DISCIPLINE` 与 `PER_CODE_LINES`）。
**本模块不抄任何阈值**：抄一份出来，两处迟早对不上，而且是静默的。
"""

from __future__ import annotations

import math

from stocklab.config.costs import CostModel
from stocklab.portfolio.discipline import DISCIPLINE, lines_for

#: A 股最小交易单位（1 手 = 100 股）。
LOT = 100

#: 浮点比较容差：`x > limit + EPS` 才算越界，避免 40.00000000000001 被判违规。
EPS = 1e-9

#: 约束名（进 `binding_constraints`，与纪律检查的名保持同源可对照）。
C_SINGLE = "single_position_max_40pct"
C_CASH_FLOOR = "cash_floor_45pct"
C_PER_TRADE = "cash_per_trade_max_5pct"
C_NO_ADD = "no_add_above_87"
C_LOT = "lot_100"

_NOTE = {
    C_SINGLE: "单票市值占比 ≤ {single:.0f}% 总资产",
    C_CASH_FLOOR: "买入后现金 ≥ {cash:.0f}% 总资产",
    C_PER_TRADE: "单次动用现金 ≤ {per:.0f}% 总资产",
    C_NO_ADD: "现价 ≥ {line:.2f} 禁止加仓（用户纪律，**非公式**）",
    C_LOT: "100 股整手",
}


def size_position(*, code: str, f_final: float, total_assets: float, cash: float,
                  price: float, qty_held: int = 0, market_price: float | None = None,
                  costs: CostModel | None = None, lot: int = LOT) -> dict:
    """把 `f_final` 换算成**可下单股数**，并报告每条起作用的纪律约束。

    `price` 是**拟成交价**（估值与实际现金流都用它）；
    `market_price` 是**现价**，只用于「现价 ≥ 87.00 禁加仓」这条纪律 ——
    这条规则写的是「现价」，拿拟成交价去判它等于自己给自己放宽
    （挂个低价单就能绕过禁补仓线）。两个价都进输出，不藏。
    """
    if price <= 0:
        raise ValueError(f"price 必须为正，收到 {price!r}")
    if total_assets <= 0:
        raise ValueError(f"total_assets 必须为正，收到 {total_assets!r}")
    if not 0.0 <= f_final <= 1.0:
        raise ValueError(f"f_final 必须在 [0, 1]，收到 {f_final!r}")
    costs = costs or CostModel()
    lines = lines_for(code)
    no_add_ref = price if market_price is None else market_price

    single_max = DISCIPLINE["single_position_max_pct"] / 100.0
    cash_floor = DISCIPLINE["cash_band_pct"][0] / 100.0
    per_trade = DISCIPLINE["cash_per_trade_max_pct"] / 100.0

    kelly_value = f_final * total_assets
    raw_shares = kelly_value / price
    kelly_shares = int(math.floor(raw_shares / lot)) * lot
    lot_clipped = raw_shares > kelly_shares          # 有余数 → 整手约束真的起作用了

    def violations(qty: int) -> list[str]:
        if qty <= 0:
            return []
        out: list[str] = []
        fill, fee = costs.total("buy", price, qty)
        outflow = fill * qty + fee
        if (qty_held + qty) * price > single_max * total_assets + EPS:
            out.append(C_SINGLE)
        if cash - outflow < cash_floor * total_assets - EPS:
            out.append(C_CASH_FLOOR)
        if outflow > per_trade * total_assets + EPS:
            out.append(C_PER_TRADE)
        if lines and no_add_ref >= lines["no_add_above"]:
            out.append(C_NO_ADD)
        return out

    qty = kelly_shares
    binding: list[str] = []
    while qty > 0:
        bad = violations(qty)
        if not bad:
            break
        binding.extend(bad)
        qty -= lot

    # 含滑点的成交价是**成本模型的性质**，与买多少股无关 —— 0 股时也照实给出来。
    fill = costs.fill_price("buy", price)
    fee = costs.fees("buy", fill, qty) if qty > 0 else 0.0
    outflow = fill * qty + fee
    cash_after = cash - outflow
    mv_after = (qty_held + qty) * price
    notes: list[str] = []

    if f_final <= 0:
        notes.append("凯利结论 f_final = 0（不下注）→ 建议 0 股。"
                     "这是**结论**，不是「没钱买」或「没算出来」。")
    elif kelly_shares <= 0:
        one_lot_value = price * lot
        notes.append(
            f"不足 1 手：f={f_final:.4f} × 总资产 ¥{total_assets:,.2f} = "
            f"¥{kelly_value:,.2f}，1 手需 ¥{one_lot_value:,.2f} → 建议 0 股"
            f"（不四舍五入买一手：那会把仓位放大到超过 f 允许的风险）")
    elif qty == 0:
        notes.append("被纪律约束压到 0 股（见 binding_constraints）—— "
                     "凯利给了仓位，但纪律不允许，**以纪律为准**")
    elif qty < kelly_shares:
        notes.append(f"凯利目标 {kelly_shares} 股被压到 {qty} 股"
                     f"（差 {kelly_shares - qty} 股，见 binding_constraints）")

    if binding:
        binding = sorted(set(binding))
        if lot_clipped and qty > 0:
            binding = sorted(set(binding) | {C_LOT})
    elif lot_clipped and qty > 0:
        binding = [C_LOT]

    fmt = {"single": DISCIPLINE["single_position_max_pct"],
           "cash": DISCIPLINE["cash_band_pct"][0],
           "per": DISCIPLINE["cash_per_trade_max_pct"],
           "line": lines["no_add_above"] if lines else None}

    return {
        "code": code,
        "price": round(price, 4),
        "market_price": None if market_price is None else round(market_price, 4),
        "no_add_judged_on": round(no_add_ref, 4),
        "fill_price": round(fill, 4),
        "qty_held": qty_held,
        "total_assets": round(total_assets, 2),
        "cash": round(cash, 2),
        "f_final": round(f_final, 6),
        "kelly_target_value": round(kelly_value, 2),
        "kelly_target_shares": kelly_shares,
        "suggested_shares": qty,
        "suggested_value": round(qty * price, 2),
        "estimated_fee": round(fee, 2),
        "cash_outflow": round(outflow, 2),
        "cash_after": round(cash_after, 2),
        "position_pct_after": round(mv_after / total_assets * 100.0, 4),
        "cash_pct_after": round(cash_after / total_assets * 100.0, 4),
        "binding_constraints": binding,
        "lot": lot,
        "constraints_checked": {
            "single_position_max_pct": DISCIPLINE["single_position_max_pct"],
            "cash_floor_pct": DISCIPLINE["cash_band_pct"][0],
            "cash_per_trade_max_pct": DISCIPLINE["cash_per_trade_max_pct"],
            "no_add_above": lines["no_add_above"] if lines else None,
            "no_add_above_reason": (None if lines else
                                    f"{code} 未配置纪律线（纪律线由入场价推出，"
                                    f"不是全局常数）→ 不套用别人的线"),
            "no_add_judged_on": round(no_add_ref, 4),
            "lot": lot,
        },
        "notes": notes,
    }


def render_sizing(s: dict) -> str:
    """人看的仓位建议（数字与 JSON **同源**）。"""
    lines = [
        f"=== 仓位裁剪 · {s['code']} ===",
        f"拟成交价 {s['price']:.4f}（含滑点 {s['fill_price']:.4f}）"
        f"   现价 {s['market_price'] if s['market_price'] is not None else '—'}"
        f"   总资产 ¥{s['total_assets']:,.2f}   现金 ¥{s['cash']:,.2f}"
        f"   已持有 {s['qty_held']} 股",
        f"凯利 f_final = {s['f_final']:.4f} → 目标 ¥{s['kelly_target_value']:,.2f}"
        f"（{s['kelly_target_shares']} 股）",
        "",
        f"建议股数：**{s['suggested_shares']} 股**"
        f"（市值 ¥{s['suggested_value']:,.2f}，费用 ¥{s['estimated_fee']:,.2f}）",
        f"买入后：现金 ¥{s['cash_after']:,.2f}（{s['cash_pct_after']:.2f}%）"
        f"   单票占比 {s['position_pct_after']:.2f}%",
    ]
    if s["binding_constraints"]:
        lines.append("")
        lines.append("起作用的约束：")
        for c in s["binding_constraints"]:
            tpl = _NOTE.get(c, c)
            lines.append("  • " + tpl.format(**s["constraints_checked"]))
    for n in s["notes"]:
        lines.append(f"  ℹ️  {n}")
    return "\n".join(lines)
