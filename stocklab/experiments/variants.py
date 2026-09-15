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


def load_index_direction(conn: sqlite3.Connection, asof: str, *,
                         symbol: str = INDEX_300_SYMBOL,
                         cache=None) -> int | None:
    """从库里取 `sh000300` 在 `asof` 的方向（**只读**，不复权口径）。

    指数没有分红送转，`adj_mode='none'` 就是它的真实点位（与
    `verify.service.index_pct_for`、`backtest.benchmark` 同一口径）。
    缺数据返回 `None` —— 由调用方决定拒绝（**不许**在这里回退成 0）。
    """
    if cache is not None:
        bars, _suspended = cache.raw_bars(conn, symbol)
    else:
        from stocklab.predict.service import _read_raw

        bars, _suspended = _read_raw(conn, symbol)
    return index_direction(bars, asof)
