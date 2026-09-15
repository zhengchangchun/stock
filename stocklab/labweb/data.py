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
from stocklab.portfolio.view import build_portfolio, nav_series
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
        """总览：`build_summary` 的全部分 + 净值曲线。"""
        with self.conn() as c:
            summary = build_summary(c, self.asof)
            summary["nav"] = nav_series(c, self.asof, n_sessions=NAV_SESSIONS)
            return summary

    def trades(self) -> dict:
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM real_trades ORDER BY date DESC, trade_id DESC")]
            # 回执要显示「写入后」的状态，所以顺带算出组合视图（同一次读库）
            view = build_portfolio(c, self.asof)
        reversed_ids = _reversed_trade_ids(rows)
        return {
            "asof": self.asof,
            "rows": rows,
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
