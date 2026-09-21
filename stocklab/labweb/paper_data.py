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

## 「自己编排的东西用上了没有」（`ai_evidence`）

对照页还要回答一个问题：**页面上那条「AI」线，究竟用没用上我们自己编排的东西**
（插桩脚本、候选池、模型预测）。这三段全部**用计数回答**：

| 段 | 来源 | 回答 |
|---|---|---|
| `accuracy` | `session.review.rolling_accuracy`（与「数据」页、`review` 报告同源） | AI 自己的准确率是多少 |
| `scripts` / `backtests` / `candidate` / `routing` | `plugin_*`、`candidate_*` 表 + `candidate.score.POOL_PLUGIN` | 自己编排的产出有哪些、谁在调 |
| `consumption` | `paper_trades.rule_citation` 对表 `paper.config.RULE_CITATIONS` + `paper_accounts.params_json` | 模拟盘**实际**消费了几条 |

第三段是**实测**而不是描述：落在写死条文之外的触发理由会被逐条列出（空 = 没有
任何一笔成交由模型或插桩脚本触发）。将来真接了模型臂，这里会自己变成非空 ——
所以它同时是一块看板和一个钩子。

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
from stocklab.paper.config import PAPER_START_DATE, RULE_CITATIONS
from stocklab.paper.engine import INDEX_300_SYMBOL, build_report
from stocklab.plugin import lifecycle as plugin_lifecycle
from stocklab.plugin import store as plugin_store
from stocklab.session.review import rolling_accuracy

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


# ---------- 「自己编排的东西用上了没有」 ----------

#: 「谁在消费自己编排的产出」这一段的判据：模拟盘账户参数里出现这些键，
#: 就说明它接了模型 / 插桩。当前实测为空 —— 空就是空，不写「应该是空」。
_CONSUMPTION_MARKERS: tuple[str, ...] = ("plugin", "plugin_id", "script_id",
                                        "model_version", "model")


def _plugin_routing(conn: sqlite3.Connection) -> list[dict]:
    """哪个 plugin 管哪一池，以及它**当前生效**的是哪一版。

    池 → plugin 的映射从 `candidate.score.POOL_PLUGIN` / `INDUSTRY_SCREEN_PLUGIN`
    **反查**，不在这里再抄一份：抄出来的那份会和打分内核漂移，页面就会指错脚本。
    生效版本走 `plugin.lifecycle.active_script_id`（与打分内核同一个函数）——
    认不出 active 版本时返回 `None` 并原样显示，不猜。
    """
    from stocklab.candidate import score   # 懒 import：展示层不反向依赖打分内核
    pairs = [("行业排雷", score.INDUSTRY_SCREEN_PLUGIN)]
    pairs += [(f"{pool} 池打分", pid)
              for pool, pid in sorted(score.POOL_PLUGIN.items())]
    out = []
    for label, pid in pairs:
        sid = plugin_lifecycle.active_script_id(conn, str(pid))
        version = None
        if sid is not None:
            row = plugin_store.get_script(conn, int(sid))
            version = str(row["version"]) if row else None
        out.append({"label": label, "plugin_id": str(pid),
                    "active_script_id": (None if sid is None else int(sid)),
                    "version": version})
    return out


def ai_evidence(conn: sqlite3.Connection, asof: str) -> dict:
    """「AI 自己编排的东西，用上了没有」—— 三段，全部用计数回答。

    `accuracy` 与「数据」页同源（`rolling_accuracy`），这里不重算；
    `consumption` 是实测：各臂成交的 `rule_citation` 与本模块 import 的
    `paper.config.RULE_CITATIONS` 逐条对表，表外的理由单独列出。
    """
    acc = rolling_accuracy(conn, end_date=asof)

    scripts: list[dict] = []
    for s in plugin_store.list_scripts(conn):
        sid = int(s["script_id"])
        scripts.append({
            "script_id": sid, "plugin_id": str(s["plugin_id"]),
            "version": str(s["version"]),
            "state": plugin_lifecycle.script_state(conn, sid),
            "created_at": str(s["created_at"]),
            "note": str(s["note"] or ""),
        })
    by_state: dict[str, int] = {}
    for s in scripts:
        by_state[s["state"]] = by_state.get(s["state"], 0) + 1

    def _count(table: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    snaps = [dict(r) for r in conn.execute(
        "SELECT snapshot_id, asof, run_kind, created_at FROM candidate_snapshots"
        " ORDER BY asof")]

    known = set(RULE_CITATIONS.values())
    rows = conn.execute("SELECT rule_citation FROM paper_trades").fetchall()
    cited = sorted({str(r["rule_citation"] or "") for r in rows})
    unknown = [c for c in cited if c not in known]
    blobs = [str(r["params_json"] or "") for r in conn.execute(
        "SELECT params_json FROM paper_accounts")]
    param_keys = sorted({k for blob in blobs for k in json.loads(blob or "{}")})
    param_refs = sorted({m for m in _CONSUMPTION_MARKERS
                         if any(m in blob for blob in blobs)})

    return {
        "accuracy": acc,
        "counts": {
            "predictions": _count("predictions"),
            "verifications": _count("verifications"),
            "plugin_scripts": len(scripts),
            "plugin_backtests": _count("plugin_backtests"),
            "candidate_snapshots": len(snaps),
            "candidate_members": _count("candidate_members"),
        },
        "scripts": scripts,
        "by_state": by_state,
        "backtests": [dict(b) for b in plugin_store.load_backtests(conn)],
        "routing": _plugin_routing(conn),
        "candidate": [{"snapshot_id": int(r["snapshot_id"]),
                       "asof": str(r["asof"]),
                       "run_kind": str(r["run_kind"]),
                       "created_at": str(r["created_at"])} for r in snaps],
        "consumption": {
            "n_accounts": _count("paper_accounts"),
            "n_trades": len(rows),
            "cited_rules": cited,
            "unknown_rules": unknown,
            "param_keys": param_keys,
            "param_refs": param_refs,
        },
    }


def _empty(asof: str, start: str, *, db_missing: bool = False,
           ai: dict | None = None) -> dict:
    return {"asof": asof, "available": False, "db_missing": db_missing,
            "start_date": start, "date": None, "dates": [], "n_sessions": 0,
            "arms": [], "index": None, "now_account_id": None,
            "mirror_equals_hold": None, "real_trades": [],
            "real_trades_after_start": None, "paper_trades": [],
            "ai": ai or {},
            "disclosure": [], "disclaimer": "", "sample_note": ""}


def track(conn: sqlite3.Connection, asof: str) -> dict:
    """对照页的全部取数。同一库 + 同一 asof → 同一结果（不含生成时刻）。"""
    accounts = paper_store.load_accounts(conn)
    start = str(accounts[0]["start_date"]) if accounts else PAPER_START_DATE
    ai = ai_evidence(conn, asof)

    # `date <= asof`：**不取全表 MAX(date)** —— 那会让 `--asof` 的历史截图
    # 显示未来某天的净值（`data._paper` 同一条纪律）。
    session_dates = [str(r["date"]) for r in conn.execute(
        "SELECT DISTINCT date FROM paper_nav_daily WHERE date <= ? ORDER BY date",
        (asof,))]
    if not accounts or not session_dates:
        return _empty(asof, start, ai=ai)

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
        "ai": ai,
        "disclosure": list(report.get("disclosure") or []),
        "disclaimer": report.get("disclaimer", ""),
        "sample_note": report.get("sample_note", ""),
    }
