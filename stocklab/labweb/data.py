"""页面取数（P15）：把**既有函数**拼成每个页面要的 dict。

本模块**不产生任何新口径**，只做拼装。每个数字都必须能指回它的来源：

| 页面段 | 来源 |
|---|---|
| 组合视图 / 纪律 / 告警 | `portfolio.view.build_portfolio` + `dashboard.summary.build_summary` |
| 净值曲线 | `portfolio.view.nav_series`（口径与 `build_portfolio` 逐字相同） |
| 风险面板 | `risk.panel.build_risk_block`（并已接进 `build_summary` 的 `risk` 段） |
| 验证统计 | `dashboard.summary` 的 `accuracy`（LIVE / REPLAY 分列 + 样本门槛） |
| 数据新鲜度 | `session.review.freshness`（经 `build_summary`） |
| 实验台账 / 数据缺口 | `session.review.experiments_state` / `gap_manifest`（与报告 §5/§6 同一个函数） |
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
from stocklab.labweb import paper_data
from stocklab.portfolio.decision import position_decision
from stocklab.portfolio.prices import resolve_prices
from stocklab.portfolio.view import build_portfolio, daily_close, nav_series
from stocklab.risk.panel import build_risk_block, risk_subject
from stocklab.session.review import experiments_state, freshness, gap_manifest
from stocklab.store.db import connect

TZ = ZoneInfo("Asia/Shanghai")

#: 净值曲线默认回看多少个交易日。
NAV_SESSIONS = 120

#: `/data` 页显示多少条最近事件。
EVENT_LIMIT = 20

#: 模拟盘段的纪律句（与 `chain/accuracy.PAPER_NO_PICK` 同一约束，页面自己的措辞）。
#: 措辞里**不出现**「建议/推荐/应该/最优/冠军」这类词 —— 哪怕是用来说「不排名」的
#: 否定句：页面被关键字扫描时，否定句与肯定句长得一样（测试与人都读不出来）。
_PAPER_POLICY = ("各臂是**并行对照**：本节只并列 —— 不做排名、不做倾向性表述、"
                 "不给出「哪条更值得跟」的结论。在几个交易日样本上挑出的第 1 名"
                 "是多重比较下的必然噪声，不是 edge。")


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
        """总览：`build_summary` 的全部分 + 净值曲线 + 标的覆盖 + 「能不能动」。

        风险块**与 `/risk` 走同一个来源**（`risk_subject` → `build_risk_block`）：
        以前这里 `build_summary` 没传 `risk_block`，于是总览的风险摘要恒为
        `null`，页面只能显示「没算」—— 而**风险其实已经算得出来**。
        两处各拼一遍就是第二个真相来源，所以这里只调 `self.risk()` 用的那一段。
        """
        with self.conn() as c:
            view = build_portfolio(c, self.asof)
            summary = build_summary(c, self.asof,
                                    risk_block=_risk_block(c, view, self.asof))
            summary["nav"] = nav_series(c, self.asof, n_sessions=NAV_SESSIONS)
            summary["actions"] = self._actions(c, view)
            summary["paper"] = self._paper(c)
        summary["coverage"] = self.coverage()
        return summary

    def paper_track(self) -> dict:
        """模拟盘对照页取数（P19 展示层）—— 见 `paper_data.track`。

        本方法不产生口径：净值/收益/回撤/超额都走 `paper.engine.build_report`，
        逐日序列读 `paper_nav_daily.cum_return` 列。
        """
        with self.conn() as c:
            return paper_data.track(c, self.asof)

    def _paper(self, conn) -> dict:
        """模拟盘各臂**最新一交易日**的净值快照（P36）。**只读已落库的表，不重算**。

        取数口径与 `build_summary` 一致：一律 `date <= asof`。这里刻意**不**取
        全表 `MAX(date)` —— 那会让历史 `--asof` 的截图显示未来某天的净值。

        多臂**只并列**：本方法不做排名、不算「谁更好」，也不挑出哪条是基准
        （`chain/accuracy.PAPER_NO_PICK` 是同一个纪律）。
        """
        row = conn.execute(
            "SELECT MAX(date) AS d FROM paper_nav_daily WHERE date <= ?",
            (self.asof,)).fetchone()
        latest = row["d"] if row is not None else None
        if latest is None:
            return {"asof": self.asof, "date": None, "arms": [],
                    "n_arms": 0,
                    "policy": _PAPER_POLICY}
        arms = [dict(r) for r in conn.execute(
            "SELECT account_id, date, cash, market_value, nav, drawdown,"
            " cum_cost, cum_return, net_deposits, index_300_level,"
            " index_300_asof FROM paper_nav_daily"
            " WHERE date = ? ORDER BY account_id", (latest,))]
        return {"asof": self.asof, "date": latest, "arms": arms,
                "n_arms": len(arms), "policy": _PAPER_POLICY}

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
        """风险页：面板 + 它是挂在哪只标的上的。

        风险块由 `_risk_block` 产出 —— **与总览用的是同一个函数**，
        所以 `/` 与 `/risk` 不可能给出两套风险数。
        """
        with self.conn() as c:
            view = build_portfolio(c, self.asof)
            subject = risk_subject(view)
            block = _risk_block(c, view, self.asof)
            summary = build_summary(c, self.asof, risk_block=block)
        return {"asof": self.asof, "subject": subject, "risk": block,
                "portfolio": view, "summary": summary}

    def data(self) -> dict:
        """`/data` 页取数。

        「实验台账」与「数据缺口」两节**直接调报告用的那两个函数**
        （`session.review.experiments_state` / `gap_manifest`）：
        报告 §5 / §6 与页面从这里读的是**同一次取数**，不存在第二套数字
        （P51 T6）。本方法不重算、不写库。
        """
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
            experiments = experiments_state(c)
            gaps = gap_manifest(c, self.asof)
        return {
            "asof": self.asof,
            "freshness": fresh,
            "accuracy": summary["accuracy"],
            "alarms": summary["alarms"],
            "events": events,
            "calendar": {"latest_open": cal["d"], "n_open_days": cal["n"]},
            "bars": {"n_rows": bars["n"], "latest": bars["d"]},
            "snapshots": {"n_rows": snaps["n"], "latest_ts": snaps["t"]},
            "experiments": experiments,
            "gaps": gaps,
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


def _risk_block(conn, view: dict, asof: str) -> dict | None:
    """风险面板：挂在**市值最大的持仓**上（`risk_subject` 的展示选择）。

    总览与 `/risk` 都调它 —— 「哪只标的是风险主体」这件事只能有一个答案，
    否则两个页面会对同一账户给出不同标的的风险结论。
    """
    subject = risk_subject(view)
    if subject is None:
        return None
    return build_risk_block(conn, subject["code"], asof=asof,
                            price=subject["price"])


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
