"""模拟盘口径（P19）：**一处定义，别处引用**，不许在代码里散落字面量。

## 这个模块不是预测模块

生产模型 `pit-rw-v1.0.1` 的方向能力 ≈ 0（行级命中 38.14%、按日聚类
0.3819±0.0070、Brier 0.6581 对随机 0.667），**唯一站得住的是 80% 区间校准 81.68%**。
因此模拟盘**禁止做方向择时**：拿「涨的概率 > 跌的概率」当买入信号、
做参数搜索挑「最优权重」、输出买卖建议，三条全禁。
模拟盘对照的是**纪律与分散本身**。实盘样本 LIVE = 0。

`test_paper_never_imports_model_or_kelly` 用源码扫描钉住这条纪律 ——
它不是注释里的君子协定。
"""

from __future__ import annotations

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

#: `paper_accounts.arm` 的取值（与 `store/schema.sql` 的 CHECK **同文**，改一处须同步）。
ARM_KIND_HOLD: str = "hold"
ARM_KIND_NOW: str = "now"
ARM_KIND_DISCIPLINE: str = "discipline"
ARM_KIND_AGENT: str = "agent"
ARM_KIND_AGENT_RANDOM: str = "agent_random"

#: 会**跑条文**（即可能下单）的臂。其余（hold / now / agent_random）只记净值。
ARM_KINDS_WITH_RULES: tuple[str, ...] = (ARM_KIND_DISCIPLINE, ARM_KIND_AGENT)

#: `arm-agent` 的默认 spec 对齐**这一条**静态臂，默认值一律从它反推
#: （见 `paper/agent_spec.py`，不另抄一份数字）。
AGENT_DEFAULT_ARM: str = f"{DISCIPLINE_PREFIX}10"

#: 每次复审最多试几版（D-17「每次 ≤3 版」）。超预算的记录会被 `record_decision` 拒绝。
MAX_TRIALS_PER_REVIEW: int = 3

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

# ---------- 免责声明（报告里逐字出现，测试钉住） ----------

DISCLAIMER: str = (
    "**模拟盘 ≠ 实盘。** LIVE 仍为 0，样本 <120 交易日不算结论；"
    "本报告不构成买卖建议，也不使用任何模型方向预测"
    "（生产模型方向能力 ≈ 0：行级命中 38.14%、按日聚类 0.3819、Brier 0.6581）。"
)

#: 报告/JSON 里必须出现的口径提示（测试逐条断言）。
DISCLOSURE_ITEMS: tuple[str, ...] = (
    "禁止方向择时：不使用模型预测作为买卖信号，不做参数搜索，不输出买卖建议",
    "起点 2026-09-15 收盘 · 初始资金 20,000 元 · 三臂并行（纪律臂 3 档 ETF 占比并列）",
    "扣成本：佣金（最低 5 元）+ 印花税（ETF 免征）+ 过户费 + 滑点；ETF 按 ADR-008 标的口径",
    "PIT：当日决策只用 ≤ 当日的收盘价，喂未来价直接报错（LookaheadError）",
    "append-only：paper_accounts / paper_trades / paper_nav_daily 只增不改不删",
    "建议接入：`paper step` 加为 15:30 收盘链第 5 步（nanobot 调度，项目内不实现 cron）",
)
