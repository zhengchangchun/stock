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
快照写在一个**显式事务**里（`write_snapshot` 内部用
`store.db.transaction()`；连接是 `isolation_level=None` 的 autocommit，
裸 `conn.commit()` 在这里是空操作 —— P90 修正），前置步骤全部完成之后才写。

## 拒绝行按 (code, stage) 去重（在**写路径**上）

`candidate_rejects` 的主键是 `(snapshot_id, code, stage)`，而打分循环对
`eligible_pools(inst)` 的每个池各判一次、`stage` 恒为 `'score'`（不带池名）。
一只标的在 mid 与 long 两池都拒 ⇒ **内核产出两行同键行**。`score_pipeline`
（回放/度量也用）保持无损原样；`run_candidate`（唯一写路径）按**流水线顺序**
保留第一条（即 `short→mid→long` 里最靠前的那个池那条），丢弃数写进快照 params
的 `n_reject_dups_dropped`（P90 D1/D2）。

## 步骤12 只记标志

`recommend_optimization` 返回布尔并写进报告，**不触发任何脚本生成**——
那是文档 01 §AI优化子流程 的下一轮任务（设计文档 §3「不做」）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace

from stocklab.candidate import pools, report, risk_adjust, score, screen, snapshot
from stocklab.candidate import cross_section, indicators
from stocklab.candidate import status as cand_status
from stocklab.candidate.seeds import SEED_UNIVERSE
from stocklab.data import adjust
from stocklab.data.models import Bar

RUN_KINDS: tuple[str, ...] = ("light", "weekly", "quarterly")

#: 打分侧的口径标记（D4：写进快照 `params_json`，与老快照区分）。
#: 语义 = 因子侧（`ctx["bars"]`）用 PIT 复权价、判定侧（`screen.py`）用未复权价。
SCORING_PRICE_MODE = "adjusted_factor_side+raw_screen"

#: 回退到未复权价的原因标签（只进计数与测试，不进快照 —— D4 只增两个键）。
FALLBACK_ETF = "etf_chain_unsupported"
FALLBACK_EMPTY_CHAIN = "empty_chain"
FALLBACK_UNUSABLE_EVENT = "unpriceable_event_in_window"
FALLBACK_STALE_BLACKOUT = "stale_blackout_table"

#: 「**这个标的的复权价算不出来**」这一类失败（数据侧不可用 ⇒ 回退未复权 + 计数）。
#: 刻意不是 `AdjustError` 全体：裸 `AdjustError`（`k > 1` = 负现金解析 bug、
#: `pre_close <= 0` = 数据坏）是**我们这边**的缺陷，必须炸出来
#: （ERROR_DIARY #81「数据脏与代码坏要分档」「兜底 catch 的是一类，不是已枚举的子类」）。
_ADJ_UNAVAILABLE = (adjust.EtfChainUnsupported, adjust.MissingFactor,
                    adjust.StaleFactorTable)

#: 因子级 IC 分解（P83 D3）需要的**输入因子**窗口长度：插桩1（`script_id=7`）的
#: `mom20` 要 `C[-1]`/`C[-20]`、`vr15` 要 `V[-5:]`/`V[-20:-5]` ⇒ 各取最近 20 根。
FACTOR_WINDOW = 20

#: `factor_inputs[code]["feats"]` 的键：管线里**已有**的 5 个财务因子 ＋ `period`。
#: 这 5 个现役短池插桩**一个都没用**（`candidate/indicators.py:90-91`），在 P83 里
#: 只作次读数。**不是新因子** —— 名单与 `research/factor.py::SECONDARY_FACTORS` 同源
#: （用例钉住两处相等），本站不扩充因子库。
FACTOR_FEATURE_KEYS: tuple[str, ...] = (
    "roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin")


def _factor_payload(ctx: dict) -> dict:
    """把**打分用的那个 ctx** 里的输入因子原样抠出来（P83 D3，只读暴露）。

    `ctx` 就是 `score.build_ctx` 喂给打分插桩的那个字典 —— 本函数**不另立价格
    口径、不重算复权**（D2），只是把插桩看得到的那两段窗口换个形状搬出来：

      - `closes` / `volumes`：`ctx["bars"]` 的**最后 ≤20 根**（`bars` 本身已按
        `date <= asof` 升序排好 ⇒ `closes[-1]` 就是插桩的 `C[-1]`）；
      - `feats`：`ctx["features"]` 里 5 个财务因子键 ＋ `period`。

    **它不进任何打分公式** —— 打分仍由插桩自己完成，本函数只做只读搬运。
    """
    bars = ctx["bars"][-FACTOR_WINDOW:]
    feats = ctx["features"]
    return {"closes": [float(b["close"]) for b in bars],
            "volumes": [float(b["volume"]) for b in bars],
            "feats": {k: feats[k] for k in (*FACTOR_FEATURE_KEYS, "period")}}


def _fallback_reason(exc: adjust.AdjustError) -> str:
    if isinstance(exc, adjust.EtfChainUnsupported):
        return FALLBACK_ETF
    if isinstance(exc, adjust.MissingFactor):
        return FALLBACK_UNUSABLE_EVENT
    return FALLBACK_STALE_BLACKOUT


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

    `scored`：`{pool: [打分行…]}`，就是本模块内部**已经构造好的**那个列表
    （含 `code` / `raw_score` / `adj_score`），`select_top` 与 `eligible` 都由它
    派生。**这是给度量用的只读暴露，不是新的打分口径** —— `research rank-ic`
    需要「该池全部已打分标的」（不只是前 N 只）来算 IC，而那条信息此前拿不到。
    `score_pipeline` 的打分口径/顺序/公式一字未动。同样是**只增字段**：老构造点
    不传即为空 dict（`frozen` ⇒ 必须给默认值）。

    `factor_inputs`（P83 D3）：`{code: {"closes": […], "volumes": […], "feats": {…}}}`，
    即短池打分插桩**这一次真正看到的输入因子**（取自同一个 `pool_ctx`，见
    `_factor_payload`）。只有 `score_pipeline(..., want_factors=True)` 才填；
    默认 `False` ⇒ 空 dict ⇒ **既有调用点的产物逐位不变**（`factor_inputs` 的
    键集合恒等于 `scored["short"]` 的 code 集合）。同样**不是新的打分口径**：
    打分仍由插桩完成，这里只是把它的输入读出来给因子级 IC 分解用。
    """

    members: list
    rejects: list
    params: dict
    eligible: dict[str, list[str]] = field(default_factory=dict)
    scored: dict[str, list[dict]] = field(default_factory=dict)
    factor_inputs: dict[str, dict] = field(default_factory=dict)


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
    """未复权 K 线（`bars_daily`, `adj_mode='none'`）—— **判定侧**用。

    只给 `screen.screen`（涨跌停 / 停牌判定）：涨跌停按**前收**判定，除权日的
    自然跳空正是「跌停」与「除权」的区别 —— 换成复权价会把除权误判成跌停、
    也会漏掉真实跌停（P77 D2，`screen.py` 一行不改）。**因子侧**另走
    `_load_bars_adjusted`（`ctx["bars"]`）。
    """
    rows = conn.execute(
        "SELECT code, date, open, high, low, close, volume, amount, turnover,"
        " source, adj_mode FROM bars_daily WHERE code = ? AND date <= ?"
        " ORDER BY date", (code, asof)).fetchall()
    return [Bar(**dict(r)) for r in rows]


def _load_bars_adjusted(conn: sqlite3.Connection, code: str, *, asof: str,
                        raw: list[Bar]) -> tuple[list[Bar], str | None]:
    """因子侧的 **PIT 复权** K 线（`ctx["bars"]` 的输入价，P77 D1/D3）。

    `as_of=asof`、**不缩窗**（`start` 不传）：成功时与同一天 `_load_bars`
    返回的 `date` 序列**逐位相同** —— 两边都由 `bars_daily` 的 `date <= asof`
    决定，复权层只做乘法、不增删行。

    返回 `(bars, fallback)`；`fallback is None` ⇒ `bars` 是真复权价，否则是
    回退原因标签，`bars` **原样等于调用方刚读的未复权 `raw`**（不重复读库）。
    四类回退（D3，`n_adj_fallback` 计数，**不许静默**）：

      - 复权层拒绝服务：ETF（`EtfChainUnsupported`）／窗口跨不过不可定价事件
        （`MissingFactor`）／缺口记录过期（`StaleFactorTable`）；
      - 复权链为空（该 code 还没采 `ingest actions`）⇒ 因子恒 1，
        `load_bars_adjusted` 的既有语义就是「价 = 未复权价」，等价于回退。

    **fail-open 的边界**：这是打分路径不是下单路径 —— 一只标的的复权链不可用
    不该让整轮 `candidate run` 死掉（真库 `csi300-500` 实测 148/800 只会走到
    「窗口跨不可定价事件」，整批 abort 不可接受）；但**必须可见**（计数进快照）。
    裸 `AdjustError`（代码/数据缺陷）**不在此列**，原样抛。
    """
    try:
        bars = adjust.load_bars_adjusted(conn, code, asof)
    except _ADJ_UNAVAILABLE as exc:
        return raw, _fallback_reason(exc)
    if conn.execute("SELECT 1 FROM corp_actions WHERE code = ? LIMIT 1",
                    (code,)).fetchone() is None:
        # 无事件 ⇒ 链上因子恒 1 ⇒ 复权价逐位等于未复权价。返回 `raw` 是把
        # 「等价」写成结构（也省掉一次浮点乘法），数值上与 `bars` 相同。
        return raw, FALLBACK_EMPTY_CHAIN
    return bars, None


def _cross_section_map(conn, *, asof: str, universe=None) -> dict:
    """每轮算一次横截面分位，避免对每个标的重复计算全样本。

    `universe`：扫描宇宙（`Instrument` 序列）。`None` ⇒ `SEED_UNIVERSE`（D2：默认
    路径一字不动）。**必须与 `score_pipeline` 的主循环用同一个集合** —— 分位是相对量，
    集合不同则分位口径与池宽不匹配（P70 设计稿 §4 #1）。
    """
    members = SEED_UNIVERSE if universe is None else universe
    rows = {}
    for inst in members:
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
                   plugin_overrides: dict[str, int] | None = None,
                   universe=None, universe_id: str | None = None,
                   members_sha256: str | None = None,
                   want_factors: bool = False
                   ) -> PipelineResult:
    """跑一遍候选池打分（步骤 3–8），**只返回、不写库**。

    回放要跑几百个历史调仓日，绝不能往 `candidate_snapshots` 灌历史回放行。
    生产主流程 `run_candidate` 也走这里 —— 两者必须产出相同成员，否则
    「回测跑的就是生产逻辑」不成立。

    `plugin_overrides`：`{plugin_id: script_id}`，用于沙盒的单变量对比
    （只换被比较的那个插件，其余仍解析 active）。

    `universe`：扫描宇宙（`Instrument` 序列）。`None` ⇒ `SEED_UNIVERSE`（**默认路径
    逐位不变**，D2）。横截面分位与主循环用**同一个**集合。
    `universe_id` / `members_sha256`：写进快照 `params_json` 的**口径增量**
    （老键 `seed_count` / `topn` 一字不动）。`None` ⇒ 按 `seed21` 派生 ——
    这样「默认路径」与「显式 `--universe seed21`」的快照参数**逐位相同**。

    `scoring_price_mode` / `n_adj_fallback`（P77 D4，同样是只增键）：因子侧
    `ctx["bars"]` 走 PIT 复权价、判定侧（`screen`）走未复权价；后者是这一轮
    回退到未复权价的标的数（口径见 `_load_bars_adjusted`）。

    `PipelineResult.scored`（P78 D2）把内部已构造好的完整打分行原样传出 ——
    给 `research rank-ic` 度量用，**不是新的打分口径**：`select_top` 与
    `eligible` 的派生逻辑、本函数的打分顺序与公式一字未动。

    `want_factors`（P83 D3）：`True` 时额外填 `PipelineResult.factor_inputs`
    （短池打分插桩这一次看到的输入因子）。默认 `False` ⇒ 一行都不多算、
    既有字段逐位不变；**打分口径与本函数的流转一字未动**。

    `rejects` 是**无损**的（P90）：同一 `(code, stage)` 可能出现多行 —— 一只标的
    在多个池都打分不通过时 `stage` 恒为 `'score'`。`candidate_rejects` 的主键是
    `(snapshot_id, code, stage)`，折叠发生在**写路径** `run_candidate`
    （`_dedup_rejects`）；本函数（回放/度量也用）与 `params` 的键集一字未动。
    """
    if universe_id is None:
        from stocklab.config.universes import SEED21_UNIVERSE_ID
        universe_id = SEED21_UNIVERSE_ID
    if members_sha256 is None:
        from stocklab.config.universes import seed21_sha256
        members_sha256 = seed21_sha256()

    members: list[snapshot.MemberRow] = []
    rejects: list[snapshot.RejectRow] = []
    scored: dict[str, list[dict]] = {p: [] for p in pools.ALL_POOLS}
    #: 短池打分插桩这一次看到的输入因子（P83 D3）。默认空 ⇒ 老调用点产物逐位不变。
    factor_inputs: dict[str, dict] = {}
    #: 打分输入价回退到未复权价的标的数（D3：fail-open 但**可见**）。
    #: 只统计**真正进入打分**的标的 —— `screen` 淘汰的标的从不构造 `ctx`，
    #: 把它们算进来会让这个数随排雷结果漂移，不是「这一轮有多少只没吃到复权」。
    n_adj_fallback = 0

    scan = SEED_UNIVERSE if universe is None else universe
    xsec = _cross_section_map(conn, asof=asof, universe=scan)

    for inst in scan:
        raw_bars = _load_bars(conn, inst.code, asof=asof)

        verdict = screen.screen(inst, raw_bars, asof=asof)   # D2：判定侧吃未复权
        if not verdict.passed:
            rejects.append(snapshot.RejectRow(
                code=inst.code, stage="pre_screen", reason=verdict.reason,
                plugin_id=None))
            continue

        bars, fallback = _load_bars_adjusted(conn, inst.code, asof=asof,
                                             raw=raw_bars)
        if fallback is not None:
            n_adj_fallback += 1

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
            if want_factors and pool == pools.POOL_SHORT:
                # 取自**打分用的同一个 `pool_ctx`**（D2/D4）—— 与上面那次
                # `score_pool` 吃的是同一个对象，不另建价格口径。
                factor_inputs[inst.code] = _factor_payload(pool_ctx)
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
                          eligible=eligible, scored=scored,
                          factor_inputs=factor_inputs,
                          params={"seed_count": len(scan),
                                  "universe_id": universe_id,
                                  "members_sha256": members_sha256,
                                  "topn": dict(pools.POOL_TOPN),
                                  "scoring_price_mode": SCORING_PRICE_MODE,
                                  "n_adj_fallback": n_adj_fallback})


def _dedup_rejects(
        rejects: list[snapshot.RejectRow]) -> tuple[list[snapshot.RejectRow], int]:
    """按 `(code, stage)` 去重，保留**流水线顺序**里的第一条；返回 (保留, 丢弃数)。

    为什么需要（P90 D1）：`candidate_rejects` 的主键是 `(snapshot_id, code, stage)`，
    而打分循环对 `eligible_pools(inst)` 的每个池各判一次、`stage` 恒为 `'score'`
    （不带池名 —— D1 明确不改 stage 词汇，那要动 schema 的 CHECK 与报告口径）。
    一只标的在 mid 与 long 两池都打分不通过 ⇒ 两行同 `(code, 'score')` ⇒ 撞主键。

    去重放在**写路径**上、不放进 `score_pipeline`：内核是回放与度量共用的只读
    产物，保持**无损**（每个池各自一行）；折叠只发生在「映射到表结构」这一步。

    丢的是「另一个池也拒了它」这条信息，**不丢任何买卖决策** —— 该标的在任一池
    不通过就不会进那一池的 `scored`（D1）。
    """
    seen: set[tuple[str, str]] = set()
    kept: list[snapshot.RejectRow] = []
    for row in rejects:
        key = (row.code, row.stage)
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept, len(rejects) - len(kept)


def run_candidate(conn: sqlite3.Connection, *, asof: str, run_kind: str,
                  now: str, universe=None, universe_id: str | None = None,
                  members_sha256: str | None = None) -> RunResult:
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

    pipe = score_pipeline(conn, asof=asof, universe=universe,
                          universe_id=universe_id, members_sha256=members_sha256)
    rejects, n_reject_dups_dropped = _dedup_rejects(pipe.rejects)
    # D2「只增键」：内核 params 一个键不动，写路径补上丢弃计数 —— 落库与
    # `RunResult.params` 用的是**同一份** dict，首跑与幂等重跑读回逐位相同。
    params = {**pipe.params, "n_reject_dups_dropped": n_reject_dups_dropped}
    # P91 D4（B7）：成员状态取**事件 overlay**（`asof_date <= asof` 的最新一行），
    # 不再硬编码 `观察中`。落点就在 `pools`/`scored` 之后的**组装**这一步：
    # 打分口径、`select_top` 的排序与截断一字未动，只换 `status` 一个字段。
    # 无任何事件时 `status_map` 给的就是 `DEFAULT_STATUS`（逐位等于 dataclass 默认值）
    # ⇒ 既有用例（`test_candidate_run*.py` / `test_candidate_snapshot.py`）逐位不变。
    statuses = cand_status.status_map(conn, [m.code for m in pipe.members],
                                      asof=asof)
    members = [replace(m, status=statuses[m.code]) for m in pipe.members]
    snapshot_id = snapshot.write_snapshot(
        conn, asof=asof, run_kind=run_kind, params=params,
        members=members, rejects=rejects, now=now)

    loaded = snapshot.load_snapshot(conn, snapshot_id)
    md = report.render_report(asof=asof, run_kind=run_kind, loaded=loaded,
                              generated_at=now)
    # 用 load_snapshot 返回的顺序（pool, adj_score DESC, code / stage, code）
    # 构建 RunResult，与幂等重跑路径保持一致。
    members_obj, rejects_obj = _hydrate(loaded)
    return RunResult(snapshot_id=snapshot_id, asof=asof, run_kind=run_kind,
                     members=members_obj, rejects=rejects_obj,
                     report_md=md, skipped=False, params=params)
