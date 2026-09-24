"""模块1 主流程编排（设计文档 §7 的 12 步）。

## 顺序不能改

文档 01 的 12 步是**主干**，AI 不可修改流转。本模块把它们串起来：

    排雷(3) → 插桩0(4) → 淘汰入库(5) → 三池(6) → 插桩1/2/3(7)
    → 插桩4(8) → 入库(9) → 快照(10) → 报告(11) → 触发判断(12)

## 幂等判据在最前面

`find_snapshot` 先问「这个 (asof, run_kind) 做过没有」，命中就整体跳过。
**不能反过来**（先跑一遍再靠 UNIQUE 兜底）—— 那样会白跑几百次插桩调用，
而且日志里看不出是「重跑」还是「新跑」。

## 缺插桩就整体不产出

任一环节找不到 active 版本 → `NoActivePlugin` 直接抛出，**不留半截快照**。
快照写在一个事务边界内（`write_snapshot` 内部 commit），前置步骤全部
完成之后才写。

## 步骤12 只记标志

`recommend_optimization` 返回布尔并写进报告，**不触发任何脚本生成**——
那是文档 01 §AI优化子流程 的下一轮任务（设计文档 §3「不做」）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from stocklab.candidate import pools, report, risk_adjust, score, screen, snapshot
from stocklab.candidate import cross_section, indicators
from stocklab.candidate.seeds import SEED_UNIVERSE
from stocklab.data.models import Bar

RUN_KINDS: tuple[str, ...] = ("light", "weekly", "quarterly")


@dataclass(frozen=True)
class RunResult:
    snapshot_id: int
    asof: str
    run_kind: str
    members: list
    rejects: list
    report_md: str
    skipped: bool
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineResult:
    """打分内核的产物。**只读** —— 不含任何落库副作用。

    `eligible`：每池在 `select_top` **截断之前**的合格 code（升序）。
    `members` 已经是「前 N 只」，截断前的集合在别处拿不到，而横截面实验的
    对照臂（持有集合 = 池内全部合格标的）需要它。这是**只增字段**：
    `members` / `rejects` / `params` 的取值与顺序一字未变。
    """

    members: list
    rejects: list
    params: dict
    eligible: dict[str, list[str]] = field(default_factory=dict)


def _hydrate(loaded: dict) -> tuple[list[snapshot.MemberRow], list[snapshot.RejectRow]]:
    """将 load_snapshot 返回的原始字典列表水合为 MemberRow/RejectRow 对象。

    供新跑路径和幂等跳过路径共用，保证两条路径返回相同结构与顺序。
    """
    members_obj = [snapshot.MemberRow(
        code=m["code"], pool=m["pool"], raw_score=m["raw_score"],
        adj_score=m["adj_score"], reason=m["reason"],
        risk_json=m["risk_json"], status=m["status"])
        for m in loaded["members"]]
    rejects_obj = [snapshot.RejectRow(
        code=r["code"], stage=r["stage"], reason=r["reason"],
        plugin_id=r.get("plugin_id"))
        for r in loaded["rejects"]]
    return members_obj, rejects_obj


def _load_bars(conn: sqlite3.Connection, code: str, *, asof: str) -> list[Bar]:
    rows = conn.execute(
        "SELECT code, date, open, high, low, close, volume, amount, turnover,"
        " source, adj_mode FROM bars_daily WHERE code = ? AND date <= ?"
        " ORDER BY date", (code, asof)).fetchall()
    return [Bar(**dict(r)) for r in rows]


def _cross_section_map(conn, *, asof: str) -> dict:
    """每轮算一次横截面分位，避免对每个标的重复计算全样本。"""
    rows = {}
    for inst in SEED_UNIVERSE:
        rows[inst.code] = indicators.factors(
            score.load_financials(conn, inst.code, asof=asof))
    return cross_section.build(rows, asof=asof)


def recommend_optimization(result: RunResult) -> bool:
    """步骤12：是否触发 AI 优化子任务。

    本轮判据：某池成员数低于文档建议下限的一半（说明插桩打分区分度太差）。
    **只返回标志**，不生成脚本。
    """
    low = {"short": 1, "mid": 2, "long": 1}
    counts = {p: 0 for p in pools.ALL_POOLS}
    for m in result.members:
        counts[m.pool] += 1
    return any(counts[p] < low[p] for p in pools.ALL_POOLS)


def score_pipeline(conn: sqlite3.Connection, *, asof: str,
                   plugin_overrides: dict[str, int] | None = None
                   ) -> PipelineResult:
    """跑一遍候选池打分（步骤 3–8），**只返回、不写库**。

    回放要跑几百个历史调仓日，绝不能往 `candidate_snapshots` 灌历史回放行。
    生产主流程 `run_candidate` 也走这里 —— 两者必须产出相同成员，否则
    「回测跑的就是生产逻辑」不成立。

    `plugin_overrides`：`{plugin_id: script_id}`，用于沙盒的单变量对比
    （只换被比较的那个插件，其余仍解析 active）。
    """
    members: list[snapshot.MemberRow] = []
    rejects: list[snapshot.RejectRow] = []
    scored: dict[str, list[dict]] = {p: [] for p in pools.ALL_POOLS}

    xsec = _cross_section_map(conn, asof=asof)

    for inst in SEED_UNIVERSE:
        bars = _load_bars(conn, inst.code, asof=asof)

        verdict = screen.screen(inst, bars, asof=asof)
        if not verdict.passed:
            rejects.append(snapshot.RejectRow(
                code=inst.code, stage="pre_screen", reason=verdict.reason,
                plugin_id=None))
            continue

        ctx = score.build_ctx(conn, inst, pools.POOL_SHORT, bars, asof=asof,
                              cross_section=xsec)
        industry = score.industry_screen(conn, inst, ctx,
                                         plugin_overrides=plugin_overrides)
        if not industry["pass_flag"]:
            rejects.append(snapshot.RejectRow(
                code=inst.code, stage="industry_screen",
                reason="; ".join(industry["risk_note"]) or "行业排雷未通过",
                plugin_id=score.INDUSTRY_SCREEN_PLUGIN))
            continue

        for pool in pools.eligible_pools(inst):
            pool_ctx = score.build_ctx(conn, inst, pool, bars, asof=asof,
                                       cross_section=xsec)
            outcome = score.score_pool(conn, inst, pool, pool_ctx,
                                       plugin_overrides=plugin_overrides)
            if not outcome.pass_flag:
                rejects.append(snapshot.RejectRow(
                    code=inst.code, stage="score",
                    reason=f"{pool}池打分未通过：{outcome.reason}",
                    plugin_id=score.POOL_PLUGIN[pool]))
                continue
            final, risks = risk_adjust.adjust(conn, outcome, pool_ctx,
                                              plugin_overrides=plugin_overrides)
            # 插桩0 的行业注记**无论排雷是否通过**都要进报告（P53 T5）。
            # 原来只在 `pass_flag=False` 的拒绝分支里用 `risk_note`，导致通过排雷的
            # 银行/保险在报告里看不到「金融业（银行Ⅱ）：…毛利率与存货周转无意义」
            # 与「行业非 PIT，仅为近似」——正是「静默排除」要避免的那种不可见。
            scored[pool].append({
                "code": inst.code, "pool": pool,
                "raw_score": outcome.raw_score, "adj_score": final,
                "reason": outcome.reason,
                "risk_json": json.dumps(
                    [*industry["risk_note"], *risks], ensure_ascii=False)})

    eligible = {p: sorted(r["code"] for r in scored[p])
                for p in pools.ALL_POOLS}
    for pool in pools.ALL_POOLS:
        for row in pools.select_top(scored[pool], pool):
            members.append(snapshot.MemberRow(**row))

    return PipelineResult(members=members, rejects=rejects,
                          eligible=eligible,
                          params={"seed_count": len(SEED_UNIVERSE),
                                  "topn": dict(pools.POOL_TOPN)})


def run_candidate(conn: sqlite3.Connection, *, asof: str, run_kind: str,
                  now: str) -> RunResult:
    if run_kind not in RUN_KINDS:
        raise ValueError(f"未知 run_kind {run_kind!r}；已知：{list(RUN_KINDS)}")

    existing = snapshot.find_snapshot(conn, asof=asof, run_kind=run_kind)
    if existing is not None:
        loaded = snapshot.load_snapshot(conn, existing)
        md = report.render_report(asof=asof, run_kind=run_kind, loaded=loaded,
                                  generated_at=now)
        members_obj, rejects_obj = _hydrate(loaded)
        return RunResult(snapshot_id=existing, asof=asof, run_kind=run_kind,
                         members=members_obj, rejects=rejects_obj,
                         report_md=md, skipped=True,
                         params=loaded["snapshot"]["params"])

    pipe = score_pipeline(conn, asof=asof)
    snapshot_id = snapshot.write_snapshot(
        conn, asof=asof, run_kind=run_kind, params=pipe.params,
        members=pipe.members, rejects=pipe.rejects, now=now)

    loaded = snapshot.load_snapshot(conn, snapshot_id)
    md = report.render_report(asof=asof, run_kind=run_kind, loaded=loaded,
                              generated_at=now)
    # 用 load_snapshot 返回的顺序（pool, adj_score DESC, code / stage, code）
    # 构建 RunResult，与幂等重跑路径保持一致。
    members_obj, rejects_obj = _hydrate(loaded)
    return RunResult(snapshot_id=snapshot_id, asof=asof, run_kind=run_kind,
                     members=members_obj, rejects=rejects_obj,
                     report_md=md, skipped=False, params=pipe.params)
