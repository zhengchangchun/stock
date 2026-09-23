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
from stocklab.paper.engine import INDEX_300_SYMBOL

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

#: 预测的事后校验分数（P48）。append-only；幂等键 = 一条预测一行分数。
TABLE_SCORES: str = "m2_forecast_scores"

# ---------- 三方对标的基准（D-43 / P55） ----------

#: `sh000905`（中证500）的符号。沪深300 的**真源**是 `paper/engine.py::INDEX_300_SYMBOL`
#: （本模块顶部 import，不另写字面量 —— 写两遍就会在改名那天漂开）。
INDEX_500_SYMBOL: str = "sh000905"

#: 基准指数（P55 起**两个**）。D-43 拍板：沪深300 必做；`sh000905`（中证500）
#: 在能证明「同一采集路径落进 `bars_daily`、同一 `adj_mode='none'` 口径」后才加。
#: P55 的只读探针拿到了（43 行 / `adj_mode='none'`，见任务书 §实施记录 T1）⇒ 接入。
#:
#: **顺序即展示顺序**：渲染层按 `role` 排序而 `sorted` 是稳定的，所以这里的
#: 先后就是表里的先后。两个基准**各自一行**，不许相减/取平均/合成「综合基准」。
BENCHMARKS: tuple[str, ...] = (INDEX_300_SYMBOL, INDEX_500_SYMBOL)

#: 基准的中文名（标签用：「市场基准（沪深300 指数）」）。
#: **不是**从 `instruments.name` 读的：指数行可能根本没 `ingest` 过（那时表里
#: 没有这一行），而标签仍要能写出来 —— 标签是口径的一部分，不是数据的投影。
BENCHMARK_NAMES: dict[str, str] = {
    INDEX_300_SYMBOL: "沪深300",
    INDEX_500_SYMBOL: "中证500",
}

#: 本轮**未接入**的基准（D-43 要求「明文写清并列出代价」，不许悄悄少一个）。
#: 取数层会检查它是否**已经**落进 `bars_daily` —— 一旦落进去了就必须显式接入，
#: 而不是继续显示「未接入」（否则那句说明会在数据到位后变成假话）。
#:
#: **P55 起为空**：`sh000905` 已按 D-43 的前置条件接进 `BENCHMARKS`。
#: 这个元组与它的渲染/待办机制**保留**（不是死代码）：下一个候选指数仍然要
#: 走「缺席 ⇒ 明文代价 / 已落库但未接入 ⇒ 显式待办」同一条路。
DEFERRED_BENCHMARKS: tuple[dict[str, str], ...] = ()

# ---------- 预测校验（P48 §2） ----------

#: 预测载荷自带 `direction=None`（插桩明说「不知道」，见 `na_reasons`）时用这个原因码。
#: **不是**数据缺口（那几种沿用 `verify/score.py` 的原因码），而是「这条预测本身
#: 没有方向可说」—— 两类不可评分必须分得开：一个要等行情，一个要人去看插桩。
REASON_NO_DIRECTION: str = "NO_DIRECTION"

#: 错判案例集的**条数上限**。**不是可选参数** —— P48 §2 明令「没有『挑好看的 /
#: 最有利的』筛选参数」，所以它只能是一个常量：要列就按时间倒序取最近 N 条。
CASE_LIMIT: int = 50

# ---------- 自评估三分支与熔断（P49 / D-44 / D-27） ----------

#: 三分支的枚举值。**与 `schema.sql` 的 `m2_judgements.branch` CHECK 逐字相同**
#: （由 `tests/test_p49_selfeval.py` 钉住）—— 枚举字符串漂一个字，判定行就写不进去。
BRANCH_FREEZE: str = "freeze"        # 读数未达标且无可归因方向 ⇒ 停用该版本（≤90 天）
BRANCH_OPTIMIZE: str = "optimize"    # 有方向且需改脚本逻辑 ⇒ 新 script 版本 + 新账户
BRANCH_TUNE: str = "tune"            # 只动参数、不动逻辑 ⇒ 同版本新参数集 + **新周期**
#: **不是第四个分支**，而是「判不了」：样本不足 120 交易日时只给读数（CLAUDE.md 度量纪律 3）。
#: 它必须与三个分支同为**枚举值**而不是 `None` —— 否则「没判定」与「判定为冻结」
#: 在库里的形状会撞在一起。
BRANCH_INSUFFICIENT: str = "insufficient"

BRANCHES: tuple[str, ...] = (BRANCH_FREEZE, BRANCH_OPTIMIZE, BRANCH_TUNE,
                             BRANCH_INSUFFICIENT)

BRANCH_LABELS: dict[str, str] = {
    BRANCH_FREEZE: "冻结",
    BRANCH_OPTIMIZE: "优化",
    BRANCH_TUNE: "微调",
    BRANCH_INSUFFICIENT: "证据不足",
}

#: 调用方声明的**改进方向性质**。三分支里唯一无法从读数推出的一格：
#: 「要不要改脚本逻辑」是**提案的属性**，不是数据的属性（D-44 ②/③）。
#: `none` = 声明「没有可归因的方向」—— 冻结的前提。
FIX_NONE: str = "none"
FIX_LOGIC: str = "logic"
FIX_PARAMS: str = "params"
FIX_KINDS: tuple[str, ...] = (FIX_NONE, FIX_LOGIC, FIX_PARAMS)

#: 熔断判据**原文**（D-27）。落进 `validation_events.criteria_text` —— 事后换口径
#: 是这一站的典型作弊方式，所以判据在这里只写一次，写库时**照抄**这一个常量。
CIRCUIT_CRITERIA_TEXT: str = (
    "验证期内账户净值自峰值回撤 ≥ CIRCUIT_BREAKER_DRAWDOWN（0.10）⇒ "
    "立即标记本轮策略失效、终止本轮验证（D-27 / D-4）")

#: 判定台账表名（P49 新增）。归 `m2/` 家族，因为存的是 m2 的建议而不是验证台账事实。
TABLE_JUDGEMENTS: str = "m2_judgements"

#: 待复核清单（P49 §3）里**恒空**的归因字段名 —— 结构位留给 P50 的人工确认（D-31）。
#: 只读汇总里出现这个键、值恒 `None`，是为了让「忘了填」与「不许填」在形状上分得开。
ATTRIBUTION_FIELD: str = "attribution"

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
