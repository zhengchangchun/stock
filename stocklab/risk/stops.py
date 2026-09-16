"""止损位（P14 / Task 65）：纪律线 + 技术线并列，**谁先触发**要说清。

## 两组线的性质不同，混在一起会骗人

- **纪律线**（82.14 收盘 / 83.25 周线）：收盘线是 `MA60 − 1×ATR14` 的规则值（ADR-012，随行情变），
  周线仍是用户建仓价推的**硬约定**（与行情无关）。
- **技术线**（MA60 / ATR(14)×2）来自行情，会随价格移动。

技术线会**贴着纪律线跑**。当 MA60 与周线止损几乎重合时，输出必须显式指出，
否则读者会以为是两个独立信号在互相确认 —— 实际是同一个数字被数了两遍。

ATR 止损是**从现价往后**算的（跟踪止损），不是从建仓价算的；这一点写在输出里。
"""

from __future__ import annotations

ATR_WINDOW = 14
ATR_MULT = 2.0
MA_STOP_WINDOW = 60
#: 两条线相对现价差距小于该百分点 → 视为「几乎重合」。
OVERLAP_PCT = 1.0


def true_ranges(bars: list) -> list[float]:
    """真实波幅 `max(H−L, |H−C₋₁|, |L−C₋₁|)`（首根退化为 H−L）。"""
    out: list[float] = []
    prev_close = None
    for b in bars:
        high, low = float(getattr(b, "high", b.close)), float(getattr(b, "low", b.close))
        if prev_close is None:
            out.append(high - low)
        else:
            out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = float(b.close)
    return out


def atr(bars: list, *, window: int = ATR_WINDOW) -> float | None:
    """ATR（简单均值，不用 Wilder 平滑 —— 口径简单可复核）。不足 `window` 根 → None。"""
    trs = true_ranges(bars)
    if len(trs) < window:
        return None
    return sum(trs[-window:]) / window


def ma(closes: list[float], *, window: int) -> float | None:
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def stop_levels(bars: list, *, lines: dict | None, price: float | None = None,
                atr_mult: float = ATR_MULT) -> dict:
    """并列所有止损/风控线，标出**最先触发**的那条与距现价百分比。"""
    closes = [float(b.close) for b in bars]
    price = float(price if price is not None else (closes[-1] if closes else 0.0))
    if price <= 0:
        raise ValueError("需要正的价格才能算「距现价 %」")

    levels: list[dict] = []
    if lines:
        levels.append({"name": "纪律止损（收盘）", "level": lines["stop_loss_close"],
                       "kind": "discipline", "source": "用户建仓价推导"})
        levels.append({"name": "纪律止损（周线）", "level": lines["stop_loss_weekly"],
                       "kind": "discipline", "source": "用户建仓价推导"})
    ma60 = ma(closes, window=MA_STOP_WINDOW)
    if ma60 is not None:
        levels.append({"name": f"MA{MA_STOP_WINDOW}（收盘跌破）", "level": ma60,
                       "kind": "technical", "source": f"近 {MA_STOP_WINDOW} 根复权收盘均值"})
    a = atr(bars)
    atr_stop = None if a is None else price - atr_mult * a
    if atr_stop is not None:
        levels.append({"name": f"ATR({ATR_WINDOW})×{atr_mult:g}（现价 − {atr_mult:g}ATR）",
                       "level": atr_stop, "kind": "technical",
                       "source": "**从现价**往后算的跟踪止损，不是从建仓价"})

    for lv in levels:
        lv["level"] = round(float(lv["level"]), 4)
        lv["distance_pct"] = round((lv["level"] - price) / price * 100.0, 4)
        lv["below_price"] = lv["level"] < price

    below = [lv for lv in levels if lv["below_price"]]
    first = max(below, key=lambda x: x["level"]) if below else None
    overlap = None
    if ma60 is not None and lines:
        gap = abs(ma60 - lines["stop_loss_weekly"]) / price * 100.0
        if gap < OVERLAP_PCT:
            overlap = {
                "ma60": round(ma60, 4),
                "stop_loss_weekly": lines["stop_loss_weekly"],
                "gap_pct": round(gap, 4),
                "note": f"MA{MA_STOP_WINDOW} 与周线止损几乎重合"
                        f"（相差 {gap:.2f}%）—— 别把它们当成两个独立信号，"
                        f"这只是同一个数字被数了两遍",
            }
    return {
        "price": round(price, 4),
        "levels": levels,
        "first_trigger": first,
        "n_below_price": len(below),
        "overlap_warning": overlap,
        "atr": None if a is None else round(a, 4),
        "n_bars": len(bars),
        "note": "纪律线是**约定**（不随行情变），技术线是**推断**（随行情变）；"
                "两者冲突时以纪律线为准",
    }


def render_stops(s: dict) -> str:
    lines = [
        f"=== 止损位 · 现价 {s['price']} ===",
        f"ATR({ATR_WINDOW}) = {s['atr'] if s['atr'] is not None else '—'}"
        f"   距现价下方的线 {s['n_below_price']} 条",
    ]
    for lv in sorted(s["levels"], key=lambda x: -x["level"]):
        mark = "← **最先触发**" if s["first_trigger"] and lv["name"] == s["first_trigger"]["name"] else ""
        lines.append(f"  {lv['level']:>9.4f}  {lv['distance_pct']:+.2f}%  "
                     f"[{'纪律' if lv['kind'] == 'discipline' else '技术'}] {lv['name']} {mark}")
    if s["first_trigger"]:
        f = s["first_trigger"]
        lines.append(f"价格下跌时**最先**碰到的是：{f['name']} {f['level']}"
                     f"（距现价 {f['distance_pct']:+.2f}%）")
    else:
        lines.append("现价在所有线之下 —— 没有「先碰到哪条」的问题，只有已破位")
    if s["overlap_warning"]:
        lines.append(f"  ⚠️  {s['overlap_warning']['note']}")
    return "\n".join(lines)
