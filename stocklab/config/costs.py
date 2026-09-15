"""交易成本模型（R4）：按**标的口径**计价。

## 为什么成本要分口径（P17 / ADR-008）

ETF 与股票在**税费**上不同口径，混用会把成本算错，而错的方向**对策略有利**
（成本算低了 → 回测虚高 → 选中一个实盘不成立的策略）。所以口径必须显式。
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

Side = Literal["buy", "sell"]

ASSET_STOCK: str = "stock"
ASSET_ETF: str = "etf"

#: 卖出印花税（按标的口径）。ETF **免征** —— 财税[2008]2 号对证券投资基金
#: （含场内 ETF）暂不征收印花税。
STAMP_TAX_BY_ASSET: Mapping[str, float] = MappingProxyType({
    ASSET_STOCK: 0.0005,
    ASSET_ETF: 0.0,
})

#: 过户费（按标的口径）。**本模型采用的口径**：过户费是股票**过户登记**环节的费用，
#: 场内 ETF 交易的是基金份额、不走股票过户登记，故按 0 计。
#: 这是一项**建模约定**（不是像印花税那样的法定免征），因此：
#:   - 它是**默认值**，可以被显式 `transfer_fee_rate=` 覆盖；
#:   - 若将来券商交割单显示 ETF 确实收了这项，改这张表即可，不必动 `fees()`。
TRANSFER_FEE_BY_ASSET: Mapping[str, float] = MappingProxyType({
    ASSET_STOCK: 0.00001,
    ASSET_ETF: 0.0,
})


@dataclass(frozen=True)
class CostModel:
    """A 股 / 场内 ETF 交易成本模型（R4）。所有费率均为小数，滑点为基点。

    **口径按 `asset_class` 取默认值**：`stamp_tax_rate` / `transfer_fee_rate` 传 `None`
    表示「按标的口径」（见两张 `*_BY_ASSET` 表），传具体数值则**显式覆盖**。
    这样「按口径」是默认行为而不是硬编码 —— 回测要压零成本时仍可逐项传 0。

    两个口径的差异（P17 / T3）：

    ==================  ==========  ==========
    项目                 stock       etf
    ==================  ==========  ==========
    佣金                 0.025%      0.025%（**相同**）
    最低佣金             5 元        5 元（**相同**）
    印花税（**仅卖出**）  0.05%       **0**
    过户费（双边）       0.001%      0（建模约定，见上表注释）
    滑点                 5 bps       5 bps（相同）
    ==================  ==========  ==========

    `asset_class` 未知 → **抛 `ValueError`**，不静默退化成股票费率：
    口径错是静默错误，代价是「回测看着更好」，必须响。
    """

    asset_class: str = ASSET_STOCK
    commission_rate: float = 0.00025    # 佣金 0.025%，双边（ETF 同）
    min_commission: float = 5.0         # 单笔最低佣金（元），双边（ETF 同）
    #: `None` → 按 `asset_class` 取（`STAMP_TAX_BY_ASSET`），仅卖出
    stamp_tax_rate: float | None = None
    #: `None` → 按 `asset_class` 取（`TRANSFER_FEE_BY_ASSET`），双边
    transfer_fee_rate: float | None = None
    slippage_bps: float = 5.0           # 滑点 5 个基点，买入上滑 / 卖出下滑

    def __post_init__(self) -> None:
        if self.asset_class not in STAMP_TAX_BY_ASSET:
            raise ValueError(
                f"未知标的口径 asset_class={self.asset_class!r}；"
                f"已知：{sorted(STAMP_TAX_BY_ASSET)} —— 口径未知时不得退化成股票费率"
            )
        # frozen=True：只能绕开 __setattr__ 写入，且必须在任何读取之前完成，
        # 否则 `fees()` 里会拿到 None 并静默算成 0（正是本模块要防的静默错）。
        if self.stamp_tax_rate is None:
            object.__setattr__(
                self, "stamp_tax_rate", STAMP_TAX_BY_ASSET[self.asset_class])
        if self.transfer_fee_rate is None:
            object.__setattr__(
                self, "transfer_fee_rate", TRANSFER_FEE_BY_ASSET[self.asset_class])

    def fill_price(self, side: Side, ref_price: float) -> float:
        sign = 1.0 if side == "buy" else -1.0
        return ref_price * (1.0 + sign * self.slippage_bps / 10_000.0)

    def fees(self, side: Side, price: float, qty: int) -> float:
        amount = price * qty
        commission = max(amount * self.commission_rate, self.min_commission)
        transfer = amount * self.transfer_fee_rate
        stamp = amount * self.stamp_tax_rate if side == "sell" else 0.0
        return round(commission + transfer + stamp, 2)

    def total(self, side: Side, ref_price: float, qty: int) -> tuple[float, float]:
        """返回 (实际成交价, 费用)。"""
        price = self.fill_price(side, ref_price)
        return price, self.fees(side, price, qty)
