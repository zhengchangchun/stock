"""风险面板（P15 / 补 P14 step4）：把凯利 + 风险预算 + 止损位拼成**一块稳定 JSON**。

## 为什么要有这一层（而不是让 CLI 和 Web 各拼一遍）

P14 落地时 `dashboard/summary.py` 的 docstring 已经写了「`risk` 段来自
`build_risk_block`」，但那个函数**当时没写**（预算触顶，见
`docs/tasks/2026-09-15-p14-task61-65.md` §4）。于是 CLI 的 `risk kelly` 里
自己拼了一遍回放 + evaluate，而页面拿不到任何东西。

本模块把那次拼装**搬出来**，让 CLI 与 Web（以及 `dashboard build`）
调**同一个**函数。口径只有一份 —— 两处各拼一遍就是第二个真相来源，
迟早一处改了另一处没改，而页面会安静地显示旧口径。

## 诚实性（红线）

- 凯利给 `NO_BET` 时**原样输出**，不许换规则/换窗口把它「救」成有仓位；
- 样本门槛未达 → 文案是「样本不足，仅供观察」，**不是**「edge 很小」；
- 数据不足（K 线太短 / 复权缺口）→ `verdict = "UNDETERMINED"` 且
  `data_status = "unavailable"`，**不猜**、不拿别的标的的数字顶上。
"""

from __future__ import annotations

import sqlite3

from stocklab.portfolio.discipline import lines_for
from stocklab.risk.kelly import evaluate
from stocklab.risk.metrics import build_metrics
from stocklab.risk.rules import (DEFAULT_RULE, load_qfq_bars, ma_pair, replay,
                                 trend_state)
from stocklab.risk.stops import stop_levels

#: 面板结构版本。字段增删要同时改这里与 `docs/architecture/dashboard-json.md`。
PANEL_VERSION = 1

#: 数据不足时的统一结论。**不是** `NO_BET` —— `NO_BET` 是「算过了，答案是
#: 不下注」，`UNDETERMINED` 是「算不出来」。把两者混成一个会让人以为
#: 系统评估过这笔交易。
UNDETERMINED = "UNDETERMINED"


def build_risk_block(conn: sqlite3.Connection, code: str, *, asof: str,
                     rule: str = DEFAULT_RULE, horizon: int = 5,
                     frac: float = 0.25,
                     price: float | None = None) -> dict:
    """`code` 在 `asof`（含）之前的风险面板。**任何数据不足都变成显式字段**。"""
    block: dict = {
        "panel_version": PANEL_VERSION,
        "code": code,
        "asof": asof,
        "rule": rule,
        "horizon": horizon,
        "frac": frac,
        "data_status": "ok",
        "errors": [],
        "verdict": UNDETERMINED,
        "verdict_label": "无法判定",
        "verdict_reason": "data_unavailable",
        "notes": [],
        "kelly": None,
        "metrics": None,
        "stops": None,
        "trend": None,
    }

    bars = None
    try:
        bars, price_window = load_qfq_bars(conn, code, asof)
    except Exception as exc:                      # noqa: BLE001（面板必须兜住）
        price_window = None
        block["errors"].append(f"{type(exc).__name__}: {exc}")

    # ---- 凯利：回放 → evaluate（p/b 的唯一来源，ADR-007 D1） ----
    try:
        rp = replay(conn, code, rule=rule, asof=asof, horizon=horizon)
    except Exception as exc:                      # noqa: BLE001
        block["data_status"] = "unavailable"
        block["errors"].append(f"回放不可用：{type(exc).__name__}: {exc}")
        block["notes"].append(
            f"回放不可用（{exc}）—— 没有回放就没有 p/b，凯利**拒绝给数**"
            f"（不是「不下注」，是「算不出来」）")
    else:
        k = evaluate(rp.stats, frac=frac)
        k["replay"] = {
            "price_window": rp.price_window,
            "cost_model": rp.cost_model,
            "meta": rp.meta,
            "trades_preview": [
                {"entry": t.entry_date, "exit": t.exit_date, "qty": t.qty,
                 "net_return": t.net_return, "cost_bps": round(t.cost_bps, 2),
                 "still_open": t.still_open}
                for t in rp.trades[-5:]],
        }
        block["kelly"] = k
        block["verdict"] = k["verdict"]
        block["verdict_label"] = k["verdict_label"]
        block["verdict_reason"] = k["verdict_reason"]
        block["notes"] = list(k["warnings"])

    # ---- 风险预算指标（历史法，重叠窗口会低估尾部，注释里已披露） ----
    if bars:
        block["metrics"] = build_metrics(bars, asof=asof)
    else:
        block["notes"].append("复权 K 线取不到 —— 风险预算指标（RV/VaR/MDD）无法计算")

    # ---- 趋势状态（**只做波动分层，不预测方向**，见 risk/rules.py 与 P14 定论） ----
    if bars:
        closes = [float(b.close) for b in bars]
        i = len(closes) - 1
        pair = ma_pair(closes, i)
        block["trend"] = {
            "state": trend_state(closes, i),
            "ma20": None if pair is None else round(pair[0], 4),
            "ma60": None if pair is None else round(pair[1], 4),
            "asof": bars[-1].date,
            "note": "趋势状态只用于波动分层，**不是方向预测**",
        }
    else:
        block["trend"] = {"state": None, "ma20": None, "ma60": None, "asof": None,
                          "note": "无复权 K 线 → 趋势状态未知（不猜）"}

    # ---- 止损位并列（纪律线来自该标的的入场价，不是全局常数） ----
    try:
        block["stops"] = stop_levels(bars or [], lines=lines_for(code), price=price)
    except Exception as exc:                      # noqa: BLE001
        block["errors"].append(f"止损位不可用：{type(exc).__name__}: {exc}")

    if price_window is not None and block["kelly"] is not None:
        block["kelly"]["replay"]["price_window"] = price_window
    return block


def risk_subject(view: dict) -> dict | None:
    """风险面板挂在**哪只**标的上：按市值最大的持仓。没有持仓 → `None`。

    「挂在哪只」是一个**展示选择**，不是口径 —— 但它必须是**同一个**选择，
    否则 CLI 与页面会对同一账户给出不同标的的风险结论。所以放在这里共用。
    """
    held = [p for p in view["positions"] if p["qty"]]
    if not held:
        return None
    return max(held, key=lambda p: ((p["market_value"] or 0.0), p["code"]))


__all__ = ["PANEL_VERSION", "UNDETERMINED", "build_risk_block", "risk_subject"]
