"""采集编排（Task 15）：离线可测的日线入库流程。

**关键设计：所有联网动作经 `fetch` 注入。**

    生产路径：`fetch` = 真实抓取（HttpClient + 适配器 + 分页，见 cli/main.py）
    测试路径：`fetch` = 固定数据或 raw_cache 回放
    复现路径：`fetch` = raw_cache 回放（B3）

三者走**同一段编排代码**，所以离线测试对生产路径有真实约束力。
不注入 `fetch` 时**不联网、不抓取**（返回 0 根），避免「测试偷偷上网」。

三条纪律：
  ① 失败留痕（铁律③）：抓取异常写 `system_events`，并计入分母（C4）。
  ② 脏数据不落库：`severity == "error"` 的批次整批跳过写入。
  ③ 作业留痕：无论成败都写 `job_runs`（失败也要能从库里看出来）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from stocklab.config.universe import Instrument
from stocklab.data.models import Bar
from stocklab.quality.checks import Issue, check_bars
from stocklab.store import repo

#: 达到这些严重度即拒绝落库（`warn`/`info` 只记录）
HARD_SEVERITIES = frozenset({"error"})


@dataclass(frozen=True)
class IngestResult:
    """单个标的的采集结果。frozen：结果一旦产生不得被下游改写。"""

    code: str
    ok: bool
    bars_written: int
    issues: tuple[Issue, ...] = ()
    error: str = ""


@dataclass
class IngestReport:
    date: str
    results: list[IngestResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed_codes(self) -> list[str]:
        return [r.code for r in self.results if not r.ok]

    @property
    def total_issues(self) -> int:
        return sum(len(r.issues) for r in self.results)

    @property
    def bars_written(self) -> int:
        return sum(r.bars_written for r in self.results)


def ingest_daily_bars(
    conn,
    client,
    cache,
    instruments: Sequence[Instrument],
    *,
    start: str,
    end: str,
    now: str,
    calendar=None,
    fetch: Callable[[str], list[Bar]] | None = None,
) -> IngestReport:
    """采集 `instruments` 的日线并入库，返回报告。

    `client` / `cache` 目前仅供 `fetch` 闭包捕获（生产路径在 `cli/main.py` 里
    构造它们）；本函数**自身不发起任何请求** —— 这正是它能离线测试的原因。
    """
    report = IngestReport(date=end)
    run_id = repo.record_job(conn, "ingest_daily_bars", status="running",
                             started_at=now)

    for inst in instruments:
        try:
            bars = list(fetch(inst.code)) if fetch is not None else []
        except Exception as exc:                       # noqa: BLE001 — 必须留痕
            msg = f"{type(exc).__name__}: {exc}"
            repo.log_event(conn, "ingest", "error",
                           f"{inst.code} 采集失败: {msg}",
                           context={"code": inst.code, "job": "ingest_daily_bars"},
                           now=now)
            report.results.append(IngestResult(inst.code, False, 0, (), msg))
            continue

        bars = [b for b in bars if start <= b.date <= end]
        issues = check_bars(bars, calendar=calendar)
        if issues:
            repo.insert_quality_issues(conn, issues, source="ingest", now=now)

        if any(i.severity in HARD_SEVERITIES for i in issues):
            repo.log_event(conn, "ingest", "error",
                           f"{inst.code} 质量校验未通过，已跳过写入",
                           context={"code": inst.code, "n_issues": len(issues),
                                    "hard": sum(1 for i in issues
                                                if i.severity in HARD_SEVERITIES)},
                           now=now)
            report.results.append(IngestResult(inst.code, False, 0, tuple(issues),
                                               "quality check failed"))
            continue

        written = repo.insert_bars(conn, bars, now=now) if bars else 0
        report.results.append(IngestResult(inst.code, True, written, tuple(issues)))

    ok = report.ok_count > 0
    repo.finish_job(conn, run_id, status="ok" if ok else "failed", finished_at=now,
                    detail=f"{report.ok_count}/{len(instruments)} ok, "
                           f"{report.total_issues} issues")
    return report
