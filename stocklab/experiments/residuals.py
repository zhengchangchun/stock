"""在 **train 段** 拟合标准化残差的经验分布（P10-a）。

## 它做什么，不做什么

对 train 段每个可回放的目标日 `t`（`asof` = 该日的上一交易日），用**模型自己的口径**
算 `mu_hat` / `sigma_hat`（`WINDOW=60` 对数收益的均值与标准差），
再用次日**实际**复权收盘算标准化残差：

```
resid_t = (log(close_t / close_asof) − mu_hat_t) / sigma_hat_t
```

池子 = 全部 train 目标日 × 全部标的的 `resid_t`；交给
`predict.residual.ResidualDistribution` 变成 ECDF / 逆 ECDF。

**不做的事**（做了就是第二个变量）：

- 不按状态条件化（那是 P9-a 已被否证的缩放轴）；
- 不平滑、不插值、不加先验、不截尾；
- 不用 validate / test 的任何一天。

## PIT 是怎么被保证的（结构，不是自觉）

1. `train_days` 由调用方从 `split_days(...)["train"]` 取，**与 validate/test 不相交**；
2. 每个目标日的 `mu_hat` / `sigma_hat` 只读 `date <= asof` 的行 —— 由
   `predict.service.load_pit_bars` 保证（它自己第一件事就是裁掉 `> asof`）；
3. `close_t` 只从**同一根**已复权序列里取，`r_t` 是 asof→t 的一步收益。

有测试把 (1)(2)(3) 一起钉住：把**训练窗之后**的 bar 全换成暴涨 100 倍的值，
拟合结果必须逐位不变。

## 与 `compute_forecast` 的关系

`mu_hat` / `sigma_hat` 的算法与 `compute_forecast` **逐位同式**（同一个 `WINDOW`、
同 `fmean` / `stdev`），并且有一条测试**反漂移**：抽样比对该日
`compute_forecast` 自己 `evidence.inputs` 里的 `mu` / `sigma`。
两处各写一套而不钉住，迟早会漂移，而漂移的后果是「残差池不是这个模型的残差池」——
数字照样算得出来，结论全错。

## 为什么每根 K 线只读一次

`load_pit_bars(conn, code, asof)` 每次都要重跑 `adjust_bars`（O(历史长度)）。
train 段有上千个目标日，逐日读会把实验拖成几十分钟。这里按标的读**一次**
锚定在 train 段最后一个目标日的复权序列，再在内存里滚动 ——
**对数收益与复权锚点无关**（锚点对序列里所有 bar 是同一个乘性因子，
比值不变），所以读数与逐日重读逐位相同。锚定在「train 段最后一天」而不是
「全库最后一天」，是为了让「训练窗之后的 bar 一个字节都没被读过」在结构上成立。
"""

from __future__ import annotations

import math
import statistics
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from stocklab.data.models import Bar
from stocklab.errors import DegenerateInput
from stocklab.predict.model import WINDOW
from stocklab.predict.residual import ResidualDistribution
from stocklab.predict.service import PitCache, UnusableWindow, load_pit_bars

#: 算一个残差所需的最少 K 线数。与 `compute_forecast` 的守卫同源：
#: `closes` 取 `WINDOW + 1` 根（WINDOW 个收益），而 `compute_forecast` 另要求
#: `max(WINDOW, LEVEL_WINDOW) + 1` —— 这里沿用后者，保证「模型肯出预测的日子」
#: 与「拟合肯收残差的日子」是同一个集合，不会出现「模型有预测、池子里却没有它」。
MIN_BARS = 61


@dataclass(frozen=True)
class ResidualSample:
    """一个 `(目标日, 标的)` 的残差及其**输入**（便于反漂移比对）。"""

    target_day: str
    asof: str
    code: str
    mu_hat: float
    sigma_hat: float
    resid: float


@dataclass(frozen=True)
class ResidualFit:
    """一次拟合的结果：分布 + 逐样本明细 + 剔除原因。"""

    distribution: ResidualDistribution
    samples: tuple[ResidualSample, ...]
    skipped: Mapping[str, str] = field(default_factory=dict)

    def as_report_block(self) -> dict[str, Any]:
        """落进实验报告的**溯源块**（纯 JSON 可序列化）。"""
        return {
            "fit_split": "train",
            "distribution": self.distribution.as_evidence(),
            "n_samples": len(self.samples),
            "n_skipped": len(self.skipped),
            "skipped_examples": dict(list(sorted(self.skipped.items()))[:10]),
            "note": (
                "残差池**只在 train 段拟合**，apply 到 validate（以及将来可能打开的 "
                "test）；分位只由训练段决定，不含任何 validate/test 的行"
            ),
        }


def fit_residual_distribution(conn: sqlite3.Connection, *,
                              train_days: Sequence[str],
                              sessions: Sequence[str],
                              codes: Sequence[str],
                              cache: PitCache | None = None,
                              window: int = WINDOW) -> ResidualFit:
    """在 `train_days` 上拟合标准化残差的经验分布（**只读**，不写任何表）。

    `sessions` 是完整的交易日轴，用来把目标日映射到它的 `asof`（上一交易日）。
    `train_days` 必须都在 `sessions` 里 —— 否则报错（拒绝「悄悄跳过不知道的日子」）。

    剔除（进 `skipped` 并写明原因，**不填 0**）：

    - 目标日是该轴的第一个交易日 → 没有 `asof`；
    - `asof` 处没有 K 线（停牌 / 缺口）；
    - K 线不足 `MIN_BARS`；
    - `sigma_hat <= 0`（分布退化）或非有限；
    - 复权链在该窗口不可用（`UnusableWindow`）或价格非正（`DegenerateInput`）。

    池子不足 `RESIDUAL_MIN_SAMPLES` → `ResidualDistribution` 构造期抛
    `InsufficientResiduals`（**拒绝**给一个「看着还行、其实全是噪声」的形状）。
    """
    cache = cache or PitCache()
    index_of = {d: i for i, d in enumerate(sessions)}
    unknown = [d for d in train_days if d not in index_of]
    if unknown:
        raise ValueError(
            f"train_days 里有 {len(unknown)} 天不在 sessions 轴上（如 {unknown[:3]}）—— "
            "拒绝静默跳过：那会让「少了几天」和「那几天不该有」长得一模一样"
        )
    if not train_days:
        raise ValueError("train_days 为空 —— 没有训练段就没有可拟合的形状")

    # 每个标的**只读一次**，锚定在 train 段最后一个目标日（结构上读不到它之后的行）。
    anchor = train_days[-1]
    series: dict[str, list[Bar]] = {}
    skipped: dict[str, str] = {}
    for code in codes:
        try:
            series[code] = load_pit_bars(conn, code, anchor, cache=cache)
        except (UnusableWindow, DegenerateInput) as exc:
            skipped[f"{code}@fit"] = f"{type(exc).__name__}: {exc}"

    samples: list[ResidualSample] = []
    for target in train_days:
        i = index_of[target]
        if i == 0:
            skipped[target] = "区间起点是本库最早的交易日，没有「上一交易日」"
            continue
        asof = sessions[i - 1]
        for code in codes:
            bars = series.get(code)
            if bars is None:
                continue                       # 整个标的不可用，已在 skipped 里记过一次
            key = f"{target}/{code}"
            hist = [b for b in bars if b.date <= asof]
            if not hist or hist[-1].date != asof:
                last = hist[-1].date if hist else "无"
                skipped[key] = (f"NoBarOnAsof: 在 {asof} 无 K 线（最后一根 {last}）"
                                "—— 停牌或采集缺口")
                continue
            if len(hist) < MIN_BARS:
                skipped[key] = f"InsufficientBars: 只有 {len(hist)} 根，需要 >= {MIN_BARS}"
                continue
            try:
                mu_hat, sigma_hat = _mu_sigma(hist, window)
                close_t = _close_on(bars, target)
            except DegenerateInput as exc:
                skipped[key] = f"DegenerateInput: {exc}"
                continue
            samples.append(ResidualSample(
                target_day=target, asof=asof, code=code, mu_hat=mu_hat,
                sigma_hat=sigma_hat, resid=(math.log(close_t / hist[-1].close)
                                            - mu_hat) / sigma_hat))

    values = tuple(sorted(s.resid for s in samples))
    dist = ResidualDistribution(
        values=values,
        codes=tuple(sorted(codes)),
        first_day=train_days[0],
        last_day=train_days[-1],
        n_days=len({s.target_day for s in samples}),
        n_skipped=len(skipped),
        window=window,
    )
    return ResidualFit(distribution=dist, samples=tuple(samples), skipped=skipped)


def _mu_sigma(hist: Sequence[Bar], window: int) -> tuple[float, float]:
    """`(mu, sigma)` —— 与 `compute_forecast` **逐位同式**。

    同一个 `window`、同样取最后 `window + 1` 根收盘、同样的 `fmean` / `stdev`。
    有测试拿 `compute_forecast` 自己的 `evidence.inputs.mu/sigma` 反比对，
    所以这段“重复实现”不会悄悄漂移。
    """
    closes = [b.close for b in hist[-(window + 1):]]
    for c in closes:
        if not isinstance(c, (int, float)) or isinstance(c, bool) \
                or not math.isfinite(c) or c <= 0:
            raise DegenerateInput(f"close={c!r} 非正/非有限 —— 对数收益不可用，拒绝猜")
    rets = [math.log(cur / prev) for prev, cur in zip(closes, closes[1:])]
    if len(rets) < window:
        raise DegenerateInput(f"只有 {len(rets)} 个收益，少于 WINDOW={window}")
    mu = statistics.fmean(rets)
    sigma = statistics.stdev(rets)
    if not (sigma > 0.0) or not math.isfinite(sigma):
        raise DegenerateInput(
            f"WINDOW 日对数收益标准差为 {sigma!r} —— 分布退化，残差不可定义")
    return mu, sigma


def _close_on(bars: Sequence[Bar], day: str) -> float:
    """`day` 当天的复权收盘（**必须正好是这一天**，不取最近一根）。"""
    for b in reversed(bars):
        if b.date == day:
            return b.close
        if b.date < day:
            break
    raise DegenerateInput(f"复权序列里没有 {day} 这根 K 线 —— 拒绝用旧价冒充今日")
