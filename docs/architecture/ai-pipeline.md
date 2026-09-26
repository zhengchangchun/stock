# AI 操盘全流程（一张图走完）

> 日期：2026-09-25
> 触发：用户问「你的股票准确率怎么样」「把 AI 整个选股流程发给我」
> 本文是**现状快照**，写的是「今天真在跑什么」。凡与代码不符的，以代码为准。
> 相关：`layers.md`（分层）、`plugin-candidate.md`（插桩与候选池）、`docs/plans/2026-09-22-模块2-需求书.md`（需求侧）

## 0. 一句话

**没有任何大模型在预测股价。** 系统的「AI」有两个不同的东西，别混：

| 名字 | 是什么 | 在哪里 | 干什么 |
|---|---|---|---|
| **生产模型** `pit-rw-v1.0.2` | 手写统计式（PIT 随机游走 + 实测波动率），零 ML 依赖 | `stocklab/predict/` | 出「方向 / 区间 / 关键位」三件事的读数，**不选股** |
| **插桩（plugin）** `m2_a1/a2/a3/b1` | 存在数据库里的 Python 脚本，`approve` 后上线 | `plugin_scripts.source_text` | A1 选股、A2 卖出、A3/B1 预测 —— **这才是「选股」** |
| **外部编码 agent**（Claude Code / DeepSeek） | 写插桩脚本的人 | 项目**外** | 产出脚本版本；项目内只做校验/落库/执行/计账 |

「AI 当操盘手接受考核」指最后一条：脚本由 AI 产出、人工 `approve` 上线，再和静态纪律臂、真实操盘对照。

## 1. 全流程总图

```
                       ┌─────────────────────────── 时钟：macOS launchd ───────────────────────────┐
                       │  close   每交易日 15:30      ops close     （13 步收盘链）                  │
                       │  patrol  工作日 09:00–15:30   ops patrol --fix（每 30 分钟，60 槽）         │
                       │  monthly 每月 1 日 08:00      ops monthly    （长窗采集 + 复盘）             │
                       └────────────────────────────────────┬──────────────────────────────────────┘
                                                            │  ① 只看时钟，项目内无常驻进程
   ┌────────────────────────────────────────────────────────▼────────────────────────────────────────┐
   │ ① 数据层  ingest（唯一写入口）                                                                     │
   │    index/sh000300 + index/sh000905 → bars(--days 30) → actions → valuation → moneyflow             │
   │    质量闸门（ohlc_inconsistent 等 → 整批拒写）· 复权链 adj_factors · 宇宙投影 universe_memberships  │
   └────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                        │ ②
   ┌────────────────────────────────────▼──────────────────────────────────────────────────────────────┐
   │ ② 特征 / 复权读取层  data/adjust.py（唯一价格出口）→ features_daily                                 │
   └────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                        │ ③
   ┌────────────────────────────────────▼──────────────────────────────────────────────────────────────┐
   │ ③ 候选池  candidate run（12 步主干，顺序不可改）                                                    │
   │    排雷(3) → 插桩0 行业排雷(4) → 淘汰入库(5) → 三池分流 short/mid/long(6)                          │
   │    → 插桩1/2/3 打分(7) → 插桩4 风险调整(8) → 入库(9) → 快照(10) → 报告(11) → 触发判断(12)          │
   └────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                        │ ④
   ┌────────────────────────────────────▼──────────────────────────────────────────────────────────────┐
   │ ④ 生产模型  predict run --asof <今天>   （pit-rw-v1.0.2：方向 / 区间 / 关键位）                     │
   │    → verify pending（到期打分）→ review daily（滚动准确率，按 LIVE/REPLAY × model_version 分桶）    │
   └────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                        │ ⑤
   ┌────────────────────────────────────▼──────────────────────────────────────────────────────────────┐
   │ ⑤ 模块2 操盘  m2 daily（链上只一步，遍历在 CLI 里）                                                 │
   │    通路 B（镜像 arm-now，人工流水复刻）                                                             │
   │    通路 A（每个「在飞版本」跑一遍）：                                                               │
   │        A1 选股（池内二次选） → 主干翻译成订单并成交 → A2 卖出指令 → 结算 → A3 逐持仓预测             │
   │    打分 m2 score（对 target_date ≤ asof 且未打分的）                                                │
   └────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                        │ ⑥
   ┌────────────────────────────────────▼──────────────────────────────────────────────────────────────┐
   │ ⑥ 账本 / 对照  paper_nav_daily（每账户每日一行）· paper_trades（唯一写入口）                         │
   │    对照臂：arm-hold / arm-now / arm-discipline-05·10·15 / arm-agent(-v1, -ds-v1, -ds-v2)            │
   │            / arm-agent-random（归因必需：没有它一切结论「不可归因」）                                │
   └────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                        │ ⑦
   ┌────────────────────────────────────▼──────────────────────────────────────────────────────────────┐
   │ ⑦ 页面  /lab/  总览 · 模拟盘对照 · 模块2 · 候选池 · 定时任务 · 成交流水 · 现金流 · 风险 · 数据 · 健康 │
   └───────────────────────────────────────────────────────────────────────────────────────────────────┘
```

侧环（不在收盘链上，按需/月度）：

```
   实验流水线 experiments/      预注册 → 单变量变体 → 台账 append-only（9 个变体全部被否证）
   插桩生命周期 plugin/        submit → sandbox 契约预检 → 人工 approve → active → （新版）archive
   红线校验 scripts/verify.sh  三目标 sha 逐位比对 + 库指纹（只读、确定性）
```

## 2. 逐段说明

### ① 时钟：launchd（ADR-020）

| 任务 | 时机 | 命令 | 语义 |
|---|---|---|---|
| `close` | 每交易日 15:30 | `ops close` | 14 步收盘链，见下表 |
| `patrol` | 工作日 09:00–15:30 每 30 min | `ops patrol --fix` | 退出码 0 全绿 / 1 有异常 / 2 判不了或断链 |
| `monthly` | 每月 1 日 08:00 | `ops monthly` | 6 步：`calendar holidays fetch → ingest bars --days 12000 → ingest index sh000905 → ingest financials → candidate review → doctor` |

`RunAtLoad=False`；日志 `data/logs/<job>.{out,err}.log`；回执 `reports/ops/latest-<job>.json`。
**采集类命令不接受 `--db`**（固定写 `paths.DB_PATH`）。

`ops close` 的 14 步（`ops/chain.py::CLOSE_STEPS`，顺序静态）：

```
ingest_index → ingest_index_500 → ingest_bars → ingest_actions → ingest_valuation
→ ingest_moneyflow → session_tick → session_backfill_close → predict_run
→ verify_pending → review_daily → paper_step → m2_daily → doctor
```

两个刻意的闸门：
- **收盘前拒绝执行**（exit 2）——否则会写半截 bar + LIVE 预测，而预测表 append-only 退不回来。
- **K 线定型闸门**：`asof == 今天` 时要求当天 `bars_daily.fetched_at ≥ 15:00`（`session/close.py::bars_finalized_on`，不读墙上时钟）。

### ② 数据层

- 唯一写入口 `data/ingest.py`；历史 `NULL` 保持 `NULL`（语义是「当日未采集」，不是 0、不插值）。
- **复权**：`adj_factors` 链是价格口径的唯一来源；`data/adjust.py` 是唯一价格出口（`load_bars_adjusted` / `load_chain`）。
  单条脏条款（如 `600602`「10派100元」）会抛 `UnpriceableTerms`，**按 code 隔离**，不拖垮整批（P75/P76）。
- **ETF 无复权链**（ADR-008）⇒ `load_chain` 抛 `EtfChainUnsupported`，调用方按 `skipped` 处理（P76）。
- 宇宙：`instruments`（805 只）＋ `universe_memberships`（800）＋ `active=1` 只 21 只种子 = **日更口径**。
  取研究池必须显式 `--universe csi300-500`，**fail-closed，不隐式回退**（P72）。

### ③ 候选池（`candidate run`）

12 步主干（设计文档 §7，AI 不可改流转）；幂等键 `(asof, run_kind)`，`run_kind ∈ {light, weekly, quarterly}`。

- 三池 `short / mid / long` ↔ 打分插桩 `1 / 2 / 3`；行业排雷插桩 `0`；风险调整插桩 `4`。
- **三池不是互斥划分**：同一 code 可同时在多池（真库 19 个槽只对应 11 只）⇒ 下游必须去重。
- 缺任一 active 插桩 ⇒ `NoActivePlugin`，**不留半截快照**（快照在一个事务边界内写）。

### ④ 生产模型与验证

`predict run --asof <今天>` 落 17 条 `pit-rw-v1.0.2` 预测 → `verify pending` 到期打分 → `review daily` 出报告。

- 准确率**必须分两个维度读**：`LIVE`（当天产生）/ `REPLAY`（历史回放）× `model_version`。
  两桶**不可相加、不可互相顶替**；混算得到的数字**既不属于这一版也不属于那一版**（ERROR_DIARY #48）。
- 报告里没写 `created_at == asof_date` 的行，**不得**被称为「实盘表现」。

### ⑤ 模块2：AI 操盘手

`m2 daily --asof <今天>` 一个入口跑三件事（`stocklab/m2/daily.py`）：

| 步 | 内容 | 何时跑 |
|---|---|---|
| `channel_b` | 镜像 `arm-now`（人工流水复刻成净值） | **总是**，与人当天有没有下单无关 |
| `channel_a` | 每个**在飞版本**跑一遍 A1→成交→A2→A3 | 账户存在且验证周期不在飞 ⇒ 记 `skipped`，不是失败 |
| `score` | `m2 score`，对 `target_date ≤ asof` 且未打分的 | **总是** |

「在飞版本」判据只有两条：账户行存在（`params.executor == m2_channel_a`）**且**最近一轮验证周期没被熔断/没冻结。**不加第三条** —— 多一条静默排除规则，「今天为什么没跑」就在回执里找不到答案。

通路 A 主干（`m2/channel_a.py`，顺序由主干定，插桩只填内容）：

```
1 主干  只取 ≤ asof 的 PIT 快照（持仓 / 收盘价 / 候选池 / 特征）
2 m2_a1 池内二次选股 → {picks, cash_pct}（只产出，不落库）
3 主干  按权重翻译成订单并成交（费用/整手/滑点全由 paper/rules.py 提供）
4 m2_a2 对【成交后】的持仓给卖出指令（止盈止损 / 调仓退出）
5 主干  结算 A2 的卖出 → 现金 / 持仓 / 净值 / 交易日志
6 m2_a3 逐持仓标的做收益预测 → m2_forecasts（append-only）
```

A1 当前口径（v1.0.4）：`reserved = Σ 全部存量持仓占比`（不因 picks 减免）→
`Σw ≤ (100 − CASH_FLOOR) − reserved`、`w = min(CAP_PCT, 该上限 / n)` 等权、单票 ≤25%、持仓 ≤5 只、现金 ≥10%、整手。

**资金闸门在执行层**：`paper/agent_decide.py::CashShortfall`（fail-closed、具名 `code="cash"`、整轮拒绝零写入）。
脚本自己的口径就算再错一次，执行层也会整轮拒绝，而不是透支（P65/P68，ERROR_DIARY #73/#75）。

**输入侧契约**（`paper/agent_context.py::build_decision_context`）：顶层键 = `arm / asof / account / marks /`
`index_300 / pool / tradability / guardrails / disclosure / non_goals / counter_arm`，其中 11 个进指纹
`decision_context_sha256`。**`tradability`（P79）** 是「一手够不够」的输入侧表达：
`{lot, min_weight_pct, by_code{one_lot_cost, min_weight_pct}}`，键集 = `marks ∩ pool`，一手成本的唯一公式是
`rules.one_lot_cost`（复用 `CostModel.fill_price` ＋ `_fee_parts`）。⚠️ 它进指纹 ⇒ **同一输入在 P79 前后的
`decision_context_sha256` 不同**，跨该版本的指纹/决策行不可直接比（旧行 append-only 不改写）。

**`tradability` 的 P81 增量（D1，只增键）**：逐只增 `tradable` / `untradable_reason`，
顶层增 `n_tradable` / `n_untradable` / `min_tradable_weight_pct` / `cheapest`。
判据**只有一处实现**：`paper/engine.py::tradable_verdict`（`tradable = affordable_lots >= 1`），
账户读数 `account_view.buying_power.by_code` 与这里调的是**同一个函数**（两处各写一份 ⇒ D-37 那类
「同一页两个数」）。`untradable_reason` 把两种成因分开并各点名两个数：
「一手 ¥X 已超过总资产 ¥Y」（这个账户无解）／「现金 ¥X 不足一手 ¥Y（先卖出其他标的可释放现金）」。
⚠️ 它是「**现金口径的今天**」，不是「这标的不许交易」，更不许被任何写入口当成硬拒绝（L1 只增不减）。

**台账两张表**（都 append-only、都靠触发器拒 `UPDATE`/`DELETE`）：`paper_agent_decisions`
（AI 的**决策**，`UNIQUE(arm, asof)`）＋ `paper_agent_evals`（**未成交腿的理由**，`UNIQUE(arm, asof, code)`，
P79 起由 `engine._step_all` 的 agent 分支落库 —— 原来那个 `_evals` 被下划线丢掉了）。
「不动的理由」是**结论不是日志**：`plan_orders` 早就算出来（如「目标市值与现市值差 < 1 手 → 不动」），
此前只是没地方落。

**本金与实时账户（P80 / ADR-032）**：AI 家族（`account_id LIKE 'arm-agent%'`）的本金口径由
**事件**表达，不由账户行表达 —— 新表 `paper_capital_events`（append-only）里各挂一条 +30,000、
**2026-09-28 生效** ⇒ 目标 50,000；`paper_accounts` / `paper_nav_daily` 的既有行一个字节都不改
（就地改 `initial_cash` 会让 09-23/09-24 的净值行当场违反 P62 的不变量）。重放读法是
「`date <= asof` 的带符号金额进现金与 `net_deposits`，`cum_cost` 不动」。

```bash
# 随时查「手上有多少钱、每只最多买几手」（只读，不等收盘链）
.venv/bin/python -m stocklab.cli.main paper account --arm arm-agent-ds-v1 --json
.venv/bin/python -m stocklab.cli.main paper capital list --arm arm-agent-ds-v1   # 资本事件台账
```

`engine.account_view` 是**唯一**投影（CLI 与 AI 输入侧都调它）；输入侧 `tradability` 增
`affordable_lots` / `sellable_qty`、顶层增 `cash` / `net_deposits`，并新增 `objective` 块
（考核目标＝扣除全部成本后的净收益最大化）且**进指纹** ⇒ 与 P80 之前的 `context_sha256` 不可比。

**未成交腿的读出口（P81 / D2）**：P79 把「AI 想动而没动成」的理由落进了 `paper_agent_evals`，
但此前**没有任何消费者**。现在有只读命令（数据源只有 `store.load_agent_evals`，不重算任何理由）：

```bash
# AI 的意图 vs 落地：它想买什么、为什么没买成（缺省 = 该臂最后一个有腿的日子）
.venv/bin/python -m stocklab.cli.main paper agent evals --arm arm-agent-ds-v2 --json
```

退出码：`0` = 读到（含「这台账还没有任何未成交腿」这种空结果，空列表**不是**「AI 没动过」）；
`2` = 账户不存在（沿用 `paper account` 的口径点名，不静默返回空）。

**意图 vs 落地对账（P81 / D3）**：`engine.agent_block` 增 `reconciliation`（**列表**，逐臂）＋
`reconciliation_note`，`paper show` 与 `/lab/paper` **同源**。覆盖面是「`paper_accounts.arm`
以 `agent` 开头且在 `paper_agent_decisions` 里有行」的**每一条臂**（各取自己最新的决策日，且
`<= asof`）—— **不写死 `arm-agent`**：真库在跑的是 `arm-agent-ds-v1/-v2`，而 `arm-agent` 一条决策都没有。
逐臂给 `n_legs_planned`（载荷条数）／`n_legs_filled`（当日 `paper_trades` 行数）／
`n_legs_unfilled`（当日 `paper_agent_evals` 行数）／`n_evals`（与前者同值，**故意重复**：一个是腿数、
一个是台账行数，不等本身就是信号，**不用 `min()` 抹平**）。

**可下手域读数（P81 / D4）**：`account_view.buying_power` 增 `tradable_domain`
（`n_pool_codes / n_priced / n_tradable / n_untradable / min_tradable_weight_pct /
one_lot_cost_p50 / one_lot_cost_min / cheapest_code / untradable[]`），`build_report` 给**每条 AI 臂**
补同一个块，`render_report` 第四节与 `/lab/paper` 各渲染一行
（`池内可下手 10/11 只；最小可成交权重 4.18%；一手成本中位数 ¥3,856.97` ＋ 买不起的**点名**）。
口径：`one_lot_cost_p50/min` 描述**池内可定价**的一手成本分布，`cheapest_code` 取**可下手子集**。
取不到（缺价 / 无池 / 账户口径读不出来）一律写「取不到」**不填 0**。

### ⑥ 账本与对照臂

- 成交只有一个写入口 `paper/store.py::insert_trade`（append-only，唯一键；错了只能冲正）。
- 账户现状（`paper_nav_daily` @ 2026-09-24）：

| 账户 | arm | NAV | 持仓 |
|---|---|---|---|
| `arm-hold` / `arm-now` / `arm-agent` / `arm-agent-random` | hold/now/agent/agent_random | 19,585.00 | 1 只（000333×100，人工种子） |
| `arm-agent-v1` | agent（**通路 A**，m2_a1/a2/a3） | 19,565.72 | 4 只（000333/600900/601398/603868） |
| `arm-agent-ds-v1` / `-ds-v2` | agent（外部决策写入口） | 19,553.68 / 19,539.16 | 0 只 / 5 只 |
| `arm-discipline-05/10/15` | discipline | 19,381.94 / 19,381.04 / 19,364.77 | ETF 目标占比 5/10/15% |

`arm-agent-random` 是**归因必需**：没有随机对照臂，一切「AI 有/没有用」的结论都算**不可归因**。
P79 起它的抽样口径是 **v4**（可成交化：抽出的敞口按整手数归一、`S=∅` 重抽 ≤8 次），
v1/v2/v3 的历史行一个字不改、读数**不可混引**。

### ⑦ 页面（`/lab/`）

`/` 总览 · `/paper` 模拟盘对照 · `/m2` 模块2 · `/candidate` 候选池 · `/ops` 定时任务 ·
`/trades` 成交流水 · `/cash` 现金流 · `/risk` 风险 · `/data` 数据 · `/health` 健康检查。

## 3. 当前读数（2026-09-25 实况）

### 3.1 生产模型准确率

| 指标 | 读数 | 参照 |
|---|---|---|
| **方向命中（全量 PIT 回放 2013-12-23→2026-09-14，`pit-rw-v1.0.2`）** | **行级 37.88%** / **按日聚类 37.95%**（n_days=2660，CI95 [37.28%, 38.61%]） | 随机 50%；「永远猜跌」基线 ≈36.1% |
| Brier | 0.66070（行）/ 0.66063（日） | 随机 0.667 |
| 区间 `range_80` 覆盖率 | **82.59%** | 目标 80%（口径内合格） |
| 关键位（支撑/压力） | **91.25%** | — |
| 近 30 交易日滚动（09-24 报告） | LIVE 33.33%（51 行/3 天）；REPLAY 33.33%（459 行/27 天） | 对照：always-up 31.15% / flat 35.73% / down 33.12% |

**结论：方向准确率约 38%，远低于 90%，而且比抛硬币差。** 这不是「再调几个参数」能到 90% 的量级问题
——A 股日频方向命中率，专业量化的公开水平是 50–55%，90% 不成立。
（`pit-rw-v1.0.2` 也**不是** ML 模型：手写统计式，`WINDOW=60`/`LEVEL_WINDOW=20`/`FLAT_BAND=0.5%`，
不做参数搜索，方向策略白名单 `ACTIVE_STRATEGIES` 为空。）

### 3.2 选股层

- 候选池在用（short 池 = 插桩 1 的 active 版本）；**A1 已在真库跑通**（`arm-agent-v1` 4 只持仓，净值 19,565.72）。
- `m2_a1` 现役 **v1.0.4**（`script_id=14`）；审计链完整（submit → sandbox → approve → active）。
- **选股有效性尚未被证明**：唯一一次横截面实验（P60，`research xsec-topn`）在 21 只事后挑的种子池上测「取前 N vs 全持」＝
  Δ −0.2431%/期、verdict `LOSE`——但它度量的是**分散度**、不是选股能力（`all` 臂本身只有 15–20 只，是同一 21 只池的子集）。
  **外推全市场不允许**；扩池（805 只）后重跑才能回答这个问题。

### 3.3 已做过、被否证的优化（别再重做）

`docs/experiments/README.md` 台账：**9 个单变量 PIT 预注册变体全部被否证**；
只有 `trend-state` 采纳为**标签层**（非交易信号）。

## 4. 护栏（写死、有测试钉住）

- 模拟盘**禁止方向择时**、禁参数搜索、不输出买卖建议（`tests/test_paper_discipline_guard.py`，AST 扫描）；
  `paper/` 不许 import `predict / verify / risk / plugin / candidate / experiments`。
- 不用未来数据（PIT 守卫 `check_no_lookahead`）、不接券商、不杠杆负权重、不池外标的。
- 主干常量只有用户能改（如熔断回撤 10%、`MODEL_VERSION`）。
- 红线**禁止「红了就 regen」**：先归因（`--fingerprint` 并排打印库指纹差异），append-only 追加台账，旧值只标失效不删。

## 5. 已知边界 / 下一步（按可验证性排序）

1. **扩池后重跑选股实验**（805 只）——这是唯一能把「选股有没有用」从「不可归因」变成有读数的事。
2. **`arm-agent-v1` 的考核样本**：目前 6 个交易日、LIVE 样本极少，任何「AI 比人强/弱」的结论都还说不出口。
3. **区间校准**：82.59% → 90% 需要收窄区间，代价是覆盖率换精度，须预注册实验（单变量）。
4. **P61 沙盒进程级隔离**（推迟中，触发条件 G1/G2/G3 见任务书 §0.5–0.7）。

## 变更记录

- 2026-09-26: 补 P81 —— `tradability` 的「可下手」结论键（判据唯一：`engine.tradable_verdict`）、
  `paper agent evals` 读出口、`agent_block.reconciliation` 逐臂对账、`buying_power.tradable_domain`
  与报告/页面的一行读数（取不到写取不到，不填 0）。
- 2026-09-26: 补 P79 —— 输入侧上下文键集（含 `tradability`）与台账两张表；随机臂 v4（可成交化）。
- 2026-09-25: 首版（回答「准确率 + 全流程」）。
