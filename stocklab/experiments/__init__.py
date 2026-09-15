"""单变量实验流水线（P8）：把「猜一个改动 → 看是否真有效」变成有纪律的过程。

四个模块，各管一件事：

- `variants`  变体**是什么**（显式 spec + 注册表 + 单变量校验 + PIT 指数方向）
- `split`     三段日期切分（train / validate / test）
- `metrics`   口径冻结（`METRIC_VERSION`）+ 配对日差 + 晋级判定
- `runner`    把上面三件拼起来跑一遍，复用 P7 的打分与报告代码路径

## 这个包**不写任何生产表**

`predictions` / `verifications` 是 append-only 的，且 `model_version` 是预测唯一键的一列。
实验变体全程在内存里算、在内存里评，一行都不落库 —— 见
`docs/plans/2026-09-15-p8-实验流水线.md` §1.1。
"""

from __future__ import annotations

from stocklab.experiments.metrics import METRIC_VERSION

__all__ = ["METRIC_VERSION"]
