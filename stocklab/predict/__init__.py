"""预测器（P6）：把「每晚一份可证伪、可复现、可回放的预测载荷」做成真命令。

分层（只允许一个方向的依赖）：

    predict/  →  strategies/（注册表 + PIT 白名单视图）
              →  data/（复权读取层 adjust）
              →  calendar/（target_date 的日历来源）
              →  store/（四态写入）

**`predict/` 不 import `features/`** —— 这不是疏漏，是纪律：当前
`regime_label` / `main_net_5d` / `pe_pct_3y` 全为 NULL，一旦预测器能拿到
特征句柄，「NULL 当 0」就会悄悄回来。模型只用复权 K 线的 OHLC。
"""

from stocklab.predict.model import (CONTRACT_FIELDS, DegenerateInput,
                                    canonical_json, compute_forecast,
                                    degenerate_strategy_mix, payload_hash)
from stocklab.predict.version import (BENCHMARK_ONLY_STRATEGIES,
                                      FALSIFIED_STRATEGIES, MODEL_ID,
                                      MODEL_SPEC, MODEL_VERSION)

__all__ = [
    "BENCHMARK_ONLY_STRATEGIES", "CONTRACT_FIELDS", "DegenerateInput",
    "FALSIFIED_STRATEGIES", "MODEL_ID", "MODEL_SPEC", "MODEL_VERSION",
    "canonical_json", "compute_forecast", "degenerate_strategy_mix",
    "payload_hash",
]
