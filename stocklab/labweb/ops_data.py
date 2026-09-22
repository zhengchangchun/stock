"""定时任务页取数（P38 展示层）：**三条链最近跑成什么样**。

## 这一页为什么必须有

2026-09-22 用户拍板：调度交给系统（launchd，见 `ops/schedule.py`），项目只负责
「跑什么、按什么顺序、什么算失败」。时钟搬走了，**结论就必须看得见** —— 否则等于
把闹钟交给系统、把回执丢在没人看的地方。

| 段 | 来源 | 回答 |
|---|---|---|
| 三条任务 | `ops/schedule.JOBS` | 什么时候该跑、跑的是哪条命令、plist 落在哪 |
| 下一次触发 | 同一个 `Job.calendar` 推算（**纯函数** `next_trigger`） | 下一次大概什么时候 |
| 最近一次 | `journal.read_latest()` 读 `<报告根>/ops/latest-<job>.json` | 上一次的完整载荷（逐步退出码 / 异常 / 退出码） |
| 摘要行 | `chain.summary_line()` / `patrol.summary_line()` | **与 launchd 日志里那一行逐字相同** |
| 历史 | `job_runs`（含采集、快照、模拟盘等所有作业） | 库里最近真的跑了什么 |

## 三条不许越的线（与巡检、收盘链同一套口径）

1. **不重算口径**：退出码、摘要行、步骤表全部**直接取回执**；页面不另算一套，
   所以「页面上说 exit=1」与「launchd 日志说 exit=1」不可能不一致（同一个函数）；
2. **只读 + 不起子进程**：连接一律 `mode=ro`（`job_runs` 也一样），**不代跑
   `launchctl`** —— 「被 launchd 加载了没 / 上次退出码几」要人跑 `ops schedule status`，
   页面只把命令原样给出来。Web 线程里起系统命令是另一条会咬人的线；
3. **没有数就说没有**：没跑过 → 「还没有回执」；回执损坏 → 「回执读不出来」，
   **不许**显示 0，也不许把上一次的旧数字当成今天的结果。

## 「下一次触发」是推算，不是承诺

`StartCalendarInterval` 的语义由 launchd 执行（含睡醒后补跑一次）。这里只做**纯函数**
展开：给定 `now`，取三条任务各自 `calendar` 里第一个 `> now` 的触发点。所以页面上
写的是「按 plist 推算」，**不是**「系统保证」。

## 测试要 hermetic：plist 目录与日志目录**都可注入**

页面上有两处会读**这台机器**的真实状态：`~/Library/LaunchAgents/<label>.plist` 在不在、
日志落在哪。它们默认指向真实位置（生产行为不变），但构造函数接 `plist_dir` / `log_dir`
——测试必须注入到 `tmp_path`。否则「文件在位 / 还没写」这一格会跟着**跑测试的机器**变：
本机 `ops schedule install` 之后，原来靠「plist 不存在所以出现 `s-fail`」而过关的断言
会突然变红（2026-09-22 实测，见错误日记 #56）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stocklab.config import paths
from stocklab.ops import chain, journal, patrol, schedule

TZ = ZoneInfo("Asia/Shanghai")

#: 历史留多少行（`job_runs` 是**全部**作业共用的表，不只是这三条链）。
HISTORY_LIMIT = 25

#: 推算下一次触发时最多往后找多少天（「每月 1 日」的最坏情况是 31 天）。
LOOKAHEAD_DAYS = 62


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def next_trigger(calendar, now: datetime) -> str | None:
    """`StartCalendarInterval` 数组 → 第一个 `> now` 的触发点（ISO8601）。

    认不出的形态（缺 `Hour` / `Minute`，或出现本函数不解释的键）一律**返回 `None`**，
    不猜 —— plist 里那些键由 launchd 解释，我猜错就是一句假话。
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    best: datetime | None = None
    for entry in calendar:
        if not isinstance(entry, dict) or "Hour" not in entry or "Minute" not in entry:
            continue
        if set(entry) - {"Hour", "Minute", "Weekday", "Day", "Month"}:
            continue
        for i in range(LOOKAHEAD_DAYS + 1):
            day = (now + timedelta(days=i)).date()
            if "Weekday" in entry and day.isoweekday() != entry["Weekday"]:
                continue
            if "Day" in entry and day.day != entry["Day"]:
                continue
            if "Month" in entry and day.month != entry["Month"]:
                continue
            cand = datetime.combine(day, time(entry["Hour"], entry["Minute"]),
                                    tzinfo=now.tzinfo)
            if cand > now and (best is None or cand < best):
                best = cand
            break                        # 这一条 entry 已经定案（要么取它、要么跳过）
    return best.isoformat(timespec="seconds") if best else None


def _read_receipt(job_name: str, report_dir: Path | None) -> tuple[dict | None, str | None]:
    """读回执：`(载荷, 读不出来的原因)`。两者互斥；都没有 = 还没跑过。"""
    path = journal.report_path(job_name, report_dir)
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, "回执不是一个 JSON 对象"
    return payload, None


class OpsLab:
    """定时任务页的只读门面（线程安全：每次请求各自开连接）。"""

    def __init__(self, db_path: Path | str, *, report_dir: Path | str | None = None,
                 history_limit: int = HISTORY_LIMIT,
                 plist_dir: Path | str | None = None,
                 log_dir: Path | str | None = None) -> None:
        self.db_path = Path(db_path)
        #: 报告**根**目录（与 `ops close --report-dir` 同一个含义，见 `ops/chain.py`）
        self.report_dir = Path(report_dir) if report_dir else paths.REPORT_DIR
        self.history_limit = int(history_limit)
        #: plist 落地目录（默认 `~/Library/LaunchAgents`）——**可注入**，否则页面上
        #: 「文件在位 / 还没写」会跟着**跑测试这台机器**的真实状态变（测试就不是 hermetic 的）。
        self.plist_dir = Path(plist_dir) if plist_dir else schedule.LAUNCH_AGENTS_DIR
        #: 日志目录（默认 `data/logs/`）——同样可注入，理由同上。
        self.log_dir = Path(log_dir) if log_dir else schedule.log_dir()

    # ---------- 基础设施 ----------

    def exists(self) -> bool:
        return self.db_path.exists()

    def _ro(self) -> sqlite3.Connection:
        """**只读**连接（页面不许写库，连 `job_runs` 也只读）。"""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _runs(self, sql: str, params: tuple = ()) -> list[dict]:
        """`job_runs` 查询；表不存在 / 库读不了 → 空列表（不炸页面）。"""
        if not self.exists():
            return []
        conn = self._ro()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ---------- 页面数据 ----------

    def job(self, name: str, *, now: datetime) -> dict:
        job = schedule.JOB_BY_NAME[name]
        payload, error = _read_receipt(name, self.report_dir)
        summary_fn = (patrol.summary_line if name == "patrol" else chain.summary_line)
        runs = self._runs(
            "SELECT run_id, job_name, status, started_at, finished_at, detail"
            " FROM job_runs WHERE job_name=? ORDER BY run_id DESC LIMIT ?",
            (name, self.history_limit))
        plist = schedule.plist_path(job, plist_dir=self.plist_dir)
        return {
            "name": name,
            "label": job.label,
            "window": job.window,
            "why": job.why,
            "argv": schedule.build_argv(job),
            "plist": str(plist),
            "plist_present": plist.exists(),
            "stdout_log": str(self.log_dir / f"{job.name}.out.log"),
            "stderr_log": str(self.log_dir / f"{job.name}.err.log"),
            "next_trigger": next_trigger(job.calendar, now),
            "receipt_path": str(journal.report_path(name, self.report_dir)),
            "receipt_present": payload is not None or error is not None,
            "latest": payload,
            "latest_error": error,
            "latest_at": (payload or {}).get("now"),
            "exit_code": (payload or {}).get("exit_code"),
            "ok": (payload or {}).get("ok"),
            "summary": summary_fn(payload) if payload is not None else None,
            "steps": self._steps(payload),
            "anomalies": list((payload or {}).get("anomalies") or []),
            "last_run": runs[0] if runs else None,
            "history": runs,
        }

    @staticmethod
    def _steps(payload: dict | None) -> list[dict]:
        """回执里的逐步结果（**原样**取，不重排、不补缺）。"""
        out: list[dict] = []
        for s in (payload or {}).get("steps") or []:
            if not isinstance(s, dict):
                continue
            out.append({
                "name": s.get("name"),
                "exit_code": s.get("exit_code"),
                "timeout": bool(s.get("timeout")),
                "duration_s": s.get("duration_s"),
                "why": s.get("why"),
                "tail": (s.get("stderr_tail") or s.get("stdout_tail")
                         or s.get("error") or ""),
            })
        return out

    def view(self, *, now: datetime | str | None = None) -> dict:
        """页面要的全部数据。"""
        when = now if isinstance(now, datetime) else (
            datetime.fromisoformat(now) if now else datetime.now(TZ))
        if when.tzinfo is None:
            when = when.replace(tzinfo=TZ)
        jobs = [self.job(name, now=when) for name in schedule.JOB_NAMES]
        return {
            "db_path": str(self.db_path),
            "db_exists": self.exists(),
            "report_dir": str(self.report_dir),
            "receipts_dir": str(journal.report_dir_of(self.report_dir)),
            "log_dir": str(self.log_dir),
            "plist_dir": str(self.plist_dir),
            "now": when.isoformat(timespec="seconds"),
            "jobs": jobs,
            "runs": self._runs(
                "SELECT run_id, job_name, status, started_at, finished_at, detail"
                " FROM job_runs ORDER BY run_id DESC LIMIT ?",
                (self.history_limit,)),
            "history_limit": self.history_limit,
        }


__all__ = ["HISTORY_LIMIT", "LOOKAHEAD_DAYS", "OpsLab", "next_trigger", "now_iso"]
