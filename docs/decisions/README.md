# 设计决策记录（ADR）

命名：`NNNN-<kebab-title>.md`，例如 `0001-use-postgres.md`

## 模板

```markdown
# NNNN. [决策标题]

- 日期：YYYY-MM-DD
- 状态：提议 / 已接受 / 已废弃

## 背景
面临什么问题？

## 备选方案
1. 方案 A — 优点 / 缺点
2. 方案 B — 优点 / 缺点

## 决策
选了哪个，为什么。

## 后果
带来的好处、代价、后续需要注意的地方。
```

## 索引

- [ADR-004 复权落点](2026-09-15-ADR-004-复权落点.md) — 读取层复权 + 自建乘性因子链（探针实测腾讯 qfq 减法式出负价）
- [ADR-005 实验决策的幂等键取语义不取呈现](2026-09-15-ADR-005-幂等键取语义不取呈现.md) — 改一个标点不得多判一条决策；20 行历史台账保留不迁移
- [ADR-008 ETF 复权口径](2026-09-15-ADR-008-etf-复权口径.md) — 腾讯通道拿不到 ETF 除息事件（显式限制）→ 读取层硬拒绝，白名单判据；禁止未复权冒充复权
- [ADR-009 raw_cache 当日时效](2026-09-15-ADR-009-raw-cache-当日时效.md) — 同日收盘前抓到的快照不算命中（读侧忽略+留痕 / 写侧不落盘）；既有毒条目不自动修复（显式限制）
- [ADR-011 估值/资金流采集口径](2026-09-17-ADR-011-估值资金流采集口径.md) — PIT 键钉死但**不称 PIT-安全**（东财重算历史 = 非 PIT 残余风险，三层对策 + 下游披露义务）；append-only + 首写保留；NULL 不填 0；**ETF 覆盖按 (code,source) 逐对判定**（实测：估值全空、资金流有数据，原假设被否证）；**采完不直接上策略**；**不做 turnover 回填**（换算倍数 97.15–101.08 非精确 100）
- [ADR-012 跌破线规则化](2026-09-17-ADR-012-跌破线规则化.md) — 000333 收盘止损线 85.00（无推导拍数）→ `MA60 − 1×ATR14` 规则值 **82.14**（asof 2026-09-16：MA60=83.7893、ATR14=1.6521、True Range 简单均值）；固定线误触频率在不复权序列上无意义（94.67%/77.20%），trailing 规则灵敏度 30.59%（全历史，被除权污染）/ **11.20%（近 250 日，干净）**；不重算历史；check id `stop_loss_close_85` 保留；周线线未动（建议 MA60−2×ATR14=80.49 待用户点头）
- [ADR-013 已公告休市表与「未来已知事实」的 PIT 纪律](2026-09-17-ADR-013-休市表与PIT纪律.md) — 交易所**提前公告**的休市安排属「预测时点已公开的未来已知事实」→ **不构成前视**；另起 `market_holidays`（前瞻）而非塞进 `trading_calendar`（回看，塞进去会静默污染 `Calendar.load` 与 `session_check`）；主键 `(date, source_url)` + 改期靠多行 `published_at` 最新者胜出、**不覆盖旧行**；覆盖判据只认 `doc_kind='annual'`，覆盖不到如实退回 `weekday_fallback`；`target_date` **不参与任何收益/因子计算**（结构性保证：算错了也够不到价格）；四条风险边界（临时休市不可预知 / 改期 / 只覆盖已公告年份 / 只采上交所）
- [ADR-014 `predictions.origin` 来源列与「断言 vs 推断」分段](2026-09-17-ADR-014-origin列与断言分段.md) — 回放/实时分段从「按 `created_at` 推断」改为「写入路径断言」：`origin TEXT CHECK(origin IN ('live','replay'))`，`insert_prediction` 的 `origin` 为**必填**关键字参数（`predict run`→`live`、`verify backfill`→`replay`），漏传即 TypeError；迁移只 `ALTER TABLE ADD COLUMN`、**老行一律 NULL 不回填**（回填等于用猜测冒充事实），`origin IS NULL` 在 `chain accuracy` 退回推断并**分列计数** `asserted/inferred`；`session/review.py` 一字不改（保 P11 复盘逐字节不变）
- [ADR-015 写库入口统一走 `ensure_schema` 自动前滚 schema](2026-09-17-ADR-015-写库入口自动前滚schema.md) — 迁移只挂在 `db init` 导致真库没前滚、收盘链 `predict run` 撞 `OperationalError`；修法：`migrate.ensure_schema`（幂等前滚 + 只在确有结构变更时备份一次 + 真失败要炸），**写库入口统一接它**（`ingest bars/actions/index`、`predict/verify run|backfill|pending`、`review daily`、`session tick/backfill-close`、`paper init|step`、`features build`、lab 应用）；`init_db` 与 `ensure_schema` 并存分工（前者每次都备份、后者只在变更时备份）；`doctor` 只读报告 schema marker 在位与否、**不擅自迁移**
- [ADR-018 单一 Web 服务](2026-09-21-ADR-018-单一Web服务.md) — `dashboard serve` 合并进 `lab serve`（两者默认端口都是 8791 ⇒ 本机跑着哪个只能靠记忆回答）；看板只留离线单文件产物（`dashboard build`），回环白名单只留一份；两条护栏钉住不回退（`dashboard.server` 公开面只剩三个名字 / `dashboard serve` 必须 `invalid choice`）
- [ADR-019 巡检归属](2026-09-22-ADR-019-巡检归属.md) — 「每 30 分钟巡检」的**判定**从 nanobot cron 正文搬进项目：`stocklab ops patrol`（体检只读 `mode=ro` / 补步=逐字复用现有 CLI、走子进程 / **退出码由项目定义** 0 全绿·1 有异常·2 判不了或断链 / `--fix` 不跨库）；四个备选（保持现状 / 项目内命令 / 脱开 nanobot 挂 launchd / 常驻 scheduler）与「为什么 `unknown` 在④算异常、在②不算」都写明；cron 只剩「非 0 就贴回来」（错误日记 #53）
- [ADR-020 调度载体改用系统 launchd](2026-09-22-ADR-020-调度载体改用系统launchd.md) — **时钟交给 OS**（用户 2026-09-22：「其实不需要 nanobot 定时任务，全部在系统里，到时候跑完页面会有展示」）：`~/Library/LaunchAgents/com.stocklab.{close,patrol,monthly}.plist`（plist 由纯函数 `render_plist()` 生成、`RunAtLoad=False`、日志显式给路径）、CLI `ops schedule generate|install|uninstall|status|kickstart`、结论落 `reports/ops/latest-*.json` ⇒ 新页面 **`/lab/ops`**（只读回执，摘要行与 launchd 日志逐字同源；没跑过写「还没有回执」不写 0）。实测阻塞：**macOS TCC 挡住 `~/Documents` 下的 launchd 进程**（`kTCCServiceSystemPolicyDocumentsFolder AUTHREQ_PROMPTING`，pid 69249 卡在 `open()`），处置与撤 nanobot 三条的前置条件见 `docs/ops/2026-09-22-launchd-定时任务.md`
- [ADR-021 模块2 状态枚举与主干常量](2026-09-22-ADR-021-模块2状态枚举与主干常量.md) — 状态机 5 态 → 7 态（新增 `validating`/`frozen` + 4 个事件，未知事件拒绝折叠）；自评估硬边界（轮次∈[2,3]、总时长≤30 天、冻结≤90 天）与熔断阈值（自峰值回撤≥10%）落主干常量，**AI 改不了**（源码扫描钉死）；验证周期台账 append-only（触发器拦 UPDATE/DELETE）；「已回滚」**不设独立态**（回滚 = 对 `archived` 版 `approve`）
- [ADR-022 模块2 插桩契约口径](2026-09-22-ADR-022-模块2插桩契约口径.md) — 四支 `m2_a1/a2/a3/b1` 不占 0–5 编号（D-33）；`m2_a3`/`m2_b1` 的字段名取**真源** `CONTRACT_FIELDS`（`range_80`+`direction`+`invalidate_if`），**不**用库里的列名 `range_lo/hi`；「不知道」= 全 None + 非空 `na_reasons`（**全有或全无**，半真半假拒绝）；引用越界校验单独成 `validate_references`（`allowed_codes` 必填、**没有跳过默认值**）；`0–5` 形状与 `approve` 闸门一个字不改
- [ADR-023 模块2 绩效指标口径](2026-09-22-ADR-023-模块2绩效指标口径.md) — 需求 02 §4 的**五个指标定死名字与顺序**（多一个都不加）；算法唯一真源 `backtest/metrics.py`（补 `profit_loss_ratio`，`None` 而不是 0/inf：只有赢或只有输时算不出）；期初基 = **起跑日值**（账户 `initial_nav` / 基准起跑日收盘）⇒ 与既有「累计收益」列（基 = 净入金）相差一个**对全部臂相同**的 0.2155% 常数，记账但**不改口径去对齐**；`n_sessions` = 日收益个数（真库 5，与 `build_report` / `track` 同值；横轴 6 个点是另一回事）；门禁 `threshold = verify.report.MIN_DAYS`（120），`insufficient` 时**只给读数**、逐词禁「跑赢/跑输/优于/劣于/领先/胜过」；回撤数据取负值、页面按既有列显示正值（同一个数）
- [ADR-024 AI 操盘手的决策空间与护栏](2026-09-22-ADR-024-AI操盘手决策空间与护栏.md) — D-34 **覆盖 D-18**：`arm-agent` 重构成「每交易日一条决策的操盘手」（方向 ＋ 仓位 ＋ 池内自由选标的），载荷 `{asof, decisions[], cash_pct, rationale}`、`Σ权重 + cash = 100`（不杠杆/不负权重）、池外即拒、**A 股无做空**（看空只能降总仓位/清仓，报错文案带 `NO_SHORT_SIDE_MSG`）；**决策在项目外产生**（`paper agent decide`，项目内只做校验/落库/执行/计账，保离线可测零密钥）；台账 `paper_agent_decisions` 一张表两段历史（`decision_kind` = spec/portfolio，新列只 `ALTER TABLE ADD COLUMN` 不回填）、成交 `reason` 自带 `decision <sha12>` 溯源、**缺台账行即判红**（`audit_decisions`）；护栏按臂分作用域（源码扫描：操盘口径只许出现在 4 个模块 + `config` 只许声明键名，静态臂执行函数一个都不许出现）；随机对照臂 `arm-agent-random` 同护栏同成本、种子可复现；D-36 对照臂 6 条同轴，基金等权臂**非指数、不可比写「不可比」不填 0**、等权 = 累计收益算术平均、清单先验选定；样本门槛 120 与 `MIN_DAYS` 靠测试对拍（不 import 验证链路）
- [ADR-025 模块2 双通路与镜像口径](2026-09-22-ADR-025-模块2双通路与镜像口径.md) — 通路 A 的账户 = `arm-agent-<策略版本>`（D-35），每个版本一行 `paper_accounts` + 独立 NAV（D-26）；**谁落这一天的净值是显式字段**（`params_json.executor='m2_channel_a'`）——`paper step` 若也认领它就会先写一条无成交净值、把这条策略永久卡死（`step` 的幂等判据正是净值行的存在性），且不能靠账户名前缀猜；**D-39** A3/B1 只写新表 `m2_forecasts`（不进 `predictions`：红线按 `model_version` 分列、`verifications` 按 `pred_id` 外键，混进去会污染分列与「模型准确率」统计），列名沿用落库列名、带 `script_id/script_version/input_sha256` 溯源三件套，幂等键 `(account_id, asof_date, code)`；**D-40** 通路 A 决策频率 = 每交易日一次（与 D-34 同源，刻意不跟随静态臂的 `rebalance_cadence`）；**D-41** 人工录入真源 = `real_trades` + `cash_flows`（`portfolio/` CLI），通路 B 只镜像复刻、复用 `arm-now`，`m2/` 不得有第二套成交/净值口径（除 `store.py` 不得出现 `INSERT INTO`）；**D-42** 与 P49/P44 的挂接点本轮只留结构位。留痕 `m2_channel_runs`（`status ∈ ran/skipped/rejected` ＋ 部分唯一索引 `WHERE status='ran'` ——不能整表 UNIQUE，否则「缺 K 线跳过」会永久占格）；CLI 退出码 `ran`/`already`=0、`skipped`=3、`rejected`=4，`already` 不落库
- [ADR-029 候选池打分的价格口径](2026-09-25-ADR-029-候选池打分价格口径.md) — 因子侧 `ctx["bars"]` 改走 PIT 复权价（`load_bars_adjusted`，不缩窗）、判定侧 `screen.py` **保持未复权**（涨跌停按前收，除权跳空是「跌停 vs 除权」的区别，一行不改）；不可复权时 fail-open 回退未复权价 + 计数 `n_adj_fallback`（**实测 259/532 只仍在回退**：226 只未采 `actions` + 窗口跨不可定价事件 148 只），只回退**数据侧不可用**那几类、裸 `AdjustError` 仍炸（#81 分档）；快照只增 `scoring_price_mode` / `n_adj_fallback` 两键、历史行不改写；实测短池 top-6 在分红季 4/6 天变化（进入者得分 5.4→100，假跌幅被还原）、中长期池 6/6 天逐位不变、单日扫描 +55%～+85%；旧（未复权）口径的实验读数不可与新读数混引
- [ADR-030 信号有效性度量口径](2026-09-26-ADR-030-信号有效性度量口径.md) — 新增只读基建 `research rank-ic`：逐调仓日以 `adj_score`（次读数 `raw_score`）对下一周期前向收益算 Spearman rank IC（Pearson 并列）＋按分数降序分 5 层（第 1 层 = 最高分，`spread = layer1 − layer5`）；`PipelineResult` **只增** `scored` 字段暴露交叉截面（`members`/`rejects`/`params`/`eligible` 逐位不变，`git stash` 前后 digest 自证）、打分口径一字未动；前向收益走 **PIT 复权价**（`load_bars_adjusted`，`as_of=marks[-1]` 一次/只）—— D3 的「比值对 as_of 不变」**数学成立、比特级不保证**（实测 5/15 样本末位差 ≤2.6e-14 ≈128 ULP，名次序实测一致 ⇒ IC 不变，照实记）；复权不可用 fail-open 但计数 `n_fwd_fallback`、缺价计 `n_no_fwd_ret`，裸 `AdjustError` 原样抛；判据**复用** `sandbox` 门槛但 verdict 词汇换成 `IC_SIGNIFICANT`/`IC_NOT_SIGNIFICANT`（IC 为负也算 significant，`WIN` 会读歪）、`MIN_XSEC_N=30` 是自创取值；**已知遗留**：`replay.py` 组合收益走未复权价 ⇒ 本报告 IC 与 `xsec-topn` 的 Δ **绝对值不可比**（本站不修）
- [ADR-031 决策可成交性](2026-09-26-ADR-031-决策可成交性.md) — 全链路没人对「一手（100 股）够不够」负责 ⇒ 两个真实故障（`arm-agent-ds-v1` 09-23 四条买入腿**全部**因「目标市值 < 一手含滑点成本」落空、该臂次日 100% 现金；`arm-agent-random` 09-23/09-24 连续零成交 ⇒ 归因分母退化成 `arm-hold`）；修法三件、**不动任何既有算法**：① 输入侧顶层键 `tradability`（`{lot, min_weight_pct, by_code{one_lot_cost,min_weight_pct}}`，键集 `marks ∩ pool`，一手成本唯一公式 `rules.one_lot_cost` 复用 `fill_price`+`_fee_parts`）并**进指纹** ⇒ 同一输入在 P79 前后 `decision_context_sha256` 不同、旧台账行一字不改写；② 新表 `paper_agent_evals`（append-only、`UNIQUE(arm,asof,code)`、`INSERT OR IGNORE`）接住原来被下划线丢掉的 `_evals` —— **只落决策驱动那条路**（静态臂 / m2 通路的 evals 仍不落库）；③ 随机臂口径 **v4**（v3 抽取序列之后追加「可成交化」：按整手数归一到不定点、`S=∅` 重抽 ≤8 次、仍空则取池内最便宜**未持有**标的 1 手），`cap` 公式与敞口上界一字不改（修下界不修上界）；不改 `plan_target_weight`/`plan_orders`/`_settle`/`_gate_cash`/`execute_decision`；站侧偏离 7 条（含「D2 示例与公式矛盾照公式实现」）见任务书 §7.6
