"""`session tick`（P11）：一次调用 =「采集 → 收盘回填 → 验证到期预测 → 落库 + 摘要」。

## 三条设计决定

### ① 「今天是不是交易日」**不用猜**，因为猜错也不造假

日历来自指数日线（ADR-001 B4），**可能不覆盖今天**（`ingest index` 还没跑）。
本模块照抓不误 —— 因为快照的身份键含源站自己的 `ts`（`quotes.py`）：非交易日源站
返回的仍是上一交易日的最后一条 tick，键相同 → `identical` → **一行都不增**。
摘要里如实报 `calendar.covers_today` 与 `captured_trade_dates`，让读者自己看。

### ② 到期判据：`cutoff` = 最近一个**已收盘**的交易日

```
cutoff = max{ d ∈ 日历交易日 : d < today, 或 d == today 且本地时间 ≥ 15:00 }
```

盘中跑 tick 时 `cutoff` = 上一交易日 → `target_date == 今天` 的预测**一根都不碰**。
这条是本模块最要紧的守卫：拿盘中半截 bar 去给今天的预测打分，会写下一行
「预测错了」而它根本不成立（P7 的 `asof/target 两根都必须正好在序列里` 规则拦不住
这种情况 —— 今天那根 bar 真的在序列里，只是还没走完）。

### ③ 接口失败**显式报错**，绝不用旧数据/空数据顶上

采集抛错 → 摘要里 `collect.error` + `system_events` 一条 error + 退出码 1，
且**不写任何快照行**。验证部分照跑（它只读库、不依赖本次采集），
因为「今天行情没采到」不该连带把「昨天的预测也没验证」变成既成事实。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from stocklab.calendar.trading_calendar import Calendar
from stocklab.session import close as close_mod
from stocklab.session.quotes import (SNAPSHOT_SOURCE, build_rows, missing_codes)
from stocklab.session.review import PROVENANCE_RULE, rolling_accuracy
from stocklab.session.store import SnapshotConflict, insert_snapshots
from stocklab.store import repo
from stocklab.verify.service import NoPredictions, verify_target
from stocklab.verify.store import VerificationConflict

TZ = ZoneInfo("Asia/Shanghai")

#: 默认回看多少个到期日（覆盖 10 个交易日的补跑窗口）。
DEFAULT_WINDOW = 10

#: 滚动准确率的默认窗口（交易日）。
DEFAULT_ROLL_SESSIONS = 30


def _as_datetime(value: datetime | str) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _closed_at(now_dt: datetime) -> bool:
    """本地时刻是否已过收盘（用 (时, 分) 比较，避开 aware/naive `time` 不可比）。"""
    return (now_dt.hour, now_dt.minute) >= (close_mod.CLOSE_HOUR,
                                            close_mod.CLOSE_MINUTE)


def load_calendar(conn: sqlite3.Connection) -> tuple[Calendar, str | None]:
    """读交易日历；**空表不报错**，返回 `(空日历, 原因)`。

    日历来自指数日线（ADR-001 B4），新库/未 `ingest index` 时是空的。
    采集快照并不需要日历（键含源站 `ts`，猜错也不造假），所以这里不把它变成硬错误；
    但凡依赖日历的判定（`cutoff`、回填准入）都必须看到 `calendar_error` 并显式降级。
    """
    try:
        return Calendar.load(conn), None
    except ValueError as exc:
        return Calendar.from_dates([]), str(exc)


def closed_through(calendar: Calendar, now: datetime | str) -> str | None:
    """最近一个**已收盘**的交易日（无则 `None`）。判据见模块 docstring ②。"""
    dt = _as_datetime(now)
    today = dt.date().isoformat()
    closed = [d for d in calendar.all_dates
              if d < today or (d == today and _closed_at(dt))]
    return max(closed) if closed else None


def is_trade_date_closed(trade_date: str, now: datetime | str) -> bool:
    """`trade_date` 这一天是否已经收盘（回填的准入条件）。"""
    dt = _as_datetime(now)
    today = dt.date().isoformat()
    return trade_date < today or (trade_date == today and _closed_at(dt))


def _compact_rolling(roll: dict) -> dict:
    """把完整滚动报告压成摘要字段（tick 的 JSON 要能一眼读完）。"""
    out: dict = {"window": roll["window"], "rule": PROVENANCE_RULE}
    for name in ("live", "replay"):
        b = roll.get(name)
        if not b:
            out[name] = None
            continue
        out[name] = {
            "n_rows": b["n_rows"], "n_scorable": b["n_scorable"],
            "unscorable": b["n_unscorable"],
            "n_days": b["effective_n_days"],
            "direction_accuracy_daily": b["direction_accuracy_daily"],
            "direction_ci95": b["direction_ci95"],
            "brier_daily": b["brier_daily"],
            "sample_gate": b["sample_gate"],
        }
    return out


def run_tick(conn: sqlite3.Connection, *, now: str,
             fetch: Callable[[list[str]], list], universe: Sequence,
             window: int = DEFAULT_WINDOW, capture: bool = True,
             source: str = SNAPSHOT_SOURCE,
             roll_sessions: int = DEFAULT_ROLL_SESSIONS) -> dict:
    """跑一次 tick，返回 JSON 可序列化的摘要（含 `ok` 与 `exit_code`）。

    `fetch(codes)` 是注入的抓取函数（CLI 传真实客户端，测试传 fixture 回放）——
    本函数**自己不联网**，因此可离线测试（与 `ingest_daily_bars` 同一模式）。
    """
    now_dt = _as_datetime(now)
    today = now_dt.date().isoformat()
    cal, cal_error = load_calendar(conn)
    cal_dates = set(cal.all_dates)
    anomalies: list[dict] = []
    run_id = repo.record_job(conn, "session_tick", status="running", started_at=now)

    summary: dict = {
        "job": "session_tick",
        "now": now,
        "today": today,
        "calendar": {
            "error": cal_error,
            "covers_today": today in cal_dates,
            "says_session_today": cal.is_open(today) if cal_dates else None,
            "n_dates": len(cal_dates),
            "range": ({"first": min(cal_dates), "last": max(cal_dates)}
                      if cal_dates else None),
        },
        "collect": None,
        "backfill": [],
        "verify": None,
        "rolling": None,
        "anomalies": anomalies,
        "ok": False,
        "exit_code": 0,
    }

    # ---------- ① 采集 ----------
    requested = [i.tencent_code for i in universe]
    captured_dates: list[str] = []
    if capture:
        try:
            quotes = fetch(requested)
        except Exception as exc:                      # noqa: BLE001 — 必须留痕
            msg = f"{type(exc).__name__}: {exc}"
            repo.log_event(conn, "session", "error", f"快照采集失败：{msg}",
                           context={"codes": requested, "job": "session_tick"},
                           now=now)
            summary["collect"] = {"requested": requested, "captured": 0,
                                  "error": msg,
                                  "note": "接口失败 —— 本次未写入任何快照行，"
                                          "拒绝用旧数据或空数据顶替"}
            anomalies.append({"kind": "collect_failed", "detail": msg})
        else:
            rows, build_errors = build_rows(quotes, fetched_at=now, source=source)
            for code, reason in sorted(build_errors.items()):
                repo.log_event(conn, "session", "error",
                               f"{code} 快照字段不可用：{reason}",
                               context={"code": code, "job": "session_tick"}, now=now)
                anomalies.append({"kind": "snapshot_field_error", "code": code,
                                  "detail": reason})
            states, conflicts = insert_snapshots(conn, rows, now=now)
            for c in conflicts:
                repo.log_event(conn, "session", "error",
                               f"{c['code']} 快照冲突（同 ts 不同内容）：{c['reason']}",
                               context={"code": c["code"], "ts": c["ts"],
                                        "job": "session_tick"}, now=now)
                anomalies.append({"kind": "snapshot_conflict", "code": c["code"],
                                  "ts": c["ts"], "detail": c["reason"]})
            captured_dates = sorted({r["trade_date"] for r in rows})
            missing = missing_codes(requested, quotes)
            if missing:
                # 缺标的**不**等于「今天只有这几只」：显式记下来
                repo.log_event(conn, "session", "warn",
                               f"源站未返回 {len(missing)} 个标的的快照：{missing}",
                               context={"missing": missing, "job": "session_tick"},
                               now=now)
                anomalies.append({"kind": "snapshot_missing_codes",
                                  "codes": missing})
            summary["collect"] = {
                "requested": requested,
                "captured": len(quotes),
                "rows": len(rows),
                "inserted": sum(1 for v in states.values()
                                if v.startswith("inserted")),
                "identical": sum(1 for v in states.values()
                                 if v.startswith("identical")),
                "states": states,
                "field_errors": build_errors,
                "conflicts": [c["code"] for c in conflicts],
                "missing_codes": missing,
                "captured_trade_dates": captured_dates,
            }
    else:
        summary["collect"] = {"skipped": "capture_disabled"}

    # ---------- ② 收盘回填 ----------
    for td in captured_dates:
        if not is_trade_date_closed(td, now_dt):
            summary["backfill"].append({"trade_date": td, "ran": False,
                                        "reason": "session_open"})
            continue
        summary["backfill"].append({"ran": True,
                                    **close_mod.backfill_close_amounts(
                                        conn, td, now=now)})

    # ---------- ③ 验证到期预测 ----------
    cutoff = closed_through(cal, now_dt)
    if cutoff is None:
        summary["verify"] = {"skipped": "no_closed_session_in_calendar",
                             "calendar_range": summary["calendar"]["range"]}
    else:
        due = [r["target_date"] for r in conn.execute(
            "SELECT DISTINCT target_date FROM predictions"
            " WHERE status='ok' AND target_date <= ?"
            " ORDER BY target_date DESC", (cutoff,))]
        selected = due[:window]
        agg = {"inserted": 0, "identical": 0, "rescored_after_data_gap": 0}
        unscorable: list[dict] = []
        for d in selected:
            try:
                rep = verify_target(conn, d, now=now)
            except NoPredictions as exc:               # 理论上选不到，留个兜底
                anomalies.append({"kind": "verify_no_predictions",
                                  "target_date": d, "detail": str(exc)})
                continue
            except VerificationConflict as exc:
                repo.log_event(conn, "session", "error",
                               f"{d} 验证冲突：{exc}",
                               context={"target_date": d, "job": "session_tick"},
                               now=now)
                anomalies.append({"kind": "verification_conflict",
                                  "target_date": d, "detail": str(exc)})
                continue
            for state in rep["storage"].values():
                key = state.split(":")[0]
                if key in agg:
                    agg[key] += 1
            for u in rep["unscorable"]:
                unscorable.append({"target_date": d, **u})
        summary["verify"] = {
            "cutoff": cutoff,
            "due_dates": len(due),
            "verified_dates": selected,
            "backlog_skipped": max(0, len(due) - len(selected)),
            "backlog_note": ("只验证最近 window 个到期日；更早的未验证日计入 "
                             "backlog_skipped（显式报出，不静默丢）"),
            **agg,
            "unscorable": unscorable,
        }
        summary["rolling"] = _compact_rolling(
            rolling_accuracy(conn, end_date=cutoff, n_sessions=roll_sessions))

    summary["ok"] = not anomalies
    summary["exit_code"] = 0 if not anomalies else 1
    repo.finish_job(conn, run_id,
                    status="ok" if summary["ok"] else "failed",
                    finished_at=now,
                    detail=f"snapshots+{summary['collect'].get('rows', 0)} "
                           f"verify_inserted+{(summary['verify'] or {}).get('inserted', 0)} "
                           f"anomalies={len(anomalies)}")
    return summary
