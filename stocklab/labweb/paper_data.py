"""模拟盘对照页取数（P19 展示层）：「我 vs AI 纪律臂 vs 大盘」。

## 四条线各是什么

| 线 | 状态来源 | 它回答的问题 |
|---|---|---|
| 我 · 实盘账本镜像（`arm-now`） | `real_trades` + `cash_flows` 逐笔重放 | 真人这笔钱现在值多少 |
| AI 纪律臂（`arm-discipline-{05,10,15}`） | 自身 `paper_trades` | 写死的规则跑出来是多少 |
| 什么都不做（`arm-hold`） | `init` 时**冻结**的快照 | 一动不动是什么结果 |
| 大盘（`sh000300`） | `bars_daily` 收盘 | 市场本身涨了多少 |

## 「AI」这个词的边界（页面文案也必须守）

`arm-discipline-*` 里**没有模型方向预测**：它执行的是写死的纪律条文
（`paper/config.RULE_CITATIONS`），选哪只 ETF 由白名单决定。生产模型的方向能力
≈ 0（行级命中 38.14%、Brier 0.6581 对随机 0.667），所以「拿涨的概率当买入信号」
被 `test_paper_never_imports_model_or_kelly` 源码扫描钉死。口径是
「AI 纪律臂（规则执行，不含方向预测）」，不是「AI 操盘手」。

## 本模块不产生新口径

- 各臂的净值 / 累计收益 / 最大回撤 / 累计成本 / 相对大盘超额 → **直接调
  `paper.engine.build_report`**（与 `paper show` 同源，页面上不会出现第二套数）；
- 逐日序列 → 读库里的 `paper_nav_daily.cum_return` 列，**不重算**；
- 大盘的累计收益 → 一律以 `paper_accounts.start_date` 那天的 `sh000300` 收盘为基
  （与 `build_report` 的 `return_since_start` **同一个基**），所以同一页面上
  「大盘涨了多少」只能有一个值；
- 起跑日锚点 → `paper_accounts.initial_nav ÷ params_json.initial_capital`
  （两列都在库里，不是补出来的）。

指标缺值时一律 `None`：折线在那里**断开**，不插值、不用前一日收盘滚动填充、
不用 0 顶替。
"""

from __future__ import annotations

import json
import sqlite3

from stocklab.paper import store as paper_store
from stocklab.paper.config import PAPER_START_DATE
from stocklab.paper.engine import INDEX_300_SYMBOL, build_report

#: 大盘显示名。指数**不可直接交易**，所以它没有成本 —— 对照时口径偏乐观，
#: 这句话在报告（`build_report`）与页面上各出现一次，措辞同源。
INDEX_LABEL = "沪深300 指数"

#: 认得出「我」的那条臂（`paper/config.ARM_NOW` 的 row 值）。
_ARM_NOW = "now"
_ARM_HOLD = "hold"


def _anchor_cum_return(account: dict) -> float | None:
    """起跑日锚点的累计收益 = `initial_nav / initial_capital − 1`。

    两个数都在 `paper_accounts` 里：`initial_nav` 是 `init` 当天按收盘价
    mark-to-market 的结果，`initial_capital` 在 `params_json`。任一项缺失或
    为 0 → `None`（锚点不画，而不是画成 0）。
    """
    try:
        capital = float(json.loads(account["params_json"])["initial_capital"])
        initial_nav = float(account["initial_nav"])
    except (KeyError, TypeError, ValueError):
        return None
    if not capital:
        return None
    return round(initial_nav / capital - 1.0, 6)


def _index_levels(conn: sqlite3.Connection, dates: list[str]) -> dict[str, float]:
    """`dates` 里每个日期的大盘收盘价（`adj_mode='none'`，与 `engine.pit_close` 同口径）。

    **只按精确日期取**，不做「取最近的前一个交易日」——那会让「那天没开盘」
    看起来像「那天指数没动」。缺的日期就是不返回，由调用方断线。
    """
    if not dates:
        return {}
    marks = ",".join("?" * len(dates))
    sql = ("SELECT date, close FROM bars_daily WHERE code = ?"
           " AND adj_mode = 'none' AND date IN (" + marks + ")")
    return {str(r["date"]): float(r["close"])
            for r in conn.execute(sql, (INDEX_300_SYMBOL, *dates))}


def _real_trades(conn: sqlite3.Connection, asof: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM real_trades WHERE date <= ? ORDER BY date, trade_id",
        (asof,))]


def _paper_trades(conn: sqlite3.Connection, asof: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM paper_trades WHERE date <= ? ORDER BY date, trade_id",
        (asof,))]


def _empty(asof: str, start: str, *, db_missing: bool = False) -> dict:
    return {"asof": asof, "available": False, "db_missing": db_missing,
            "start_date": start, "date": None, "dates": [], "n_sessions": 0,
            "arms": [], "index": None, "now_account_id": None,
            "mirror_equals_hold": None, "real_trades": [],
            "real_trades_after_start": None, "paper_trades": [],
            "disclosure": [], "disclaimer": "", "sample_note": ""}


def track(conn: sqlite3.Connection, asof: str) -> dict:
    """对照页的全部取数。同一库 + 同一 asof → 同一结果（不含生成时刻）。"""
    accounts = paper_store.load_accounts(conn)
    start = str(accounts[0]["start_date"]) if accounts else PAPER_START_DATE

    # `date <= asof`：**不取全表 MAX(date)** —— 那会让 `--asof` 的历史截图
    # 显示未来某天的净值（`data._paper` 同一条纪律）。
    session_dates = [str(r["date"]) for r in conn.execute(
        "SELECT DISTINCT date FROM paper_nav_daily WHERE date <= ? ORDER BY date",
        (asof,))]
    if not accounts or not session_dates:
        return _empty(asof, start)

    display_date = session_dates[-1]
    # 锚点（起跑日）永远在横轴上：没有它，「相对大盘」就没有公共起点。
    axis = sorted({start, *session_dates})

    rows = conn.execute(
        "SELECT account_id, date, cum_return FROM paper_nav_daily WHERE date <= ?",
        (asof,))
    by_account: dict[str, dict[str, float]] = {}
    for r in rows:
        by_account.setdefault(str(r["account_id"]), {})[str(r["date"])] = \
            float(r["cum_return"])

    report = build_report(conn, display_date)
    summary = {str(a["account_id"]): a for a in report["accounts"]}
    index_report = report.get("index_300") or {}

    levels = _index_levels(conn, axis)
    base_level = levels.get(start)
    index_points = [None if base_level is None or d not in levels
                    else round(levels[d] / base_level - 1.0, 6) for d in axis]

    arms: list[dict] = []
    for account in accounts:
        aid = str(account["account_id"])
        series = dict(by_account.get(aid, {}))
        anchor = _anchor_cum_return(account)
        if anchor is not None:
            series.setdefault(start, anchor)
        s = summary.get(aid) or {}
        arms.append({
            "account_id": aid, "arm": str(account["arm"]),
            "etf_target_pct": (None if account["etf_target_pct"] is None
                               else float(account["etf_target_pct"])),
            "anchor_cum_return": anchor,
            "points": [series.get(d) for d in axis],
            "latest_nav_date": max(by_account.get(aid, {}), default=None),
            "has_nav_on_display_date": display_date in by_account.get(aid, {}),
            "nav": s.get("nav"), "cash": s.get("cash"),
            "market_value": s.get("market_value"),
            "net_deposits": s.get("net_deposits"),
            "cum_return": s.get("cum_return"),
            "max_drawdown": s.get("max_drawdown"),
            "cum_cost": s.get("cum_cost"),
            "excess_vs_index_300": s.get("excess_vs_index_300"),
            "excess_vs_now": None,
            "n_positions": (len(s.get("positions") or {})
                            if "positions" in s else None),
            "n_trades": (len(s.get("trades") or []) if "trades" in s else None),
            "discipline": list(s.get("discipline") or []),
        })

    now = next((a for a in arms if a["arm"] == _ARM_NOW), None)
    hold = next((a for a in arms if a["arm"] == _ARM_HOLD), None)
    for a in arms:
        if now is None or a["cum_return"] is None or now["cum_return"] is None:
            continue
        if a is not now:
            a["excess_vs_now"] = round(a["cum_return"] - now["cum_return"], 6)

    idx_ret = index_report.get("return_since_start")
    index = {
        "code": INDEX_300_SYMBOL, "label": INDEX_LABEL,
        "base_date": start, "base_level": base_level,
        "base_level_missing": base_level is None,
        "points": index_points,
        "n_missing": sum(1 for v in index_points if v is None),
        "level": index_report.get("level"),
        "price_asof": index_report.get("price_asof"),
        "return_since_start": idx_ret,
        "excess_vs_now": (None if idx_ret is None or now is None
                          or now["cum_return"] is None
                          else round(idx_ret - now["cum_return"], 6)),
        "note": index_report.get("note"),
    }

    real_trades = _real_trades(conn, asof)
    return {
        "asof": asof, "available": True, "db_missing": False,
        "start_date": start, "date": display_date,
        "dates": axis, "n_sessions": len(session_dates),
        "arms": arms, "index": index,
        "now_account_id": now["account_id"] if now else None,
        # 「我」现在还等于「什么都不做」吗（账本自起跑日以来有没有新成交）。
        # 这是**事实判断**，不是渲染细节 —— 页面据此决定要不要解释两条线重合。
        "mirror_equals_hold": (None if now is None or hold is None
                               else now["points"] == hold["points"]),
        "real_trades": real_trades,
        "real_trades_after_start": sum(1 for t in real_trades
                                       if str(t["date"]) > start),
        "paper_trades": _paper_trades(conn, asof),
        "disclosure": list(report.get("disclosure") or []),
        "disclaimer": report.get("disclaimer", ""),
        "sample_note": report.get("sample_note", ""),
    }
