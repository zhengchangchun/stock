"""跨层共享的异常 —— 本模块**不 import 任何项目模块**，所以它谁都能依赖。

## 为什么 `DegenerateInput` 从这里搬出来

它原本定义在 `stocklab/predict/model.py`。P9-a 起 `stocklab/features/pit_regime.py`
也要在「输入退化」时**硬拒绝**（`volume` 缺失 / 非正 / 价格非正），而
`predict.model` 反过来要 import `features.pit_regime`（用它的 `PitFeatures` 类型）——
异常留在 predict 就会形成 import 环。

搬到这个中立位置后，`predict.model` 原样再导出（`from stocklab.errors import
DegenerateInput`），既有的 `from stocklab.predict.model import DegenerateInput`
一行都不用改（`tests/test_predict_model.py` 里有一条测试钉住两者是**同一个对象**，
不是两个长得很像的类 —— 后者会让 `except` 静默失效）。
"""

from __future__ import annotations


class DegenerateInput(ValueError):
    """输入不足以给出分布 —— **拒绝**，绝不编一个默认预测。

    触发情形（每一种都对应一条已知的数据/口径陷阱）：
      - 当日无 K 线（停牌 / 采集缺口）→ 拿「最近一根」当今日 = 用昨天决定今天；
      - 历史不足 `WINDOW + 1` 根 → 估计量不可计算；
      - `sigma == 0`（价格恒定）→ 分布退化，`range_80` 宽度为 0 是个假区间；
      - （P9-a）变体所需 PIT 特征缺失：`volume` 为 NULL、量能/波动率历史不足、
        指数在 `asof` 没有 K 线 —— 一律拒绝，**不许**静默当成 0/1 或回退到基线口径。
    """
