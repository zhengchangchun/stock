"""看板摘要（P14 / Task 61）：把**既有接口**拼成一份稳定 JSON。

本模块**不产生任何新口径**，只做拼装 —— 每个数字都来自已有实现：

| 段 | 来源 |
|---|---|
| `portfolio` | `stocklab.portfolio.view.build_portfolio`（ADR-006 口径） |
| `freshness` | `stocklab.session.review.freshness` + 全库最新快照 `ts` |
| `accuracy`  | `stocklab.session.review.rolling_accuracy`（LIVE / REPLAY 分列） |
| `risk`      | `stocklab.risk.panel.build_risk_block`（由调用方注入） |

## 确定性

同一份库 + 同一个 `asof` → 同一串 JSON（`sort_keys=True`）。页面上的「生成时刻」
由 `html.render_html(built_at=...)` 单独加，**不进 payload**：否则 `/api/summary`
每次请求都在变，既钉不住测试也 diff 不了 —— 一个「看起来在监控」的接口
如果自己不稳定，它监控不了任何东西。

## `risk=None` 的含义

`risk` 为 `null` 表示**风险面板没接入**，不是「风险为零」。页面必须把这两种
情况渲染成不同的东西（见 `html._risk_section`）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from stocklab.portfolio.view import build_portfolio
from stocklab.session.review import freshness, rolling_accuracy
from stocklab.verify.report import MIN_DAYS

#: 摘要结构版本。字段增删必须同时改这里、`docs/architecture/dashboard-json.md`
#: 与 `tests/test_dashboard_summary.py::test_top_level_fields_are_pinned`。
#: v2 = P50 新增 `m2`（模块2 的三条曲线，与 `/lab/m2` 同源）。
SCHEMA_VERSION = 2

SERVICE = "stocklab-dashboard"

#: 顶层字段集合 —— 页面消费的接口契约。
TOP_LEVEL_FIELDS = (
    "schema_version", "service", "asof", "portfolio", "freshness",
    "accuracy", "risk", "m2", "alarms",
)


def _latest_snapshot(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """全库最新的快照 `(ts, trade_date)`；一条都没有时 `(None, None)`。

    注意这**不是** `asof` 当日的快照 —— 它是「库里的数据最新到什么时候」。
    两个都输出，且各自带日期，读者不会把盘中快照误当 `asof` 的数据。
    """
    row = conn.execute(
        "SELECT ts, trade_date FROM quote_snapshots"
        " ORDER BY ts DESC, code DESC LIMIT 1").fetchone()
    if row is None:
        return None, None
    return str(row["ts"]), str(row["trade_date"])


def build_alarms(view: Mapping, fresh: Mapping, asof: str) -> list[str]:
    """需要人来看的事。**只做拼装与提示，不判定「没事」**。

    刻意不返回「一切正常」这类字符串：一个永远非空的告警列表等于没有告警。
    """
    alarms: list[str] = list(view["warnings"])

    latest = fresh["bars_latest_date"]
    if latest is None:
        alarms.append("bars_daily 一行都没有 —— 所有价格相关数字都不可用")
    elif latest < asof:
        alarms.append(
            f"行情落后：bars_daily 最新 {latest} < asof {asof} —— "
            f"组合视图的现价/收盘判定用的是 {latest} 及更早的数据")
    for code in fresh["codes_without_bars"]:
        alarms.append(f"{code} 在 bars_daily 里一根 K 线都没有（标的在册但无行情）")
    if fresh["snapshots"]["n_rows"] == 0:
        alarms.append(
            f"{fresh['snapshots']['trade_date']} 无盘中快照 —— "
            f"当日买点/盘中价不可用（不影响日收盘口径）")
    return alarms


def build_summary(conn: sqlite3.Connection, asof: str, *,
                  n_sessions: int = 30, min_days: int = MIN_DAYS,
                  risk_block: Mapping | None = None) -> dict:
    """构建看板摘要。`asof` 是「那一天收盘后我知道什么」—— 取数一律 `date <= asof`。"""
    view = build_portfolio(conn, asof)
    fresh = freshness(conn, asof)
    ts, ts_date = _latest_snapshot(conn)
    accuracy = rolling_accuracy(conn, end_date=asof, n_sessions=n_sessions,
                               min_days=min_days)

    return {
        "schema_version": SCHEMA_VERSION,
        "service": SERVICE,
        "asof": asof,
        "portfolio": view,
        "freshness": {
            "bars_latest_date": fresh["bars_latest_date"],
            "bars_latest_by_code": fresh["bars_latest_by_code"],
            "codes_without_bars": fresh["codes_without_bars"],
            "snapshot_latest": {"ts": ts, "trade_date": ts_date},
            "snapshots_on_asof": fresh["snapshots"],
        },
        "accuracy": accuracy,
        "risk": risk_block,
        "m2": m2_charts(conn, asof),
        "alarms": build_alarms(view, fresh, asof),
    }


def m2_charts(conn: sqlite3.Connection, asof: str) -> dict:
    """模块2 的三条曲线（P50 §3）—— 与 `/lab/m2` 页面**同一个取数函数**。

    库里没有模块2 的表（老库未前滚）时返回「不可用 + 理由」，**不报 500、不编数**：
    离线报告少一段，比整份报告生不出来强。
    """
    from stocklab.labweb import m2_charts as charts

    try:
        return charts.chart_panel(conn, asof)
    except sqlite3.OperationalError as exc:      # 表缺失 / 老库未前滚
        return {"asof": asof, "keys": list(charts.KEYS), "blocks": [],
                "available": False,
                "reason": f"模块2 的表不可读（{exc}）—— 先跑 `stocklab db init` 前滚",
                "source_note": "", "signal_note": "", "notes": []}


def summary_json(summary: Mapping) -> str:
    """稳定序列化：`sort_keys` + 不转义中文 + 固定缩进。"""
    return json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2)
