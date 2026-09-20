"""沙盒对比回测（设计文档 §8）—— 「口径守铁律」在这里落地。

## 为什么需要一条固定的下游链路

插桩输出的是 `score` / `pass_flag`，**没有收益** —— 直接比较两个版本的
分数没有意义（分数高不代表选出来的标的涨得好）。所以必须把候选池接到
一个下游：池内标的等权持有到下一个调仓日、扣成本、对照基准。

## 下游规则写死，不随插桩版本变化

如果下游也能改，那「改下游」就成了另一条提分路径，归因立刻失效
（`CLAUDE.md` 单变量原则）。所以 `REBALANCE_DAYS` 是常量，不是参数。

## 判定守铁律

- 有效调仓周期数 < 120 → `INCONCLUSIVE`，报告逐字写「样本不足，不构成结论」
- 验证段 Δ 的 bootstrap 95% CI 不跨 0 且方向为正 → `WIN`
- 其余 → `LOSE`

**观测单元是调仓周期**，不是交易日 —— 同一周期内的多个交易日是同一持仓
产生的、高度自相关，不可当独立样本。

## 首版没有 baseline

`baseline_script_id is None` 时只能看绝对表现，verdict 一律
`INCONCLUSIVE`。**不美化**：没有对照就说没有对照。此时 **不调用 replay**。

## verdict 只看验证段

回放产出的 Δ 序列由 `split_train_validate`（Task 4）切成训练段（前 70%）
与验证段（后 30%）。**只有验证段用于出 verdict**；训练段均值进 `detail`
供 Task 6 的过拟合标记使用。
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from stocklab.config.replay import REBALANCE_DAYS
from stocklab.plugin import store
from stocklab.plugin.lifecycle import active_script_id

#: 出结论所需的最少有效**调仓周期**数。
#:
#: ⚠️ **它比原来的 `MIN_VALID_DAYS = 120`（120 个交易日）严格得多** ——
#: 一个 60 日调仓周期含 60 个交易日，所以 120 个周期 ≈ 7200 个交易日 ≈ 29 年。
#: 这是「观测单元改成周期」的算术后果（设计文档 §2 D1），不是缺陷。
#: **不许为了让某个池能出结论而调低它** —— 那是改口径迁就结果。
MIN_VALID_PERIODS: int = 120

#: 注入的回放函数：`(conn, *, candidate_script_id, baseline_script_id, pool,
#: window_start, window_end, **kw) -> (训练段 Δ, 验证段 Δ)`。
#:
#: **为什么要注入而不是直接调**：回放要跑 `candidate/` 的打分内核，而
#: `plugin/` 不得 import `candidate/`（依赖方向）。注入保住分层，也沿用
#: 项目既有模式（`ingest_daily_bars` 的 `fetch` 同样注入）。
#: **不注入 → 保持 fail-closed**（空序列 → `INCONCLUSIVE`），绝不假装
#: 「没有回放」等于「没有差异」。
ReplayFn = Callable[..., tuple[list[float], list[float]]]


@dataclass(frozen=True)
class SandboxDeps:
    """sandbox 所需的注入依赖（Task 5 起开始填充）。

    `replay`：回放函数（Task 5 接线）。
    `benchmark_excess`：基准超额函数（Task 7 接线）。
    `rebalance_marks`：调仓日序列函数（Task 7 接线）。

    `deps is None` 或 `deps.replay is None` → fail-closed（空序列 →
    `INCONCLUSIVE`）。
    """
    replay: ReplayFn | None = None
    benchmark_excess: Callable | None = None
    rebalance_marks: Callable | None = None


#: 训练段比验证段好看的容忍上限。
#:
#: ⚠️ **这个数是我定的**（设计文档 §8.3），来源是「两段差距到了半个
#: 百分点，多半不是噪声」这个直觉的数值化 —— 编不出来源。与 ADR-012
#: 的跌破线、ADR-016 的断更阈值同属**自创取值**，已列在 ADR-017 里。
OVERFIT_TRAIN_GAP: float = 0.005

#: 日度超额收益的 bootstrap 重采样次数与随机种子（固定 = 可复现）。
_BOOTSTRAP_N = 2000
_BOOTSTRAP_SEED = 20260918


@dataclass(frozen=True)
class SandboxVerdict:
    verdict: str                     # WIN / LOSE / INCONCLUSIVE
    pool: str
    n_periods: int
    delta: float | None
    ci_low: float | None
    ci_high: float | None
    baseline_script_id: int | None
    note: str
    detail: dict = field(default_factory=dict)

    def as_metrics(self) -> dict:
        return {"n_periods": self.n_periods, "delta": self.delta,
                "ci_low": self.ci_low, "ci_high": self.ci_high,
                "pool": self.pool, "note": self.note, **self.detail}


def _bootstrap_ci(values: list[float]) -> tuple[float, float]:
    """周期序列的均值 bootstrap 95% CI（每个周期一个值）。"""
    rng = random.Random(_BOOTSTRAP_SEED)
    n = len(values)
    means = []
    for _ in range(_BOOTSTRAP_N):
        means.append(sum(rng.choice(values) for _ in range(n)) / n)
    means.sort()
    lo = means[int(0.025 * _BOOTSTRAP_N)]
    hi = means[int(0.975 * _BOOTSTRAP_N) - 1]
    return lo, hi


def overfit_flag(train_mean: float | None,
                 validate_mean: float | None) -> str | None:
    """过拟合标记：**训练段比验证段好看**超过阈值即 `'suspected'`。

    方向固定 —— `train_gap = train_mean − validate_mean`，只有 **> 0**
    （训练段更好）才是过拟合的形态；验证段反而更好不标记。

    返回：
    - ``'suspected'`` — 命中，建议人工复查。
    - ``None``        — 输入缺失或未命中。两种「无标记」统一用 ``None``，
      避免下游真值判断把 ``'none'`` 字符串误判为阳性。
    """
    if train_mean is None or validate_mean is None:
        return None
    if train_mean - validate_mean > OVERFIT_TRAIN_GAP:
        return "suspected"
    return None


def run_sandbox(conn: sqlite3.Connection, *, candidate_script_id: int,
                baseline_script_id: int | None, pool: str, window_start: str,
                window_end: str, now: str,
                deps: SandboxDeps | None = None) -> SandboxVerdict:
    _replay = deps.replay if deps is not None else None
    _benchmark_excess = deps.benchmark_excess if deps is not None else None
    _rebalance_marks = deps.rebalance_marks if deps is not None else None

    if pool not in REBALANCE_DAYS:
        raise ValueError(f"未知池 {pool!r}；已知：{sorted(REBALANCE_DAYS)}")

    if baseline_script_id is None:
        return SandboxVerdict(
            verdict="INCONCLUSIVE", pool=pool, n_periods=0, delta=None,
            ci_low=None, ci_high=None, baseline_script_id=None,
            note="首版没有 baseline，只能看绝对表现，不构成新旧对比结论",
            detail={"overfit_flag": None, "reason": "no_baseline"})

    if baseline_script_id == candidate_script_id:
        return SandboxVerdict(
            verdict="INCONCLUSIVE", pool=pool, n_periods=0, delta=None,
            ci_low=None, ci_high=None, baseline_script_id=baseline_script_id,
            note="候选与基线是同一脚本，Δ 恒为 0，不构成任何对比结论",
            detail={"overfit_flag": None, "reason": "self_comparison"})

    candidate = store.get_script(conn, candidate_script_id)
    baseline = store.get_script(conn, baseline_script_id)
    if candidate is None or baseline is None:
        raise LookupError(
            f"脚本不存在：candidate={candidate_script_id} "
            f"baseline={baseline_script_id}")

    if _replay is None:
        train, validate = [], []
    else:
        train, validate = _replay(
            conn, candidate_script_id=candidate_script_id,
            baseline_script_id=baseline_script_id, pool=pool,
            window_start=window_start, window_end=window_end)

    n_periods = len(validate)
    train_mean = (sum(train) / len(train)) if train else None
    validate_mean = (sum(validate) / len(validate)) if validate else None
    detail: dict = {"rebalance_days": REBALANCE_DAYS[pool],
                    "train_n": len(train), "validate_n": n_periods,
                    "train_mean": train_mean,
                    "validate_mean": validate_mean,
                    "overfit_flag": overfit_flag(train_mean, validate_mean)}

    # script_id 留痕：candidate / baseline + 其余插件的当前 active 版本。
    # `active_script_id` 返回 None 表示该插件尚无 active 版本——此时
    # replay_period_deltas 会调 score_pipeline，而 score_pipeline 对无
    # active 版本的插件会抛 NoActivePlugin，所以回放本身就不会成功；
    # 因此这里记 None 是合法状态（仅出现在测试里），不静默忽略。
    detail["scripts"] = {
        "candidate": candidate_script_id,
        "baseline": baseline_script_id,
        "active": {pid: active_script_id(conn, pid)
                   for pid in ("0", "1", "2", "3", "4")},
    }

    # 两版相对 index_300 的超额（铁律：跑不赢就明说，并列报告）。
    # 未注入 benchmark_excess 时不写进 detail，避免「算出来是 0」与「未算」混淆。
    # 注入了 benchmark_excess 但未注入 rebalance_marks 时，同样不写（写 None 而非
    # 0.0，确保「不可区分于真零」的情况不会静默发生）。
    if _benchmark_excess is not None and _rebalance_marks is not None:
        marks = _rebalance_marks(conn, pool=pool, start=window_start,
                                 end=window_end)
        # 单变量：candidate 只换被测插件，baseline 同理。
        # plugin_id 由 candidate_script_id 所属的插件决定。
        pid = str(store.get_script(conn, candidate_script_id)["plugin_id"])
        detail["candidate_excess_index300"] = _benchmark_excess(
            conn, asof_dates=marks, pool=pool,
            plugin_overrides={pid: candidate_script_id})
        detail["baseline_excess_index300"] = _benchmark_excess(
            conn, asof_dates=marks, pool=pool,
            plugin_overrides={pid: baseline_script_id})

    if n_periods < MIN_VALID_PERIODS:
        return SandboxVerdict(
            verdict="INCONCLUSIVE", pool=pool, n_periods=n_periods, delta=None,
            ci_low=None, ci_high=None, baseline_script_id=baseline_script_id,
            note=f"样本不足（{n_periods} 个有效调仓周期 < "
                 f"{MIN_VALID_PERIODS}），不构成结论", detail=detail)

    delta = sum(validate) / n_periods
    lo, hi = _bootstrap_ci(validate)
    verdict = "WIN" if lo > 0 else "LOSE"
    return SandboxVerdict(
        verdict=verdict, pool=pool, n_periods=n_periods, delta=delta,
        ci_low=lo, ci_high=hi, baseline_script_id=baseline_script_id,
        note=f"验证段 Δ 周期均值 {delta:+.4%}，95% CI [{lo:+.4%}, {hi:+.4%}]，"
             f"周期数 n={n_periods}", detail=detail)
