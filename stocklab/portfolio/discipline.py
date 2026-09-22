"""用户纪律的自动判定（P12 / Task 57）：纯函数，输入是数字，输出是判定。

## 状态语义（四态，**没有第五态**）

| 状态 | 含义 | 典型场景 |
|---|---|---|
| `PASS` | 满足 | 在带内、未破位 |
| `WARN` | 偏离目标 / 触发了禁止动作 | 现金在 45–60% 之外；现价 ≥ 87 禁补仓 |
| `FAIL` | 破了硬约束 | 单票 > 40%；跌破止损线 |
| `UNDETERMINED` | **数据不足，不猜** | 没有现价 → 没有权重 |

`UNDETERMINED` 不是 PASS。这是刻意的：没有价格时权重**不存在**，
把它当成 0% 会自动 PASS 掉一条本该报警的规则，而且是悄无声息的。

## 为什么这里不做「单次减仓 10%/20%」的判定

那两条是**动作规则**（要减多少），不是**状态规则**（现在合规吗）。
本模块只判状态；动作建议由 `view.py` 的 `advisory` 段按规则换算成股数回显。
"""

from __future__ import annotations

#: 用户纪律的**唯一数字来源**。文档里不许再抄一遍 —— 两处写迟早会对不上。
DISCIPLINE = {
    "single_position_max_pct": 40.0,
    "cash_band_pct": (45.0, 60.0),
    "cash_per_trade_max_pct": 5.0,
    "trim_light_pct": 10.0,
    "trim_on_break_pct": 20.0,
}

#: **按标的**的纪律线，不是全市场常数。
#
# 三条线里两条仍来自 000333 的 86.80 建仓价：
#   周线止损 83.25（≈ −4.1%）、禁补仓 87.00（≈ +0.2%）是**入场价**推出来的硬约定。
# 收盘止损线在 2026-09-17 换成**规则值**（ADR-012）：`MA60 − 1×ATR14`，
# 用 `bars_daily` 不复权收盘价序列、asof 最新 bar 日期 —— 不再是无推导的拍数。
# 把它们当全局常数套到别的标的上会产出**看起来像结论的垃圾**：
# 一只 20 元的股票会被判「跌破 82.14 止损线 62 元」，而这条判定毫无意义。
# 没有配置纪律线的标的，对应检查一律 `UNDETERMINED`（不猜），
# 而不是拿别人的线去量它。
PER_CODE_LINES = {
    "000333": {"stop_loss_close": 82.14, "stop_loss_weekly": 83.25,
               "no_add_above": 87.00},
}

UNDETERMINED_REASON = "数据不足（无可用现价），未判定"


def lines_for(code: str) -> dict | None:
    """该标的的纪律线；没配过返回 `None`。"""
    return PER_CODE_LINES.get(code)


def _out(check: str, status: str, detail: str, numbers: dict,
         subject: str | None = None) -> dict:
    return {"check": check, "subject": subject, "status": status,
            "detail": detail, "numbers": numbers}


# ---------- 单票集中度 ----------

def check_position_weight(code: str, weight_pct: float | None) -> dict:
    """单票市值 ≤ 总资产 40%。"""
    limit = DISCIPLINE["single_position_max_pct"]
    if weight_pct is None:
        return _out("single_position_max_40pct", "UNDETERMINED",
                    f"{code}：{UNDETERMINED_REASON}（没有现价就没有市值，也就没有权重）",
                    {"weight_pct": None, "limit_pct": limit}, subject=code)
    excess = round(weight_pct - limit, 4)
    status = "PASS" if weight_pct <= limit else "FAIL"
    if status == "PASS":
        detail = f"{code} 占总资产 {weight_pct:.2f}%，未超 {limit:.0f}% 上限"
    else:
        detail = (f"{code} 占总资产 {weight_pct:.2f}%，**超** {limit:.0f}% 上限 "
                  f"{excess:.2f} 个百分点")
    return _out("single_position_max_40pct", status, detail,
                {"weight_pct": round(weight_pct, 4), "limit_pct": limit,
                 "excess_pct": excess}, subject=code)


# ---------- 现金带 ----------

def check_cash_band(cash_pct: float | None) -> dict:
    """现金占总资产 45%–60%（目标带；带外是 WARN，不是 FAIL）。"""
    low, high = DISCIPLINE["cash_band_pct"]
    if cash_pct is None:
        return _out("cash_band_45_60pct", "UNDETERMINED",
                    f"现金占比：{UNDETERMINED_REASON}",
                    {"cash_pct": None, "low_pct": low, "high_pct": high})
    if low <= cash_pct <= high:
        status, gap = "PASS", 0.0
        detail = f"现金占总资产 {cash_pct:.2f}%，在 {low:.0f}–{high:.0f}% 目标带内"
    else:
        status = "WARN"
        gap = round(cash_pct - high, 4) if cash_pct > high else round(cash_pct - low, 4)
        side = "高于" if gap > 0 else "低于"
        detail = (f"现金占总资产 {cash_pct:.2f}%，{side} {low:.0f}–{high:.0f}% "
                  f"目标带 {abs(gap):.2f} 个百分点")
    return _out("cash_band_45_60pct", status, detail,
                {"cash_pct": round(cash_pct, 4), "low_pct": low, "high_pct": high,
                 "gap_pct": gap})


# ---------- 止损 ----------

def check_stop_loss_close(code: str, close: float | None, *,
                          source: str | None = None,
                          price_asof: str | None = None,
                          line: float | None = None) -> dict:
    """收盘价口径止损线：**跌破**才动，正好落在线上不算破。

    `close` 必须是**日收盘价**（`bars_daily`），不是盘中快照价 ——
    拿盘中价去判「收盘有没有破位」，盘中每一分钟都会给出不同的答案。
    收盘价取不到、或该标的没配过纪律线 → `UNDETERMINED`。

    `line` 给定时用它当线（`arm-agent` 按 spec 的 `stop_loss_pct` 推出的绝对值）；
    不给才回落到 `PER_CODE_LINES`。**同一份判据只写一次** —— 让调用方自己在外面
    比大小，就等于把「跌破才动、正好在线上不算破」这条语义拄成两份，
    而两份迟早在某个边界上不一致（那种不一致看起来就像是真实信号）。
    """
    lines = lines_for(code)
    if line is None:
        if lines is None:
            return _out("stop_loss_close_85", "UNDETERMINED",
                        f"{code} 未配置纪律线（止损线由**入场价**推出，不是全局常数）；"
                        "不拿别的标的的线去量它",
                        {"line": None, "close": close}, subject=code)
        line = lines["stop_loss_close"]
    if close is None:
        return _out("stop_loss_close_85", "UNDETERMINED",
                    f"{code}：{UNDETERMINED_REASON}（收盘价需 bars_daily，当前取不到）",
                    {"line": line, "close": None}, subject=code)
    distance = round(close - line, 4)
    where = f"（{source}，{price_asof}）" if source else ""
    if close >= line:
        status = "PASS"
        detail = f"{code} 收盘{where} {close:.2f}，在止损线 {line:.2f} 上方 {distance:.2f}"
    else:
        status = "FAIL"
        detail = f"{code} 收盘{where} {close:.2f}，**跌破**止损线 {line:.2f}（{distance:.2f}）"
    return _out("stop_loss_close_85", status, detail,
                {"line": line, "close": close, "distance": distance,
                 "close_source": source, "close_asof": price_asof}, subject=code)


def check_stop_loss_weekly(code: str, weekly_close: float | None, *,
                           source: str | None = None,
                           price_asof: str | None = None) -> dict:
    """周线口径止损线。`weekly_close` 由调用方从 bars_daily 算出并标明出处。

    取不到、或该标的没配过纪律线 → `UNDETERMINED` —— 周线猜不出来，
    也不该装作用成本价算得出来。
    """
    lines = lines_for(code)
    if lines is None:
        return _out("stop_loss_weekly_83_25", "UNDETERMINED",
                    f"{code} 未配置纪律线（周线止损线由**入场价**推出，不是全局常数）",
                    {"line": None, "weekly_close": weekly_close}, subject=code)
    line = lines["stop_loss_weekly"]
    if weekly_close is None:
        return _out("stop_loss_weekly_83_25", "UNDETERMINED",
                    f"{code}：{UNDETERMINED_REASON}"
                    f"（周线需 bars_daily，当前取不到）",
                    {"line": line, "weekly_close": None}, subject=code)
    distance = round(weekly_close - line, 4)
    if weekly_close >= line:
        status = "PASS"
        detail = (f"{code} 周线（{source}，{price_asof}）{weekly_close:.2f}，"
                  f"在 {line:.2f} 上方 {distance:.2f}")
    else:
        status = "FAIL"
        detail = (f"{code} 周线（{source}，{price_asof}）{weekly_close:.2f}，"
                  f"**跌破** {line:.2f}（{distance:.2f}）")
    return _out("stop_loss_weekly_83_25", status, detail,
                {"line": line, "weekly_close": weekly_close, "distance": distance,
                 "weekly_source": source, "weekly_asof": price_asof}, subject=code)


# ---------- 不追高 ----------

def check_no_add_above(code: str, price: float | None) -> dict:
    """现价 ≥ 禁补仓线 → WARN。

    WARN 而不是 FAIL：这是**禁止一个动作**，不是「当前持仓违规」。
    已经持有的仓位不会因为价格高而变成错误。
    """
    lines = lines_for(code)
    if lines is None:
        return _out("no_add_above_87", "UNDETERMINED",
                    f"{code} 未配置禁补仓线（该线由**入场价**推出，不是全局常数）",
                    {"threshold": None, "price": price}, subject=code)
    threshold = lines["no_add_above"]
    if price is None:
        return _out("no_add_above_87", "UNDETERMINED",
                    f"{code}：{UNDETERMINED_REASON}", {"threshold": threshold},
                    subject=code)
    if price >= threshold:
        status = "WARN"
        detail = (f"{code} 现价 {price:.2f} ≥ {threshold:.2f} —— **禁止补仓**"
                  f"（高出禁补仓线 {price - threshold:.2f}）")
    else:
        status = "PASS"
        detail = f"{code} 现价 {price:.2f} < {threshold:.2f}，未触发禁补仓线"
    return _out("no_add_above_87", status, detail,
                {"threshold": threshold, "price": price,
                 "over_by": round(price - threshold, 4)}, subject=code)


# ---------- 单次动用现金上限 ----------

def check_cash_per_trade(total_assets: float | None) -> dict:
    """单次动用现金 ≤ 总资产 5% —— 给出**可动用的绝对额度**。"""
    pct = DISCIPLINE["cash_per_trade_max_pct"]
    if not total_assets:
        return _out("cash_per_trade_max_5pct", "UNDETERMINED",
                    f"总资产不可用，算不出单次额度（{UNDETERMINED_REASON}）",
                    {"limit_pct": pct, "limit_amount": None})
    limit = round(total_assets * pct / 100.0, 2)
    return _out("cash_per_trade_max_5pct", "PASS",
                f"单次可动用现金上限 ¥{limit:,.2f}（总资产 {total_assets:,.2f} 的 {pct:.0f}%）",
                {"limit_pct": pct, "limit_amount": limit,
                 "total_assets": round(total_assets, 2)})


# ---------- 汇总 ----------

def run_checks(*, positions, cash_pct, close, weekly_close, price,
               total_assets, close_source=None, close_asof=None,
               weekly_source=None, weekly_asof=None) -> list[dict]:
    """跑全部六条。`positions` 是 `[(code, weight_pct_or_None), ...]`。

    ⚠️ `close` / `weekly_close` / `price` 是**单标的**的简化入参，
    用于「只有一只票」的本仓现状。多标的要按标的分别调用单条 check。
    """
    checks: list[dict] = []
    for code, weight in positions:
        checks.append(check_position_weight(code, weight))
    checks.append(check_cash_band(cash_pct))
    code = positions[0][0] if positions else "-"
    checks.append(check_stop_loss_close(code, close, source=close_source,
                                        price_asof=close_asof))
    checks.append(check_stop_loss_weekly(code, weekly_close,
                                         source=weekly_source,
                                         price_asof=weekly_asof))
    checks.append(check_no_add_above(code, price))
    checks.append(check_cash_per_trade(total_assets))
    return checks
