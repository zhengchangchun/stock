"""P83 因子级 IC 分解：把短池打分的**输入因子**逐个算 rank IC。

## 它回答什么（`rank-ic` 答不了的那一半）

`research rank-ic`（P78）证明的是「**合成后**的分数对前向收益没有可测的横截面排序
能力」（`IC_NOT_SIGNIFICANT`）。但那个分数只是两个输入因子的线性组合 + 裁剪：

    score = clip(50 + 150 × mom20 + 30 × (vr15 − 1), 0, 100)

所以「分数没 IC」有三种互斥的解释，**只有把分数拆开才能区分**：

1. 两个因子都没 IC ⇒ **这套输入没有 edge**，方向应转向换信号源（新数据），
   而不是继续调权重；
2. 某因子有 IC 而合成分数没有 ⇒ **组合/裁剪把信息毁了**，下一步是改合成方式；
3. 两个因子都有 IC ⇒ 与合成分数矛盾 ⇒ 先查实现。

本模块做的是**度量**，不是因子挖掘：只测**现役插桩真正用的那两个输入**
（`mom20` / `vr15`）＋ 管线里**已有**的 5 个财务因子（次读数、只报不判）。
**一个新增因子都没有** —— 加均线/换手/资金流会把「度量」变成「因子挖掘」，
而且是多变量（任务书 §0.5 D1）。

## 只增读数，不改任何口径

- 因子值**取自打分用的同一个 `ctx`**：`candidate/run.py::_load_bars_adjusted` ＋
  `_cross_section_map` ＋ `score.build_ctx` 三条原样复用（D2）—— 本模块**不另建
  价格口径、不另算复权**，只是把 `score_pipeline(want_factors=True)` 已经抠出来的
  输入换个形状读（`PipelineResult.factor_inputs`，只增字段）；
- IC / 分层 / bootstrap **直接复用 `research/signal.py` 的既有实现**
  （`spearman_ic` / `assign_layers` / `layer_means` / `ascending_steps` /
  `ForwardPrices` / `ic_stats`）—— 抽公共 helper 时**只改名、无逻辑改动**，
  `rank-ic` 的输出逐位不变（见 `signal.ForwardPrices` 的说明与 ADR-034）；
- 判定门槛（`MIN_VALID_PERIODS` / `MIN_XSEC_N` / bootstrap 次数与种子 / CI 函数）
  **全部 import 自 `plugin/sandbox.py` 与 `research/signal.py`**，本模块一个数字不抄。

## 裁剪诊断只报不判（D5）

逐日统计「被打分插桩裁到 0 或 100 的标的本数 / 当日截面数」，出 P50 与极值 ——
**这是读数，不进任何 verdict**。

## 只读

连库由调用方走 `stocklab/cli/research.py::open_read_only`（`file:…?mode=ro`）；
本模块**不写任何表、不写任何文件**（落盘交调用方）。产物只落 `reports/research/`。

## fail-closed 预注册

`--prereg` 指向一份**跑之前就提交**的 md（内含 ```json``` 块），命令行实参必须与它
**逐字段**一致，否则 exit 2、**零输出**。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from stocklab.candidate import replay
from stocklab.candidate.run import FACTOR_FEATURE_KEYS
from stocklab.config import paths
from stocklab.config.universes import UniverseError, resolve_universe
from stocklab.plugin import sandbox
from stocklab.research import signal, xsec

#: 实验短名。与预注册 json 的 `experiment` 字段逐字相同。
EXPERIMENT = "factor-ic"

#: 只跑短期池：`mid` 需 32.6 年、`long` 需 97.9 年才够 120 个验证周期（纯算术）。
#: 与 `signal.ONLY_POOL` 同值 —— 从那里 import，不手抄。
ONLY_POOL = signal.ONLY_POOL

#: 窗口起点下界（**窗口即结论**）。从 `signal` import。
MIN_START = signal.MIN_START

#: 相关类型（预注册字段，必须等于它）。从 `signal` import。
IC_TYPE = signal.IC_TYPE

#: 分层数（预注册字段，改要新预注册）。从 `signal` import。
N_LAYERS = signal.N_LAYERS

#: 某日截面的最小有效标的数。从 `signal` import —— **不手抄 30**。
MIN_XSEC_N = signal.MIN_XSEC_N

#: 主读数因子（D1）：**现役短池打分插桩真正用的那两个**。
MAIN_FACTORS: tuple[str, ...] = ("mom20", "vr15")

#: 次读数因子（D1）：管线里**已有**、现役插桩一个都没用的 5 个财务因子。
#: 名单从 `candidate/run.py::FACTOR_FEATURE_KEYS` import（载荷形状的单一真源），
#: 不在本模块重抄一遍。
SECONDARY_FACTORS: tuple[str, ...] = FACTOR_FEATURE_KEYS

#: 预注册 json 的**必填字段**，与 `docs/experiments/2026-09-26-factor-ic-short-csi300-500.md`
#: §0 那段 json 的键**逐字一致**（比 `rank-ic` 多 `factors` / `secondary_factors` /
#: `min_xsec_n` 三个键）。少一个即 exit 2 —— 缺字段的预注册不构成预注册。
#: `universe` 在**本站是必填**（与 `rank-ic` 的「缺席 ⇒ seed21」不同）：P83 只跑
#: `csi300-500`，宇宙写进预注册才钉得住。
PREREG_FIELDS: tuple[str, ...] = (
    "experiment", "pool", "start", "universe", "factors", "secondary_factors",
    "ic_type", "n_layers", "horizon", "min_periods", "min_xsec_n",
    "bootstrap_n", "bootstrap_seed", "rule")

#: 插桩裁剪边界。**不是判据常量** —— 它是现役插桩源文本里的事实
#: （`clip(…, 0, 100)`，见 `_reconstruct_score` 的出处注释），抄来这里只是为了
#: 判定「这一只被裁到边界了吗」。
CLIP_LO = 0.0
CLIP_HI = 100.0

#: 现役短池打分插桩的身份（**只用于注释与报告溯源自证，不参与任何计算**）。
SCORING_SCRIPT = {
    "plugin_id": "1",
    "script_id": 7,
    "version": "1.1.0",
    "source_sha256":
        "558a6a08b62e508c9355c607d29217269c5897cfc374cefdb15cd2fc42c2fe0e",
    "formula": "score = clip(50 + 150 × mom20 + 30 × (vr15 − 1), 0, 100)",
}

#: P78 合成分数读数的**并排引用**（T4：**只引用不重跑**）。
#: 数字抄自 `source_report` 那份 json 的 `ic.adj_score`，本站**没有**再跑一遍
#: `rank-ic`（跑它 ≈100 min，且那是另一个实验的产物）。
RANK_IC_REFERENCE: dict = {
    "experiment": "rank-ic",
    "source_report": "reports/research/2026-09-24-rank-ic-csi300-500.json",
    "universe_id": "csi300-500",
    "start": "2015-01-01",
    "end": "2026-09-24",
    "reading": "adj_score",
    "n_validate": 171,
    "mean_validate": -0.01584306308710765,
    "ci_low": -0.045778761310970736,
    "ci_high": 0.014322973954005074,
    "verdict": "IC_NOT_SIGNIFICANT",
    "note": ("**只引用不重跑**：上列数字抄自 `source_report`（P78 全窗跑），"
             "本报告未重跑 `rank-ic`；若那份产物被重新生成，本引用可能过期。"),
}

#: 裁剪诊断的说明（只报不判）。
CLIP_DIAG_NOTE = (
    "裁剪诊断（D5，**只报不判，不进任何 verdict**）：逐调仓日统计「被打分插桩裁到 "
    "0 或 100 的标的本数 / 当日截面数」。它量化的是裁剪对 rank 分辨率的破坏 —— "
    "被裁到同一边界的标的在秩相关里**并列**，横截面的可用名次因此变少。"
)

#: 次读数的说明（预注册 §4.2）。
SECONDARY_NOTE = (
    "次读数（`exploratory=true`）：管线里已有、现役短池插桩**一个都没用**的 5 个"
    "财务因子。**只报读数、不作判据**（预注册 §4.2）—— 因此它们**没有 verdict**"
    "（结论词汇只给主读数）。财务因子在短池上很可能取不到值，取不到就标 "
    "`LOW_COVERAGE`、如实报，不拿 0 顶替。"
)

#: verdict 词汇的说明（与 `rank-ic` 同一套词表）。
VERDICT_VOCAB_NOTE = (
    "verdict 词汇是**度量专用**的：`IC_SIGNIFICANT` / `IC_NOT_SIGNIFICANT` 只说"
    "「IC 的 95% CI 含不含 0」，**IC 为负也叫 significant**；`INCONCLUSIVE` = "
    "验证段有效日期不足；`LOW_COVERAGE` = 该因子在验证段里基本取不到值 ⇒ **不出 "
    "verdict**（覆盖度不足不是「没效果」）。"
)

#: 窗口即结论（同 `signal`）。
WINDOW_IS_CONCLUSION_NOTE = signal.WINDOW_IS_CONCLUSION_NOTE


def _selection_bias_note(universe_id: str, n: int) -> str:
    """选择偏差限定句（与 `rank-ic` 同一套措辞，符号是 IC 不是 Δ）。"""
    if universe_id == signal.DEFAULT_PREREG_UNIVERSE:
        return signal.SELECTION_BIAS_SENTENCE
    return (f"选择偏差：IC 显著也只能说「**在 `{universe_id}` 这 {n} 只、"
            "这段历史上成立**」，不能说「打分函数有效」。")


class PreregError(signal.PreregError):
    """预注册缺失 / 不合规 / 与命令行实参不一致。

    继承 `signal.PreregError`（⇒ 也是 `xsec.PreregError`），CLI 才不用为本站
    再加 except 分支。
    """


# ---------------------------------------------------------------------------
# 纯函数层（无 DB、只用 stdlib；可单独测）
# ---------------------------------------------------------------------------

def mom20(payload: Mapping) -> float | None:
    """`mom20 = (C[-1] − C[-20]) / C[-20]`（D4：**复述一次**现役插桩的公式）。

    出处：`plugin_scripts.script_id=7` / `plugin_id='1'` / v1.1.0 /
    `source_sha256=558a6a08b62e508c…`（真库只读读出，见 `SCORING_SCRIPT`）。
    与插桩**逐条对齐**的细节：不足 20 根 ⇒ 取不到（插桩那边直接 `pass_flag=False`，
    这种标的根本不会进 `factor_inputs`）；`C[-20]` 为 0 ⇒ 0.0（插桩同）。

    一致性由 `tests/test_research_factor.py` 钉死：同一 ctx 上
    `reconstruct_score(...)` 必须**逐位等于**插桩实际返回的 `score`。
    """
    closes = payload["closes"]
    if len(closes) < 20:
        return None
    return (closes[-1] - closes[-20]) / closes[-20] if closes[-20] else 0.0


def vr15(payload: Mapping) -> float | None:
    """`vr15 = mean(V[-5:]) / mean(V[-20:-5])`（D4，同 `mom20` 的出处与对齐规则）。

    分母为 0 ⇒ 1.0（插桩同）。
    """
    volumes = payload["volumes"]
    if len(volumes) < 20:
        return None
    v5 = sum(volumes[-5:]) / 5.0
    v15 = sum(volumes[-20:-5]) / 15.0
    return (v5 / v15) if v15 else 1.0


def reconstruct_score(payload: Mapping) -> float | None:
    """`clip(50 + 150×mom20 + 30×(vr15−1), 0, 100)` —— **插桩公式的复述**（D4）。

    它不是第二套打分口径：作用是用例里拿它与插桩**实际返回的** `score` 对拍；
    对不上就说明本站算的因子不是插桩的因子，**先修再谈结论**（预注册 §2）。
    """
    mom, vol_ratio = mom20(payload), vr15(payload)
    if mom is None or vol_ratio is None:
        return None
    raw = 50.0 + mom * 150.0 + (vol_ratio - 1.0) * 30.0
    return max(CLIP_LO, min(CLIP_HI, raw))


def secondary_value(payload: Mapping, key: str) -> float | None:
    """次读数取值：`payload["feats"][key]`（`None` = 算不出，**不补 0**）。"""
    v = payload["feats"].get(key)
    return None if v is None else float(v)


def factor_values(payloads: Mapping[str, Mapping],
                  value_of: Callable[[Mapping], float | None]
                  ) -> dict[str, float]:
    """`{code: 因子值}`，只留**非空**的那些（`None` 不进截面，也不拿 0 顶替）。"""
    out: dict[str, float] = {}
    for code, payload in payloads.items():
        v = value_of(payload)
        if v is not None:
            out[code] = v
    return out


def clip_diag(scored_by_mark: Mapping[str, list[dict]]) -> dict:
    """逐调仓日的裁剪比例 ＋ P50/极值（D5，**只报不判**）。

    「被裁」= 插桩返回的 `raw_score` 落在 0 或 100 这两个边界上（`clip(…, 0, 100)`
    的输出）；分母 = 当日短池**已打分**的标的数。两个数都是 `None` 时该日不进统计。
    """
    ratios: list[float] = []
    n_clipped: list[int] = []
    n_xsec: list[int] = []
    for day in sorted(scored_by_mark):
        rows = [r for r in scored_by_mark[day] if r.get("raw_score") is not None]
        if not rows:
            continue
        clipped = sum(1 for r in rows
                      if r["raw_score"] <= CLIP_LO or r["raw_score"] >= CLIP_HI)
        ratios.append(clipped / len(rows))
        n_clipped.append(clipped)
        n_xsec.append(len(rows))
    if not ratios:
        return {"n_dates": 0, "rule": CLIP_DIAG_NOTE, "ratio_p50": None,
                "ratio_min": None, "ratio_max": None, "n_clipped_p50": None,
                "n_clipped_max": None, "xsec_p50": None}
    return {
        "n_dates": len(ratios),
        "rule": CLIP_DIAG_NOTE,
        "ratio_p50": float(statistics.median(ratios)),
        "ratio_min": min(ratios),
        "ratio_max": max(ratios),
        "n_clipped_p50": float(statistics.median(n_clipped)),
        "n_clipped_max": max(n_clipped),
        "xsec_p50": float(statistics.median(n_xsec)),
    }


# ---------------------------------------------------------------------------
# 预注册（fail-closed）
# ---------------------------------------------------------------------------

def load_prereg(path: Path) -> tuple[dict, str]:
    """读出预注册 json 与整份文件的 sha256。

    文件 IO / ```json``` 块解析 / sha **复用 `signal.load_prereg`**（它已经做了
    11 个共有键的「缺即拒」检查）；本站再补上自己多出来的三个键的检查。
    """
    data, sha = signal.load_prereg(path)
    missing = [f for f in PREREG_FIELDS if f not in data]
    if missing:
        raise PreregError(f"预注册 json 缺字段：{missing}")
    return data, sha


def validate_prereg(data: Mapping, *, pool: str, start: str, horizon: int,
                    universe: str | None = None) -> None:
    """命令行实参 ＋ 代码常量 vs 预注册：**任一不一致即拒跑**（fail-closed）。

    与 `signal.validate_prereg` 同一套逐字段比对，外加本站独有的三个字段：
    `factors` / `secondary_factors` / `min_xsec_n`。**每一处比较的期望值都
    来自 import 的常量**（`sandbox` / `signal`），没有一个数字是手抄的。
    """
    def _mismatch(field: str, got, want) -> PreregError:
        return PreregError(
            f"预注册不一致：{field} 预注册={want!r} 命令行/代码={got!r}"
            f" —— exit 2，零输出、不跑度量")

    if data["experiment"] != EXPERIMENT:
        raise _mismatch("experiment", EXPERIMENT, data["experiment"])
    if list(data["factors"]) != list(MAIN_FACTORS):
        raise _mismatch("factors", list(MAIN_FACTORS), list(data["factors"]))
    if list(data["secondary_factors"]) != list(SECONDARY_FACTORS):
        raise _mismatch("secondary_factors", list(SECONDARY_FACTORS),
                        list(data["secondary_factors"]))
    if data["min_xsec_n"] != MIN_XSEC_N:
        raise _mismatch("min_xsec_n", MIN_XSEC_N, data["min_xsec_n"])

    # 共有字段：把 `experiment` 换成本站的短名后走同一套比对。
    common = dict(data)
    common["experiment"] = signal.EXPERIMENT
    signal.validate_prereg(common, pool=pool, start=start, horizon=horizon,
                           universe=universe)


# ---------------------------------------------------------------------------
# 单因子读数
# ---------------------------------------------------------------------------

def _keep(values: Sequence[float | None]) -> list[float]:
    return [v for v in values if v is not None]


def one_factor(name: str, *, kind: str, value_of: Callable[[Mapping], float | None],
               payloads_by_mark: Mapping[str, dict],
               periods: Sequence[tuple[str, str]],
               fwd_by_period: Sequence[Mapping[str, float]],
               val_idx: Sequence[int]) -> dict:
    """一个因子的全套读数（IC / 分层 / 判定 / 覆盖度）。

    门槛、切分、CI、verdict 全部走 `signal` 的既有实现与 `sandbox` 的常量；
    本函数只负责「喂哪个因子值」与「LOW_COVERAGE 的覆盖度口径」。
    """
    ic_series: list[float | None] = []
    pearson_series: list[float | None] = []
    cov_counts: list[int] = []
    sizes: list[int] = []
    n_skipped = 0
    per_day_layer_means: dict[int, list[float]] = {k: [] for k in range(1, N_LAYERS + 1)}
    layer_sizes: dict[int, list[int]] = {k: [] for k in range(1, N_LAYERS + 1)}
    spread_series: list[float] = []
    steps_per_day: list[int] = []

    for k, (d0, _d1) in enumerate(periods):
        values = factor_values(payloads_by_mark.get(d0, {}), value_of)
        cov_counts.append(len(values))          # 非空覆盖度（LOW_COVERAGE 判据）
        fwd = fwd_by_period[k]
        usable = sorted(set(values) & set(fwd))
        sizes.append(len(usable))
        if len(usable) < MIN_XSEC_N:
            # 截面太薄 ⇒ 该日**不出任何读数**（IC 与分层都记不上）并计数 ——
            # **不许**用 0 顶替（预注册 §2）。门槛对 IC / 分层 / spread 是同一个。
            n_skipped += 1
            continue
        ic_series.append(signal.spearman_ic(values, fwd))
        pearson_series.append(signal.pearson_ic(values, fwd))
        layers = signal.assign_layers(usable, values, N_LAYERS)
        means = signal.layer_means(layers, fwd)
        for kk in range(1, N_LAYERS + 1):
            layer_sizes[kk].append(len(layers[kk]))
            if means[kk] is not None:
                per_day_layer_means[kk].append(means[kk])
        steps_per_day.append(
            signal.ascending_steps([means[kk] for kk in range(1, N_LAYERS + 1)]))
        if means[1] is not None and means[N_LAYERS] is not None:
            spread_series.append(means[1] - means[N_LAYERS])

    ic_vals = _keep(ic_series)
    n_undefined = len(ic_series) - len(ic_vals)     # 有截面但相关系数算不出（方差为 0 等）

    # LOW_COVERAGE：验证段里「非空覆盖度 < MIN_XSEC_N」的日子占比 > 50%（预注册 §2）。
    # 验证段 = 按**周期序号**的后 30%（`replay.split_train_validate` 同一函数）；
    # 这里的分母是**全部周期**（缺读数的日子也算一格）—— 覆盖度说的正是「有多少天
    # 这个因子取不到值」，把那些天从分母里剔掉会把这个信号本身抹掉。
    below = sum(1 for i in val_idx if cov_counts[i] < MIN_XSEC_N)
    low_coverage = bool(val_idx) and (below / len(val_idx)) > 0.5

    stats = signal.ic_stats(ic_vals)
    layer_mean = {k: (sum(v) / len(v)) if v else None
                  for k, v in sorted(per_day_layer_means.items())}
    ordered = [layer_mean[k] for k in range(1, N_LAYERS + 1)]
    if spread_series:
        lo, hi = sandbox._bootstrap_ci(list(spread_series))
        spread = {"n_days": len(spread_series), "mean": sum(spread_series) / len(spread_series),
                  "ci_low": lo, "ci_high": hi}
    else:
        spread = {"n_days": 0, "mean": None, "ci_low": None, "ci_high": None}

    out = {
        "factor": name,
        "kind": kind,
        "exploratory": kind == "secondary",
        "low_coverage": low_coverage,
        "coverage_flag": "LOW_COVERAGE" if low_coverage else None,
        "n_dates_skipped": n_skipped,
        "n_ic_undefined": n_undefined,
        "coverage_validate_below_min": below,
        "coverage_validate_n": len(val_idx),
        "value_coverage_p50": (float(statistics.median(cov_counts))
                               if cov_counts else None),
        "xsec_size_p50": (float(statistics.median(sizes)) if sizes else None),
        "xsec_size_min": min(sizes) if sizes else None,
        "xsec_size_max": max(sizes) if sizes else None,
        "layers": {
            "n_layers": N_LAYERS,
            "mean_by_layer": {str(k): layer_mean[k] for k in range(1, N_LAYERS + 1)},
            "size_p50_by_layer": {
                str(k): (float(statistics.median(layer_sizes[k]))
                         if layer_sizes[k] else None)
                for k in range(1, N_LAYERS + 1)},
            "n_ascending_steps": signal.ascending_steps(ordered),
            "n_ascending_steps_per_day_mean": (
                sum(steps_per_day) / len(steps_per_day)) if steps_per_day else None,
            "spread": spread,
        },
        "pearson": {
            "n_dates": len(_keep(pearson_series)),
            "mean_validate": signal.tail_mean(pearson_series),
        },
        **stats,
    }
    if low_coverage:
        # 覆盖度不足 ⇒ **不出 verdict**（「测不出」不是「没效果」）。实测点估计
        # 仍留在 `mean_validate` 里，不抹掉。
        out["verdict"] = "LOW_COVERAGE"
        out["note"] = (
            f"LOW_COVERAGE：验证段里非空覆盖度 < MIN_XSEC_N({MIN_XSEC_N}) 的日子占 "
            f"{below}/{len(val_idx)} > 50% ⇒ 该因子在短池上基本取不到值，"
            "**不出 verdict**（覆盖度不足不是「没效果」）。")
    elif kind == "secondary":
        # 次读数只报读数、不作判据 ⇒ 不给 verdict（词汇表里没有「不判」这一档，
        # 所以留 `None`，把 CI 那句话原样收进 `ci_note`）。
        out["ci_note"] = stats["note"]
        out["verdict"] = None
        out["note"] = SECONDARY_NOTE
    return out


# ---------------------------------------------------------------------------
# 实验主体
# ---------------------------------------------------------------------------

def run_factor_ic(conn: sqlite3.Connection, *, pool: str, start: str, end: str,
                  prereg_path: Path, universe: str | None = None) -> dict:
    """跑因子级 IC 分解，返回报告 dict（**不写任何文件**，落盘交给调用方）。

    失败一律 `PreregError`（调用方 exit 2、零输出）。
    """
    if pool != ONLY_POOL:
        raise PreregError(
            f"本实验只跑 {ONLY_POOL!r}（mid 需 32.6 年、long 需 97.9 年才够 "
            f"{sandbox.MIN_VALID_PERIODS} 个验证周期）；收到 {pool!r}")
    if start < MIN_START:
        raise PreregError(
            f"窗口起点不得早于 {MIN_START}（收到 {start!r}）—— 窗口即结论")
    horizon = replay.REBALANCE_DAYS[pool]          # 读，不手抄

    try:
        universe_id, members, members_sha256 = resolve_universe(universe)
    except UniverseError as exc:
        raise PreregError(f"宇宙载入失败（{universe!r}）：{exc}") from exc

    prereg, prereg_sha = load_prereg(prereg_path)
    validate_prereg(prereg, pool=pool, start=start, horizon=horizon,
                    universe=universe_id)

    marks = replay.rebalance_marks(conn, pool=pool, start=start, end=end)
    if len(marks) < 2:
        raise PreregError(
            f"窗口内调仓边界只有 {len(marks)} 个（需 ≥ 2 才能构成周期）："
            f"{start}~{end}")

    from stocklab.candidate.run import score_pipeline

    fwd_asof = marks[-1]
    t0 = time.time()
    scan_s = 0.0
    #: 每个调仓日跑一次（D2）；`want_factors=True` 让打分内核把**它自己看到的**
    #: 输入因子一并带出来（只增字段，默认路径逐位不变）。
    scored_by_mark: dict[str, list[dict]] = {}
    payloads_by_mark: dict[str, dict] = {}
    for day in marks:
        ts = time.time()
        res = score_pipeline(conn, asof=day, universe=members, want_factors=True)
        scan_s += time.time() - ts
        scored_by_mark[day] = list(res.scored.get(pool, []))
        payloads_by_mark[day] = dict(res.factor_inputs)

    tf = time.time()
    provider = signal.ForwardPrices(conn, as_of=fwd_asof, dates=set(marks))
    fwd_s = time.time() - tf

    periods = list(zip(marks, marks[1:]))
    _train_idx, val_idx = replay.split_train_validate(list(range(len(periods))))

    #: 前向收益按 (周期, code) 算**一次**、7 个因子共用（口径与 `rank-ic` 同源）。
    fwd_by_period: list[dict[str, float]] = []
    n_no_fwd_ret = 0
    for d0, d1 in periods:
        fwd: dict[str, float] = {}
        for code in sorted({r["code"] for r in scored_by_mark[d0]}):
            r = provider.fwd_return(code, d0, d1)
            if r is None:
                n_no_fwd_ret += 1
            else:
                fwd[code] = r
        fwd_by_period.append(fwd)

    accessors: dict[str, Callable[[Mapping], float | None]] = {
        "mom20": mom20, "vr15": vr15}
    for key in SECONDARY_FACTORS:
        accessors[key] = (lambda k: (lambda p: secondary_value(p, k)))(key)

    factors = {
        name: one_factor(name, kind="main", value_of=accessors[name],
                         payloads_by_mark=payloads_by_mark, periods=periods,
                         fwd_by_period=fwd_by_period, val_idx=val_idx)
        for name in MAIN_FACTORS}
    secondary = {
        name: one_factor(name, kind="secondary", value_of=accessors[name],
                         payloads_by_mark=payloads_by_mark, periods=periods,
                         fwd_by_period=fwd_by_period, val_idx=val_idx)
        for name in SECONDARY_FACTORS}

    fwd_s += provider.load_s
    elapsed = time.time() - t0

    xsec_sizes = [len(scored_by_mark[d0]) for d0, _d1 in periods]
    return {
        "experiment": EXPERIMENT,
        "pool": pool,
        "start": start,
        "end": end,
        "universe": universe_id,
        "universe_id": universe_id,
        "universe_n": len(members),
        "universe_members_sha256": members_sha256,
        "prereg_universe": prereg["universe"],
        "ic_type": IC_TYPE,
        "n_layers": N_LAYERS,
        "horizon": horizon,
        "min_periods": sandbox.MIN_VALID_PERIODS,
        "min_xsec_n": MIN_XSEC_N,
        "bootstrap_n": sandbox._BOOTSTRAP_N,
        "bootstrap_seed": sandbox._BOOTSTRAP_SEED,
        "main_factors": list(MAIN_FACTORS),
        "secondary_factors": list(SECONDARY_FACTORS),
        "scoring_script": dict(SCORING_SCRIPT),
        "fwd_asof": fwd_asof,
        "rule": prereg["rule"],
        "prereg_path": str(prereg_path),
        "prereg_sha256": prereg_sha,
        "n_marks": len(marks),
        "n_periods": len(periods),
        "elapsed_s": elapsed,
        "scan_s": scan_s,
        "fwd_load_s": fwd_s,
        "factors": factors,
        "secondary": secondary,
        "clip_diag": clip_diag(scored_by_mark),
        "rank_ic_reference": dict(RANK_IC_REFERENCE),
        "n_fwd_fallback": provider.n_fallback,
        "n_dates_skipped": {name: factors[name]["n_dates_skipped"]
                            for name in MAIN_FACTORS},
        "coverage": {
            "n_periods": len(periods),
            "n_dates_with_ic": {name: factors[name]["n_dates"] for name in MAIN_FACTORS},
            "xsec_size_p50": (float(statistics.median(xsec_sizes))
                              if xsec_sizes else None),
            "xsec_size_min": min(xsec_sizes) if xsec_sizes else None,
            "xsec_size_max": max(xsec_sizes) if xsec_sizes else None,
            "n_no_fwd_ret": n_no_fwd_ret,
        },
        "non_pit_items": list(xsec.NON_PIT_ITEMS),
        "non_pit_universe_items": list(xsec.NON_PIT_UNIVERSE_ITEMS),
        "universe_note": (
            f"宇宙：`{universe_id}`（{len(members)} 只，`members_sha256` "
            f"`{members_sha256[:12]}…`）。**本 IC 的横截面来自 `{universe_id}`，与 "
            f"`seed21` 版（21 只）的 IC 不可直接比**。"),
        "selection_bias_note": _selection_bias_note(universe_id, len(members)),
        "window_note": WINDOW_IS_CONCLUSION_NOTE,
        "verdict_vocab_note": VERDICT_VOCAB_NOTE,
        "clip_diag_note": CLIP_DIAG_NOTE,
        "secondary_note": SECONDARY_NOTE,
        "fallback_note": signal.FALLBACK_NOTE,
        "replay_raw_price_note": signal.REPLAY_RAW_PRICE_NOTE,
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _num(x: float | None, fmt: str = "{:+.4f}") -> str:
    return "—" if x is None else fmt.format(x)


def summary_line(report: Mapping) -> str:
    """md 结尾那一行 `summary:`（nanobot 直接贴给用户）。"""
    parts = []
    for name in report["main_factors"]:
        f = report["factors"][name]
        parts.append(f"{name}[n={f['n_dates']}/{f['n_validate']} "
                     f"IC={_num(f['mean_validate'])} "
                     f"CI({_num(f['ci_low'])},{_num(f['ci_high'])}) "
                     f"{f['verdict']}]")
    cd = report["clip_diag"]
    second = []
    for name in report["secondary_factors"]:
        s = report["secondary"][name]
        flag = s["coverage_flag"] or "-"
        second.append(f"{name}[cov={s['value_coverage_p50']} {flag} "
                      f"IC={_num(s['mean_validate'])}]")
    return (f"summary: factor-ic pool={report['pool']} "
            f"{report['start']}~{report['end']} universe={report['universe_id']} "
            + " ".join(parts)
            + f" | 裁剪日比P50={_num(cd['ratio_p50'], '{:.4f}')}"
            + " | 次读数 " + " ".join(second))


def render_md(report: Mapping) -> str:
    """人读报告。披露项（非 PIT、回退、口径不可比）**并列写出**。"""
    cov = report["coverage"]
    cd = report["clip_diag"]
    ref = report["rank_ic_reference"]
    lines: list[str] = [
        f"# factor-ic 报告（{report['pool']} 池，{report['start']} ~ "
        f"{report['end']}，宇宙 `{report['universe_id']}`）",
        "",
        "> 本报告由 `research factor-ic` 只读生成：**未写任何表**，产物只在 "
        "`reports/`。因子值取自**打分用的同一个 ctx**（`score_pipeline` 现算、只读），"
        "本站**不出任何信号、不改任何阈值/公式/参数** —— 它是度量基建。",
        "",
        "## 0. 预注册",
        "",
        f"- 预注册文件：`{report['prereg_path']}`",
        f"- `prereg_sha256` = `{report['prereg_sha256']}`",
        f"- 判据原文：{report['rule']}",
        f"- {report['window_note']}",
        f"- {report['verdict_vocab_note']}",
        "",
        "## 1. 主读数（逐因子 rank IC，`exploratory=false`）",
        "",
        f"被分解的打分函数（`{report['scoring_script']['plugin_id']}` / "
        f"v{report['scoring_script']['version']} / `script_id="
        f"{report['scoring_script']['script_id']}` / `source_sha256="
        f"{report['scoring_script']['source_sha256'][:12]}…`）：",
        "",
        f"    {report['scoring_script']['formula']}",
        "",
        "| 因子 | 有效日期 n | 全窗均值 | 训练段均值 | 验证段均值 | 验证段 std | "
        "验证段 IR | 验证段 t | 过拟合标记 | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in report["main_factors"]:
        f = report["factors"][name]
        v = f["validate"]
        lines.append(
            f"| `{name}` | {f['n_dates']} | {_num(f['mean_all'], '{:+.5f}')} | "
            f"{_num(f['train']['mean'], '{:+.5f}')} | "
            f"{_num(v['mean'], '{:+.5f}')} | {_num(v['std'], '{:.5f}')} | "
            f"{_num(v['ir'], '{:+.3f}')} | {_num(v['t'], '{:+.3f}')} | "
            f"`{f['overfit_flag']}` | **{f['verdict']}** |")
    lines += ["", "### 1.1 逐因子细节", ""]
    for name in report["main_factors"]:
        f = report["factors"][name]
        sp = f["layers"]["spread"]
        lines += [
            f"- **`{name}`**：{f['note']}",
            f"  - 跳过日（截面 < `MIN_XSEC_N`）{f['n_dates_skipped']} 个；"
            f"有截面但相关系数算不出 {f['n_ic_undefined']} 个；"
            f"非空覆盖度日 P50 = {f['value_coverage_p50']}",
            f"  - Pearson 次读数（并列报出、不作判据）：验证段均值 "
            f"{_num(f['pearson']['mean_validate'])}",
            f"  - 分层（分数降序、等量、并列按 code 定序；第 1 层 = 最高分）："
            f"各层平均前向收益 "
            + "、".join(f"L{k} {_num(f['layers']['mean_by_layer'][str(k)], '{:+.5f}')}"
                        for k in range(1, report['n_layers'] + 1)),
            f"  - `spread = layer1 − layer{report['n_layers']}`：n={sp['n_days']} 天，"
            f"均值 {_num(sp['mean'], '{:+.5f}')}，95% CI "
            f"[{_num(sp['ci_low'])}, {_num(sp['ci_high'])}]",
            f"  - 单调性 `n_ascending_steps` = **{f['layers']['n_ascending_steps']}** "
            f"/ {report['n_layers'] - 1}；按日平均 "
            f"{_num(f['layers']['n_ascending_steps_per_day_mean'], '{:.2f}')}",
        ]
    lines += [
        "",
        f"- 门槛：`MIN_VALID_PERIODS`={report['min_periods']}、bootstrap "
        f"n={report['bootstrap_n']} seed={report['bootstrap_seed']}"
        f"（**import 自 `plugin/sandbox.py`，未另定**）、"
        f"`MIN_XSEC_N`={report['min_xsec_n']}（import 自 `research/signal.py`）",
        "",
        "## 2. 次读数（5 个财务因子，`exploratory=true`，**只报不判**）",
        "",
        f"- {report['secondary_note']}",
        "",
        "| 因子 | 有效日期 n | 验证段均值 | 验证段 n | 非空覆盖度日 P50 | "
        "覆盖标记 | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in report["secondary_factors"]:
        s = report["secondary"][name]
        lines.append(
            f"| `{name}` | {s['n_dates']} | "
            f"{_num(s['mean_validate'], '{:+.5f}')} | {s['n_validate']} | "
            f"{s['value_coverage_p50']} | "
            f"{('`' + s['coverage_flag'] + '`') if s['coverage_flag'] else '—'} | "
            f"{'—（次读数不判）' if s['verdict'] is None else s['verdict']} |")
    lines += [
        "",
        "## 3. 裁剪诊断（D5，**只报不判**）",
        "",
        f"- {report['clip_diag_note']}",
        f"- 逐调仓日「被裁标的本数 / 当日截面数」：P50 = "
        f"{_num(cd['ratio_p50'], '{:.4f}')}、min = "
        f"{_num(cd['ratio_min'], '{:.4f}')}、max = {_num(cd['ratio_max'], '{:.4f}')}"
        f"（{cd['n_dates']} 个调仓日）",
        f"- 被裁本数：P50 = {cd['n_clipped_p50']}、max = {cd['n_clipped_max']}；"
        f"当日截面数 P50 = {cd['xsec_p50']}",
        "",
        "## 4. 合成分数读数并排（P78 `rank-ic`，**只引用不重跑**）",
        "",
        f"- 来源：`{ref['source_report']}`（`{ref['experiment']}` 全窗跑，"
        f"{ref['start']} ~ {ref['end']}，宇宙 `{ref['universe_id']}`）",
        f"- `{ref['reading']}`：验证段均值 {_num(ref['mean_validate'], '{:+.5f}')}，"
        f"95% CI [{_num(ref['ci_low'])}, {_num(ref['ci_high'])}]，"
        f"有效日期 n={ref['n_validate']}，verdict = **{ref['verdict']}**",
        f"- {ref['note']}",
        "",
        "## 5. 覆盖度（可见性读数）",
        "",
        f"- 调仓边界 {report['n_marks']} 个（短池 {report['horizon']} 日一调）、"
        f"周期 {cov['n_periods']} 个；有 IC 的日期 "
        + "、".join(f"{k} {v}" for k, v in cov['n_dates_with_ic'].items()),
        f"- 跳过日（截面 < `MIN_XSEC_N`）："
        + "、".join(f"{k} {v}" for k, v in report['n_dates_skipped'].items()),
        f"- 每日**已打分**截面：P50 = {cov['xsec_size_p50']}、"
        f"min = {cov['xsec_size_min']}、max = {cov['xsec_size_max']}",
        f"- 前向收益口径：`load_bars_adjusted(as_of={report['fwd_asof']})`（D3，"
        f"一次/只）；回退未复权价 {report['n_fwd_fallback']} 只、"
        f"缺价剔除 {cov['n_no_fwd_ret']} 个 (日期, code)",
        "",
        f"- {report['fallback_note']}",
        "",
        "## 6. 必须并列披露的口径",
        "",
    ]
    lines += [f"- {item}" for item in report["non_pit_items"]]
    lines += [f"- {item}" for item in report.get("non_pit_universe_items", ())]
    lines += ["", f"- {report['selection_bias_note']}", "",
              f"- {report['universe_note']}", "",
              f"- {report['replay_raw_price_note']}", "",
              "## 7. 复现与耗时", "",
              "```bash",
              ".venv/bin/python -m stocklab.cli.main research factor-ic \\",
              f"    --pool {report['pool']} --start {report['start']} \\",
              f"    --universe {report['universe_id']} \\",
              f"    --prereg {report['prereg_path']} --out reports/research/",
              "```",
              "",
              f"- 总耗时 {report['elapsed_s']:.1f} s（逐调仓日 `score_pipeline` "
              f"{report['scan_s']:.1f} s ＋ 前向收益载入 {report['fwd_load_s']:.1f} s）",
              "",
              summary_line(report),
              ""]
    return "\n".join(lines)


def write_report(report: Mapping, out_dir: Path) -> tuple[Path, Path]:
    """落 `<out>/<end>-factor-ic-<universe_id>.{json,md}`，返回两个路径。

    **文件名必须带宇宙 id**（与 `rank-ic` 同规矩：不带时换宇宙重跑同一个 end 会
    静默覆盖上一份产物）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report['end']}-factor-ic-{report['universe_id']}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_md(report), encoding="utf-8")
    return json_path, md_path


def default_out_dir() -> Path:
    """默认产物目录 = `reports/research/`（读 `paths.REPORT_DIR`，调用时取）。"""
    return paths.REPORT_DIR / "research"
