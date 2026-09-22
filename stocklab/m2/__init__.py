"""模块2：双通路与镜像（P47）。

两条并行通路，用**既有模拟盘引擎**跑出两个可比较的账户：

| 通路 | 输入 | 执行 | 预测 |
|---|---|---|---|
| A（纯模拟） | 候选池（短/中/长） | A1 选股 → 模拟买入 → A2 调仓退出 → 净值 | A3 |
| B（人工 → 镜像） | `real_trades` + `cash_flows`（唯一真源） | 既有 `arm-now` 逐笔复刻 | B1 |

## 为什么本包不在 `stocklab/paper/` 里

`tests/test_paper_discipline_guard.py` 钉住「`paper/` 不 import 选股链路与插桩」——
静态臂的纪律（只做纪律与分散）不因为模块2 开工而作废。通路 A 必须同时碰
候选池（`stocklab.candidate`）与插桩执行器（`stocklab.plugin`），所以它**不能**
住在 `paper/` 里。本包是那条线的落点：**编排在上面，执行口径在下面**
（成交 / 费用 / 整手 / 净值一律由 `paper/` 提供，本包一行都不另写）。

## 不新造任何口径（本包的铁律）

- 成交与费用：`paper/rules.py` 的 `plan_target_weight` / `_fee_parts`；
- 净值与回撤：`paper/engine.py` 的 `_mark_to_market` / `_drawdown` / `arm_state_for`；
- 落库：`paper/store.py` 的 `insert_trade` / `insert_nav`（成交只有一个写入口）；
- PIT：`paper/rules.py::check_no_lookahead`；
- 镜像：既有 `arm-now`（D-25：不新建第二套镜像代码）；
- 预测载荷：`plugin/contract.py` 的 `m2_a3` / `m2_b1` 形状（不另造概率口径）。
"""
