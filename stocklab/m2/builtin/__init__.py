"""模块2 四支插桩脚本的**源文本**（`m2_a1` / `m2_a2` / `m2_a3` / `m2_b1`，D-33）。

形状照 `stocklab/candidate/builtin/`：每个模块一个 `SOURCE: str` 常量，
这里给出 `plugin_id → 初始版本源文本` 的映射。可执行版本来自**数据库**
（`plugin_scripts.source_text`，经 `plugin submit` → 沙盒 → 人工 `approve`），
本目录只是「首次提交用的那一版」的真源。

## 四支**不占**既有 0–5 编号

0–5 的语义已被模块1 钉住（需求 01 的插桩清单），混用会让审计对不上号
（`plugin/contract.py::M2_PLUGIN_IDS` 的原话）。

## 首版（v1.0.0）是「接线用的朴素实现」，不是策略

A1 用模块1 的成品分数 `adj_score` 做池内二次选股；A2 是三条固定规则
（止损/止盈/调仓退出）；A3/B1 是模块1 同款的对数收益正态分位口径。
上限（单票 25% / 5 只 / 现金 10%）与阈值（−8% / +20% / 20 根 K 线）
**写死在源文本里** —— 首版刻意不做成读配置（可配置的上限会让
「今天为什么只买了 3 只」变成一句没人能复现的话）。

**未经验证**：这四支的样本外表现尚未经过 walk-forward（首版提交时沙盒给
`INCONCLUSIVE`，见 P57 任务书 §1.6 与 §8 的口径问题）。
"""

from stocklab.m2.builtin import (a1_pick, a2_sell, a3_forecast, b1_forecast)

#: `plugin_id` → 初始版本源文本。
BUILTIN_PLUGINS: dict[str, str] = {
    "m2_a1": a1_pick.SOURCE,
    "m2_a2": a2_sell.SOURCE,
    "m2_a3": a3_forecast.SOURCE,
    "m2_b1": b1_forecast.SOURCE,
}
