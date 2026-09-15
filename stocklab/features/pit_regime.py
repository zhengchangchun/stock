"""PIT 状态特征（P9-a）：量能 z / 自身波动率分位 / 指数波动率分位。

三条信息全部只依赖 `date <= asof` 的行，且全部被映射成同一个东西：
**一个 (0, 1] 的分位 `q`**，供 `predict.model` 条件化 `sigma`（见
`docs/plans/2026-09-15-p9a-信息扩展.md` §1.1–1.2）。

## 三条结构性约束（靠代码，不靠自觉）

1. **拿不到未来**：每个函数的第一个动作都是裁掉 `> asof` 的行 —— 调用方把**全量**
   序列传进来，裁剪发生在这里。有「塞一根未来暴涨 bar → 值纹丝不动」的测试，
   另有一条**正向对照**（故意写一个不裁剪的实现，断言值确实变了）证明断言有鉴别力。
2. **不许碰 `amount` / `turnover`**：这两列在当前库里**全为 NULL**
   （`bars_daily` 14164/14164），任何用到它们的特征值都是假的。本模块的 **AST 里
   不出现这两个属性名**（测试直接扫 AST）；`volume` 缺失（NULL）或非正 → 抛
   `DegenerateInput`（**硬拒绝**），不填 0 —— 那会把「没数据」写成「零成交」。
3. **不许调参**：所有窗口都是先验固定的模块常量，没有环境变量、没有模块级开关、
   没有除入参以外的旋钮。改窗口就是换了一个变量，不是「调一下」。

## 与 `indicators` 的关系

`percentile_rank(values, window)` 已有 pandas 实现，本模块**刻意用自己的实现**
（逐日回放里每行都要算，纯 Python 更快，也便于证明 PIT）。两处必须能被独立核对，
所以 `tests/test_features_pit_regime.py` 有一条测试把两者在**同一输入**上的输出钉成相等
—— 与 `experiments/metrics.daily_stats` 对 `verify.report._daily` 的处理同一套路。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import ClassVar

from stocklab.data.models import Bar
from stocklab.errors import DegenerateInput

#: 量能 z 的回看窗（交易日）。与 `indicators.vol_ratio` 的长窗同量级（20）。
VOLUME_WINDOW = 20

#: 已实现波动率（RV）的估计窗：`RV = stdev(最近 RV_WINDOW 个对数收益)`。
RV_WINDOW = 20

#: 分位的历史回看窗：当前 RV 在**自身**过去这么多期 RV 中的分位。
RV_LOOKBACK = 250

#: 算出一个 RV 分位所需的最少 K 线数。
#: RV 序列长度 = `len(closes) - RV_WINDOW`，要求 >= `RV_LOOKBACK`
#: → `len(closes) >= RV_LOOKBACK + RV_WINDOW`。**这个数必须实算**，不能口算
#: （ERROR_DIARY 2026-09-15「含 ceil/窗口对齐的算术必须实算」）。
MIN_BARS = RV_LOOKBACK + RV_WINDOW


def usable_until(bars, asof: str) -> list[Bar]:
    """`date <= asof` 的行（**保持入参顺序**）。

    刻意不排序、不去重：`predict.service.load_pit_bars` 保证升序且最后一行是 `asof`，
    `snapshot.usable_bars` 负责乱序/冲突检测。这里再排一次只会**掩盖**上游的乱序，
    那正是 ERROR_DIARY 2026-09-14「宽松回退」要防的。
    """
    return [b for b in bars if b.date <= asof]


def _log_volume(bar: Bar) -> float:
    """一根 K 线的对数成交量。缺失/非正 → `DegenerateInput`（不填 0）。"""
    v = bar.volume
    if v is None:
        raise DegenerateInput(
            f"{bar.code} 在 {bar.date} 的 volume 为 NULL —— 拒绝静默填 0"
            "（那会把「没数据」写成「零成交」，量能特征会整段失真）"
        )
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(v) or v <= 0:
        raise DegenerateInput(
            f"{bar.code} 在 {bar.date} 的 volume={v!r} 非正/非有限 —— 对数不可用，拒绝猜"
        )
    return math.log(float(v))


def volume_z(bars, asof: str, *, window: int = VOLUME_WINDOW) -> float | None:
    """`asof` 当日成交量的 z 分数：`(log v_t − mean(log v)) / stdev(log v)`。

    窗口 = 最近 `window` 根（含 `asof` 当日），**只用 `<= asof` 的行**。
    历史不足 `window` 根、或窗口内对数成交量恒为常数（stdev=0）→ `None`
    （= 「算不出」，由调用方拒绝该行，不是 0）。

    用对数是为了压掉成交量的右偏（放量日动辄 3–5 倍）；这是**变量定义**的一部分，
    不是可调参数。
    """
    usable = usable_until(bars, asof)
    if len(usable) < window:
        return None
    xs = [_log_volume(b) for b in usable[-window:]]
    sd = statistics.stdev(xs)
    if not (sd > 0.0):
        return None
    return (xs[-1] - statistics.fmean(xs)) / sd


def rv_series(bars, asof: str, *, rv_window: int = RV_WINDOW,
              lookback: int = RV_LOOKBACK) -> list[float]:
    """从 `<= asof` 的行算出**递减窗内**的 RV 序列（最后一个 = 截至 `asof`）。

    只取**够算 `lookback` 期分位**的那一段尾窗（`lookback + rv_window + 1` 根收盘），
    所以「某天价格坏了」只会拒绝它真正影响到的那几行，不会因为十年前的脏数据
    把今天的特征整段废掉。

    价格非正 → `DegenerateInput`（对数不可用）。历史不足则返回**短的**序列
    （由 `rv_percentile` 判 `None`）。
    """
    usable = usable_until(bars, asof)
    need = lookback + rv_window + 1
    tail = usable[-need:] if len(usable) > need else usable
    closes = []
    for b in tail:
        c = b.close
        if isinstance(c, bool) or not isinstance(c, (int, float)) \
                or not math.isfinite(c) or c <= 0:
            raise DegenerateInput(
                f"{b.code} 在 {b.date} 的 close={c!r} 非正/非有限 —— 对数收益不可用，拒绝猜"
            )
        closes.append(float(c))
    rets = [math.log(cur / prev) for prev, cur in zip(closes, closes[1:])]
    if len(rets) < rv_window:
        return []
    return [statistics.stdev(rets[i - rv_window + 1:i + 1])
            for i in range(rv_window - 1, len(rets))]


def rv_percentile(bars, asof: str, *, rv_window: int = RV_WINDOW,
                  lookback: int = RV_LOOKBACK) -> float | None:
    """`asof` 的 RV 在**自身**过去 `lookback` 期 RV 中的分位，取值 `(0, 1]`。

    与 `features.indicators.percentile_rank` 同式：`(# 窗口内 ≤ 当前) / 窗口长度`
    （含当期，所以严格递增序列的分位恒为 1.0）。历史不足 → `None`（不是 0/0.5）。

    传入 `sh000300` 的**指数** K 线即得市场级波动率状态 —— 同一个函数，
    区别只在喂给它的序列（指数没有分红送转，用不复权点位就是真实点位）。
    """
    rv = rv_series(bars, asof, rv_window=rv_window, lookback=lookback)
    if len(rv) < lookback:
        return None
    tail = rv[-lookback:]
    return sum(1 for v in tail if v <= tail[-1]) / len(tail)


# ---------- 特征包：spec 的哪个取值需要哪些特征 ----------

class UnknownSigmaMode(ValueError):
    """`sigma_mode` 的取值没有登记对应的特征来源 —— 拒绝静默当成「不需要特征」。"""


@dataclass(frozen=True)
class PitFeatures:
    """一次预测可用的 PIT 状态特征（**数据**，不是配置）。

    三个字段各自可为 `None`（= 该特征在这天算不出来）。**`None` 是「算不出」，
    不是 0**：`predict.model` 见到自己需要的那个字段是 `None` 就抛 `DegenerateInput`，
    该行对变体侧不可评分并在 `skipped` 里计数 —— 不许静默回退到基线口径
    （那会让报告里的样本量悄悄变少而没人知道）。

    为什么是 dataclass 而不是给 `compute_forecast` 加三个可选参数：与 `index_dir`
    同理 —— 它们是**当日的事实**，不是模型配置。放进 `ForecastSpec` 会让
    「改了一个变量」的计数变成 2 个以上，单变量纪律就没法机械校验了。
    """

    volume_z: float | None = None
    rv_pct: float | None = None
    index_rv_pct: float | None = None

    #: `sigma_mode` 的**非基线**取值 → 它需要哪些特征。
    #: 与 `predict.model.SIGMA_MODES` 必须一一对应，有测试钉住（改一处必须改两处）。
    REQUIRED: ClassVar[dict[str, frozenset[str]]] = {
        "vol_z": frozenset({"volume_z"}),
        "rv_pct": frozenset({"rv_pct"}),
        "index_rv_pct": frozenset({"index_rv_pct"}),
    }

    @classmethod
    def required_for(cls, sigma_mode: str) -> frozenset[str]:
        """该 `sigma_mode` 需要哪些特征；`const` → 空集（基线一行都不读）。"""
        if sigma_mode == "const":
            return frozenset()
        try:
            return cls.REQUIRED[sigma_mode]
        except KeyError:
            raise UnknownSigmaMode(
                f"未登记的 sigma_mode={sigma_mode!r}；已登记 {sorted(cls.REQUIRED)}。"
                "拒绝默认「不需要特征」—— 那会让变体静默退化成基线口径"
            ) from None

    def quantile_for(self, sigma_mode: str) -> float | None:
        """把该模式对应的特征映射成 `[0, 1]` 的分位；算不出 → `None`。

        - `vol_z`：z 已经标准化过了，用**标准正态 CDF** 映射到 `(0,1)` ——
          不用任何拟合参数（`clip((z+3)/6, 0, 1)` 那种写法有先验上界，这里是
          分布自身的 CDF，更少自由）。
        - `rv_pct` / `index_rv_pct`：分位本身就是 `(0,1]`，原样透出。
        - `const`：`None`（基线不缩放 sigma）。
        """
        if sigma_mode == "const":
            return None
        if sigma_mode == "vol_z":
            z = self.volume_z
            return None if z is None else float(statistics.NormalDist().cdf(z))
        if sigma_mode == "rv_pct":
            return self.rv_pct
        if sigma_mode == "index_rv_pct":
            return self.index_rv_pct
        raise UnknownSigmaMode(
            f"未登记的 sigma_mode={sigma_mode!r}；拒绝猜一个分位出来"
        )
