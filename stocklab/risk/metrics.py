"""风险预算指标（P14 / Task 65）：把「这一仓有多险」变成可复核的数字。

## 每个数都带自己的窗口与样本量

风险指标最容易变成「一个看起来专业的数字」：不给窗口、不给样本量、
不说清是历史法还是参数法，读者无从怀疑。所以这里每个输出都带上
`window / n_obs / method`，样本不足一律挂「样本不足，仅供观察」。

| 指标 | 口径（写死） |
|---|---|
| 20 日 RV | 日对数收益的**总体**标准差 × √252（年化） |
| VaR95 / CVaR95 | **历史法**（不做正态假设）· 5 日视界 · 重叠 5 日对数收益的 5% 分位 |
| MDD | 收盘价序列的最大回撤（正百分数），近 250 日与区间全段各报一次 |
| 索提诺 | 年化超额均值 ÷ 年化下行标准差（下行 = 低于目标的收益） |
| Calmar | 年化收益 ÷ |全段 MDD| |
| 波动率目标 | `w = 15% / RV`，clip `[0,1]`（现货无杠杆） |
| 破产风险 | `R ≈ ((1−A)/(1+A))^U`；`A ≤ 0` → `R = 1.0` 且标「长期必败」 |

`A`（每单位期望收益）与 `U`（资金分成多少单位）**必须由调用方给出**，
本模块不给默认值：默认值会被当成「算出来的」，而它其实是输入者的假设。
"""

from __future__ import annotations

import math
from statistics import fmean, pstdev

TRADING_DAYS = 252
RV_WINDOW = 20
VAR_LEVEL = 0.95
VAR_HORIZON = 5
VOL_TARGET = 0.15
MIN_DAYS = 120


def log_returns(closes: list[float]) -> list[float]:
    """日对数收益（`len(closes) - 1` 个）。"""
    out = []
    for a, b in zip(closes, closes[1:]):
        if a <= 0 or b <= 0:
            raise ValueError("价格必须为正")
        out.append(math.log(b / a))
    return out


def realized_vol(closes: list[float], *, window: int = RV_WINDOW,
                 periods: int = TRADING_DAYS) -> float | None:
    """近 `window` 日年化实现波动率。样本不足 → `None`（不是 0）。"""
    if len(closes) < window + 1:
        return None
    return pstdev(log_returns(closes[-(window + 1):])) * math.sqrt(periods)


def _quantile(xs: list[float], q: float) -> float:
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return xs[int(pos)]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def var_cvar(closes: list[float], dates: list[str] | None = None, *,
             level: float = VAR_LEVEL, horizon: int = VAR_HORIZON) -> dict:
    """历史法 VaR/CVaR（正数 = 亏损比例）。重叠 h 日收益，**声明**窗口与样本量。"""
    if len(closes) < horizon + 2:
        return {"method": "historical", "level": level, "horizon_days": horizon,
                "n_obs": 0, "var": None, "cvar": None, "window": None,
                "label": "样本不足，仅供观察",
                "note": "重叠收益不足 1 个，无法给分位数"}
    rets = []
    for i in range(len(closes) - horizon):
        rets.append(math.log(closes[i + horizon] / closes[i]))
    rets.sort()
    var = -_quantile(rets, 1.0 - level)
    tail = [r for r in rets if r <= -var]
    cvar = -(fmean(tail) if tail else -var)
    d0 = dates[0] if dates else None
    d1 = dates[-1] if dates else None
    return {
        "method": "historical",
        "level": level,
        "horizon_days": horizon,
        "n_obs": len(rets),
        "var": round(var, 6),
        "cvar": round(cvar, 6),
        "window": {"start": d0, "end": d1},
        "label": "样本充足" if len(rets) >= MIN_DAYS else "样本不足，仅供观察",
        "note": f"{int(level * 100)}% 分位上的 {horizon} 日亏损；VaR 是分位点，"
                f"CVaR 是**尾部均值**（破了 VaR 之后平均亏多少，比 VaR 更该看）",
    }


def max_drawdown(closes: list[float], dates: list[str] | None = None) -> dict:
    """最大回撤（正百分数）与峰谷。空/单点 → 0，但注明没有可比区间。"""
    if len(closes) < 2:
        return {"mdd_pct": None, "peak": None, "trough": None, "n_days": len(closes),
                "note": "不足 2 根收盘价，没有回撤可言"}
    peak = closes[0]
    best = 0.0
    peak_i = trough_i = 0
    cur_peak_i = 0
    for i, c in enumerate(closes):
        if c > peak:
            peak, cur_peak_i = c, i
        dd = (peak - c) / peak
        if dd > best:
            best, peak_i, trough_i = dd, cur_peak_i, i
    return {
        "mdd_pct": round(best * 100.0, 4),
        "peak": dates[peak_i] if dates else None,
        "trough": dates[trough_i] if dates else None,
        "n_days": len(closes),
    }


def downside_deviation(returns: list[float], *, target: float = 0.0) -> float:
    """下行标准差（只惩罚低于目标的部分）——索提诺的分母。"""
    if not returns:
        return 0.0
    d = [min(0.0, r - target) ** 2 for r in returns]
    return math.sqrt(sum(d) / len(d))


def sortino(returns: list[float], *, periods: int = TRADING_DAYS,
            target: float = 0.0) -> float | None:
    """年化索提诺。没有下行波动（全是正收益）→ `None`（不是无穷大）。"""
    dd = downside_deviation(returns, target=target)
    if not returns or dd == 0:
        return None
    return (fmean(returns) - target) * periods / (dd * math.sqrt(periods))


def calmar(returns: list[float], mdd_pct: float | None, *,
           periods: int = TRADING_DAYS) -> float | None:
    """年化收益 ÷ |MDD|。没有回撤（或没有收益序列）→ `None`。"""
    if not returns or mdd_pct in (None, 0):
        return None
    return fmean(returns) * periods / (mdd_pct / 100.0)


def vol_target_position(rv: float | None, *, target: float = VOL_TARGET) -> dict:
    """`w = 目标波动 / RV`，clip `[0,1]`。RV 未知 → `w=None`（不许当满仓）。"""
    if rv is None or rv <= 0:
        return {"target_vol": target, "rv": rv, "w": None, "clipped": None,
                "note": "没有波动估计 → 没有波动率目标仓位（不是 0，也不是满仓）"}
    raw = target / rv
    w = min(1.0, max(0.0, raw))
    return {
        "target_vol": target,
        "rv": round(rv, 6),
        "w_raw": round(raw, 6),
        "w": round(w, 6),
        "clipped": raw > 1.0,
        "note": ("RV 低于目标 → 按公式该加杠杆，现货无杠杆 → clip 到 100%"
                 if raw > 1.0 else "RV 高于目标 → 降仓到 w"),
    }


def ruin_risk(edge_ratio: float, units: float) -> dict:
    """破产风险 `R ≈ ((1−A)/(1+A))^U`。

    `A` = 每单位期望收益（每笔期望净收益 ÷ 单笔风险），`U` = 资金分成多少单位。
    `A ≤ 0` → `R = 1.0` 并标「长期必败」：期望不为正时，交易次数越多越接近归零，
    这不是「风险高」，是**确定性**。
    """
    if units <= 0:
        raise ValueError(f"units 必须为正，收到 {units!r}")
    base = {"edge_ratio": round(edge_ratio, 6), "units": round(units, 6),
            "formula": "R = ((1−A)/(1+A))^U"}
    if edge_ratio <= 0:
        return dict(base, ruin_risk=1.0, note="**长期必败**：每单位期望收益 A ≤ 0，"
                                             "交易次数越多越接近归零（不是风险高，是确定性）")
    if edge_ratio >= 1.0:
        return dict(base, ruin_risk=0.0,
                    note="A ≥ 1（每单位期望收益 ≥ 风险）在现实中不存在 —— "
                         "先怀疑输入，而不是相信 0 破产概率")
    r = ((1.0 - edge_ratio) / (1.0 + edge_ratio)) ** units
    return dict(base, ruin_risk=round(r, 8), note="按固定比例下注的经典近似")


def build_metrics(bars: list, *, asof: str, min_days: int = MIN_DAYS) -> dict:
    """`≤ asof` 的复权 K 线 → 风险预算块（稳定 JSON）。"""
    closes = [float(b.close) for b in bars]
    dates = [b.date for b in bars]
    rv = realized_vol(closes)
    vc = var_cvar(closes, dates)
    rets = log_returns(closes) if len(closes) > 1 else []
    mdd_all = max_drawdown(closes, dates)
    mdd_250 = max_drawdown(closes[-250:], dates[-250:])
    return {
        "asof": asof,
        "n_bars": len(bars),
        "window": {"start": dates[0] if dates else None,
                   "end": dates[-1] if dates else None},
        "sample": {"n_days": len(bars) - 1, "min_days": min_days,
                   "meets": (len(bars) - 1) >= min_days,
                   "label": "样本充足" if (len(bars) - 1) >= min_days else "样本不足，仅供观察"},
        "price": round(closes[-1], 4) if closes else None,
        "rv20_annual": None if rv is None else round(rv, 6),
        "rv_window": RV_WINDOW,
        "var_cvar": vc,
        "mdd_250d": mdd_250,
        "mdd_all": mdd_all,
        "sortino": _r(sortino(rets)),
        "calmar": _r(calmar(rets, mdd_all["mdd_pct"])),
        "vol_target": vol_target_position(rv),
        "method_note": "全部为**历史法**（不做正态/参数假设）；重叠窗口会低估极端尾部，"
                       "样本量已随指标报出",
    }


def _r(x, digits: int = 6):
    return None if x is None else round(float(x), digits)


def render_metrics(m: dict) -> str:
    """人看的风险面板（数字与 JSON 同源）。"""
    vc, vt = m["var_cvar"], m["vol_target"]
    lines = [
        f"=== 风险预算 · {m['asof']} ===",
        f"价格 {m['price']}   窗口 {m['window']['start']} ~ {m['window']['end']}"
        f"（{m['n_bars']} 根）   样本 {m['sample']['label']}"
        f"（{m['sample']['n_days']} / {m['sample']['min_days']} 交易日）",
        f"20 日年化波动率 RV = {_p(m['rv20_annual'])}"
        f"   → 波动率目标仓位 w = {_p(vt['w'])}（目标 {_p(vt['target_vol'])}）",
        f"VaR{int(vc['level'] * 100)}（{vc['horizon_days']} 日，历史法，"
        f"{vc['n_obs']} 个重叠样本） = {_p(vc['var'])}"
        f"   CVaR = {_p(vc['cvar'])}",
        f"最大回撤：近 250 日 {_p(m['mdd_250d']['mdd_pct'], 100)}"
        f"（{m['mdd_250d']['peak']} → {m['mdd_250d']['trough']}）"
        f"   全段 {_p(m['mdd_all']['mdd_pct'], 100)}"
        f"（{m['mdd_all']['peak']} → {m['mdd_all']['trough']}）",
        f"索提诺 {_f(m['sortino'], 3)}   Calmar {_f(m['calmar'], 3)}",
        f"  ℹ️  {vt['note']}",
        "  ℹ️  破产风险 R=((1−A)/(1+A))^U 需要 A、U —— 见 `risk show` 的 Kelly 段，"
        "本表不给默认值（默认值会被当成算出来的）",
    ]
    return "\n".join(lines)


def _p(x, scale: float = 1.0) -> str:
    return "—" if x is None else f"{float(x) * scale:.2f}%"


def _f(x, digits: int = 4) -> str:
    return "—" if x is None else f"{float(x):.{digits}f}"
