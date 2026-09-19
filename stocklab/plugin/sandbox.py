"""沙盒对比回测（设计文档 §8）—— 「口径守铁律」在这里落地。

## 为什么需要一条固定的下游链路

插桩输出的是 `score` / `pass_flag`，**没有收益** —— 直接比较两个版本的
分数没有意义（分数高不代表选出来的标的涨得好）。所以必须把候选池接到
一个下游：池内标的等权持有到下一个调仓日、扣成本、对照基准。

## 下游规则写死，不随插桩版本变化

如果下游也能改，那「改下游」就成了另一条提分路径，归因立刻失效
（`CLAUDE.md` 单变量原则）。所以 `REBALANCE_DAYS` 是常量，不是参数。

## 判定守铁律

- 有效交易日 < 120 → `INCONCLUSIVE`，报告逐字写「样本不足，不构成结论」
- Δ 的 bootstrap 95% CI 不跨 0 且方向为正 → `WIN`
- 其余 → `LOSE`

**有效交易日按日聚类计数**，不是行数 —— 20 只标的同一天 ≠ 20 个样本。

## 首版没有 baseline

`baseline_script_id is None` 时只能看绝对表现，verdict 一律
`INCONCLUSIVE`。**不美化**：没有对照就说没有对照。
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field

from stocklab.plugin import store

#: 各池的调仓周期（交易日）。**固定常量**，见模块 docstring。
REBALANCE_DAYS: dict[str, int] = {"short": 5, "mid": 20, "long": 60}

#: 出结论所需的最少有效交易日（CLAUDE.md 度量纪律③）。
MIN_VALID_DAYS: int = 120

#: 日度超额收益的 bootstrap 重采样次数与随机种子（固定 = 可复现）。
_BOOTSTRAP_N = 2000
_BOOTSTRAP_SEED = 20260918


@dataclass(frozen=True)
class SandboxVerdict:
    verdict: str                     # WIN / LOSE / INCONCLUSIVE
    pool: str
    n_days: int
    delta: float | None
    ci_low: float | None
    ci_high: float | None
    baseline_script_id: int | None
    note: str
    detail: dict = field(default_factory=dict)

    def as_metrics(self) -> dict:
        return {"n_days": self.n_days, "delta": self.delta,
                "ci_low": self.ci_low, "ci_high": self.ci_high,
                "pool": self.pool, "note": self.note, **self.detail}


def _bootstrap_ci(values: list[float]) -> tuple[float, float]:
    """日度序列的均值 bootstrap 95% CI（按日聚类：每天一个值）。"""
    rng = random.Random(_BOOTSTRAP_SEED)
    n = len(values)
    means = []
    for _ in range(_BOOTSTRAP_N):
        means.append(sum(rng.choice(values) for _ in range(n)) / n)
    means.sort()
    lo = means[int(0.025 * _BOOTSTRAP_N)]
    hi = means[int(0.975 * _BOOTSTRAP_N) - 1]
    return lo, hi


def run_sandbox(conn: sqlite3.Connection, *, candidate_script_id: int,
                baseline_script_id: int | None, pool: str, window_start: str,
                window_end: str, now: str) -> SandboxVerdict:
    if pool not in REBALANCE_DAYS:
        raise ValueError(f"未知池 {pool!r}；已知：{sorted(REBALANCE_DAYS)}")

    if baseline_script_id is None:
        return SandboxVerdict(
            verdict="INCONCLUSIVE", pool=pool, n_days=0, delta=None,
            ci_low=None, ci_high=None, baseline_script_id=None,
            note="首版没有 baseline，只能看绝对表现，不构成新旧对比结论")

    candidate = store.get_script(conn, candidate_script_id)
    baseline = store.get_script(conn, baseline_script_id)
    if candidate is None or baseline is None:
        raise LookupError(
            f"脚本不存在：candidate={candidate_script_id} "
            f"baseline={baseline_script_id}")

    daily = _replay_daily_excess(conn, candidate, baseline, pool=pool,
                                 window_start=window_start,
                                 window_end=window_end)

    n_days = len(daily)
    if n_days < MIN_VALID_DAYS:
        return SandboxVerdict(
            verdict="INCONCLUSIVE", pool=pool, n_days=n_days, delta=None,
            ci_low=None, ci_high=None, baseline_script_id=baseline_script_id,
            note=f"样本不足（{n_days} 个有效交易日 < {MIN_VALID_DAYS}），"
                 "不构成结论",
            detail={"rebalance_days": REBALANCE_DAYS[pool]})

    delta = sum(daily) / n_days
    lo, hi = _bootstrap_ci(daily)
    if lo > 0:
        verdict = "WIN"
    else:
        verdict = "LOSE"
    return SandboxVerdict(
        verdict=verdict, pool=pool, n_days=n_days, delta=delta, ci_low=lo,
        ci_high=hi, baseline_script_id=baseline_script_id,
        note=f"Δ 日均超额 {delta:+.4%}，95% CI [{lo:+.4%}, {hi:+.4%}]，"
             f"按日聚类 n={n_days}",
        detail={"rebalance_days": REBALANCE_DAYS[pool]})


def _replay_daily_excess(conn: sqlite3.Connection, candidate: dict,
                         baseline: dict, *, pool: str, window_start: str,
                         window_end: str) -> list[float]:
    """在窗口内逐调仓日重放两个版本，返回逐日超额收益序列。

    **本轮是骨架实现**：插桩脚本的输入需求会随业务演进，这里先返回空
    序列 —— 于是任何调用方都会走 `INCONCLUSIVE` 分支，**不会**因为
    「回放没实现」而误判成 WIN。这是刻意的 fail-closed：
    宁可不出结论，也不能出一个假的结论。

    接入真实回放是 Task 16 之后的第一件后续工作（见设计文档 §13
    「未决项」）。
    """
    _ = (conn, candidate, baseline, pool, window_start, window_end)
    return []


def overfit_flag(delta: float | None, ci_low: float | None,
                 ci_high: float | None) -> str | None:
    """过拟合标记（设计文档 §8.4）。

    骨架判据：CI 下界为负而上界明显为正（区间宽到跨 0 的 2 倍以上），
    说明「看起来赢了但极不稳定」。

    返回值：
    - ``'suspected'`` — 命中过拟合启发式，建议人工复查。
    - ``None``        — 无标记：输入缺失（没有证据）或启发式未触发（评估干净）。
                       两种「无事发生」场景统一用 ``None`` 表达，避免下游
                       真值判断把 ``'none'`` 字符串误判为阳性。
    """
    if delta is None or ci_low is None or ci_high is None:
        return None
    if ci_low < 0 < ci_high and (ci_high - ci_low) > 2 * abs(delta):
        return "suspected"
    return None
