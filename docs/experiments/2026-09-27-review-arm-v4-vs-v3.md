# 实验：复盘对照臂 · `arm-agent-ds-v4`（先复盘再决策）vs `arm-agent-ds-v3`（无复盘）

- **日期**：2026-09-27
- **提出人**：nanobot（用户 2026-09-26 要求 AI 操盘手具备「**不断分析市场、复盘总结自己**」的能力；
  2026-09-24 授权「只要能提高 ai 模拟的准确率，就都按照你想的优化」）
- **状态**：**预注册（第一条 v4 决策落库之前提交）** —— 本文件在观察到任何 v4 读数**之前**落盘；
  跑完**不许改**，要改只能追加新实验（`docs/experiments/TEMPLATE.md` 第 7 条）
- **上游**：`docs/tasks/2026-09-27-p86-提示词v4与复盘对照臂.md`（G1–G6）、
  `docs/decisions/2026-09-26-ADR-036-复盘台账与教训回注.md`（P85）、
  `docs/decisions/2026-09-26-ADR-035-市场与自身历史上下文.md`（P84）

## 0. 预注册参数（机器可读）

```json
{"experiment": "review-arm-ab", "arm_treatment": "arm-agent-ds-v4", "arm_control": "arm-agent-ds-v3",
 "model_id": "deepseek/deepseek-v4-pro", "prompt_v4_sha256": "0a154fc5d5d70e9ab0f6bfe9ae25c75f68eb5d83d9de29daa3f1131034f54257",
 "prompt_v3_sha256": "66200c1d386dd6850a0d7c14d7067cd4e0fa0c454d90366f08d44f689f81b5d6",
 "cadence": "per_trading_day", "paired": true, "cost_included": true,
 "min_periods": 120,
 "primary": "两臂同日净值序列的累计收益差（扣佣金/印花税/滑点；共同起点归一）",
 "rule": "INCONCLUSIVE = 共同窗口交易日数 < MIN_VALID_PERIODS(120)；否则报 Δ 的 95% bootstrap CI 与符号，不做方向预设"}
```

> `min_periods = 120` 是 `stocklab/plugin/sandbox.py::MIN_VALID_PERIODS` 的既有常量（由代码读出后比对，
> 预注册只把它钉在纸上）。**在样本不足之前，任何「v4 更好 / 更差」的结论都不成立**（与 O9 同一条纪律）。

## 1. 假设

> 在**同模型（`deepseek/deepseek-v4-pro`）、同池（候选池快照同一份）、同交易日**下，
> 让模型**先写一条带证据指针的复盘、再给决策**，并且把最近复盘与已确认教训（`facts`）
> 回注进下一轮的 PIT 上下文，**能提高**扣成本后的净收益（相对不写复盘的 v3 臂）。

**主读数**：两臂**同日**净值序列的累计收益差（`paper_nav_daily.cum_return`，已含成本）。
**次读数（并列报出、不作判据）**：`paper_nav_daily.cum_cost`（成本是否被复盘行为抬高）、
`paper_agent_evals` 的未成交腿数、`paper_agent_reviews` 的被拒次数（`review_written=false` 的天数）、
以及复盘的**读数可核率**（模型自报的 `metric`/`market` 断言被写入口拒掉的比例 —— 拒得越多说明
模型越爱编数，那本身就是要观察的行为）。

**失效条件（提前写死）**：

- 共同窗口 < 120 交易日 ⇒ `INCONCLUSIVE`（**样本不足 = 「测不出」，不得读成「没效果」**）；
- 两臂在同一交易日**不是同一份池快照 / 不是同一 asof** ⇒ 该日剔除并点名（对照不成立的日子不能混进来）；
- v4 出现**连续 ≥5 个交易日**复盘写不进去（`review_written=false`）⇒ 停实验、先修链路 ——
  那时测的已经不是「复盘有没有用」，而是「链路坏没坏」。

**verdict 词汇**：`WIN` / `LOSE` / `INCONCLUSIVE`，判据只看扣成本净收益差的 CI 与符号。
**禁止**为了救结果改提示词、改池、改窗口、改成本口径（`CLAUDE.md` 度量纪律 6）。

## 2. 两个差异面（分开记账）

| # | 差异面 | 生效时间 | 说明 |
| --- | --- | --- | --- |
| ① | **提示词要求复盘**（同一次回答里先交 `review` 再交 `decisions`） | **当日生效** | 输出更长 ⇒ 温度/采样路径整体不同，**从第一天起**就与 v3 不同 |
| ② | **复盘回注上下文**（`own_history.recent_reviews` ≤3 条 / `facts` ≤10 条） | **随样本累积** | 第 1 天必然为空（无历史复盘）；第 2 天起才出现 |

⇒ 本实验测的是「①②**合起来**（＝完整的复盘环）有没有用」，**不是**①单独的作用。
要把 ① 与 ② 拆开需要第三条臂（只写不复读），**本实验不做**（多一条臂 = 多一个自变量，
且每日多一次模型调用；留作后续实验的候选）。

## 3. 可复现性口径（必须写清，免得把正常现象读成异常）

1. **`decision_context_sha256` 从 v4 起会每天不同**，即使市场数据完全一样 —— 因为
   `recent_reviews`/`facts` 在长，而这正是设计。**可复现的是「同一天、同一份上下文 ⇒ 同一输出」**，
   不是「跨天同输出」。把「sha 天天变」当异常告警是**误读**。
2. **两臂的复现窗口不同**：v3 的 `prompt_sha256` 是 `66200c1d…`、v4 是 `0a154fc5…`，
   各自落在自己的账户行里（D-48：换提示词 = 换口径 = 开新版本账户）。`paper agent decide` 会
   逐字段比对，**配错即 exit 2、零写入**。
3. **两臂的起跑状态同源**：`engine.enroll_agent_arm` 与其余各臂共用 `_initial_state`
   （D-48：换版本不换起跑口径），本金口径靠 append-only 的 `paper_capital_events`
   （P80：2 万 → 5 万走事件，不改历史行）。

## 4. 数据与披露

- **臂**：treatment = `arm-agent-ds-v4`（本实验第一天开户）／control = `arm-agent-ds-v3`
  （2026-09-26 起在跑，留有历史）
- **池**：两臂读**同一份** `candidate_snapshots`（候选池快照不随臂变化）；per-day `asof` 必须相同
- **成本**：佣金（最低 5 元/笔）、印花税、滑点，全部走项目既有执行层口径，**不另行假设**
- **非 PIT 披露**（沿用既有清单，逐条并列）**：候选池建在**21 只种子的现成分**上（`candidate/run.py`），
  种子宇宙是**事后挑选**；行业分类取当前 `sector`；ST 判定取当前名称。⇒ 读数的外推边界
  与 `xsec-topn` / `rank-ic` 同：**只能说「在这 11–21 只池、这段历史上」**
- **数据来源**：真库 `data/stocklab.db`（只读用于分析；写只经项目 CLI 的既有写入口）

## 5. 结果（样本外）

> **跑完之后填这一节；本文件在第一条 v4 决策之前提交，跑完不许改。**

| 指标 | `arm-agent-ds-v4`（treatment） | `arm-agent-ds-v3`（control） |
|---|---|---|
| 首条净值日 / 末条净值日 | | |
| 共同窗口交易日数 | | |
| 累计收益（共同起点归一） | | |
| 累计成本 `cum_cost` | | |
| 未成交腿数（`paper_agent_evals`） | | |
| 复盘写入成功 / 失败天数 | | |

- Δ（v4 − v3）与 95% bootstrap CI：
- verdict：

## 6. 复现

```bash
cd ~/Documents/Workspace/stock
# 两臂每日各跑一次（同 asof、同池）；第二条命令的 ⑥ 一次点名在飞的臂
.venv/bin/python -m stocklab.cli.main paper agent decide --asof <D> --arm arm-agent-ds-v4 --file <review+decision.json>
.venv/bin/python -m stocklab.cli.main paper agent review --asof <D> --arm arm-agent-ds-v4 --file <review.json>
.venv/bin/python -m stocklab.cli.main paper agent run    --asof <D> --arm arm-agent-ds-v3 --arm arm-agent-ds-v4 --arm arm-agent-random
```

（生成器 `tools/stock/ai-trader.py` 把上面三步包成一次运行：
`ai-trader.py --asof <D> --arm arm-agent-ds-v4 --run-arm arm-agent-ds-v3 --run-arm arm-agent-ds-v4 --run-arm arm-agent-random`）

## 7. 执行记录（跑完由 nanobot 填）

- v4 开户（`paper agent enroll`）时刻 / `created` / `account_id` / 起跑 `initial_nav`：
- 首个决策日 / 首个净值日：
- 复盘写入读数（成功天数 / 被拒天数、拒因分布）：
- 只读证据（两臂读数取自 `paper_nav_daily` / `paper_agent_reviews`）：
- 结论落点：
