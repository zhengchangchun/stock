"""`m2_b1`（人工镜像持仓收益预测）**源文本**。

逻辑与 `m2_a3` **逐字相同**（同一份 `_BODY` 渲染），只差版本串 —— 这正是
D-30 的可比性要求：A3 预测的是通路 A 的模拟持仓、B1 预测的是通路 B 的人工
镜像持仓，两份载荷放进同一张表、同一个校验口径，形状与语义漂一个字就不再可比。

`SOURCE` 从 `a3_forecast` 取，**不复制一份逻辑**：两份手写的逻辑迟早会漂，
而漂移是静默的（测试只会在归一比对的那一处报红，不会告诉你哪一侧是「对」的）。
"""

from stocklab.m2.builtin.a3_forecast import SCHEMA_VERSION, source_for

#: `m2_b1` 的初始版本源文本（与 A3 同逻辑、同形状版本）。
SOURCE: str = source_for(SCHEMA_VERSION)
