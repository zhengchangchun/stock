"""全链路准确率视图（P26）。

`stocklab.chain.accuracy` 把「股票分析 → 购进方案 → 模拟持仓 → 实际交易 → 持有分析」
这条链上**已经存在**的真实数据，按**来源**分成四段并排列出来。

包内不含任何写入路径：本模块只读库、只产报告。
"""

from __future__ import annotations

__all__ = ["accuracy"]
