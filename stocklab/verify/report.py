"""准确率报告（Task 35，P7）：从 `verifications` 的行聚合出**可上报**的数字。

## 这份报告的三条纪律（违反即视为无效结论）

1. **有效样本量 = 交易日数，不是行数。** A 股同涨同跌，2 只标的同一天 ≠ 2 个独立样本。
   所以每个指标都同时给「行级」与**按日聚合后**的均值、标准误、简单 95% 区间；
   报告里 `effective_n` 一律指交易日数，行数只作参考（ERROR_DIARY 2026-09-15）。
2. **样本不足就明说。** 不足 `MIN_DAYS` 个交易日 → 打上「样本不足，仅供观察」，
   **不得**用它选策略、**不得**写进优化理由。
3. **跑不赢就明说。** 方向准确率必须与「永远猜 flat / up / down」和「跟着 index_300 走」
   同表并列；`action` 超额必须同时给 `buy_and_hold`（可执行）与 `index_300`（不可交易）
   两个对照。只报赢的那个 = 骗自己。

## 本模块是**纯函数**

输入是 `verifications` 的行（含 `notes` 里的中间量），输出是 dict / markdown。
不读时钟、不读库 —— 所以「同 `--from/--to` 重复跑 → 报告逐字节一致」是结构性的，
不是靠「跑两次碰巧一样」。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

#: 样本量门槛（交易日）。不足此数一律标注「样本不足，仅供观察」。
MIN_DAYS = 120

#: 方向三分类的枚举序（算 Brier 与基准都按它）。
_DIRECTIONS = ("up", "flat", "down")

#: 把 `notes.undetermined` 里含该字段的行排除在「失效统计」之外：
#: `invalidated` 列是 `NOT NULL`，UNDETERMINED 落库时被迫写成 0，
#: 若照单全收就会把「不知道」算成「没失效」。
_UNDETERMINED_INVALIDATED = "invalidate_if"


def _mean(vals: Sequence[float]) -> float | None:
    return statistics.fmean(vals) if vals else None


def _daily(values_by_day: Mapping[str, list[float]]) -> dict[str, Any]:
    """按日聚类：先把同一交易日的所有标的聚成一个日均值，再跨日统计。

    这是**唯一**合法的区间估计口径：2 只标的日子不是 2 个独立样本。
    标准误用日均值的样本标准差 / sqrt(天数)；天数很少时区间会宽 —— 那是对的，
    宽区间正是在说「你还不知道」。
    """
    daily = [statistics.fmean(v) for v in values_by_day.values() if v]
    n = len(daily)
    if n == 0:
        return {"n_days": 0, "mean": None, "sd": None, "se": None, "ci95": None}
    mean = statistics.fmean(daily)
    sd = statistics.stdev(daily) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    return {"n_days": n, "mean": mean, "sd": sd, "se": se,
            "ci95": [mean - 1.96 * se, mean + 1.96 * se]}


def _classify(pct: float | None) -> str | None:
    """±0.5% 三分类（与 `score` 同一阈值来源，报告侧只用于基准对照）。"""
    from stocklab.predict.model import FLAT_BAND

    if pct is None:
        return None
    if pct > FLAT_BAND:
        return "up"
    if pct < -FLAT_BAND:
        return "down"
    return "flat"


def summarize(rows: Sequence[Mapping], *, from_date: str, to_date: str,
              min_days: int = MIN_DAYS) -> dict:
    """把 verification 行聚合成报告数据（按 `model_version` 分组）。"""
    groups: dict[str, list[Mapping]] = {}
    for row in rows:
        groups.setdefault(row["model_version"], []).append(row)
    return {
        "from_date": from_date,
        "to_date": to_date,
        "min_days": min_days,
        "model_versions": {mv: _summarize_group(g, min_days)
                           for mv, g in sorted(groups.items())},
        "notes": {
            "provenance": (
                "**历史回放（PIT 逐日重放）**：每个 asof 只用库里 <= asof 的行算出预测，"
                "再用次日真实复权 OHLC 打分。**不是实盘记录**、也不是纸面模拟 —— "
                "它证明的是「模型口径在历史上会给出什么分数」，不含任何执行摩擦之外的东西"
            ),
            "effective_n": (
                "`effective_n` = **交易日数**。A 股同涨同跌，2 只标的同一天不是 2 个独立样本，"
                "`rows` 只作参考、**不得**当样本量用于统计或选策略"
            ),
            "unscorable": (
                "不可评分的预测（无 bar / 停牌 / 复权不可用）结果列全空，"
                "**不进任何分母**，只在 `unscorable_*` 里计数并给出原因"
            ),
            "attribution": (
                "`DATA` 由程序判定；`SIGNAL`/`STRATEGY`/`MODEL`/`NOISE` 一律 `UNDETERMINED`，"
                "需人工在 `attribution_manual` 列标注后才能用于归因统计"
            ),
        },
    }


def _summarize_group(rows: Sequence[Mapping], min_days: int) -> dict:
    scorable = [r for r in rows if r["scorable"]]
    unscorable = [r for r in rows if not r["scorable"]]

    by_day: dict[str, list[Mapping]] = {}
    for r in scorable:
        by_day.setdefault(r["target_date"], []).append(r)

    def per_day(fn) -> dict[str, list[float]]:
        return {d: [x for x in (fn(r) for r in rs) if x is not None]
                for d, rs in by_day.items()}

    direction = _daily(per_day(lambda r: float(r["hit_direction"])))
    brier = _daily(per_day(lambda r: r["notes"].get("brier")))
    coverage = _daily(per_day(lambda r: float(r["hit_range"])))
    excess = _daily(per_day(lambda r: r["notes"].get("excess_ret")))

    excess_vals = [r["notes"]["excess_ret"] for r in scorable
                   if r["notes"].get("excess_ret") is not None]
    sim_vals = [r["notes"]["sim_ret"] for r in scorable
                if r["notes"].get("sim_ret") is not None]
    bh_vals = [r["notes"]["bh_ret"] for r in scorable
               if r["notes"].get("bh_ret") is not None]
    index_vals = [r["notes"]["index_pct"] for r in scorable
                  if r["notes"].get("index_pct") is not None]

    invalidated_known = [r for r in scorable
                         if _UNDETERMINED_INVALIDATED
                         not in r["notes"].get("undetermined", [])]

    return {
        "n_predictions": len(rows),
        "n_scorable": len(scorable),
        "n_unscorable": len(unscorable),
        "unscorable_reasons": _count(unscorable, "reason_code"),
        # 有效样本量口径：交易日数（行数只作参考）
        "effective_n": len(by_day),
        "n_rows": len(scorable),
        "rows_by_day_avg": (len(scorable) / len(by_day)) if by_day else None,
        "direction": {
            "accuracy_row": _mean([float(r["hit_direction"]) for r in scorable]),
            "accuracy_daily": direction,
            "brier_row": _mean([r["notes"].get("brier") for r in scorable
                                if r["notes"].get("brier") is not None]),
            "brier_daily": brier,
        },
        "range": {"coverage_row": _mean([float(r["hit_range"]) for r in scorable]),
                  "coverage_daily": coverage,
                  "nominal": 0.80},
        "levels": {
            "hit_all_row": _mean([float(r["hit_levels"]) for r in scorable
                                  if r["hit_levels"] is not None]),
            "level_realized_row": _mean([r["score_level"] for r in scorable
                                         if r["score_level"] is not None]),
        },
        "action": {
            "sim_ret_sum": sum(sim_vals) if sim_vals else None,
            "bh_ret_sum": sum(bh_vals) if bh_vals else None,
            "index_ret_sum": sum(index_vals) if index_vals else None,
            "excess_mean": _mean(excess_vals),
            "excess_daily": excess,
            "excess_win_rate": (_mean([1.0 if x > 0 else 0.0 for x in excess_vals])
                                if excess_vals else None),
        },
        "invalidated": {
            "n_known": len(invalidated_known),
            "rate": _mean([float(r["invalidated"]) for r in invalidated_known]),
            "n_undetermined": len(scorable) - len(invalidated_known),
            # ↓ 以下两个字段是**纯文字**（P24，照 Task 41 的 `_baselines.note` 形态）：
            #   不参与任何计算，也不改变上面三个数字。作用是把这组数字的**口径**写在
            #   它旁边，避免读者把「事后子群的纯度」当成「模型的方向能力」。
            "note": (
                "**分组键 = 次日收盘**（次日收盘价是否越过 asof 当日算出的支撑/阻力边界）。"
                "它是**结果条件、不可交易** —— 该子群只能在收盘后才被划分出来，"
                "预测时点不存在可执行的选行规则；"
                "本节的命中率**不得作为模型能力证据**"
            ),
            "pit_comparable": (
                "该子群**没有** PIT 口径的可比对照：诊断"
                "（`docs/diagnostics/2026-09-15-invalidated-subgroup.md`）用「预测时点可观测"
                "的边界最窄 10%」选行复现该子群，模型准确率与全体无差别，反向对照"
                "（最宽 20%）方向也不相反 → 机制不存在。故**不得作为变体假设的来源**；"
                "要看方向能力，请读上面的按日聚类准确率与 `always_up` / `always_down`"
            ),
        },
        "baselines": _baselines(scorable),
        "sample_gate": _gate(len(by_day), min_days),
    }


def _count(rows: Sequence[Mapping], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return out


def _baselines(scorable: Sequence[Mapping]) -> dict[str, Any]:
    """四个「不用模型也能做」的对照：永远 flat / 永远 up / 永远 down / 跟着 index_300。"""
    actual = [(r, r["notes"].get("actual_class")) for r in scorable]
    actual = [(r, a) for r, a in actual if a]
    out: dict[str, Any] = {}
    for name, guess in (("always_flat", "flat"), ("always_up", "up"),
                        ("always_down", "down")):
        out[name] = {
            "accuracy_row": _mean([1.0 if a == guess else 0.0 for _, a in actual]),
            "accuracy_daily": _daily({
                r["target_date"]: [1.0 if a == guess else 0.0]
                for r, a in actual}),
        }
    index_rows = [(r, a, _classify(r["notes"].get("index_pct")))
                  for r, a in actual]
    index_rows = [(r, a, ic) for r, a, ic in index_rows if ic]
    out["index_300"] = {
        "n_rows": len(index_rows),
        "accuracy_row": _mean([1.0 if ic == a else 0.0 for _, a, ic in index_rows]),
        "accuracy_daily": _daily({
            r["target_date"]: [1.0 if ic == a else 0.0]
            for r, a, ic in index_rows}),
        "note": (
            "index_300 同期（asof→target，与个股被预测窗口**同一段**）涨跌方向。"
            "**同窗口、含未来信息、不可交易**，仅作参照，"
            "**不作为可比的预测对手**；缺指数数据的交易日不参与"
        ),
        "pit_comparable": (
            "PIT 口径的可比对手是 `index-mom-dir`（只用 <=asof 的指数方向）"
            "与 `always_up` / `always_down`；`index_300` 不在此列"
        ),
    }
    return out


def _gate(n_days: int, min_days: int) -> dict:
    ok = n_days >= min_days
    return {
        "effective_n": n_days,
        "min_days": min_days,
        "sufficient": ok,
        "label": "样本充足" if ok else "样本不足，仅供观察（不得用于选策略）",
    }


# ---------- 呈现 ----------

def _pct(x: float | None, digits: int = 2) -> str:
    return "—" if x is None else f"{100 * x:.{digits}f}%"


def _num(x: float | None, digits: int = 4) -> str:
    return "—" if x is None else f"{x:.{digits}f}"


def _daily_cell(d: Mapping) -> str:
    if not d or d.get("mean") is None:
        return "—"
    ci = d.get("ci95") or [0, 0]
    return (f"{d['mean']:.4f} ± {d['se']:.4f} "
            f"[{ci[0]:.4f}, {ci[1]:.4f}] (n={d['n_days']} 日)")


def render_markdown(summary: Mapping) -> str:
    """把 summary 渲染成 markdown（确定性：不含任何生成时间）。"""
    L: list[str] = []
    L.append(f"# 准确率报告（历史回放） {summary['from_date']} → {summary['to_date']}")
    L.append("")
    L.append(f"> {summary['notes']['provenance']}")
    L.append("")
    for mv, g in summary["model_versions"].items():
        L.append(f"## `{mv}`")
        L.append("")
        L.append(f"- 预测条数 {g['n_predictions']} / 可评分 {g['n_scorable']} / "
                 f"不可评分 {g['n_unscorable']} {g['unscorable_reasons'] or ''}")
        L.append(f"- **有效样本量（交易日数）{g['effective_n']}**，行数 "
                 f"{g['n_rows']}（参考值，**不得**当样本量）"
                 f"；日均 {_num(g['rows_by_day_avg'], 2)} 行/日")
        L.append(f"- 样本门槛：{g['sample_gate']['label']}"
                 f"（阈值 {g['sample_gate']['min_days']} 交易日）")
        L.append("")
        L.append("### 方向（三分类，±0.5%）")
        L.append("")
        L.append("| 口径 | 值 |")
        L.append("|------|----|")
        L.append(f"| 行级准确率 | {_pct(g['direction']['accuracy_row'])} |")
        L.append(f"| **按日聚类准确率 ± 标准误 [95% CI]** | "
                 f"{_daily_cell(g['direction']['accuracy_daily'])} |")
        L.append(f"| Brier（行级均值，越小越好；随机猜 ≈ 0.667） | "
                 f"{_num(g['direction']['brier_row'])} |")
        L.append(f"| Brier 按日聚类 | {_daily_cell(g['direction']['brier_daily'])} |")
        L.append("")
        L.append("### 基准对照（同样的日、同样的样本）")
        L.append("")
        L.append("| 预测器 | 行级准确率 | 按日聚类 |")
        L.append("|--------|-----------|----------|")
        for name, b in g["baselines"].items():
            L.append(f"| {name} | {_pct(b['accuracy_row'])} | "
                     f"{_daily_cell(b['accuracy_daily'])} |")
        L.append(f"| **{mv}** | {_pct(g['direction']['accuracy_row'])} | "
                 f"{_daily_cell(g['direction']['accuracy_daily'])} |")
        L.append("")
        if "index_300" in g["baselines"]:
            b = g["baselines"]["index_300"]
            L.append(f"> ⚠️ `index_300`：{b['note']}")
            L.append(">")
            L.append(f"> {b['pit_comparable']}")
            L.append("")
        L.append("### 区间 / 关键位 / 动作")
        L.append("")
        L.append("| 项 | 值 |")
        L.append("|----|----|")
        L.append(f"| `range_80` 覆盖率（标称 80%） | "
                 f"{_pct(g['range']['coverage_row'])} |")
        L.append(f"| `range_80` 覆盖率按日聚类 | "
                 f"{_daily_cell(g['range']['coverage_daily'])} |")
        L.append(f"| 关键位「会/不会触及」全部兑现率（行级） | "
                 f"{_pct(g['levels']['hit_all_row'])} |")
        L.append(f"| 关键位逐位兑现率（行级） | "
                 f"{_pct(g['levels']['level_realized_row'])} |")
        L.append(f"| `action` 累计收益（扣成本，按日算术和） | "
                 f"{_pct(g['action']['sim_ret_sum'])} |")
        L.append(f"| `buy_and_hold` 同日累计（同成本、可执行） | "
                 f"{_pct(g['action']['bh_ret_sum'])} |")
        L.append(f"| `index_300` 同日累计（无成本、不可交易） | "
                 f"{_pct(g['action']['index_ret_sum'])} |")
        L.append(f"| **超额（vs buy_and_hold）均值 ± 标准误 [95% CI]** | "
                 f"{_daily_cell(g['action']['excess_daily'])} |")
        L.append(f"| 超额为正的比例 | {_pct(g['action']['excess_win_rate'])} |")
        L.append(f"| `invalidate_if` 命中率（n={g['invalidated']['n_known']}，"
                 f"另有 {g['invalidated']['n_undetermined']} 条不可判定） | "
                 f"{_pct(g['invalidated']['rate'])} |")
        L.append("")
        # 口径**紧挨着数字**输出（照 Task 41 的 index_300 caveat 形态）：
        # 这段话不改变上面任何一个数字，只声明它们该怎么读。
        inv = g["invalidated"]
        L.append(f"> ⚠️ `invalidate_if` 子群：{inv['note']}")
        L.append(">")
        L.append(f"> {inv['pit_comparable']}")
        L.append("")
    L.append("## 口径声明（读数字前必看）")
    L.append("")
    for text in summary["notes"].values():
        L.append(f"- {text}")
    L.append("- `action` 的收益是**逐日独立进出**的反事实（当日 asof 收盘建仓、"
             "次日收盘平仓），按日**算术和**而非复利 —— 它度量的是「每天听它的」"
             "期望超额，不是一条可投资的净值曲线（组合模拟属 P8）。")
    L.append("- `score_action` 用的是**参数无关**判据（严格跑赢同日 buy_and_hold 记 1），"
             "原始超额在 `excess_*` 里，两者都要看。")
    L.append("")
    return "\n".join(L) + "\n"
