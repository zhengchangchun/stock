"""变体注册表（P8）：相对基线 `pit-rw-v1.0.1` **只改一个变量**的命名配置。

## 三条结构性保证（靠代码，不靠自觉）

1. **显式配置**：变体 = 一个 `ForecastSpec`（frozen dataclass）。
   没有环境变量、没有模块级开关、没有「全局模式」——`runner` 只能把 spec
   **按调用传进** `compute_forecast`。所以「这次跑的是哪个口径」永远能从参数读出来。
2. **单变量可机械校验**：`assert_single_variable()` 数出 spec 相对基线改掉的字段数，
   必须**恰好 1**，且等于 `Variant.changed_axis` 声明的那一个。
   一次改两个变量的实验**跑不起来**，而不是「跑起来了但没法归因」。
3. **口径冻结**：`ForecastSpec` 里根本没有标签带/窗口/标的集合/区间这些字段，
   所以「为了让数字好看把 ±0.5% 改成 ±1%」在结构上不可能 ——
   没有那个旋钮，比「记得别动它」强。

## 变体 ≠ 模型版本

变体只是**实验**，不是模型。跑完变体**不改变** `MODEL_VERSION`、不写生产表。
只有 `promoted` 的变体才允许另开一条 ADR 与新 `model_version`（那是下一次决策的事）。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from stocklab.data.models import Bar
from stocklab.features.pit_regime import rv_percentile
from stocklab.predict.model import FLAT_BAND, ForecastSpec

#: 指数基准的代码（与 `backtest.benchmark` 同一口径：指数走不复权点位）。
from stocklab.backtest.benchmark import INDEX_300_SYMBOL


class UnknownVariant(KeyError):
    """注册表里没有这个变体名。

    刻意报错而不是「回退到基线」：静默回退会让一次「实验」跑出基线的数字，
    然后被当成变体的成绩上报 —— 这正是本包要防的那类自欺。
    """


class MultiVariableVariant(ValueError):
    """一个变体相对基线改了不止一个变量（或声明与实际不符）—— 无效实验，拒绝执行。"""


@dataclass(frozen=True)
class Variant:
    """一个**具名**的单变量变体。

    `changed_axis` 必须等于 `spec.changed_fields()` 里唯一的那个字段名。
    两者不一致 = 有人改了 spec 却没改声明（或反之），必须当场炸掉。
    """

    name: str
    changed_axis: str
    spec: ForecastSpec
    hypothesis: str          # 预注册假设（报告里原样打印）
    prereg_doc: str          # 预注册文件路径（先写文件、后跑命令）

    @property
    def model_tag(self) -> str:
        """报告分组用的标签。带 `+` 前缀，一眼看出**不是**已发布的 model_version。"""
        return f"pit-rw-v1.0.1+{self.name}"


def assert_single_variable(variant: Variant) -> None:
    """校验「只改一个变量」。**不是**警告，是硬拒绝。"""
    changed = variant.spec.changed_fields()
    if len(changed) != 1:
        raise MultiVariableVariant(
            f"变体 {variant.name!r} 相对基线改了 {len(changed)} 个字段 {changed} —— "
            "一次实验只允许改一个变量，否则无法归因（总纲 R7）"
        )
    if changed[0] != variant.changed_axis:
        raise MultiVariableVariant(
            f"变体 {variant.name!r} 声明改的是 {variant.changed_axis!r}，"
            f"实际改的是 {changed[0]!r} —— 声明与实际不符，拒绝执行（否则台账会写错归因）"
        )


#: 变体注册表。新增变体 = 只改一个变量 + 先写预注册文件 + 在这里加一条。
VARIANTS: dict[str, Variant] = {
    "rw-mu0": Variant(
        name="rw-mu0",
        changed_axis="mu_mode",
        spec=ForecastSpec(mu_mode="zero"),
        hypothesis=(
            "基线把最近 60 日对数收益均值当作次日漂移项；而 `drift_t` 显示它的 t 值很小、"
            "基本是噪声。**假设**：把这个噪声项置 0 后，方向准确率与校准度都不劣于基线。"
        ),
        prereg_doc="docs/experiments/2026-09-15-rw-mu0.md",
    ),
    "index-mom-dir": Variant(
        name="index-mom-dir",
        changed_axis="mu_mode",
        spec=ForecastSpec(mu_mode="index_sign"),
        hypothesis=(
            "个股自身的 60 日漂移几乎没有方向信息（Brier 0.658 ≈ 随机 0.667），"
            "而 `sh000300` 当日方向本身作为「预测」有 51.95% 的准确率。**假设**："
            "把漂移项的**方向来源**换成指数前一日方向（幅度仍用 |mu_sample|），"
            "方向准确率与校准度都优于基线。"
        ),
        prereg_doc="docs/experiments/2026-09-15-index-mom-dir.md",
    ),
    # ---- P9-a 第一轮：把三条**尚未用过**的 PIT 信息接进 sigma ----
    # 三条都改 `sigma_mode`（同一个字段的三个取值），所以每个变体相对基线
    # **恰好改 1 个字段**，单变量纪律没有任何松动；它们彼此是三次独立比较，
    # 多重比较的处理写死在各自台账的「§0.3 多重比较声明」里。
    "sigma-vol-z": Variant(
        name="sigma-vol-z",
        changed_axis="sigma_mode",
        spec=ForecastSpec(sigma_mode="vol_z"),
        hypothesis=(
            "基线的 sigma 是最近 60 日对数收益的**无条件**标准差，它把「今天放量还是缩量」"
            "这件事平均掉了。而成交量与波动率同向（mixture-of-distributions）是"
            "最稳健的经验事实之一。**假设**：用 `q = Φ(量能 z)` 缩放 sigma"
            "（`factor = 0.5 + 1.0×q`，先验固定、不拟合）能改善样本外**校准度**，"
            "方向准确率不劣化。"
        ),
        prereg_doc="docs/experiments/2026-09-15-sigma-vol-z.md",
    ),
    "sigma-rv-pct": Variant(
        name="sigma-rv-pct",
        changed_axis="sigma_mode",
        spec=ForecastSpec(sigma_mode="rv_pct"),
        hypothesis=(
            "波动率聚集意味着「20 日已实现波动率在**自身**过去 250 日 RV 中的分位」"
            "携带了 60 日无条件标准差丢掉的状态信息。**假设**：用该分位缩放 sigma"
            "（`factor = 0.5 + 1.0×q`）能改善样本外**校准度**（校准度是这条信息"
            "最直接的落点），方向准确率不劣化。"
        ),
        prereg_doc="docs/experiments/2026-09-15-sigma-rv-pct.md",
    ),
    "sigma-index-rv-pct": Variant(
        name="sigma-index-rv-pct",
        changed_axis="sigma_mode",
        spec=ForecastSpec(sigma_mode="index_rv_pct"),
        hypothesis=(
            "市场级（`sh000300`）的波动率状态是**不依赖个股自身历史**的第二条来源，"
            "它可能包含个股 20 日 RV 尚未反映的共同波动。**假设**：用指数 RV 分位"
            "缩放 sigma 也能改善样本外校准度，但其增量应当**小于** `sigma-rv-pct`"
            "（个股自身的 RV 已经含了大部分市场成分）。注意这里只取**波动率**，"
            "不取方向 —— 指数方向那条路已由 `index-mom-dir` 否证。"
        ),
        prereg_doc="docs/experiments/2026-09-15-sigma-index-rv-pct.md",
    ),
    # ---- P10-a：第三条轴（预测分布的**形状来源**）----
    # 与上面三条刻意不同：那三条缩放 `sigma`（已被一致否证），这一条不动 `sigma`，
    # 改的是「形状从哪里来」。每个变体相对基线仍然**恰好改 1 个字段**。
    "residual-quantile-interval": Variant(
        name="residual-quantile-interval",
        changed_axis="dist_mode",
        spec=ForecastSpec(dist_mode="resid_emp"),
        hypothesis=(
            "基线把次日收盘收益假设成 `Normal(mu, sigma)`，`range_80` 与三分类概率"
            "全是解析分位 —— 这个**形状假设**（对称、尾部按 exp(−z²/2) 衰减）从未被"
            "检验过。**假设**：把形状来源换成**训练窗内**标准化残差 "
            "`(r_t − mu_hat_t)/sigma_hat_t` 的经验分位（分位只由 train 段拟合，"
            "apply 到 validate），能改善样本外 **Brier（校准度）**，"
            "且 `range_80` 覆盖率不劣化。"
        ),
        prereg_doc="docs/experiments/2026-09-15-residual-quantile-interval.md",
    ),
}


def get_variant(name: str) -> Variant:
    """按名取变体并**当场**做单变量校验（不合格的变体进不了流水线）。"""
    try:
        v = VARIANTS[name]
    except KeyError:
        raise UnknownVariant(
            f"未注册的变体 {name!r}；已注册：{sorted(VARIANTS)}。"
            "拒绝回退到基线口径（那会让实验报告写出基线的数字）"
        ) from None
    assert_single_variable(v)
    return v


# ---------- PIT 指数方向 ----------

def index_direction(bars: Sequence[Bar], asof: str,
                    flat_band: float = FLAT_BAND) -> int | None:
    """`sh000300` 在 `asof` 当日的涨跌方向：`+1` / `-1` / `0`；数据不足或缺口 → `None`。

    **PIT 硬约束**：只允许用 `date <= asof` 的指数行（本函数第一行就把
    `> asof` 的行全部丢掉）。这不是「记得裁」，是**函数签名决定了拿不到未来**：
    调用方传进来的是全量序列，裁剪发生在这里。

    刻意要求**最后一根正好是 `asof`**：指数在 `asof` 没有 K 线时返回 `None`
    （缺口），而不是「取最近一根」。后者会拿**别的交易日**冒充今天 ——
    数字合法、结论全错（ERROR_DIARY 2026-09-14「宽松回退」同款）。

    `flat_band` 默认就是 `predict.model.FLAT_BAND`（±0.5%），与打分口径**同源**；
    形参存在只为让「口径漂移」这件事可被测到（见 `tests/test_experiments_variants.py`）。
    """
    hist = [b for b in bars if b.date <= asof]
    if len(hist) < 2:
        return None
    prev, cur = hist[-2], hist[-1]
    if cur.date != asof:                 # 指数在 asof 当天没有数据 → 缺口，不猜
        return None
    if prev.close <= 0 or cur.close <= 0:  # 负价/零价：指数序列坏了
        return None
    r = math.log(cur.close / prev.close)
    if r > flat_band:
        return 1
    if r < -flat_band:
        return -1
    return 0


def load_index_bars(conn: sqlite3.Connection, *, symbol: str = INDEX_300_SYMBOL,
                    cache=None) -> list[Bar]:
    """取 `sh000300` 的**全量**不复权 K 线（**只读**）。

    指数没有分红送转，`adj_mode='none'` 就是它的真实点位（与
    `verify.service.index_pct_for`、`backtest.benchmark` 同一口径）。
    读取走 `PitCache.raw_bars`（同一场实验里所有 `asof` 共用一次读）。

    刻意返回**全量**而不是裁剪后的：裁剪是各特征函数自己的第一件事，
    「调用方已经裁好了」这种约定一旦有人忘，前视就静默发生了。
    """
    if cache is not None:
        bars, _suspended = cache.raw_bars(conn, symbol)
    else:
        from stocklab.predict.service import _read_raw

        bars, _suspended = _read_raw(conn, symbol)
    return list(bars)


def load_index_direction(conn: sqlite3.Connection, asof: str, *,
                         symbol: str = INDEX_300_SYMBOL,
                         cache=None) -> int | None:
    """从库里取 `sh000300` 在 `asof` 的方向（**只读**）。

    缺数据返回 `None` —— 由调用方决定拒绝（**不许**在这里回退成 0）。
    """
    return index_direction(load_index_bars(conn, symbol=symbol, cache=cache), asof)


def load_index_rv_percentile(conn: sqlite3.Connection, asof: str, *,
                             symbol: str = INDEX_300_SYMBOL,
                             cache=None) -> float | None:
    """`sh000300` 在 `asof` 的 20 日已实现波动率分位（PIT，只用 `<= asof` 的行）。

    这是**市场级波动率状态**，与 `load_index_direction` 是两件事：
    后者取**方向**（已被 `index-mom-dir` 否证），这里只取**波动率高低**。
    历史不足返回 `None`（不是 0.5）—— 由调用方拒绝。
    """
    return rv_percentile(load_index_bars(conn, symbol=symbol, cache=cache), asof)
