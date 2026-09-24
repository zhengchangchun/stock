"""模拟盘口径（P19）：**一处定义，别处引用**，不许在代码里散落字面量。

## 这个模块不是预测模块

生产模型 `pit-rw-v1.0.2` 的方向能力 ≈ 0（区间 2013-12-23 → 2026-09-14：行级命中
37.883%、按日聚类 37.946% ±0.0067（n_days 2660）、Brier 0.66070（行）/ 0.66063（日）
对随机 0.667），**唯一站得住的是 80% 区间校准 81.68%**。
因此模拟盘**禁止做方向择时**：拿「涨的概率 > 跌的概率」当买入信号、
做参数搜索挑「最优权重」、输出买卖建议，三条全禁。
模拟盘对照的是**纪律与分散本身**。实盘样本 LIVE = 0。

`test_paper_never_imports_model_or_kelly` 用源码扫描钉住这条纪律 ——
它不是注释里的君子协定。
"""

from __future__ import annotations

import re

# ---------- 起点 ----------

#: 起跑日（**收盘口径**）。当日的决策只用 ≤ 当日的收盘价（PIT）。
PAPER_START_DATE: str = "2026-09-15"

#: 初始资金（元）。三条臂共用同一个起点口径。
INITIAL_CAPITAL: float = 20_000.0

#: 起跑持仓：美的 100 股 @86.80（用户实盘首笔，见 `real_trades`），
#: 入场费 5.09 元计入现金 —— 现金因此是 20000 − 8680 − 5.09。
HOLD_CODE: str = "000333"
HOLD_QTY: int = 100
HOLD_COST_PRICE: float = 86.80

# ---------- 三臂 ----------

ARM_HOLD: str = "arm-hold"
ARM_NOW: str = "arm-now"
DISCIPLINE_PREFIX: str = "arm-discipline-"

#: arm-discipline 的 **3 档 ETF 目标占比**（%）。并列展示，**不许挑一个当「推荐」**。
#: 三档只差这一个数，其余规则完全相同 —— 单变量原则。
ETF_TRANCHES: tuple[float, ...] = (5.0, 10.0, 15.0)

#: 分散工具**白名单**：红利 ETF(510880) / 沪深300 ETF(510300)，按 code 升序取用。
#: **换家电股（600690）或家电 ETF 不算分散** —— 那不是分散，是加倍下注同一个行业。
ETF_WHITELIST: tuple[str, ...] = ("510300", "510880")

#: 整手 = 100 股/份（沪深两市个股与场内 ETF 同为 100，见 `Instrument.lot`）。
LOT: int = 100

#: 单笔金额目标（元）：凑到接近 1,000 元以摊薄 5 元最低佣金。
#: 1,000 元 × 0.025% = 0.25 元 < 5 元 → 低于此额佣金全是「最低佣金税」。
ORDER_TARGET_AMOUNT: float = 1_000.0

#: 「单次动用现金 ≤ 总资产 5%」的「单次」= **每个 `paper step`（每日）累计**。
#: 这是风险节奏规则：按「单笔订单」解读会被拆单绕过，而风险规则的方向性错误
#: 应当是**少投而非多投**。两种解读在起跑日恰好重合（5% × 20,037.91 ≈ 1,000 元
#: ≈ 一笔最小佣金摊薄单）。见 ADR-010。
PER_STEP_CASH_PCT: float = 5.0

# ---------- 规则条文（写进 paper_trades.rule_citation，可追溯） ----------

RULE_CITATIONS: dict[str, str] = {
    "stop_loss": "止损：000333 收盘价 < 82.14 → 整清（收盘价口径，"
                 "`discipline.PER_CODE_LINES['000333'].stop_loss_close`）",
    "single_max": "单票 ≤40%：超出即减到 ≤40%（整手向下取整；不足 1 手则不动）",
    "etf_first_build": "分散建仓（首次建仓/未达目标）：白名单 510300/510880，"
                       "其余落现金；单次动用现金 ≤5% 总资产、单笔 ≈1,000 元、整手",
}

# ---------- 智能体动态编排臂（P37；设计见 docs/superpowers/specs/2026-09-21-…） ----------

#: 账户 id。`arm-agent` 的条文数字来自台账（`paper_agent_decisions`）里的当前 spec；
#: `arm-agent-random` 是**必需的对照臂**（同预算、同变更空间，但 spec 随机抽）。
#: 阶段 1–2 期间 random 臂不产生任何 spec、也不产生任何成交 —— 这是占位，
#: 不是「随机改没用」的结论。没有它，`arm-agent` 领先静态臂这件事无法归因。
ARM_AGENT: str = "arm-agent"
ARM_AGENT_RANDOM: str = "arm-agent-random"

#: `paper_accounts.params_json` 里的**执行者声明**（P47 / D-35）。
#: 值非空 ⇒ 该账户的日终净值由**外部通路**认领（模块2 通路 A 的 `arm-agent-<版本>`），
#: `paper step` 让出它。见 `engine.external_executor` 与 `m2/config.py`。
#: 键名放在这里而不是 `m2/` 里：`paper/` 不许 import 模块2（方向是 m2 → paper）。
EXECUTOR_KEY: str = "executor"

# ---------- P56：认领关系（D-50） ----------

#: AI 操盘手的日终认领者（P56 / D-50）：账户带这个值 ⇒ `paper step` **让出**它，
#: 日终由 `paper agent run --asof <交易日>` 落（先要决策在台账里，再成交并写净值）。
#: **为什么必须让出**：`paper step` 的幂等判据是「当日净值行存在」，收盘链 15:30
#: 先跑完 ⇒ 之后写进去的决策**永远不会被执行**（同 P47 那条坑：症状出现在净值表上，
#: 排查方向会跑到订单生成上去）。
EXECUTOR_AGENT_DECISION: str = "agent_decision"

#: 通路 A 的认领者。**与 `m2/config.py::EXECUTOR_CHANNEL_A` 同值但不 import**：
#: 方向是 m2 → paper，paper 反向 import 会成环。两边是同一个值由
#: `tests/test_paper_agent_arms.py` **对拍**钉住（与 `SAMPLE_THRESHOLD` 同款手法）。
EXECUTOR_CHANNEL_A: str = "m2_channel_a"

#: `params.executor` 的**全部合法值**。认领关系 fail-closed（P56 §1.7）：
#: 出现这个集合之外的值 ⇒ **点名报错**，不许静默跳过 —— 静默跳过的后果是
#: 那条策略悄悄不下单，而症状只会在净值表上出现。
KNOWN_EXECUTORS: tuple[str, ...] = (EXECUTOR_CHANNEL_A, EXECUTOR_AGENT_DECISION)

# ---------- P56：预注册（D-48） ----------

#: AI 操盘手的**版本账户**前缀（D-48：`arm-agent-<版本>`）。
#: ⚠️ 它与通路 A 的账户同前缀 —— **不是冲突**：两者靠 `params.executor` 区分
#: （通路 A = `m2_channel_a`，AI 操盘手 = `agent_decision`），这正是那个字段存在的理由。
AGENT_ARM_PREFIX: str = f"{ARM_AGENT}-"

#: 版本号的合法形状。与 `m2/config.py::STRATEGY_VERSION_RE` **同形但不 import**
#: （同一个理由：方向不许反过来）；形状一样由对拍测试钉住。
ARM_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

#: 预注册字段（写进 `paper_accounts.params_json`）。**换模型 / 换提示词 = 开新版本账户**，
#: 旧账户保留不删 —— 这条纪律只挡一件事：「换到好看为止」。
PREREGISTERED_KEY: str = "preregistered"
PREREGISTRATION_KEYS: tuple[str, ...] = ("model_id", "prompt_sha256")

#: 决策载荷里回传的 PIT 上下文指纹（D-49）。生成器只从 `paper agent context` 取输入、
#: 把 `context_sha256` 原样回传，`paper agent decide` 落库前**重算比对**，不一致即拒。
CONTEXT_SHA256_KEY: str = "context_sha256"

#: `paper_accounts.arm` 的取值（与 `store/schema.sql` 的 CHECK **同文**，改一处须同步）。
ARM_KIND_HOLD: str = "hold"
ARM_KIND_NOW: str = "now"
ARM_KIND_DISCIPLINE: str = "discipline"
ARM_KIND_AGENT: str = "agent"
ARM_KIND_AGENT_RANDOM: str = "agent_random"

#: 会**跑条文**（即可能下单）的臂。其余（hold / now）只记净值。
#: P52 起 `agent_random` 也在这个集合里 —— 它不再是占位臂，而是真的随机下单
#: （D-19：没有它，`arm-agent` 的读数一律不可归因）。
ARM_KINDS_WITH_RULES: tuple[str, ...] = (ARM_KIND_DISCIPLINE, ARM_KIND_AGENT,
                                        ARM_KIND_AGENT_RANDOM)

#: 自身成交驱动状态的那些臂（`arm-hold` 冻结、`arm-now` 从实盘账本重放）。
#: 与 `ARM_KINDS_WITH_RULES` 是两个问题：这条问「现金/持仓从哪来」，
#: 那条问「会不会下单」。将来若有「只记净值不下单」的臂，两者会分叉。
ARM_KINDS_SELF_DRIVEN: tuple[str, ...] = (ARM_KIND_DISCIPLINE, ARM_KIND_AGENT,
                                          ARM_KIND_AGENT_RANDOM)

#: `arm-agent` 的默认 spec 对齐**这一条**静态臂，默认值一律从它反推
#: （见 `paper/agent_spec.py`，不另抄一份数字）。
AGENT_DEFAULT_ARM: str = f"{DISCIPLINE_PREFIX}10"

#: 每次复审最多试几版（D-17「每次 ≤3 版」）。超预算的记录会被 `record_decision` 拒绝。
MAX_TRIALS_PER_REVIEW: int = 3

# ---------- P52：AI 操盘手（决策台账，D-34） ----------

#: 决策载荷的**唯一形状**（D-34）：每交易日一条。
#: `code` 必须落在当日候选池；`Σ target_weight_pct + cash_pct = 100`；不许负数。
#: `context_sha256`（P56 / D-49）是回传的 PIT 上下文指纹 —— 写入口会**重算比对**。
DECISION_PAYLOAD_KEYS: tuple[str, ...] = (
    "asof", "decisions", "cash_pct", "rationale", "context_sha256")
DECISION_ITEM_KEYS: tuple[str, ...] = (
    "code", "side", "target_weight_pct", "reason")

#: 权重和的容差。取 1e-6：这是**浮点写法的容差**，不是「允许差一点」的额度 ——
#: 载荷里的数字是两位小数，写错一位就是 0.01 级别的差，远在容差之外。
WEIGHT_SUM_TOLERANCE: float = 1e-6

#: A 股**没有做空**。这条不是限制「看空」的表达，而是限制表达的方式：
#: 「看空」只能靠**降低总仓位 / 清仓**来表达（把 target_weight_pct 调低、把
#: `cash_pct` 调高），不能靠 `side="sell"` 一个没持有的标的 —— 那是融券。
#: 这段文案要出现在**报错里**，否则「AI 想卖空」会被读成静默失败。
NO_SHORT_SIDE_MSG: str = (
    "A 股无做空：`side=\"sell\"` 只对**已持有**的标的成立。"
    "看空只能表达为**降低总仓位 / 清仓**（调低 target_weight_pct、调高 cash_pct），"
    "不能对未持有的标的卖出 —— 那是融券，本臂不做"
)

#: 决策台账里 `agent_kind` 的取值：外部编码 agent 产出的决策。
#: 与 `agent_spec.AGENT_KIND_LLM` 同一个字面量（那边是 spec 路径，这边是操盘路径）。
DECISION_AGENT_KIND: str = "llm"

#: 随机对照臂的产出者标识。**不是占位符**：它如实说明这条臂的载荷不是任何模型
#: 产出的，因此「同 prompt + 同 context ⇒ 同结果」这条复现性判据对它天然成立
#: （随机由固定种子决定，见 `agent_decide.random_payload`）。
RANDOM_MODEL_ID: str = "random-control"

#: 随机对照臂**一次抽几只**（含端点）。固定这个区间是为了让「同预算」可比：
#: 换区间 = 换口径，必须与净值一起读。
RANDOM_N_CODES: tuple[int, int] = (1, 4)

#: 随机对照臂的**现金下限**（% 总资产）。**镜像** `m2_a1` v1.0.4 的 `CASH_FLOOR`
#: （`m2/builtin/a1_pick.py`，同为 10.0），为了让两条臂**同护栏**：随机臂的敞口
#: 上界也是 `(100 − 本常量) − 全部存量持仓占比`（口径 v3 / P68）。
#: 两条臂的 docstring 都写着「同护栏、同成本」—— 那个词只能靠**同一个数**兑现，
#: 所以两边是同一个值由 `tests/test_paper_agent_decide.py` **对拍**钉住
#: （不 import `m2`：方向是 m2 → paper，反向 import 会成环）。
#:
#: ⚠️ 数值 10.0 **不动**（P66 定的，P68 也没改）：v3 改的是**扣谁**，不是扣多少 ——
#: `reserved` 从「不在 picks 里的存量」改成「**全部**存量」。口径 v2 只扣了一半：
#: 被 picks 抽中的存量票被当成「一减仓就变成现金」，而执行层是**整手**的
#: （差额 < 1 手 ⇒ `hold`）⇒ 真库 seed 97 仍被 `CashShortfall` 拒（¥657.06）。
RANDOM_CASH_FLOOR: float = 10.0

#: 决策账（`paper_agent_decisions`）里 `decision_kind` 的两个取值。
#: `spec` = P37 的「改纪律数字」（历史行，保留不删）；`portfolio` = P52 的操盘决策。
DECISION_KIND_SPEC: str = "spec"
DECISION_KIND_PORTFOLIO: str = "portfolio"

# ---------- P52：对照臂（D-36 五条 + 随机臂） ----------

#: 对照臂的**展示顺序**（与 D-36 的编号一致）。这不是排名，是阅读顺序。
COMPARISON_ARM_IDS: tuple[str, ...] = (
    ARM_AGENT, ARM_NOW, "fund-equal-weight", ARM_HOLD, "sh000300")
COMPARISON_RANDOM_ARM: str = ARM_AGENT_RANDOM

#: 基金等权臂的**固定清单**（先验选定，见 `stocklab/fund/nav.py` 的模块说明）。
#: ⚠️ 它是**持仓未知的基金组合**，不是指数 —— 页面与报告里都不许写成「指数」。
FUND_EQUAL_WEIGHT_ID: str = "fund-equal-weight"
FUND_EQUAL_WEIGHT_LABEL: str = "真实主动权益基金等权平均"

#: 基金净值源（非官方）。`pingzhongdata/<code>.js` 里的 `Data_netWorthTrend`。
#: 标注口径是**交付物的一部分**：这两个词必须出现在页面上。
FUND_NAV_SOURCE_URL: str = "https://fund.eastmoney.com/pingzhongdata/<code>.js"

#: 样本量门槛（交易日）。与 `verify.report.MIN_DAYS` **同源同值**，但**不 import 它**：
#: `paper/` 的源码护栏禁止依赖验证链路（`tests/test_paper_discipline_guard.py`），
#: 而「两边是同一个 120」由 `tests/test_paper_comparison.py` 直接对拍钉住 ——
#: 靠对拍而不是靠 import，护栏才不用为这个数字开洞。
SAMPLE_THRESHOLD: int = 120

#: 「不可比」的**唯一**措辞：缺数据/不扣成本的行写它，而不是 0。
#: 写 0 会把「没有数据」显示成「那天没涨没跌」，那是把「不知道」当结论。
NOT_COMPARABLE: str = "不可比"
#: ⚠️ 措辞里**只用 Markdown 反引号、不写 HTML 标签**：这段文字同时进
#: Markdown 报告、CLI 的 JSON 与网页。网页上 `rich()` 会把反引号变成真的
#: `<code>`；写成字面 `<code>` 的话，网页会显示「被转义成文本的标签」（踩过）。
FUND_NAV_APPROX_NOTE: str = (
    "近似 / 非官方：净值取自天天基金 `pingzhongdata/{code}.js` 的 "
    "`Data_netWorthTrend`（非官方接口），等权平均；基金**持仓不公开**，"
    "所以只能比净值曲线，不能比持仓、不能改写成指数"
)

#: `arm-agent*` 的条文表。与 `RULE_CITATIONS` **并列而不合并**：静态表是「写死的条文」，
#: 这张表是「同一批被 spec 参数化的规则」，数字来源不同 —— 合成一张表之后，
#: `paper_data.ai_evidence` 就再也数不清「有几笔成交由智能体的 spec 触发」。
#: 这几条**刻意不带数字**（数字进 `reason`），这样它们能按规则类型被稳定计数。
RULE_CITATIONS_AGENT: dict[str, str] = {
    "stop_loss": "智能体 spec·止损：收盘价跌破 spec.stop_loss_pct 推出的线 → 整清"
                 "（参数台账见 paper_agent_decisions）",
    "single_max": "智能体 spec·单票上限：超出 spec.max_single_pct 即减到该上限"
                  "（整手向下取整；不足 1 手则不动）",
    "etf_first_build": "智能体 spec·分散建仓：按 spec.etf_target_pct 建白名单 ETF，"
                       "单次动用现金与现金下限同守",
}

#: `arm-agent*` 在 **P52 操盘口径**下的条文表（与上面那张并列，不合并）。
#: 第三条表而不是往上面加一行，理由与 P37 一样：`ai_evidence` 要能分开数出
#: 「有几笔成交照 spec 下的」与「有几笔成交照当日决策下的」——
#: 合成一张，这两件事就再也分不开了。
#: 数字一律不进条文（数字进 `reason`），这样它们能按规则类型被稳定计数。
RULE_CITATIONS_AGENT_DECISION: dict[str, str] = {
    "target_weight": "AI 操盘手·目标权重：按决策台账里当日那一条的 target_weight_pct "
                     "调仓到目标市值（整手向下取整；池外/越界/负权重在**写入口**即拒）",
    "random_target_weight": "随机对照臂·目标权重：标的与权重由固定种子随机抽取，"
                            "护栏与成本口径与 AI 臂**完全相同**（不构成任何判断）",
}

# ---------- 免责声明（报告里逐字出现，测试钉住） ----------

DISCLAIMER: str = (
    "**模拟盘 ≠ 实盘。** LIVE 仍为 0，样本 <120 交易日不算结论；"
    "本报告不构成买卖建议，也不使用任何模型方向预测"
    "（生产模型方向能力 ≈ 0：`pit-rw-v1.0.2` 行级命中 37.883%、按日聚类 37.946%、"
    "Brier 0.66070；区间 2013-12-23 → 2026-09-14）。"
)

#: 报告/JSON 里必须出现的口径提示（测试逐条断言）。
DISCLOSURE_ITEMS: tuple[str, ...] = (
    "禁止方向择时：不使用模型预测作为买卖信号，不做参数搜索，不输出买卖建议",
    "起点 2026-09-15 收盘 · 初始资金 20,000 元 · 各臂并列（纪律臂 3 档 ETF 占比 + "
    "AI 操盘手及其随机对照；对照另含基金等权与沪深300）",
    "扣成本：佣金（最低 5 元）+ 印花税（ETF 免征）+ 过户费 + 滑点；ETF 按 ADR-008 标的口径",
    "PIT：当日决策只用 ≤ 当日的收盘价，喂未来价直接报错（LookaheadError）",
    "append-only：paper_accounts / paper_trades / paper_nav_daily 只增不改不删",
    "建议接入：`paper step` 加为 15:30 收盘链第 5 步（nanobot 调度，项目内不实现 cron）",
)
