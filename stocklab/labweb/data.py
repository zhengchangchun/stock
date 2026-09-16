"""页面取数（P15）：把**既有函数**拼成每个页面要的 dict。

本模块**不产生任何新口径**，只做拼装。每个数字都必须能指回它的来源：

| 页面段 | 来源 |
|---|---|
| 组合视图 / 纪律 / 告警 | `portfolio.view.build_portfolio` + `dashboard.summary.build_summary` |
| 净值曲线 | `portfolio.view.nav_series`（口径与 `build_portfolio` 逐字相同） |
| 风险面板 | `risk.panel.build_risk_block`（并已接进 `build_summary` 的 `risk` 段） |
| 验证统计 | `dashboard.summary` 的 `accuracy`（LIVE / REPLAY 分列 + 样本门槛） |
| 数据新鲜度 | `session.review.freshness`（经 `build_summary`） |
| 事件 | `system_events`（只读最近 N 条） |

## 每次请求重新读库

不缓存：页面显示的是账本与行情的当前状态，缓存 30 秒就多一个
「页面显示的和库里的不一样」的时段，而且没人知道是哪个。读库是只读的局部查询。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stocklab.dashboard.summary import build_summary
from stocklab.portfolio.decision import position_decision
from stocklab.portfolio.prices import resolve_prices
from stocklab.portfolio.view import build_portfolio, daily_close, nav_series
from stocklab.risk.panel import build_risk_block, risk_subject
from stocklab.session.review import freshness
from stocklab.store.db import connect

TZ = ZoneInfo("Asia/Shanghai")

#: 净值曲线默认回看多少个交易日。
NAV_SESSIONS = 120

#: `/data` 页显示多少条最近事件。
EVENT_LIMIT = 20


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(TZ).date().isoformat()


class Lab:
    """一个仓库路径 + 一个 asof 的取数门面。线程安全（每次请求各自开连接）。"""

    def __init__(self, db_path: Path | str, *, asof: str | None = None) -> None:
        self.db_path = Path(db_path)
        self._fixed_asof = asof

    # ---------- 基础设施 ----------

    @property
    def asof(self) -> str:
        """页面口径的「今天」。`--asof` 传了就用它（便于对历史截图/复现）。"""
        return self._fixed_asof or today()

    def exists(self) -> bool:
        return self.db_path.exists()

    @contextmanager
    def conn(self):
        c = connect(self.db_path)
        try:
            yield c
        finally:
            c.close()

    # ---------- 各页面 ----------

    def overview(self) -> dict:
        """总览：`build_summary` 的全部分 + 净值曲线 + 标的覆盖 + 「能不能动」。"""
        with self.conn() as c:
            summary = build_summary(c, self.asof)
            summary["nav"] = nav_series(c, self.asof, n_sessions=NAV_SESSIONS)
            summary["actions"] = self._actions(c, summary["portfolio"])
        summary["coverage"] = self.coverage()
        return summary

    def _actions(self, conn, view: dict) -> dict:
        """「能不能动」结论区（P1b）：逐只持仓一条结论。

        本方法**不产生新口径**：收盘价、成本、权重、总资产全部来自
        `view`（`build_portfolio`），它只补一样 view 没有的东西 ——
        **每只票自己的日收盘价和它的日期**（`daily_close`，与
        `build_portfolio` 判止损用的是同一个函数、同一口径）。

        ⚠️ `build_portfolio` 的 `discipline` 段只对**第一只**标的判止损
        （`run_checks` 的单标的简化入参），所以这里的收盘价要按标的分别取 ——
        否则第二只票会拿到第一只票的止损结论。
        """
        rows = []
        for p in view.get("positions") or []:
            close, close_asof = daily_close(conn, p["code"], self.asof)
            rows.append(position_decision(
                p["code"], close=close, close_asof=close_asof,
                qty=int(p["qty"]), cost_basis=p.get("cost_basis_incl_fee"),
                avg_cost=p.get("avg_cost_incl_fee"), price=p.get("price"),
                price_asof=p.get("price_asof"), price_source=p.get("price_source"),
                total_assets=view.get("total_assets"), cash=view.get("cash"),
                name=p.get("name")))
        return {
            "asof": self.asof,
            "rows": rows,
            "n_unknown": sum(1 for r in rows if r["state"] == "unknown"),
            "policy": ("结论只看日收盘价（bars_daily），不看盘中报价；"
                       "只摆选项，不替人做买卖决定"),
        }

    def trades(self, *, code: str | None = None) -> dict:
        """成交流水。`code` 是**显示筛选**，不改任何口径。

        `all_rows` 一并带出：回执核对必须看全量（筛掉的可能是刚写的那笔）。
        """
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM real_trades ORDER BY date DESC, trade_id DESC")]
            # 回执要显示「写入后」的状态，所以顺带算出组合视图（同一次读库）
            view = build_portfolio(c, self.asof)
        # 冲正标记永远按**全量**算：筛掉的那笔被冲正了，留在页上的这笔也该显示状态
        reversed_ids = _reversed_trade_ids(rows)
        shown = [r for r in rows if r["code"] == code] if code else rows
        return {
            "asof": self.asof,
            "rows": shown,
            "all_rows": rows,
            "filter": code,
            "reversed_ids": sorted(reversed_ids),
            "view": view,
        }

    def trade_detail(self, trade_id: int) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM real_trades WHERE trade_id = ?",
                            (trade_id,)).fetchone()
            if row is None:
                return None
            all_rows = [dict(r) for r in c.execute(
                "SELECT * FROM real_trades ORDER BY date, trade_id")]
        trade = dict(row)
        # 冲正目标：这笔是「被冲正的那一笔」吗？按 note 里的 `冲正 #<id>` 反查。
        trade["reversed_by"] = sorted(
            r["trade_id"] for r in all_rows
            if f"冲正 #{trade_id}（" in (r["note"] or ""))
        trade["is_reversal"] = (trade["note"] or "").startswith("冲正 #")
        return trade

    def cash(self) -> dict:
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM cash_flows ORDER BY date DESC, flow_id DESC")]
            view = build_portfolio(c, self.asof)
        return {"asof": self.asof, "rows": rows,
                "summary": view["cash_breakdown"],
                "cash": view["cash"], "total_assets": view["total_assets"],
                "view": view}

    def risk(self) -> dict:
        """风险页：面板 + 它是挂在哪只标的上的。"""
        with self.conn() as c:
            view = build_portfolio(c, self.asof)
            subject = risk_subject(view)
            block = (build_risk_block(c, subject["code"], asof=self.asof,
                                      price=subject["price"])
                     if subject else None)
            summary = build_summary(c, self.asof, risk_block=block)
        return {"asof": self.asof, "subject": subject, "risk": block,
                "portfolio": view, "summary": summary}

    def data(self) -> dict:
        with self.conn() as c:
            fresh = freshness(c, self.asof)
            summary = build_summary(c, self.asof)
            events = [dict(r) for r in c.execute(
                "SELECT ts, module, level, message FROM system_events"
                " ORDER BY ts DESC, event_id DESC LIMIT ?", (EVENT_LIMIT,))]
            cal = c.execute(
                "SELECT MAX(date) AS d, COUNT(*) AS n FROM trading_calendar"
                " WHERE is_open = 1").fetchone()
            bars = c.execute(
                "SELECT COUNT(*) AS n, MAX(date) AS d FROM bars_daily").fetchone()
            snaps = c.execute(
                "SELECT COUNT(*) AS n, MAX(ts) AS t FROM quote_snapshots").fetchone()
        return {
            "asof": self.asof,
            "freshness": fresh,
            "accuracy": summary["accuracy"],
            "alarms": summary["alarms"],
            "events": events,
            "calendar": {"latest_open": cal["d"], "n_open_days": cal["n"]},
            "bars": {"n_rows": bars["n"], "latest": bars["d"]},
            "snapshots": {"n_rows": snaps["n"], "latest_ts": snaps["t"]},
        }

    def coverage(self) -> dict:
        """标的覆盖：每只已登记标的的**现价 + 来源 + 来源日期**（P17 / T5）。

        定价口径**完全复用** `portfolio.prices.resolve_prices`
        （同日快照 → 日线最近收盘 → 没有），本方法不产生第二个口径 ——
        它只负责把「标的清单」和「定价结果」并起来展示。

        `adjustable` 来自 `adjust.ADJUSTABLE_TYPES`（**同一个白名单对象**，
        不是抄一份）：ETF 为 `False`，页面据此显示 ADR-008 的复权口径限制。
        两处共用同一判据，才不会出现「页面说能算、代码说不能算」。
        """
        from stocklab.data.adjust import ADJUSTABLE_TYPES

        with self.conn() as c:
            insts = [dict(r) for r in c.execute(
                "SELECT code, name, type FROM instruments ORDER BY code")]
            prices = resolve_prices(c, [i["code"] for i in insts], self.asof)
            n_bars = {r["code"]: r["n"] for r in c.execute(
                "SELECT code, COUNT(*) AS n FROM bars_daily GROUP BY code")}

        rows = []
        for i in insts:
            p = prices.get(i["code"])
            rows.append({
                **i,
                "bars_rows": n_bars.get(i["code"], 0),
                # 拿不到就是 None —— 不用成本价冒充、不插值（prices 模块铁律）
                "price": None if p is None else p.price,
                "source": None if p is None else p.source,
                "price_asof": None if p is None else p.price_asof,
                "adjustable": i["type"] in ADJUSTABLE_TYPES,
            })
        return {
            "asof": self.asof,
            "rows": rows,
            "missing": [r["code"] for r in rows if r["price"] is None],
            "unadjustable": [r["code"] for r in rows if not r["adjustable"]],
        }

    def health(self) -> dict:
        """健康检查：**不泄露密钥**，并把「摘要能不能算出来」纳入判定。

        一个只回 `{"status":"ok"}` 的健康检查在库损坏时照样是绿的 ——
        那种绿是有害的。
        """
        import stocklab.labweb as labweb

        out: dict = {
            "status": "ok",
            "service": labweb.SERVICE,
            "version": labweb.VERSION,
            "server_time": now_iso(),
            "asof": self.asof,
            "db": str(self.db_path),
            "db_exists": self.exists(),
        }
        if not self.exists():
            out["status"] = "degraded"
            out["error"] = "db not found; run `stocklab db init`"
            return out
        try:
            summary = self.overview()
            out["bars_latest_date"] = summary["freshness"]["bars_latest_date"]
            out["alarms"] = len(summary["alarms"])
            risk = summary["risk"]
            out["risk_verdict"] = None if risk is None else risk["verdict"]
        except Exception as exc:                     # noqa: BLE001（健康检查必须兜住）
            out["status"] = "degraded"
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out


def _reversed_trade_ids(rows: list[dict]) -> set[int]:
    """被冲正过的原 `trade_id`。

    判据是 `reverse_trade()` 写进 note 的那句 `冲正 #<id>（…）` ——
    与 `portfolio.ledger` 的文案**同源**（那边改这里也要改，
    所以这里只解析一处前缀，不复制格式）。
    """
    out: set[int] = set()
    for r in rows:
        note = r["note"] or ""
        if not note.startswith("冲正 #"):
            continue
        head = note.split("（", 1)[0]
        try:
            out.add(int(head[len("冲正 #"):]))
        except ValueError:
            continue
    return out


__all__ = ["EVENT_LIMIT", "NAV_SESSIONS", "Lab", "now_iso", "today"]
