"""模块2 主干硬约束常量（D-27 / D-28）——**唯一真源**。

## 为什么这些数字必须集中且不可覆盖

需求书 §1 与 05 §业务边界 3 把「熔断阈值 / 自评估边界」定为**主干常量**：
AI 不可改，只能由用户在对话里拍板后人工改。落成模块级标量 + 本模块内的
校验函数，配合 `tests/test_p44_constants_guard.py` 的三条源码扫描，
使「改它」在结构中只有一条路径 —— **改这个文件的源码**。

## 越界一律拒绝，不 clamp

D-28 原文：「AI 给的周期不满足 → **拒绝并要求重新生成**（不许 clamp）」。
clamp 会把「一个不合规的方案」悄悄变成一个合规的方案，实验因此无法归因
（与 ERROR_DIARY「参数静默夹紧」同源）。所以这里的函数只抛错、不返回值。
"""

from __future__ import annotations

#: 熔断阈值（D-4 / D-27）：验证期内账户净值自峰值回撤 ≥ 10% → 本轮策略失效。
CIRCUIT_BREAKER_DRAWDOWN: float = 0.10

#: 自评估验证周期的轮次边界（需求 02 §5 / D-28）。
VALIDATION_ROUNDS_MIN: int = 2
VALIDATION_ROUNDS_MAX: int = 3

#: 单轮验证周期的最大天数（需求 02 §5 / D-28）。
VALIDATION_MAX_DAYS: int = 30

#: 策略冻结的最长天数（需求 02 §5 / D-28）。
FREEZE_MAX_DAYS: int = 90

#: **强制归档**的连续优化失败次数阈值（需求 00 §全局硬性约束 6 / 06 §D-6；P87 的 D5）：
#: 「同一策略版本连续 3 次优化验证失败，强制归档，不再迭代」。
#: 它是**读出口的判据**（`m2/lifecycle.py` 从 append-only 的判定台账 fold 出连续失败
#: 次数），不新增任何列、不改 `branch` 的取值域 —— 归档动作仍走人工 `approve`
#: （D-1 / D-24），本常量只决定「提示提不提」。
STRATEGY_FAIL_STREAK_LIMIT: int = 3

#: 单次插桩调用的**峰值增量**上限（P93，P61 §0.7 的 G1 最小档）。
#: 超了 ⇒ 该次调用结果**作废**（`plugin/runtime.py` 抛 `PluginResourceError`）
#: 并留一条 `plugin_resource_events`。**不做中途 kill** —— 单次 C 调用不可中断
#: （P61 §0.6 实测），所以本档只声称「判废 ＋ 留痕 ＋ 后续调用被挡」。
#: 与 `CIRCUIT_BREAKER_DRAWDOWN` 同级：AI 不可改、不做参数搜索、不自动调参。
PLUGIN_CALL_RSS_LIMIT_BYTES: int = 512 * 1024 ** 2

#: 插桩进程**已用峰值**上限（P93，P61 §0.7 的 G1 最小档）：超了**连脚本都不执行**。
#: 为什么是 1.5GB 而不是 G1 判据的 2GB：**要在被打死之前先停** —— 2GB 是 P61 §0.7
#: 的**事后**判据（进程已被顶到那么高、再去看日志有没有 `MemoryError`），
#: 本常量是**事前**闸门，两者不是一回事。同样 AI 不可改。
PLUGIN_PROCESS_RSS_LIMIT_BYTES: int = 1536 * 1024 ** 2


class PlanOutOfBounds(ValueError):
    """AI / 调用方给的周期或冻结方案不满足主干边界。**不 clamp**，直接拒绝。"""


def check_validation_plan(*, rounds: int, days: int) -> None:
    """校验验证周期方案；不满足边界抛 `PlanOutOfBounds`（不返回值、不改参数）。

    `rounds` 允许闭区间 `[VALIDATION_ROUNDS_MIN, VALIDATION_ROUNDS_MAX]`；
    `days` 允许 `[1, VALIDATION_MAX_DAYS]`（下限 1：0 天的周期不构成一轮验证）。
    """
    if not (VALIDATION_ROUNDS_MIN <= rounds <= VALIDATION_ROUNDS_MAX):
        raise PlanOutOfBounds(
            f"验证轮次 {rounds} 不在 [{VALIDATION_ROUNDS_MIN}, "
            f"{VALIDATION_ROUNDS_MAX}] 内 —— 拒绝，请重新生成方案（不做 clamp）")
    if not (1 <= days <= VALIDATION_MAX_DAYS):
        raise PlanOutOfBounds(
            f"验证天数 {days} 不在 [1, {VALIDATION_MAX_DAYS}] 内 —— "
            "拒绝，请重新生成方案（不做 clamp）")


def check_freeze_days(*, days: int) -> None:
    """校验冻结天数；越界抛 `PlanOutOfBounds`（不 clamp）。"""
    if not (0 <= days <= FREEZE_MAX_DAYS):
        raise PlanOutOfBounds(
            f"冻结天数 {days} 不在 [0, {FREEZE_MAX_DAYS}] 内 —— "
            "拒绝，请重新生成方案（不做 clamp）")
