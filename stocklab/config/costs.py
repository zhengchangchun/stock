from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class CostModel:
    """A 股交易成本模型（R4）。所有费率均为小数，滑点为基点。"""

    commission_rate: float = 0.00025    # 佣金 0.025%，双边
    min_commission: float = 5.0         # 单笔最低佣金（元）
    stamp_tax_rate: float = 0.0005      # 印花税 0.05%，仅卖出
    transfer_fee_rate: float = 0.00001  # 过户费 0.001%，双边
    slippage_bps: float = 5.0           # 滑点 5 个基点，买入上滑 / 卖出下滑

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
