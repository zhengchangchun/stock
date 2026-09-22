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

import json
from dataclasses import dataclass, field
from typing import Callable, Sequence

from stocklab.config.universe import Instrument
from stocklab.data import notice_date
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


# ---------------------------------------------------------------------------
# Task 5：财报落库（幂等、首写保留、勾稽留痕）
# ---------------------------------------------------------------------------

#: 总资产的合理量级（元）。超出即怀疑量纲错（万元当元、千元当元）。
_ASSET_MIN, _ASSET_MAX = 1e8, 1e14


@dataclass(frozen=True)
class IngestResultF:
    """财报采集结果。`conflicts` 是「同键重采值变了」的次数。"""

    code: str
    ok: bool
    rows_written: int
    conflicts: int = 0
    issues: tuple[str, ...] = ()
    error: str = ""


def _notice_date_issue(r) -> str | None:
    """公告日质检：`notice_date_source` 与 `notice_date` 取值必须自洽。

    三种不合的情形（任一命中即 warn `notice_date_suspect`）：

    1. 源站给过公告日但被判不合理（`notice_date_suspect`）—— 这是 A1 那个 bug
       的表现（DMSK 历史行把公告日指向次年同类报告的公告日）。
    2. 标 `f10` 却算不出 `0 < 滞后 <= 120 天`：源站值越界却没被回退。
    3. 标 `statutory` 却不等于法定披露截止日：回退值被改过。

    ②③ 是**回归护栏**：当前的 `notice_date.resolve` 不会产出这样的行，但 ingest
    是系统边界（数据来自外部），将来任何新的写入方（迁移、手工补数）走这条路
    都会被拦住并留痕。**只记不拒**，与会计恒等式检查同款。
    """
    src, got = r.notice_date_source, r.notice_date
    if getattr(r, "notice_date_suspect", False):
        return ("notice_date_suspect: 源站公告日不合理（要求 report_date < "
                f"notice_date ≤ report_date+120 天），已回退法定截止日 {got}")
    if src == "f10" and not notice_date.plausible(r.report_date, got):
        return (f"notice_date_suspect: 标 f10 但 {r.report_date} → {got} 不满足 "
                "report_date < notice_date ≤ report_date+120 天")
    if src == "statutory" and got != notice_date.statutory_deadline(r.report_date):
        return (f"notice_date_suspect: 标 statutory 但 {got} 不等于法定披露截止日 "
                f"{notice_date.statutory_deadline(r.report_date)}")
    return None


def _sanitize(r) -> list[str]:
    """会计恒等式勾稽 + 量级检查 + 公告日质检。返回 issue 文案列表（可能为空）。

    **只记不拒**：财报是公开数据，异常值可能是真实的（巨额商誉减值之类），
    丢掉它等于静默篡改历史。留痕，让下游自己判。
    """
    out: list[str] = []
    if r.total_assets is not None and not (_ASSET_MIN <= r.total_assets <= _ASSET_MAX):
        out.append(f"量级异常：total_assets={r.total_assets!r} 不在 "
                   f"[{_ASSET_MIN:.0e}, {_ASSET_MAX:.0e}] 元")
    if (r.total_assets and r.total_liabilities is not None
            and r.total_equity is not None):
        diff = abs(r.total_assets - r.total_liabilities - r.total_equity)
        if diff / abs(r.total_assets) >= 1e-6:
            out.append(
                f"会计恒等式不成立：|资产−负债−权益|/资产 = "
                f"{diff / abs(r.total_assets):.2e}（口径探针：若此处长期不过，"
                "检查 TOTAL_EQUITY 是不是被当成了归母权益）")
    issue = _notice_date_issue(r)
    if issue is not None:
        out.append(issue)
    return out


_F_COLS = ("code", "report_date", "notice_date", "notice_date_source",
           "report_type", "total_assets", "parent_equity", "total_equity",
           "total_liabilities", "inventory", "total_operate_income",
           "operate_cost", "parent_netprofit", "netcash_operate",
           "construct_long_asset", "industry_name", "source", "fetched_at",
           "created_at", "raw_refs_json", "cache_key", "unit")


def ingest_financial_reports(conn, reports, raw_refs, *, now: str) -> IngestResultF:
    """把一批财报写入 `financial_reports`。

    - **幂等**：`(code, report_date, notice_date)` 已存在即跳过
    - **首写保留**：已存在但值不同 → 保留旧值、`conflicts += 1`（不写 system_events），**不覆盖**
    - 会计恒等式与量级异常**只记 issue，不拒写**
    """
    if not reports:
        return IngestResultF(code="", ok=True, rows_written=0)
    code = reports[0].code
    refs_json = json.dumps(raw_refs, ensure_ascii=False, sort_keys=True)
    issues: list[str] = []
    written = conflicts = 0

    for r in reports:
        issues.extend(_sanitize(r))
        existing = conn.execute(
            f"SELECT {', '.join(_F_COLS)} FROM financial_reports"
            " WHERE code=? AND report_date=? AND notice_date=?",
            (r.code, r.report_date, r.notice_date)).fetchone()
        if existing is not None:
            incoming = {c: getattr(r, c, None) for c in _F_COLS}
            for col in _F_COLS:
                if col in ("created_at", "fetched_at", "raw_refs_json",
                           "cache_key", "unit", "source"):
                    continue
                if incoming.get(col) != existing[col]:
                    conflicts += 1
                    break
            continue

        conn.execute(
            f"INSERT INTO financial_reports ({', '.join(_F_COLS)})"
            f" VALUES ({', '.join('?' * len(_F_COLS))})",
            tuple(
                {
                    "code": r.code,
                    "report_date": r.report_date,
                    "notice_date": r.notice_date,
                    "notice_date_source": r.notice_date_source,
                    "report_type": r.report_type,
                    "total_assets": r.total_assets,
                    "parent_equity": r.parent_equity,
                    "total_equity": r.total_equity,
                    "total_liabilities": r.total_liabilities,
                    "inventory": r.inventory,
                    "total_operate_income": r.total_operate_income,
                    "operate_cost": r.operate_cost,
                    "parent_netprofit": r.parent_netprofit,
                    "netcash_operate": r.netcash_operate,
                    "construct_long_asset": r.construct_long_asset,
                    "industry_name": r.industry_name,
                    "source": r.source,
                    "fetched_at": now,
                    "created_at": now,
                    "raw_refs_json": refs_json,
                    "cache_key": raw_refs[0]["cache_key"] if raw_refs else None,
                    "unit": "CNY",
                }[col] for col in _F_COLS
            ))
        written += 1
    conn.commit()
    return IngestResultF(code=code, ok=True, rows_written=written,
                         conflicts=conflicts, issues=tuple(issues))
