"""标准化残差的**经验分布**（P10-a）：用残差池的形状替换高斯解析分位。

## 它替换的是什么

基线 `pit-rw-v1.0.1` 把次日收盘收益假设成 `Normal(mu, sigma)`，于是

- `range_80` 的两端是解析分位 `mu ± Φ⁻¹(0.90)·sigma`；
- `direction` 的三分类概率是解析 CDF `Φ((log(1±FLAT_BAND) − mu)/sigma)`。

这个**形状假设**（对称、尾部按 `exp(−z²/2)` 衰减）从未被检验过。本模块把形状来源
换成**训练窗内**标准化残差 `(r_t − mu_hat_t)/sigma_hat_t` 的**经验分布** ——
一个纯粹的 ECDF，没有拟合参数、没有平滑、没有插值。

## 为什么是一个对象而不是两个

次日的 `direction` 概率与 `range_80` 是**同一个分布**的 CDF 与分位数。
只换其中一个会让两者互相矛盾（CDF 与分位不再是同一个分布的），
那不是一次单变量实验，而是两个自相矛盾的半成品。所以本模块只暴露
`cdf` / `quantile` 一对互逆函数，两者由**同一份残差池**定义。

## 为什么分位数非对称

偏斜的残差池给出不同的左右分位 —— 这正是本实验相对「重跑缩放」的**全部区别**。
实现里没有任何对称化步骤（有测试专门钉住偏斜池必须给出非对称分位）。

## 依赖

**只用标准库**（`bisect` / `math` / `dataclasses`）。刻意不 import `predict.service`：
`predict.model` 需要本模块的类型，而 `predict.service` 需要 `predict.model` ——
一旦本模块依赖 service，就会形成 `model → residual → service → model` 的 import 环。

## 门槛

`RESIDUAL_MIN_SAMPLES` 是**结构性守卫**（池子太小 → 0.10/0.90 分位纯噪声 →
直接拒绝），不是可调参数，也不参与任何判定阈值。实测训练池约 3900 个残差，
离门槛很远。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

#: 分位估计所需的最小残差数。低于它 → 拒绝构造（不是「警告后照算」）。
#: 0.10 / 0.90 分位在 `n` 个点上估计，其位置误差量级 `1/√n`；
#: 250 个点是「分位还站得住」的下限，先验固定、不随样本调整。
RESIDUAL_MIN_SAMPLES = 250


class InsufficientResiduals(ValueError):
    """残差池为空或少于 `RESIDUAL_MIN_SAMPLES` —— 分位不可估计，拒绝构造。

    刻意是一个**构造期**异常：拿到一个「池子太小但形状看着还行」的分布，
    会一路安静地走到报告里，变成一条没人知道它在噪声上的结论。
    """


@dataclass(frozen=True)
class ResidualDistribution:
    """训练窗标准化残差的经验分布（**升序**元组 + 溯源字段）。

    字段全部是**数据**（形状的由来），不是配置：`values` 由
    `experiments.residuals.fit_residual_distribution` 在 **train 段** 上算出，
    apply 到 validate / test。本类不做任何裁剪、不读任何别的数据。

    `n_days` / `n_skipped` / `codes` / `first_day` / `last_day` / `window`
    只作**溯源**（进 `as_evidence()`），一个都不参与 `cdf` / `quantile` 的计算
    —— 「这个形状是用多少样本、哪一段、哪几个标的拟合的」必须能被审计者读出来，
    但它不能反过来影响数字（那会让分布偷偷依赖样本量）。
    """

    values: tuple[float, ...]
    codes: tuple[str, ...]
    first_day: str
    last_day: str
    n_days: int
    n_skipped: int
    window: int

    def __post_init__(self) -> None:
        n = len(self.values)
        if n < RESIDUAL_MIN_SAMPLES:
            raise InsufficientResiduals(
                f"残差池只有 {n} 个样本，少于门槛 {RESIDUAL_MIN_SAMPLES} —— "
                "0.10 / 0.90 分位在这个样本量上不可估计。**拒绝**构造一个"
                "「看起来正常、其实全是噪声」的分布"
            )
        for i, v in enumerate(self.values):
            if not isinstance(v, float) or not math.isfinite(v):
                raise ValueError(
                    f"values[{i}]={v!r} 不是有限浮点数 —— 残差池坏了，拒绝猜"
                )
            if i and v < self.values[i - 1]:
                raise ValueError(
                    f"values 必须升序：values[{i}]={v!r} < values[{i - 1}]="
                    f"{self.values[i - 1]!r} —— 未排序的池子会让 cdf 静默错掉"
                    "（二分查找假设有序），必须由调用方排好再传进来"
                )

    @property
    def n(self) -> int:
        return len(self.values)

    def cdf(self, z: float) -> float:
        """`F(z) = #{v <= z} / n`（右连续的阶梯函数，值域 `[0, 1]`）。

        用 `bisect_right`（**含等于**）而不是 `bisect_left`：`F` 必须是
        「小于等于」的累积比例，否则 `p_flat = F(z_hi) − F(z_lo)` 在
        `z_lo == z_hi`（收益带退化）时可能变成负数，而概率恒非负是硬约束。
        """
        return bisect.bisect_right(self.values, z) / self.n

    def quantile(self, p: float) -> float:
        """`Q(p) = min{v : F(v) >= p}` —— `cdf` 的逆（含并列值时取并列组的最大者）。

        `p <= 0` 取最小值、`p >= 1` 取最大值：`Q` 在边界上**饱和**而不是越界，
        因为「取 0.10 分位」这件事在 p 稍越界时仍然是同一个问题。
        """
        if not (isinstance(p, float) or isinstance(p, int)) or not math.isfinite(p):
            raise ValueError(f"分位 {p!r} 非法 —— 拒绝猜")
        if p <= 0.0:
            return self.values[0]
        if p >= 1.0:
            return self.values[-1]
        k = max(0, math.ceil(p * self.n) - 1)
        return self.values[k]

    def as_evidence(self) -> dict:
        """落进报告 `evidence` 的**溯源 + 形状摘要**（纯 JSON 可序列化）。

        三个分位与 `cdf` / `quantile` 同源（有测试把 `ev["q_10"]` 与
        `quantile(0.10)` 钉成相等）—— 审计者能拿这一块自己复算实验里的每个数字。
        """
        return {
            "n": self.n,
            "n_days": self.n_days,
            "n_skipped": self.n_skipped,
            "codes": list(self.codes),
            "first_day": self.first_day,
            "last_day": self.last_day,
            "window": self.window,
            "q_10": self.quantile(0.10),
            "q_50": self.quantile(0.50),
            "q_90": self.quantile(0.90),
            "mean": math.fsum(self.values) / self.n,
            "definition": (
                "训练窗（strictly before validate）内标准化残差 "
                "(r_t − mu_hat_t)/sigma_hat_t 的 ECDF；"
                "F(z)=#{v<=z}/n、Q(p)=min{v:F(v)>=p}，**无拟合参数、无平滑、无插值**，"
                "左右分位各自取值（非对称是这条轴要检验的东西）"
            ),
        }
