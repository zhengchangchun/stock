"""模块2 通路口径（P47 / D-35）——**一处定义，别处引用**。

## 通路 A 的账户命名（D-35 已拍板）

`arm-agent-<策略版本>`。「策略版本」= 这一版策略的**插桩组合**
（`plugin_audit` 里那几版 `m2_a1`/`m2_a2`/`m2_a3`），不是 `MODEL_VERSION` ——
后者是 `predict` 管线的版本，红线基线按它分列，拿它当插桩版本号会污染分列。

每个策略版本 = **一个 `paper_accounts` 行 + 独立 NAV**（D-26），不共享现金池。

## 为什么账户行要带 `executor`

`paper step` 会遍历**所有**账户。通路 A 的账户如果也被它认领，就会出现
「`paper step` 先写了一条没有任何成交的净值 → 通路 A 再跑时发现当日净值已存在
→ 整条策略永远不下单」。这不是假设：`step` 的幂等判据就是
`paper_nav_daily` 那一行的存在性。

所以通路 A 的账户行在 `params_json` 里显式声明 `executor`，
`paper/engine.py::external_executor` 据此**让出**它的日终认领权 ——
「谁落这一天的净值」是一个显式字段，不是靠账户名的前缀猜。
"""

from __future__ import annotations

import re

from stocklab.paper.config import ARM_AGENT, ARM_NOW

# ---------- 通路 ----------

CHANNEL_A: str = "A"
CHANNEL_B: str = "B"
CHANNELS: tuple[str, ...] = (CHANNEL_A, CHANNEL_B)

# ---------- 账户 ----------

#: 通路 A 的账户前缀（D-35：`arm-agent-<策略版本>`，不新开第三套命名）。
ACCOUNT_PREFIX: str = f"{ARM_AGENT}-"

#: 通路 B 的镜像账户 = **既有 `arm-now`**（D-25：复用实现，不新建第二套）。
MIRROR_ACCOUNT: str = ARM_NOW

#: 账户行 `params_json` 里的执行者声明（见模块 docstring）。
EXECUTOR_KEY: str = "executor"
EXECUTOR_CHANNEL_A: str = "m2_channel_a"

#: 策略版本的合法形状：**保守窄口径**（小写字母数字开头，只允许 `. _ -`）。
#: 放宽它等于允许把 `strategy_version` 写成任意串，而它会进账户 id、进
#: `params_json`、进报告 —— 口径标识符必须可读、可比较、不许带空格。
STRATEGY_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


class StrategyVersionError(ValueError):
    """策略版本号不合形状。**拒绝，不 sanitize** —— 悄悄替换字符会让
    「报告里的版本」与「台账里的版本」变成两个东西。"""


def account_id_for(strategy_version: str) -> str:
    """策略版本 → 账户 id（`arm-agent-<版本>`）。形状不对直接拒绝。"""
    if not STRATEGY_VERSION_RE.match(str(strategy_version)):
        raise StrategyVersionError(
            f"策略版本 {strategy_version!r} 不合形状 "
            f"{STRATEGY_VERSION_RE.pattern}（小写字母数字开头，只允许 . _ -，≤32 字符）"
            "—— 拒绝，不做字符替换（替换会让报告与台账对不上号）")
    return f"{ACCOUNT_PREFIX}{strategy_version}"


# ---------- 决策频率（D-34 / P37 同源） ----------

#: **每交易日一次**。与 `arm-agent` 的「每交易日一条操盘决策」同一个心跳 ——
#: 两条 AI 臂用不同频率的话，「谁跑赢了」里就混着「谁动得多」。
#: 非交易日不出决策（走 T7 的「跳过并留痕」）。
DECISION_CADENCE: str = "per_trading_day"

# ---------- 四支插桩（D-33 / P45 已在 SHAPES 里登记） ----------

PLUGIN_A1: str = "m2_a1"        # AI 模拟选股：池内二次选股 + 权重
PLUGIN_A2: str = "m2_a2"        # AI 模拟卖出条件：止盈止损 / 调仓退出
PLUGIN_A3: str = "m2_a3"        # AI 持仓收益预测
PLUGIN_B1: str = "m2_b1"        # 人工镜像持仓收益预测

CHANNEL_PLUGINS: dict[str, tuple[str, ...]] = {
    CHANNEL_A: (PLUGIN_A1, PLUGIN_A2, PLUGIN_A3),
    CHANNEL_B: (PLUGIN_B1,),
}

#: 预测口径的落库表（§6.1 的归属规则：模块2 的预测**不进** `predictions`）。
TABLE_RUNS: str = "m2_channel_runs"
TABLE_FORECASTS: str = "m2_forecasts"

# ---------- 运行状态 ----------

STATUS_RAN: str = "ran"
STATUS_SKIPPED: str = "skipped"
STATUS_REJECTED: str = "rejected"

#: 幂等重放的返回值（**不是**台账里的状态，故不落库）。见 `m2/store.py` 的说明。
STATUS_ALREADY: str = "already"

#: 跳过的理由码。**与拒绝分开**：跳过等数据补齐后重跑即可，拒绝要人去看脚本
#: 或账户状态。合成一个「没跑成」会让「等一天」与「出事了」在读数上长得一样。
SKIP_NO_BARS: str = "no_bars"


class ChannelSkip(Exception):
    """**可自愈**的中止：数据不齐（缺当日 K 线 / 非交易日）。

    与 `ChannelReject` 分开：这一类的处置是「等数据补齐后重跑这一天」，
    通路**没有**产出任何成交或净值（事务已回滚）。
    """

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


class ChannelReject(Exception):
    """**要人看**的中止：插桩缺失/越界、账户状态异常。

    与 `ChannelSkip` 的区别不是「严重程度」，而是**谁的责任**：跳过是数据的，
    拒绝是脚本或调用的。两者的留痕文案也因此必须不同（否则运维会去补数据，
    而真正的问题在脚本里）。
    """

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)
