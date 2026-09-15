"""三段日期切分（P8）：train / validate / test。

## 为什么「参数无关的模型」还要切三段

`pit-rw-v1.0.1` 是纯 PIT、零拟合的，所以**每一个交易日都是样本外**。
切分在这里的作用**不是**划出训练/测试的边界，而是防一件具体的事：

> **不许用 test 的数据去挑变体。**

如果只有一个「样本外」区间，那么「跑一个变体 → 看结果 → 觉得不行 → 再跑一个」
这个过程本身就已经在**对那一段数据过拟合**了，而它看起来完全合法
（「我只是看了样本外成绩而已」）。把最后一段**封存**起来、规定「只在晋级评审时打开一次」，
才能让「预注册的判据」真的约束得住人。

`train` 段因此有两个身份：它是**调试区**（可以随便看，但不许据此下结论），
也是**历史储备**（模型需要 61 根 K 线才能出预测，评分区间之前必须有历史）。

## 切分规则

按**日期**在评分日轴上连续切（不是按行切）：`train` 在最前、`test` 在最后。
比例写死在 `SplitConfig` 默认值里，边界（首日/末日/天数）全部落进报告 ——
读者能自己核对「test 到底是哪一段」，而不是信作者一句话。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

#: 三段的固定顺序（报告与判定都按它）。
SPLIT_NAMES: tuple[str, ...] = ("train", "validate", "test")

#: 允许被用作**选择依据**的 split。`test` 不在其中 —— 它是封存段。
SELECTION_SPLITS: tuple[str, ...] = ("train", "validate")


class SplitConfigError(ValueError):
    """切分配置不合法（比例之和不为 1、或有非正的比例）。"""


@dataclass(frozen=True)
class SplitConfig:
    """三段比例。默认 60% / 20% / 20%。

    比例写进报告，**不许事后挪**：调比例就是调「哪一段数据参与判定」，
    属于改口径一类（总纲 R7 反过拟合红线）。
    """

    train: float = 0.60
    validate: float = 0.20
    test: float = 0.20

    def __post_init__(self) -> None:
        for name in SPLIT_NAMES:
            if getattr(self, name) <= 0:
                raise SplitConfigError(
                    f"{name}={getattr(self, name)!r} 必须为正 —— "
                    "空的一段会让「没跑过」看起来像「跑过了」"
                )
        total = self.train + self.validate + self.test
        if abs(total - 1.0) > 1e-9:
            raise SplitConfigError(f"三段比例之和为 {total!r}，必须为 1.0")

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in SPLIT_NAMES}


def split_days(days: Sequence[str], config: SplitConfig | None = None
               ) -> dict[str, list[str]]:
    """把**已排序**的交易日序列按日期连续切成三段。

    返回 `{"train": [...], "validate": [...], "test": [...]}`，三段**互不重叠**、
    并集 = 输入（每个交易日恰好属于一段）。最后一段吸收取整余数，
    所以不会出现「有几天谁都不要」的情况。
    """
    cfg = config or SplitConfig()
    ordered = list(days)
    n = len(ordered)
    if n < len(SPLIT_NAMES):
        raise SplitConfigError(
            f"只有 {n} 个交易日，不足以切成 {len(SPLIT_NAMES)} 段 —— "
            "拒绝产出「某一段是空的」的报告"
        )
    i1 = int(n * cfg.train)
    i2 = i1 + int(n * cfg.validate)
    seg = {"train": ordered[:i1],
           "validate": ordered[i1:i2],
           "test": ordered[i2:]}
    # 空的一段必须当场炸掉：`summarize([])` 会产出一份「0 个交易日」的报告，
    # 而 `gate` 只会说「样本不足」—— 于是「这一段根本没跑」和「这一段跑了但样本少」
    # 长得一模一样（ERROR_DIARY：「为空时是『没有』还是『没填』？」）。
    empty = [name for name in SPLIT_NAMES if not seg[name]]
    if empty:
        raise SplitConfigError(
            f"{n} 个交易日按 {cfg.as_dict()} 切分后 {empty} 段为空 —— "
            "拒绝产出「某一段没跑」的实验报告"
        )
    return seg


def boundaries(segments: Mapping[str, Sequence[str]]) -> dict[str, dict]:
    """每段的首日 / 末日 / 交易日数（报告里原样打印）。"""
    out: dict[str, dict] = {}
    for name in SPLIT_NAMES:
        seg = list(segments.get(name, ()))
        out[name] = {
            "first_date": seg[0] if seg else None,
            "last_date": seg[-1] if seg else None,
            "n_days": len(seg),
        }
    return out
