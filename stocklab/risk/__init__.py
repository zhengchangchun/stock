"""风险与仓位工具箱（P14）：凯利 / 分数凯利 / 波动率目标 / 破产风险 / VaR / 止损位。

## 定位（**先读这一句**）

凯利在本项目里的定位是 **风险预算 + 不下注开关**，**不是买点生成器**。

本项目的硬事实：方向预测能力 ≈ 0（`pit-rw-v1.0.1` 行级 38.14%、Brier 0.6581），
趋势状态**不预测方向**（只做波动分层）。所以严格凯利在这里的正确答案
**就是「不下注」**（f* ≤ 0）。程序必须如实输出 `NO_BET`。

## 输入只许来自回放统计

`p` / `b` 的**唯一合法来源**：`--rule` 指定的规则在 PIT 历史数据上的回放统计，
**扣成本**、**按日聚类**、**报样本量**。

❌ 禁止：人工指定 p/b（「假设胜率 55%」）；样本内调参；
为了让输出「有仓位」而换窗口 / 换规则 / 挑 `invalidated` 子群。
"""

from stocklab.risk.kelly import EdgeStats, evaluate, verdict_label
from stocklab.risk.panel import build_risk_block
from stocklab.risk.rules import RULES, RuleReplay, replay
from stocklab.risk.sizing import size_position

__all__ = ["RULES", "EdgeStats", "RuleReplay", "build_risk_block", "evaluate",
           "replay", "size_position", "verdict_label"]
