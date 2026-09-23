"""模块2 页面与报告的取数（P48 §3）：三方对标 / 预测校验读数 / 错判案例集。

**本模块一个指标都不算。** 每一样都从既有真源取：

| 块 | 真源 | 本模块做的事 |
|---|---|---|
| ① 三方对标 | `paper_data.performance`（P41 / ADR-023 的五指标 + 120 门禁） | **按账户挑出三方**，逐字段原样搬运 |
| ② 预测校验读数 | `m2_forecast_scores` 的**已落库行** | 分组、数数、取分位 |
| ② 的盈亏比 / 最大回撤 | `backtest/metrics.py::profit_loss_ratio`、`paper/engine.py::drawdown` | 只调，不写第二份 |
| ③ 错判案例集 | 同一批已落库行 | 按 `target_date` 倒序取最近 `CASE_LIMIT` 条 |

## 为什么三方对标是「挑行」而不是「算数」

三方（`arm-agent-<策略版本>` / `arm-now` / `sh000300`）本来就在
`paper_data.performance` 的返回里各占一行 —— 它是**全部**模拟盘账户 + 基准。
本模块只按账户把它挑出来，`METRIC_KEYS` 五个值**逐字段复制**。
再算一遍（哪怕公式一模一样）就会有两个「总收益」，而它们迟早会在某次
边界处理上分歧（P41 T4 与 `CLAUDE.md` 度量纪律第 6 条要防的就是这个）。

## 为什么 AI 那一方按 `executor` 认，不按名字前缀认

`arm-agent-random`（P52 的随机对照臂）与 `arm-agent-<策略版本>`（通路 A）**前缀相同**。
前缀认法会把随机臂当成一个「策略版本」列进三方对标 —— 那等于把对照组
混进被考核的那一方。所以 AI 方按账户行里的 `params.executor == 'm2_channel_a'`
认（与 `m2/channel_a.py::load_account` **同一条判据**，同一份真源）。

## 缺数据一律 `None` + 原因，**不写 0**

`0` 在这几块里全都是**有意义的读数**（0% 收益、0 次命中、0% 回撤），
拿它表示「没有」会让「这天没涨没跌」与「不知道这天」长得一模一样。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from stocklab.backtest import metrics as bt
from stocklab.labweb import paper_data
from stocklab.m2 import config as m2_config
from stocklab.m2 import store as m2_store
from stocklab.paper import store as paper_store
from stocklab.paper.engine import drawdown

#: 五指标的名字与顺序、门禁、措辞全部**引用** P41 的实现，不另开一份
#: （ADR-023 定死名字与顺序；`sample_gate` 是那把唯一的尺子）。
METRIC_KEYS: tuple[str, ...] = paper_data.METRIC_KEYS
METRIC_LABELS: dict[str, str] = paper_data.METRIC_LABELS
INSUFFICIENT: str = paper_data.PERFORMANCE_INSUFFICIENT
SAMPLE_THRESHOLD: int = paper_data.PERFORMANCE_THRESHOLD

SIDE_AI = "ai"
SIDE_MIRROR = "mirror"
SIDE_BENCHMARK = "benchmark"

SIDE_LABELS: dict[str, str] = {
    SIDE_AI: "AI 模拟（arm-agent-<策略版本>）",
    SIDE_MIRROR: "人工镜像（arm-now）",
    SIDE_BENCHMARK: "市场基准（沪深300 指数）",
}

#: 基准的口径标注。指数**不可直接交易、无成本** ⇒ 与各臂比时口径偏乐观。
BENCHMARK_CAVEAT = (
    "基准是**指数**：不可直接交易、无成本、无仓位约束。各臂的净值都扣了自己的成本，"
    "所以拿臂与指数相减时，那条线上少了一笔真实交易要付的钱 —— 口径**偏乐观**，"
    "读数只能当参照，不能当「这条臂能拿到这个数」")

#: 错判案例的条数上限（P48 §2：**不是可选参数**，见 `m2/config.CASE_LIMIT`）。
CASE_LIMIT: int = m2_config.CASE_LIMIT

#: 归因字段的处置（D-31）：四分类代码判不了，这里**恒空**，结构位留给 P50 的人工确认。
ATTRIBUTION_NOTE = (
    "归因恒空：D-31 明令四分类（大盘冲击 / 行业黑天鹅 / 个股突发利空 / 因子失效）"
    "代码判不了，只能由人工确认（P50）。本页与报告**不自动填充**任何一档 —— "
    "编一个标签比留空危险得多，因为它看起来像一个结论")


# ---------- ① 三方对标 ----------


def _channel_a_accounts(conn: sqlite3.Connection) -> list[str]:
    """通路 A 的账户 id（`params.executor == 'm2_channel_a'`）—— 每个策略版本一行（D-26）。

    这条判据与 `m2/channel_a.py::load_account` **逐字相同**：换个地方认账户，
    就会出现「页面列的这一方」与「通路下单的那一方」不是同一批（前缀认法会
    把 `arm-agent-random` 混进来，见模块 docstring）。
    """
    out = []
    for account in paper_store.load_accounts(conn):
        try:
            params = json.loads(account["params_json"] or "{}")
        except ValueError:
            continue
        if params.get(m2_config.EXECUTOR_KEY) == m2_config.EXECUTOR_CHANNEL_A:
            out.append(str(account["account_id"]))
    return sorted(out)


def _absent_row(role: str, account_id: str, reason: str) -> dict:
    """这一方**不存在**（账户没建 / 没净值行）时的行：全 `None` + 原因，不写 0。"""
    return {
        "role": role, "label": SIDE_LABELS[role], "account_id": account_id,
        "kind": None, "available": False, "reason": reason,
        **{k: None for k in METRIC_KEYS}, "n_sessions": None,
        "missing": {k: reason for k in METRIC_KEYS},
        "excess_vs_index_300": None,
    }


def _side_row(role: str, base: dict, *, excess: float | None) -> dict:
    """P41 的那一行 → 三方对标的一行。**五个值逐字段复制**（一个都不重算）。"""
    return {
        "role": role, "label": SIDE_LABELS[role],
        "account_id": str(base["account_id"]), "kind": base["kind"],
        "available": True, "reason": None,
        **{k: base[k] for k in METRIC_KEYS},
        "n_sessions": base["n_sessions"],
        "missing": dict(base["missing"]),
        "excess_vs_index_300": excess,
    }


def _deferred(conn: sqlite3.Connection) -> list[dict]:
    """未接入的基准：**逐个查它在不在库里**。

    为什么查库而不是照抄一句「拿不到」：那句说明会在数据到位那天变成假话。
    一旦 `sh000905` 有了行，这里就变成「已入库但本轮未接入」——
    「悄悄少一个基准」在页面上变成一句显式的待办，而不是一个沉默的缺口。
    """
    out = []
    for item in m2_config.DEFERRED_BENCHMARKS:
        present = conn.execute(
            "SELECT COUNT(*) FROM bars_daily WHERE code = ? AND adj_mode = 'none'",
            (item["code"],)).fetchone()[0]
        out.append({**dict(item), "present_in_db": int(present) > 0})
    return out


def three_way(conn: sqlite3.Connection, asof: str) -> dict:
    """三方对标：AI 模拟（每策略版本一行）/ 人工镜像 / 市场基准。

    行**全部来自** `paper_data.performance(conn, asof)` 的返回
    （P41 = ADR-023 的五指标 + 120 交易日门禁），本函数只挑行。
    """
    perf = paper_data.performance(conn, asof)
    by_id = {str(r["account_id"]): r for r in perf["rows"]}
    excess = perf["excess_vs_index_300"]

    sides: list[dict] = []
    ai_ids = _channel_a_accounts(conn)
    if ai_ids:
        for aid in ai_ids:
            base = by_id.get(aid)
            if base is None:                       # pragma: no cover - 账户行与读数同源
                sides.append(_absent_row(
                    SIDE_AI, aid, f"`{aid}` 在绩效读数里没有行 —— 账户在、读数不在"))
                continue
            sides.append(_side_row(SIDE_AI, base, excess=excess.get(aid)))
    else:
        sides.append(_absent_row(
            SIDE_AI, f"{m2_config.ACCOUNT_PREFIX}<策略版本>",
            "库里没有通路 A 的账户（`params.executor == 'm2_channel_a'`）—— "
            "建一个：`stocklab m2 account init --strategy-version <版本>`。"
            "**没有版本就没有这一方**，不拿别的账户顶替"))

    mirror = by_id.get(m2_config.MIRROR_ACCOUNT)
    if mirror is None:
        sides.append(_absent_row(
            SIDE_MIRROR, m2_config.MIRROR_ACCOUNT,
            f"库里没有 `{m2_config.MIRROR_ACCOUNT}` 的绩效读数 —— "
            "它由 `paper init` 建、由 `paper step` / `m2 channel b` 推进"))
    else:
        sides.append(_side_row(
            SIDE_MIRROR, mirror, excess=excess.get(m2_config.MIRROR_ACCOUNT)))

    bench_code = m2_config.BENCHMARKS[0]
    bench = by_id.get(bench_code)
    if bench is None:
        sides.append(_absent_row(
            SIDE_BENCHMARK, bench_code,
            f"库里没有 {bench_code} 的读数行（`bars_daily` 没有窗口内的收盘价）—— "
            "基准缺一天就是整行不可判定，**不用相邻日顶替**"))
    else:
        sides.append(_side_row(SIDE_BENCHMARK, bench, excess=None))

    deferred = _deferred(conn)
    blocked = [d for d in deferred if d["present_in_db"]]
    scope = (f"本轮基准只对 `{bench_code}`（D-43）"
             + ("" if not deferred else
                "；未接入：" + "、".join(
                    f"`{d['code']}`（{d['name']}）" for d in deferred)))
    return {
        "asof": asof, "available": True, "reason": None,
        "metric_keys": list(perf["metric_keys"]),
        "sample_gate": dict(perf["sample_gate"]),
        "window": list(perf["window"]),
        "n_sessions": perf["n_sessions"],
        "sides": sides,
        "benchmark_caveat": BENCHMARK_CAVEAT,
        "scope": scope,
        "deferred": deferred,
        "deferred_blocked": blocked,
        # P41 的口径注释原样带出（窗口/期初/回撤符号），不在这一层改写它
        "notes": list(perf["notes"]),
    }


# ---------- ② 预测校验读数 ----------


def _quantile(sorted_values: Sequence[float], q: float) -> float | None:
    """分位（**线性插值**，与 `numpy.percentile` 的默认口径一致）。

    「按分位给（不要只给均值）」是 P48 §2 第 2 行的原文要求：偏差分布右偏时
    均值既不是典型也不是上界，一个数回答不了「偏差长什么样」。方法写死成线性插值
    并用手算用例钉住 —— 分位算法不写清楚，「p90 是多少」就会随实现漂。
    """
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_values[0])
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_values[lo]) * (1.0 - frac) + float(sorted_values[hi]) * frac


#: 分位表要报的分位点。**固定**，不是参数 —— 挑分位点是「挑好看的」的一种。
QUANTILES: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)


def _quantiles(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    return {f"p{int(q * 100)}": _quantile(ordered, q) for q in QUANTILES}


def _bet_nav(bets: Sequence[float]) -> list[float]:
    """按预测方向 1 单位名义下注的净值序列（起于 1.0）。

    **不含成本、不含仓位** —— m2 的预测契约里没有 `size_pct`
    （`plugin/contract.py::_M2_FORECAST_SHAPE`），所以这是一条「方向本身」的
    假想曲线，不是账户收益。页面上必须这么写，否则它会被读成「这条臂赚了多少」。
    """
    nav, out = 1.0, []
    for b in bets:
        nav *= 1.0 + float(b)
        out.append(nav)
    return out


def _drawdown_of(bets: Sequence[float]) -> float | None:
    """上面那条假想曲线的最大回撤（**复用 `paper/engine.py::drawdown`**）。

    与 `backtest/metrics` 的符号口径不同：`drawdown` 给的是**正值**幅度
    （与 `/lab/paper` 既有那一列同款，ADR-023 的展示约定）。
    """
    navs = _bet_nav(bets)
    if not navs:
        return None
    return drawdown(navs[:-1], navs[-1])


def _group_key(row: dict) -> tuple[str, str, str]:
    """分列键：`plugin_id` + `script_version` + `account_id`。

    P48 §2 点名的是前两项；`account_id` 一起进键是因为 D-35 把「策略版本」
    落在账户名上（`arm-agent-<版本>`）—— 两个账户若共用同一版 `m2_a3`，
    合起来数就是「把两个策略的结果相加」，而那条正是本轮明令禁止的。
    **多一列只可能更保守，不可能把两个版本混起来。**
    """
    return (str(row["plugin_id"]), str(row["script_version"]),
            str(row["account_id"]))


def _group_readings(key: tuple[str, str, str], rows: list[dict], *,
                    n_pending: int) -> dict:
    """一组（同一 `plugin_id` + `script_version` + `account_id`）的五件读数。"""
    plugin_id, script_version, account_id = key
    scored = [r for r in rows if r["scorable"]]
    unscorable = [r for r in rows if not r["scorable"]]
    bets = [float(r["bet_pct"]) for r in scored if r["bet_pct"] is not None]
    hits = [r for r in scored if r["hit_direction"] == 1]
    devs = [float(r["dev_pct"]) for r in scored if r["dev_pct"] is not None]
    plr = bt.profit_loss_ratio(bets)
    missing: dict[str, str] = {}
    if not scored:
        for name, why in (
                ("win_rate", "这一组还没有可评分的预测"),
                ("profit_loss_ratio", "这一组还没有可评分的预测"),
                ("max_drawdown", "这一组还没有可评分的预测"),
                ("deviation", "这一组还没有可评分的预测"),
                ("range_hit_rate", "这一组还没有可评分的预测")):
            missing[name] = why
    if plr is None and scored:
        missing["profit_loss_ratio"] = (
            f"{len(bets)} 个观测里只有赢或只有输 —— 盈亏比算不出"
            "（**不是 0、不是无穷大**：分母那一侧还没出现观测）")
    reasons: dict[str, int] = {}
    for r in unscorable:
        code = str(r["reason_code"])
        reasons[code] = reasons.get(code, 0) + 1
    return {
        "plugin_id": plugin_id, "script_version": script_version,
        "account_id": account_id,
        # 1 胜率（方向命中率）
        "n_scored": len(scored), "n_hits": len(hits),
        "win_rate": (len(hits) / len(scored)) if scored else None,
        # 2 收益偏差分布（按分位）+ 区间命中
        "deviation": _quantiles(devs),
        "n_inside_range": sum(1 for r in scored if r["range_hit"] == 1),
        "range_hit_rate": (
            sum(1 for r in scored if r["range_hit"] == 1) / len(scored)
            if scored else None),
        "n_invalidated": sum(1 for r in scored if r["invalidated"] == 1),
        "n_invalidate_undetermined": sum(1 for r in scored
                                         if r["invalidated"] is None),
        # 3 盈亏比 / 4 最大回撤
        "profit_loss_ratio": plr,
        "max_drawdown": _drawdown_of(bets),
        # 不可评分与未到期
        "n_unscorable": len(unscorable), "unscorable_reasons": reasons,
        "n_pending": n_pending,
        "missing": missing,
        "gate": paper_data.sample_gate(len(scored)),
    }


def forecast_readings(conn: sqlite3.Connection, asof: str) -> dict:
    """五件读数，按 `plugin_id` + `script_version`（+ 账户）**分列**，不许相加。

    样本门禁与三方对标用**同一个** `sample_gate`（120 交易日，同源同值）：
    预测对数不足 120 同样判 `insufficient`，同样只给读数、不给结论。
    """
    scores = m2_store.list_scores(conn, asof=asof)
    forecasts = m2_store.list_forecasts(conn)
    # 「未到期 / 尚未打分」= 窗口内的预测里，没有对应分数行的那些
    window = [f for f in forecasts if str(f["asof_date"]) <= asof]
    scored_ids = {int(s["forecast_id"]) for s in scores}
    n_scored_in_window = sum(1 for f in window
                             if int(f["forecast_id"]) in scored_ids)

    buckets: dict[tuple[str, str, str], list[dict]] = {}
    pending: dict[tuple[str, str, str], int] = {}
    for f in window:
        key = _group_key(f)
        if int(f["forecast_id"]) in scored_ids:
            continue
        pending[key] = pending.get(key, 0) + 1
    for s in scores:
        buckets.setdefault(_group_key(s), []).append(s)
    for key in pending:
        buckets.setdefault(key, [])

    groups = [_group_readings(key, buckets[key], n_pending=pending.get(key, 0))
              for key in sorted(buckets)]
    return {
        "asof": asof, "available": bool(scores or window),
        "reason": (None if (scores or window) else
                   "`m2_forecasts` 里没有 ≤ asof 的预测 —— 先跑 "
                   "`stocklab m2 channel a|b --asof <交易日>`；"
                   "这一块不拿 0 顶替（0 次命中与「没有预测」是两件事）"),
        "groups": groups,
        "n_groups": len(groups),
        "n_forecasts": len(window),
        "n_scored": n_scored_in_window,
        "n_unscored": len(window) - n_scored_in_window,
        "threshold": SAMPLE_THRESHOLD,
        "insufficient_wording": INSUFFICIENT,
        "per_version_rule": (
            "读数按 `plugin_id` + `script_version`（+ 账户）**分列**："
            "不同版本**不许相加、不许求平均** —— 相加出来的那个数不对应任何一版脚本，"
            "而它会被当成「这条策略的成绩」（与 P52 的 `delta_vs_random` 同一条纪律）"),
        "metric_note": (
            "胜率 = 方向命中率（判据与 `verify/` 逐位相同：|次收益| ≤ `FLAT_BAND` "
            "记平，概率 argmax 平手按 flat > up > down）；"
            "盈亏比 / 最大回撤吃同一条「按预测方向 1 单位下注」的序列 —— "
            "**不含成本、不含仓位**（m2 预测契约里没有 `size_pct`），"
            "所以它们是「方向本身」的读数，不是账户收益"),
    }


# ---------- ③ 错判案例集 ----------


def miss_cases(conn: sqlite3.Connection, asof: str) -> dict:
    """方向错的样本，按 `target_date` **倒序**取最近 `CASE_LIMIT` 条（P48 §2 第 5 行）。

    **没有筛选参数**：不按幅度、不按标的、不按版本挑 —— 能挑就等于能把
    不好看的那几条藏起来。样本总量与条数上限都显示出来，读者自己知道
    看到的是最近的一段，不是全部。
    """
    scores = m2_store.list_scores(conn, asof=asof)
    by_forecast = {int(f["forecast_id"]): f
                   for f in m2_store.list_forecasts(conn)}
    misses = [s for s in scores if s["scorable"] and s["hit_direction"] == 0]
    misses.sort(key=lambda s: (str(s["target_date"]), str(s["code"]),
                               int(s["forecast_id"])), reverse=True)
    cases = []
    for s in misses[:CASE_LIMIT]:
        f = by_forecast.get(int(s["forecast_id"])) or {}
        cases.append({
            "code": str(s["code"]), "asof_date": str(s["asof_date"]),
            "target_date": str(s["target_date"]),
            "plugin_id": str(s["plugin_id"]),
            "script_version": str(s["script_version"]),
            "account_id": str(s["account_id"]),
            # 溯源：这条预测是在哪份 PIT 输入上算出来的
            "input_sha256": f.get("input_sha256"),
            "predicted_class": s["pred_class"], "actual_class": s["actual_class"],
            "actual_pct": s["actual_pct"],
            "direction": f.get("direction"),
            # D-31：恒空，结构位留给 P50 的人工确认
            "attribution": None,
        })
    return {
        "asof": asof,
        "cases": cases,
        "n_cases": len(cases),
        "n_miss_total": len(misses),
        "n_scored": sum(1 for s in scores if s["scorable"]),
        "limit": CASE_LIMIT,
        "attribution_note": ATTRIBUTION_NOTE,
        "empty_reason": (None if cases else
                         ("窗口内没有可评分的预测" if not scores else
                          "窗口内没有方向判错的样本")),
    }


# ---------- 三块合一（页面 / 报告的唯一取数入口） ----------


def panel(conn: sqlite3.Connection, asof: str) -> dict:
    """`/lab/m2` 与 `m2 report` 的**同一个**取数函数（P48 §3 的「同源」判据）。

    页面与报告各写各的取数，就会出现两个「总收益」/两个「胜率」——
    两处各算一遍必然走样（P41 T4 同款）。
    """
    return {
        "asof": asof,
        "three_way": three_way(conn, asof),
        "forecast": forecast_readings(conn, asof),
        "cases": miss_cases(conn, asof),
    }
