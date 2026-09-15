"""实验的度量层（P8）：口径冻结、配对日差、晋级判定。

## 为什么需要「配对日差」而不是两个均值相减

基线预测与变体预测跑在**同一批交易日、同一批标的**上。把两者按 `(交易日, 标的)`
配对相减，得到的是**每一天的差值**；再按日聚类求均值与标准误，
当天全市场共同的涨跌（A 股同涨同跌）就在相减时被消掉了。

两个独立均值相减则会把这份共同波动留在标准误里 —— 区间被撑大，
「看起来不显著」于是掩盖了真实差异（或反过来，噪声被当成 edge）。
**按日聚类的纪律必须作用在「比较」这一步，不能只作用在单边指标上。**

## 口径冻结

`METRIC_VERSION` 是这一整套口径的版本号，写进每份实验报告。
两份报告相比之前先 `assert_metric_version()`：不一致**报错**，不许照比 ——
「P7 的口径」和「P8 改过的口径」混着比，得到的差值没有任何含义，
而它看起来会像一个正常的数字。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

#: 本模块定义的口径版本。**改任何一条度量定义（配对方式、分母、标签带来源）
#: 都必须升这个号** —— 否则新旧报告会被静默地放在一起比。
METRIC_VERSION = "p8-metrics-v1"

#: 样本量门槛（交易日）。与 `verify.report.MIN_DAYS` 同源同值。
from stocklab.verify.report import MIN_DAYS  # noqa: E402

#: 配对比较的四个指标。`better` 说明「哪个方向算赢」——
#: 方向/覆盖率/超额越大越好，Brier 越小越好。写成显式表，
#: 是为了让「Brier 忘了取反」这种错**不可能**悄悄发生。
PAIRED_METRICS: tuple[tuple[str, str], ...] = (
    ("direction", "higher"),
    ("brier", "lower"),
    ("coverage", "higher"),
    ("excess", "higher"),
)


class MetricVersionMismatch(RuntimeError):
    """两份报告的口径版本不一致 —— 拒绝比较。

    照比会给出一份**看起来正常**的差值，而它把两套分母/两套标签带混在了一起。
    这是本项目最贵的一类错：数字合法、结论全错。
    """


class TestSetLeak(RuntimeError):
    """试图用 `test` 段做选择/判定 —— 封存段只能被「晋级评审」读取一次。"""

    __test__ = False        # 名字以 Test 开头，但它是异常不是测试用例


def assert_metric_version(left: Mapping, right: Mapping) -> str:
    """校验两份报告的 `metric_version` 一致，返回它。不一致 → 报错。"""
    a, b = left.get("metric_version"), right.get("metric_version")
    if a != b:
        raise MetricVersionMismatch(
            f"口径版本不一致：{a!r} vs {b!r} —— 拒绝比较。"
            "不同口径下的数字放在一起相减，得到的差值没有任何含义"
        )
    return str(a)


# ---------- 按日聚类的统计量 ----------

def daily_stats(values_by_day: Mapping[str, Sequence[float]]) -> dict[str, Any]:
    """先把同一交易日的值聚成日均值，再跨日统计（与 `verify.report._daily` 同式）。

    刻意自己写一遍而不是 import 私有函数：两处必须能被**独立核对**，
    `tests/test_experiments_metrics.py` 里有一条测试把两者在同一输入上的输出钉成相等。
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


# ---------- 配对日差 ----------

def _key(row: Mapping) -> tuple[str, str]:
    return (row["target_date"], row["code"])


def _side_value(row: Mapping, metric: str) -> float | None:
    """一行的某个指标值；本行不可评分或缺该指标 → `None`（**不进分母**）。"""
    if not row.get("scorable"):
        return None
    if metric == "direction":
        v = row.get("hit_direction")
        return None if v is None else float(v)
    if metric == "coverage":
        v = row.get("hit_range")
        return None if v is None else float(v)
    notes = row.get("notes") or {}
    v = notes.get({"brier": "brier", "excess": "excess_ret"}[metric])
    return None if v is None else float(v)


def pair_rows(baseline: Sequence[Mapping], variant: Sequence[Mapping]
              ) -> tuple[list[tuple[Mapping, Mapping]], dict[str, int]]:
    """按 `(target_date, code)` 配对两侧都可评分的行。

    返回 `(pairs, counts)`。`counts` 显式记下**被丢掉的**行：
    只在一边出现的、以及任一边不可评分的 —— 这些必须能被读出来，
    否则「样本量为什么比单边报告少」就成了没人能解释的谜。
    """
    b = {_key(r): r for r in baseline}
    v = {_key(r): r for r in variant}
    pairs: list[tuple[Mapping, Mapping]] = []
    counts = {"baseline_rows": len(baseline), "variant_rows": len(variant),
              "keys_baseline_only": 0, "keys_variant_only": 0,
              "unscorable_either": 0}
    for k in sorted(set(b) | set(v)):
        if k not in v:
            counts["keys_baseline_only"] += 1
            continue
        if k not in b:
            counts["keys_variant_only"] += 1
            continue
        if not (b[k].get("scorable") and v[k].get("scorable")):
            counts["unscorable_either"] += 1
            continue
        pairs.append((b[k], v[k]))
    counts["n_pairs"] = len(pairs)
    return pairs, counts


def paired_daily_delta(baseline: Sequence[Mapping], variant: Sequence[Mapping]
                       ) -> dict[str, Any]:
    """变体 − 基线，按 `(日, 标的)` 配对后按日聚类。

    返回每个指标的 `{n_days, mean, sd, se, ci95}` 与配对计数。
    **`None` 值一律不参与**：不可评分的行既不在分子也不在分母里。
    """
    pairs, counts = pair_rows(baseline, variant)
    out: dict[str, Any] = {"counts": counts}
    for metric, _better in PAIRED_METRICS:
        by_day: dict[str, list[float]] = {}
        for rb, rv in pairs:
            a, b = _side_value(rb, metric), _side_value(rv, metric)
            if a is None or b is None:
                continue
            by_day.setdefault(rb["target_date"], []).append(b - a)
        out[metric] = daily_stats(by_day)
    return out


# ---------- 晋级判定 ----------

#: 判定的三种结果（外加「样本不足」这一种结构性否决）。
STATUS_PROMOTED = "promoted"
STATUS_FALSIFIED = "falsified"
STATUS_INCONCLUSIVE = "inconclusive"


def gate(paired: Mapping, *, n_days: int | None = None,
         min_days: int = MIN_DAYS) -> dict:
    """把一个 split 的配对日差判成 `WIN` / `LOSE` / `FLAT` / `INSUFFICIENT`。

    判据（**提前定死，事后不许换**）：

    - `WIN`：方向差值 95% CI **下界 > 0** 且 Brier 差值 95% CI **上界 < 0**
      （两个指标都显著变好，且方向一致 —— 「Brier 同向」就是这条）；
    - `LOSE`：任一指标的**均值符号是错的**（方向变差 或 Brier 变差）——
      前提被证伪，不需要再看显著性；
    - `FLAT`：符号对但没到显著 —— **不许**当成赢（「差一点」不是结论）；
    - `INSUFFICIENT`：有效交易日 < `min_days`，一律不算数（总纲：<120 交易日仅供观察）。
    """
    d, br = paired["direction"], paired["brier"]
    days = n_days if n_days is not None else (d.get("n_days") or 0)
    reasons: list[str] = []
    if days < min_days:
        return {"status": "INSUFFICIENT", "n_days": days, "min_days": min_days,
                "beat_direction": False, "beat_brier": False,
                "reasons": [f"有效交易日 {days} < 门槛 {min_days} —— 样本不足，仅供观察"]}
    if d["mean"] is None or br["mean"] is None:
        return {"status": "INSUFFICIENT", "n_days": days, "min_days": min_days,
                "beat_direction": False, "beat_brier": False,
                "reasons": ["配对样本为空 —— 无法判定"]}

    beat_dir = bool(d["mean"] > 0 and d["ci95"][0] > 0)
    beat_brier = bool(br["mean"] < 0 and br["ci95"][1] < 0)
    reasons.append(
        f"方向 Δ={d['mean']:+.4f} ± {d['se']:.4f}，95% CI "
        f"[{d['ci95'][0]:+.4f}, {d['ci95'][1]:+.4f}] → "
        + ("显著变好" if beat_dir else "未达显著变好"))
    reasons.append(
        f"Brier Δ={br['mean']:+.4f} ± {br['se']:.4f}，95% CI "
        f"[{br['ci95'][0]:+.4f}, {br['ci95'][1]:+.4f}]（越小越好）→ "
        + ("显著变好" if beat_brier else "未达显著变好"))

    if d["mean"] < 0 or br["mean"] > 0:
        status = "LOSE"
        reasons.append("至少一个指标的差值**符号是错的** —— 前提被证伪")
    elif beat_dir and beat_brier:
        status = "WIN"
        reasons.append("两个指标都显著变好且方向一致 → 该 split 上赢了基线")
    else:
        status = "FLAT"
        reasons.append("符号对但未达显著 —— 「差一点」不是结论")

    return {"status": status, "n_days": days, "min_days": min_days,
            "beat_direction": beat_dir, "beat_brier": beat_brier,
            "reasons": reasons}


def decide(*, validate_gate: Mapping, test_gate: Mapping | None,
           selection_split: str, test_evaluated: bool) -> dict:
    """把两个 split 的 gate 合成一个结论：`promoted` / `falsified` / `inconclusive`。

    **判定顺序就是纪律本身**，不能重排：

    1. `selection_split == "test"` → 直接抛 `TestSetLeak`（封存段不能当选择依据）；
    2. `selection_split == "train"` → `inconclusive`（train 是调试区，不是样本外证据）；
    3. validate 不到 `WIN` → **test 根本不会被打开**（`test_evaluated=False`），
       结论只能是 `falsified` / `inconclusive` —— 于是「test 赢不了」这件事
       没有机会救活一个 validate 输掉的变体；
    4. validate `WIN` → 晋级评审，**此时必须**已打开 test 一次；
       test 也 `WIN` → `promoted`，否则 `falsified`（validate 的胜利没能复现 = 不许嘴硬）。

    最后一条覆盖一切：样本不足 → `inconclusive`（即使符号很好看）。
    """
    if selection_split == "test":
        raise TestSetLeak(
            "selection_split='test' —— `test` 是封存段，只能被晋级评审读取一次，"
            "**不许**用来挑选变体。用 test 挑出来的变体，其 test 成绩不再是样本外"
        )
    if selection_split not in ("train", "validate"):
        raise ValueError(f"未知 selection_split={selection_split!r}")

    base = {"selection_split": selection_split, "test_evaluated": bool(test_evaluated),
            "validate_gate": dict(validate_gate),
            "test_gate": dict(test_gate) if test_gate else None}

    if validate_gate["status"] == "INSUFFICIENT":
        return {**base, "status": STATUS_INCONCLUSIVE,
                "reasons": ["validate 段样本不足 —— 结论不成立，仅供观察"]}
    if selection_split == "train":
        return {**base, "status": STATUS_INCONCLUSIVE,
                "reasons": ["selection_split='train'：train 是调试区，"
                            "在它上面赢不构成样本外证据，不得据此晋级"]}
    if validate_gate["status"] == "LOSE":
        return {**base, "status": STATUS_FALSIFIED,
                "reasons": ["validate 段被证伪（前提不成立）→ 不打开 test"]}
    if validate_gate["status"] == "FLAT":
        return {**base, "status": STATUS_INCONCLUSIVE,
                "reasons": ["validate 段未达显著 → 不打开 test；"
                            "「差一点」不是结论，也不许挪口径"]}

    # ---- validate WIN：晋级评审，test 必须已打开一次 ----
    if test_gate is None:
        raise RuntimeError(
            "validate 赢了却没打开 test —— 晋级评审必须一次性读取封存段，"
            "否则「promoted」这个结论没有被任何样本外证据支持"
        )
    if test_gate["status"] == "WIN":
        return {**base, "status": STATUS_PROMOTED,
                "reasons": ["validate 与 test **两段都**显著赢基线，且方向与 Brier 同向"]}
    return {**base, "status": STATUS_FALSIFIED,
            "reasons": [f"validate 赢了但 test 是 {test_gate['status']} —— "
                        "样本外的胜利没能复现，不许嘴硬"]}
