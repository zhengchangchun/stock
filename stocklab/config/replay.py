"""回放引擎配置常量（中性位置，避免 candidate/ 与 plugin/ 互相 import）。

`REBALANCE_DAYS` 从这里取，不要从 `stocklab.plugin.sandbox` 取。
"""

#: 各候选池调仓周期（单位：交易日）。
REBALANCE_DAYS: dict[str, int] = {"short": 5, "mid": 20, "long": 60}
