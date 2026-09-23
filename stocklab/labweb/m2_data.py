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
from collections.abc import Mapping, Sequence

from stocklab.backtest import metrics as bt
from stocklab.labweb import paper_data
from stocklab.m2 import config as m2_config
from stocklab.m2 import selfeval
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

#: 归因字段名（`m2/config.ATTRIBUTION_FIELD`）。P50 的人工确认填的就是这一格，
#: 本期它**恒为 `None`**（D-31）。
ATTR_FIELD: str = m2_config.ATTRIBUTION_FIELD

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
            # 分数行与预测行的主键都带出来：回流汇总要按 `forecast_id` 取来源行的
            # 落库时间（生成时间），页面/报告里也是「这条案例连着哪条预测」的锚
            "score_id": int(s["score_id"]), "forecast_id": int(s["forecast_id"]),
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


# ---------- ④ 周期读数 / 依据 / 待复核清单（P49） ----------


def cycle_readings(conn: sqlite3.Connection, cycle: Mapping, asof: str) -> dict:
    """某周期在 `asof` 的读数：**全部由 P41/P48 的既有函数算出**。

    本函数不重算任何一个指标：五指标与门禁逐字段复制
    `paper_data.performance`（P41 / ADR-023），错判案例与预测读数复用
    `miss_cases` / `forecast_readings`（P48）。窗口口径因此**只有一套** ——
    调用方不给窗口，也不给指标，只给「哪个周期、哪一天」。

    账户在绩效读数里没有行（账户没建 / 没有净值）⇒ 五个值一律 `None` + 说明，
    **不写 0**（0% 收益是「没涨没跌」，与「不知道」不是一回事）。
    """
    perf = paper_data.performance(conn, asof)
    account = str(cycle["account_id"])
    keys = tuple(perf["metric_keys"])
    row = next((r for r in perf["rows"] if str(r["account_id"]) == account), None)
    unavailable = None
    if row is None:
        unavailable = (f"绩效读数里没有账户 `{account}` 的行（账户没建 / 窗口内没有"
                       "净值行）—— 五个值一律留空，**不拿 0 顶替**")
        metrics = {k: None for k in keys}
        missing = {k: unavailable for k in keys}
    else:
        metrics = {k: row[k] for k in keys}
        missing = dict(row["missing"])
    forecast = forecast_readings(conn, asof)
    cases = case_summary(conn, cycle, asof)
    return {
        "source": ("`paper_data.performance`（P41 五指标 + 120 门禁）"
                   " + `m2_data.forecast_readings` / `miss_cases`（P48）"
                   "—— 逐字段复制，本层不重算"),
        "account_id": account, "asof": asof,
        "window": [str(perf["window"][0]), str(perf["window"][1])],
        "n_sessions": perf["n_sessions"],
        "metric_keys": list(keys),
        "metrics": metrics, "missing": missing,
        "excess_vs_index_300": (perf["excess_vs_index_300"] or {}).get(account),
        "sample_gate": dict(perf["sample_gate"]),
        "forecast": {
            "n_groups": forecast["n_groups"], "n_forecasts": forecast["n_forecasts"],
            "n_scored": forecast["n_scored"], "n_unscored": forecast["n_unscored"],
            "threshold": forecast["threshold"],
        },
        "cases": cases,
        "unavailable": unavailable,
    }


def _cycle_cases(conn: sqlite3.Connection, cycle: Mapping, asof: str) -> list[dict]:
    """周期内的错判案例 —— **P48 那一批**的子集（按账户 + 起跑日过滤）。

    为什么是「子集」而不是另一遍筛选：`miss_cases` 是错判案例集的**唯一**取数
    （P48 §2：没有筛选参数）。这里再写一遍筛选就等于开了第二个口径。代价是
    周期内错判条数超过 `CASE_LIMIT` 时只看得到最近的那一段 —— 所以两个数
    （窗口内总量 / 周期内条数）都返回，读者自己知道看到的是哪一段。
    """
    data = miss_cases(conn, asof)
    account = str(cycle["account_id"])
    start = str(cycle["start_date"])
    return [c for c in data["cases"]
            if str(c["account_id"]) == account and str(c["asof_date"]) >= start]


def _fingerprints(cases: Sequence[dict]) -> dict:
    """来源指纹：这一批案例是**哪几版脚本、在哪份 PIT 输入上**算出来的。"""
    return {
        "input_sha256": sorted({str(c["input_sha256"]) for c in cases
                                if c.get("input_sha256")}),
        "script_versions": sorted({str(c["script_version"]) for c in cases}),
        "plugin_ids": sorted({str(c["plugin_id"]) for c in cases}),
    }


def case_summary(conn: sqlite3.Connection, cycle: Mapping, asof: str) -> dict:
    """错判案例集的**汇总**（判定依据用；归因恒空，D-31）。

    「有没有可归因的方向」在判定里的机械判据就是这一条的 `n_cases`：
    0 条 ⇒ 拿不出方向 ⇒ 不许声称「优化 / 微调」（`m2/selfeval.py` 会拒绝）。
    """
    cases = _cycle_cases(conn, cycle, asof)
    dates = sorted(str(c["target_date"]) for c in cases)
    return {
        "n_cases": len(cases),
        "limit": CASE_LIMIT,
        "target_date_range": ([dates[0], dates[-1]] if dates else None),
        "codes": sorted({str(c["code"]) for c in cases}),
        **_fingerprints(cases),
        ATTR_FIELD: None,          # D-31：归因恒空，结构位留给 P50 的人工确认
        "note": ATTRIBUTION_NOTE,
    }


def review_backlog(conn: sqlite3.Connection, cycle: Mapping, asof: str) -> dict:
    """错判案例回流模块1 的**只读汇总**（P49 §3 / D-31）：按 `code` + 区间聚成待复核清单。

    ## 三个不许

    1. 不写任何表 —— 回流是「看」，不是「记事实」；
    2. 不填归因 —— `attribution` 是结构位，值恒 `None`（P50 才有人工确认）；
       清单**不是结论**，页面与报告都不许把它当结论渲染；
    3. 不挑案例 —— 不按幅度/标的/版本筛（P48 §2 同款纪律）。

    ## 生成时间不是墙上时钟

    `generated_at` = 清单所依据的那批**已落库分数行的落库时间上界**（取不到 ⇒ `None`）。
    墙上时钟在重放时会让同一份输入长出两个不同的「生成时间」，而这一站的
    判据是「同输入重放逐位相同」（ERROR_DIARY「不读墙上时钟做判定」）。页面
    顶部的「页面生成于 …」仍是墙上时钟 —— 两者是不同的东西，所以名字不同。
    """
    cases = _cycle_cases(conn, cycle, asof)
    ids = {int(c["forecast_id"]) for c in cases}
    stamps = [str(s["created_at"]) for s in m2_store.list_scores(conn, asof=asof)
              if int(s["forecast_id"]) in ids]
    groups: dict[str, dict] = {}
    for c in cases:
        g = groups.setdefault(str(c["code"]), {
            "code": str(c["code"]), "n": 0, "items": [],
            "window": [str(c["asof_date"]), str(c["target_date"])],
        })
        g["n"] += 1
        g["window"][0] = min(g["window"][0], str(c["asof_date"]))
        g["window"][1] = max(g["window"][1], str(c["target_date"]))
        g["items"].append({
            "asof_date": str(c["asof_date"]), "target_date": str(c["target_date"]),
            "plugin_id": str(c["plugin_id"]), "script_version": str(c["script_version"]),
            "input_sha256": c.get("input_sha256"),
            "predicted_class": c["predicted_class"], "actual_class": c["actual_class"],
            ATTR_FIELD: None,      # D-31：恒空
        })
    ordered = [groups[k] for k in sorted(groups)]
    return {
        "asof": asof, "cycle_id": int(cycle["cycle_id"]),
        "account_id": str(cycle["account_id"]),
        "generated_at": (max(stamps) if stamps else None),
        "generated_at_note": ("生成时间 = 清单所依据的**已落库分数行的落库时间上界**"
                              "（不读墙上时钟：同输入重放必须逐位相同）"),
        "fingerprints": _fingerprints(cases),
        "codes": ordered, "n_codes": len(ordered), "n_cases": len(cases),
        "attribution_note": ATTRIBUTION_NOTE,
        "read_only_note": ("只读汇总：回流**不写任何表**，也不是结论 —— "
                           "它是给人看的待复核清单（D-31：四分类只能人工确认）"),
    }


# ---------- 三块合一（页面 / 报告的唯一取数入口） ----------


def cycle_panel(conn: sqlite3.Connection, asof: str) -> dict:
    """④ 自评估判定与熔断（P49 §1/§2/§3）—— **只读台账**，一个数都不重算。

    判定行、熔断事件、轮次读数全部来自已经落库的台账（`m2_judgements` /
    `validation_events` / `validation_rounds`）—— 页面**不触发**判定、
    **不触发**熔断（触发只走 CLI，P49 §4）。所以这里连 `performance` 都不调：
    显示的是「当时落库的那一份」，不是「现在重算的那一份」。
    """
    from stocklab.store import validation as ledger

    cycles = ledger.list_cycles(conn)
    out: list[dict] = []
    for cycle in cycles:
        cid = int(cycle["cycle_id"])
        rounds = ledger.list_rounds(conn, cid)
        events = ledger.list_events(conn, cid)
        judgements = m2_store.list_judgements(conn, cycle_id=cid, asof=asof)
        fused = [e for e in events if e["kind"] == "circuit_breaker"]
        ended = [e for e in events if e["kind"] == "validation_end"]
        out.append({
            "cycle_id": cid, "script_id": int(cycle["script_id"]),
            "account_id": str(cycle["account_id"]),
            "planned_rounds": int(cycle["planned_rounds"]),
            "planned_days": int(cycle["planned_days"]),
            "params": json.loads(cycle["params_json"] or "{}"),
            "criteria_text": str(cycle["criteria_text"]),
            "start_date": str(cycle["start_date"]),
            "n_rounds": len(rounds),
            "latest_round": (rounds[-1] if rounds else None),
            "rounds": rounds,
            "events": events,
            "judgements": judgements,
            "latest_judgement": (judgements[-1] if judgements else None),
            "fuse_events": fused,
            "ended_events": ended,
            "state": ("fused" if fused else ("ended" if ended else "open")),
            "review": review_backlog(conn, cycle, asof),
        })
    return {
        "asof": asof, "available": bool(out), "cycles": out, "n_cycles": len(out),
        "reason": (None if out else
                   "库里还没有验证周期 —— 开一轮：`stocklab m2 cycle start "
                   "--script-id <N> --account-id <arm-agent-<版本>> --rounds 3 "
                   "--days 30 --criteria-text '<判据原文>' --start-date <YYYY-MM-DD>`"),
        # 主干边界的**只读视图**：值来自 `m2/selfeval.py` 的单一入口，
        # 本文件不直接引用那 5 个常量名（ADR-021 结构保证 3 的扫描判据）。
        "boundaries": dict(selfeval.BOUNDARIES),
        "boundary_labels": dict(selfeval.BOUNDARY_LABELS),
        "branch_labels": dict(m2_config.BRANCH_LABELS),
        "no_write_note": ("判定与熔断**只出建议 / 只追加事件**：本页没有任何写入口，"
                          "也不提供「触发判定」按钮 —— 触发只走 CLI（P49 §4）"),
        "criteria_note": ("判据原文（`criteria_text`）在建周期时落库，之后**逐字节不许改**"
                          "（D-31：事后换口径是这一站的典型作弊方式）"),
    }


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
        "cycles": cycle_panel(conn, asof),
    }
