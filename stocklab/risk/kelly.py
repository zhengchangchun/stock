"""凯利公式（P14 / Task 63）：**风险预算 + 不下注开关**。

## 公式（写死，可单测复核）

| 项 | 式 |
|---|---|
| 二元凯利 | `f* = p − (1−p)/b` |
| 盈亏平衡胜率 | `p_be = 1/(1+b)` |
| 连续近似 | `f* ≈ μ/σ²`（同一频率） |

## 保守化（**强制**，不是可选项）

点估计直接喂凯利会系统性地高估仓位 —— 因为它把「样本恰好偏高」当成 edge。
所以两个输入都要**朝不利方向**取：

- `p_used` = 胜率的 **Wilson 95% 单侧下界**（不是点估 `p̂`）
- `b_used` = 赢幅的**下四分位** ÷ 亏幅的**上四分位**（对赢打折、对亏加码）

## NO_BET（本模块最重要的输出）

`p_used ≤ p_be` → `f* = 0`、`verdict = "NO_BET"`、`edge_significant = false`。
样本量不过门槛（`< 120` 个交易日）同样 `NO_BET` —— **两道门是分开报的**，
读者必须能看出到底是「没 edge」还是「没样本」。

这不是保守，这是**本项目唯一诚实的答案**：方向能力 ≈ 0 的系统，
严格凯利的正确输出就是不下注。任何让输出看起来「有仓位」的实现都禁止。
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field

#: 95% 单侧正态分位（`norm.ppf(0.95)`）。写常数而不是引 scipy —— 本项目不引 scipy，
#: 而这个数是确定值，可被单测钉住（见 `test_risk_kelly.py`）。
Z_95_ONE_SIDED = 1.6448536269514722

#: 分数凯利的默认分数（Thorp 的常用值：留出 3/4 的余量给估计误差）。
DEFAULT_FRAC = 0.25

#: 单票上限 = 用户纪律的 40%（`portfolio.discipline.DISCIPLINE` 的同源值）。
DEFAULT_F_CAP = 0.40

#: 样本量门槛：不足 120 个交易日 → 不许据此下注（CLAUDE.md《准确率优先》度量纪律）。
MIN_DAYS = 120

#: 按日聚类 bootstrap 的重采样次数与种子（**先验固定**，看结果后不许改）。
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 20260915

VERDICT_LABELS = {
    "NO_BET": "不下注",
    "BET": "可以下注（仓位见 f_final）",
    "UNDETERMINED": "无法判定（样本或输入不足）",
}


def verdict_label(verdict: str) -> str:
    return VERDICT_LABELS.get(verdict, verdict)


# ---------- 基础统计 ----------

def wilson_lower_bound(wins: int, n: int, *,
                       z: float = Z_95_ONE_SIDED) -> float:
    """胜率的 Wilson 95% **单侧下界**。

    Wilson 而不是 Wald（`p̂ − z·√(p̂(1−p̂)/n)`）：Wald 在 `p̂` 接近 0/1 或 n 小时
    会给出荒谬区间（甚至越界），而本项目的胜率恰好常在 0.5 附近、n 常常很小。
    """
    if n <= 0:
        raise ValueError(f"n 必须为正，收到 {n!r}：没有样本就没有胜率")
    if not 0 <= wins <= n:
        raise ValueError(f"wins({wins}) 必须在 [0, n={n}] 内")
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return max(0.0, center - half)


def quantile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（位置 = `(n−1)·q`，与 numpy 默认口径一致）。"""
    if not values:
        raise ValueError("空序列没有分位数")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q 必须在 [0,1]，收到 {q!r}")
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[int(pos)]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def lower_quartile(values: Sequence[float]) -> float:
    return quantile(values, 0.25)


def upper_quartile(values: Sequence[float]) -> float:
    return quantile(values, 0.75)


def binary_kelly(p: float, b: float) -> float:
    """`f* = p − (1−p)/b`（`b` = 赢幅/亏幅，> 0）。"""
    if b <= 0:
        raise ValueError(f"赔率 b 必须为正，收到 {b!r}")
    return p - (1.0 - p) / b


def breakeven_p(b: float) -> float:
    """`p_be = 1/(1+b)` —— 胜率低于它，长期必亏。"""
    if b <= 0:
        raise ValueError(f"赔率 b 必须为正，收到 {b!r}")
    return 1.0 / (1.0 + b)


def continuous_kelly(mu: float, sigma: float) -> float | None:
    """`f* ≈ μ/σ²`。`σ ≤ 0`（没有波动）→ `None`（不是 0：0 会被读成「算出来是 0」）。"""
    if sigma is None or sigma <= 0:
        return None
    return mu / (sigma * sigma)


def daily_clustered_winrate_ci(by_day: Sequence[Sequence[float]], *,
                               n_boot: int = BOOTSTRAP_N,
                               seed: int = BOOTSTRAP_SEED) -> list[float] | None:
    """按**日**聚类的胜率 95% bootstrap CI。

    重采样的是**天**不是**笔**：同一天的多笔/多标的共享同一段市场波动，
    按笔重采样会把它们当独立样本，CI 会窄得离谱。
    """
    days = [d for d in by_day if d]
    if not days:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_boot):
        picks = [days[rng.randrange(len(days))] for _ in range(len(days))]
        flat = [x for pick in picks for x in pick]
        means.append(sum(flat) / len(flat))
    means.sort()
    return [quantile(means, 0.025), quantile(means, 0.975)]


# ---------- 输入与结论 ----------

@dataclass(frozen=True)
class EdgeStats:
    """一条规则在 PIT 历史回放上的**全部**统计输入（本模块不认识数据库）。"""

    rule: str
    code: str
    window_start: str | None
    window_end: str | None
    n_trades: int
    n_days: int                                  # 有效样本量 = 交易日数（按日聚类）
    win_amplitudes: tuple[float, ...]            # 赢的幅度（净收益，正）
    loss_amplitudes: tuple[float, ...]           # 亏的幅度（净收益的绝对值，正）
    day_winrates: tuple[tuple[float, ...], ...]  # 每天一个簇（聚类 CI 用）
    net_returns: tuple[float, ...]               # 全部往返净收益（连续近似用）
    cost_bps: float                              # 往返成本（基点，实算）
    horizon: int = 5
    extra_warnings: tuple[str, ...] = ()
    meta: dict = field(default_factory=dict)

    @property
    def wins(self) -> int:
        return len(self.win_amplitudes)

    @property
    def losses(self) -> int:
        return len(self.loss_amplitudes)


def _pstdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _round(x, digits: int = 6):
    return None if x is None else round(float(x), digits)


def evaluate(stats: EdgeStats, *, frac: float = DEFAULT_FRAC,
             f_cap: float = DEFAULT_F_CAP, min_days: int = MIN_DAYS) -> dict:
    """回放统计 → 凯利结论（稳定 JSON）。

    两道门**分开报**：`gates.sample`（样本够不够）与 `gates.edge`（有没有 edge）。
    合成一个 `verdict` 是为了让调用方有个单一判断点，但理由必须能追溯。
    """
    if frac <= 0:
        raise ValueError(f"分数凯利 k 必须为正，收到 {frac!r}")
    if not 0 < f_cap <= 1:
        raise ValueError(f"f_cap 必须在 (0, 1]，收到 {f_cap!r}")

    warnings: list[str] = list(stats.extra_warnings)
    n = stats.n_trades
    wins = stats.wins
    p_point = (wins / n) if n else None
    p_used = wilson_lower_bound(wins, n) if n else None

    # 赢幅均值 / 亏幅均值（点估）与四分位保守化（used）
    b_point = None
    if wins and stats.losses:
        mean_win = sum(stats.win_amplitudes) / wins
        mean_loss = sum(stats.loss_amplitudes) / stats.losses
        if mean_loss > 0:
            b_point = mean_win / mean_loss
    b_used = None
    if wins and stats.losses:
        q_win = lower_quartile(stats.win_amplitudes)     # 对赢打折
        q_loss = upper_quartile(stats.loss_amplitudes)   # 对亏加码
        if q_loss > 0:
            b_used = q_win / q_loss

    if n == 0:
        warnings.append("回放窗口内没有任何一次完整往返 —— 无法估计 p/b")
    elif not wins:
        warnings.append("窗口内**一次都没赢过** —— 无法估计赢幅分布")
    elif not stats.losses:
        warnings.append("窗口内**一次都没亏过** —— 无法估计亏幅分布，"
                        "赔率不可信（这类样本通常是样本太短）")

    p_be = breakeven_p(b_used) if b_used else None
    f_star = binary_kelly(p_used, b_used) if (p_used is not None and b_used) else 0.0
    f_star = max(0.0, f_star)

    sample_ok = stats.n_days >= min_days
    edge_ok = bool(p_be is not None and p_used is not None and p_used > p_be)

    p_ci = daily_clustered_winrate_ci(stats.day_winrates)
    if not sample_ok:
        # 样本不够 → 不许据此下注（哪怕算出来是正的）
        warnings.append(
            f"样本不足：有效交易日 {stats.n_days} < 门槛 {min_days} —— "
            f"**不得据此给仓位**（不是「edge 很小」，是「还看不清」）")
    if not edge_ok and p_be is not None:
        warnings.append(
            f"edge 不显著：保守胜率 p_used={p_used:.4f} ≤ 盈亏平衡 p_be={p_be:.4f}"
            f"（赔率 b_used={b_used:.4f}）—— 长期期望为负，不下注")

    # 分数凯利
    f_fractional = frac * f_star
    overbet_rejected = False
    if f_star > 0 and f_fractional > 2.0 * f_star:
        # f > 2f* → 期望对数增长转负。硬拒绝（clip 到 2f*）并留痕。
        warnings.append(
            f"过注拒绝：请求 f={f_fractional:.4f} > 2·f*={2.0 * f_star:.4f}"
            f"（超过 2 倍最优仓位时期望对数增长为负），已 clip 到 2f*")
        f_fractional = 2.0 * f_star
        overbet_rejected = True

    f_cap_applied = f_fractional > f_cap
    f_final = min(f_fractional, f_cap)

    verdict = "BET" if (sample_ok and edge_ok) else "NO_BET"
    if verdict == "NO_BET":
        f_final = 0.0                       # 两道门任一不过 → 一个仓位都不给
    verdict_reason = ""
    if not sample_ok:
        verdict_reason = "sample_insufficient"
    elif not edge_ok:
        verdict_reason = "edge_not_significant"

    mu = (sum(stats.net_returns) / len(stats.net_returns)) if stats.net_returns else None
    sigma = _pstdev(stats.net_returns) if stats.net_returns else None
    f_continuous = continuous_kelly(mu, sigma) if mu is not None else None
    if f_continuous is not None:
        if f_continuous > 1.0:
            warnings.append(
                f"连续近似 f*≈μ/σ²={f_continuous:.4f} > 1：该式给的是**杠杆**倍数，"
                f"而 A 股现货无杠杆；且它按「每笔」频率、忽略交易在时间上不重叠 —— "
                f"只作对照，**不参与** f_final")
        if mu is not None and mu > 0 and not edge_ok and p_used is not None:
            warnings.append(
                f"两个口径给出**相反**信号：μ={mu:.4f} > 0（连续近似为正）但"
                f"保守胜率 {p_used:.4f} ≤ 盈亏平衡 {p_be:.4f}（二元口径为负）。"
                f"这说明收益靠**少数大赢**而不是靠胜率 —— 这类分布下 μ 的估计误差被 "
                f"1/σ² 放大，凯利对它极其敏感，**不足以支撑仓位**")

    return {
        "rule": stats.rule,
        "code": stats.code,
        "verdict": verdict,
        "verdict_label": verdict_label(verdict),
        "verdict_reason": verdict_reason,
        "edge_significant": edge_ok,
        "gates": {
            "sample": {"n_days": stats.n_days, "min_days": min_days,
                       "meets": sample_ok,
                       "label": "样本充足" if sample_ok else "样本不足，仅供观察"},
            "edge": {"meets": edge_ok, "p_used": _round(p_used, 6),
                     "p_be": _round(p_be, 6), "b_used": _round(b_used, 6)},
        },
        "p_point": _round(p_point, 6),
        "p_used": _round(p_used, 6),
        "p_ci95": [_round(x, 6) for x in p_ci] if p_ci else None,
        "p_be": _round(p_be, 6),
        "b_point": _round(b_point, 6),
        "b_used": _round(b_used, 6),
        "f_star": _round(f_star, 6),
        "f_star_continuous": _round(f_continuous, 6),
        "mu_per_trade": _round(mu, 6),
        "sigma_per_trade": _round(sigma, 6),
        "k": frac,
        "f_fractional": _round(f_fractional, 6),
        "f_cap": f_cap,
        "f_cap_applied": f_cap_applied,
        "f_final": _round(f_final, 6),
        "overbet_rejected": overbet_rejected,
        "warnings": warnings,
        "inputs": {
            "rule": stats.rule,
            "code": stats.code,
            "window": {"start": stats.window_start, "end": stats.window_end},
            "n_trades": n,
            "n_wins": wins,
            "n_losses": stats.losses,
            "n_days": stats.n_days,
            "horizon": stats.horizon,
            "cost_bps": _round(stats.cost_bps, 2),
            "cost_policy": "往返扣成本（佣金含最低 5 元 + 印花税 + 过户费 + 滑点 5bps/边），"
                           "实算值随名义本金变化",
        },
        "meta": stats.meta,
    }


def render_report(k: dict) -> str:
    """人看的凯利报告（数字与 JSON **同源**）。"""
    g = k["gates"]
    lines = [
        f"=== 凯利仓位建议 · {k['code']} · 规则 {k['rule']} ===",
        f"回放窗口 {k['inputs']['window']['start']} ~ {k['inputs']['window']['end']}"
        f"   往返 {k['inputs']['n_trades']} 次"
        f"（赢 {k['inputs']['n_wins']} / 亏 {k['inputs']['n_losses']}）"
        f"   有效交易日 {k['inputs']['n_days']}",
        f"往返成本（实算）{k['inputs']['cost_bps']:.1f} bps",
        "",
        f"胜率：点估 {_f(k['p_point'])}   保守（Wilson 95% 下界）{_f(k['p_used'])}"
        f"   CI {_ci(k['p_ci95'])}",
        f"赔率：点估 {_f(k['b_point'], 4)}   保守（赢下四分位/亏上四分位）"
        f"{_f(k['b_used'], 4)}",
        f"盈亏平衡胜率 p_be = {_f(k['p_be'])}",
        f"严格凯利 f* = {_f(k['f_star'])}   连续近似 μ/σ² = {_f(k['f_star_continuous'])}",
        f"分数凯利 k={k['k']} → f = {_f(k['f_fractional'])}"
        f"   上限 f_cap={k['f_cap']} → f_final = {_f(k['f_final'])}",
        "",
        f"结论：{k['verdict_label']}（{k['verdict']}）",
        f"  样本门：{'过' if g['sample']['meets'] else '不过'}"
        f"（{g['sample']['n_days']} / {g['sample']['min_days']} 交易日）",
        f"  edge 门：{'过' if g['edge']['meets'] else '不过'}",
    ]
    for w in k["warnings"]:
        lines.append(f"  ⚠️  {w}")
    lines.append("")
    lines.append("凯利在本项目的定位是**风险预算 + 不下注开关**，不是买点生成器。")
    return "\n".join(lines)


def _f(x, digits: int = 4) -> str:
    return "—" if x is None else f"{float(x):.{digits}f}"


def _ci(ci) -> str:
    return "—" if not ci else f"[{ci[0]:.4f}, {ci[1]:.4f}]"
