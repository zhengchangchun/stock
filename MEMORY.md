# stock-lab 分本（2026-09-18 从总本拆出）

> 来源：`/root/.nanobot/workspace/memory/MEMORY.md` 第15-52行（stock-lab节）+ 第7-13行公共约定摘要。
> 总本只留公共，以后看股票只翻这本，不翻电商本。数字口径遵循总本：实测/历史存档/样本内估计/外部口径四分，出处必填。

## 公共（摘要）

- 项目根目录：`/root/.nanobot/workspace/projects/`，一项目一子目录一 git 仓库。
- 实际编码交给本地 Claude Code CLI（`/usr/bin/claude`），nanobot 只定需求、起任务、盯过程、验结果；详见 skill `claude-dev`。
- 并发口径：每项目同时最多1个claude任务，不同项目可并行；起任务前 `pgrep -af "[c]laude -p"` 查本项目。
- 对外统一走nginx：后端只绑 `127.0.0.1`，改前备份，改后必 `nginx -t` 再 `reload`。
- 模型名必须完整 `meta/muse-spark-1.3-contributor`，首行 `[claude-code:unrecognized_model]` 警告忽略；本机无 `jq`/无 `sqlite3` CLI，用 `python3`；`claude -p --output-format json` 按行取 `{` 开头最后一行解析。

### stock-lab（A 股自优化分析系统，2026-09-14 起）

- **最高目标**：样本外预测准确率（不是「跑通代码」）。P1–P8 建成后长期循环：收集数据 → 预测 → 次日验证/归因 → 挑假设 → 单变量实验 → walk-forward 判断 → 达标才改策略；另含模拟盘 vs 实盘对比 + 定期提醒，不等用户催
- **优化顺序铁律**：先把评测做可信（walk-forward 切分、按日聚类、扣成本、与 index_300 对照、指标提前定死），再谈准确率提升；禁止全样本调参；跑不赢基准就明说，不美化结论；不做方向择时（模型方向能力≈0），对照实验只并列「不动 / 复制现状 / 按纪律分散」三臂、不许挑一个当推荐
- **位置**：`/root/.nanobot/workspace/projects/stock-lab/`（独立 git 仓库，由 `_template` 生成）
- **关键文档**：`docs/plans/`（系统设计总纲、P0 设计评审、phase1-4 实施计划）、`docs/tasks/`、`docs/decisions/NNNN-`（ADR 编号顺延、不回收）、`docs/experiments/`（append-only 实验台账：从 TEMPLATE.md 复制为 `YYYY-MM-DD-<name>.md`，失败/否证也必须留档并更新 README 索引表；**该目录由 nanobot 维护，claude 只读不写**）、`docs/errors/ERROR_DIARY.md`；项目 `CLAUDE.md` 含《准确率优先》章节
- **铁律核心**：预测必须可证伪（含失效条件）、point-in-time 禁未来函数、回测必须扣成本、「市场噪声」是合法结论、append-only、失败必须留痕；不删任何历史数据、不加滚动清理（2026-09-15 用户明确取消「只留近 30 天」）
- **数据源（ADR-003）**：腾讯不复权日 K 是唯一主源——单次上限 2000 根、用 `end` 锚点分页可回溯至上市首日（`beg` 被服务端忽略）；腾讯 qfq 上限 800，801–2000 区间**静默降级回 640 且不报错**，且是按最新因子重算的整条序列（非 PIT）→ 禁用，写入口对 `adj_mode != 'none'` 硬拒绝；东方财富在本环境完全不可达，已退出主路径（旧「900 根窗口」结论作废）
- **复权（ADR-001/004）**：落不复权 OHLC + 自建 PIT `adj_factors` 因子链，factor(t)=Π(1−(现金分红+送转)/除权前收盘)，只允许 cqr ≤ T 的事件参与，无事件显式入库 1（禁 NULL；纯现金公式曾漏送转，已修正）；600690 早期 4 个事件无条款原文无法定价 → `adj_factor_blackout` + 读取层/引擎硬拒绝（跨缺口报 MissingFactor 并给出可用起点），禁止下游静默使用错因子
- **样本量（实测，替代旧估算）**：walk-forward 131 折、3007 交易日、样本外 **2751 交易日 ≥ 120 门槛 → 够用**；达标最少需 376 交易日历史（≈1.55 年）；折间不独立（训练窗重叠），检验必须按日聚类；**加标的不增加有效样本量**
- **统计纪律**：A 股同涨同跌，20 标的 × 7 策略 ≠ 独立样本（有效 n≈1–5/日）；≥120 交易日样本外才谈优化
- **归因两级**：DATA 由程序自动分类；SIGNAL/STRATEGY/MODEL 记 UNDETERMINED + 人工标注字段（代码不可判定，强实现等于造假归因）
- **调度归属**：项目只产出报告文件（reports/），推送/提醒由 nanobot 负责（项目内不建 cron，避免双重触发）；15:30 收盘链含 `predict run`（生成次日预测，原先缺失会让预测断供，2026-09-15 补上）；盘中 tick 09:35/11:35/13:35/15:05。**2026-09-17 18:41 自动化已全停等用户规划（此前8→7合并备份 `cron/jobs.json.bak.2026-09-17-merge`仅留档）：5个自建cron已删（stock-lab-dev-2h `bceb09d7`、ecom-dev-2h `03acdfba`、盘中tick `3fde77c1`、收盘tick `e8fb0023`、收盘链 `fd494142`），只剩heartbeat+dream（`cron/jobs.json`实测）；恢复从备份 `cron/jobs.json.bak.2026-09-17-pause` 加回；停后果：次日无新bars入库、后日预测断供**；巡检第 0 优先级＝「当天链路体检 + 缺步补跑」（查今日 bars/明日预测/当日 review/模拟盘净值，缺就按收盘链顺序补跑，LATEST 须在 ingest 后取）；stock-lab job 的 `payload.deliver=false`（`/root/.nanobot/workspace/cron/jobs.json` 实测）→ 结果只进会话日志、不主动推送，用户 2026-09-16 问「为什么看不到定时任务输出」根因为此**
- **首个真实准确率（PIT 历史回放 3096 日）**：方向行级 38.14%，按日聚类 0.3819 ± 0.0070；Brier 0.6581（随机≈0.667）→ **方向能力 ≈ 0**，仅略高于 always_up/down（37.5%）且置信区间重叠；`range_80` 覆盖率 81.68%（标称 80%，校准合格）是唯一站得住的量化事实；关键位逐位兑现 90.62%；决策层加凯利准则：f*=0 → NO_BET → 不押单、不做方向择时
- **P8 实验结论**：预注册变体 `rw-mu0`(33.10%) / `index-mom-dir`(33.72%) 在 validate 段（651 日，基线 36.87%）双双 `falsified`，gate 正确未打开 test 段 → 「动量当方向先验」无效；旧的「index_300 领先 13.8pp」说法已推翻（那版基线用目标日当天指数涨跌预测当天方向，含未来信息、不可交易，不是可比对手）
- **趋势状态探索（2026-09-15，预注册 `trend-state-hit-rate`，commit 8a8e2d8）**：用户口径改为「不判涨跌，先输出上升/下降趋势状态，再据此判定准确性」。UP/DOWN 5 日趋势延续命中率 71.9%/73.7%/72.5%（UP）、68.2%/72.9%/67.9%（DOWN），较基线增益 +17~+45pp；但对**未来涨跌方向无增量**（Δ 仅 +0.7pp，CI 跨基线）→ 「趋势会持续 ≠ 趋势能赚钱」，不得当买点。趋势状态的正确定位是**风险而非方向**：000333 在 UP 状态下未来 5 日波动 6.44%（FLAT 4.42%）、跌超 5% 概率 11.1%（FLAT 7.3%）→ 用于仓位与风险预算
- **`invalidated` 子群已定性**：字段语义 =「预测的失效条件被命中」，不是该行作废；命中 665/6038 条方向准确率 66.9% vs 其余 34.5%，但该子群由**次日收盘**构造、不可 PIT 化、不可交易（PIT 可观测窄边界选行仅 37.48%，全体 38.14%；反向最宽 20% 反而 42.42%）→ 禁止当策略/变体假设来源
- **已否证的实验轴（不许重跑）**：① `mu_mode` 漂移方向（`rw-mu0`）② 指数动量当方向先验（`index-mom-dir`）③ σ 状态缩放三变体（sigma-vol-z / sigma-rv-pct / sigma-index-rv-pct：方向 Δ 全负、Brier Δ 显著为正 = 过度自信）④ 区间构造 `dist_mode` 残差分位（`range_80` 覆盖率改善到 80.03%，但整体仍被否证）⑤ 趋势状态当方向信号（`trend-state-hit-rate` 判据 a 两段皆否）。`pit-rw-v1.0.1` 基线仍未被打赢，方向能力 ≈ 0
- **数据缺口**：`valuation_daily` / `money_flow_daily` 无采集路径 → `regime_label`/`main_net_5d`/`pe_pct_3y` 全 NULL；`bars_daily.amount`/`turnover` 亦全 NULL（随新调度链上线当天起积累，历史保持 NULL）；`dividend_hold`、`regime_switch`、`fund_flow_follow` 因此搁置；标的池已于 P17 纳入 ETF（510300/510880/512890/518880，`type=etf`，ETF 除息事件在腾讯通道不可见）；横截面假设仍需 ≥20 只
- **策略结论**：P5 策略库已落地（价量类为主）。首个样本外否证：`trend_ma` 默认参数扣成本 +510.79% / 年化 18.03% / MDD −63.89% / Sharpe 0.702，输给 `buy_and_hold` +881.75%（−370.96pp）→ 择时不如躺平；参数未做搜索
- **实盘账本**：只记成交流水（real_trades: date/code/side/price/qty/fee/note），持仓/净值由流水推导，不做每日快照；写入入口已随 P12/P15 落地（`trade`/`cash`/`portfolio` CLI + lab 应用录入成交、冲正错单、录入本金与出入金，append-only + 冲正纠正、禁 UPDATE）；卖出侧已有实现（`ledger.validate_trade` 卡超持仓、`render` 有卖出选项），用户实盘仅 1 笔买入故卖出尚未实测
- **交易规则（深交所 3.3.8）**：持仓为 100 股整数倍时，卖出申报也必须是 100 股整数倍、不得拆零 → 100 股美的只能全清或不动；原「单次减仓 10%」动作不可执行，已作废
- **交易日历**：取自腾讯指数日线日期集合（`trading_calendar` 3265 行到 2026-09-17，2013-04-16 起，非空）；不预知未来休市，日历耗尽时 `target_date` 退化为 `weekday_fallback`（工作日近似、会把节假日当交易日，已在载荷标注来源），实盘前需每日刷新
- **成本模型**：佣金 0.025%（最低 5 元/笔）、卖出印花税 0.05%、过户费 0.001%、滑点 5bps；小资金单次往返 ≈0.17%（含滑点 ≈0.27%）；场内 ETF 卖出**免印花税**，`CostModel` 需按标的口径区分股票/ETF（否则系统性高估 ETF 成本）；受 5 元最低佣金拖累，单笔金额须凑到接近 1,000 元（约 2 手 ETF）——1 手 452 元时单边成本 1.1%、2 手降到 0.55%；国债 ETF 511260 一手≈1.36 万元，超「单次动用现金 ≤5%」约束，直接排除
- **features_daily**：append-only（`insert_feature_snapshot` 是唯一写入口，不做 upsert），自增 snapshot_id 主键 + UNIQUE(code, date, feature_version)，下游按 feature_snapshot_id 引用；复权因子链落地后按新 feature_version（v1→v2）全量重算、旧快照保留不覆盖；特征稳定哈希可逐字节复现；`features build` 暂只支持单日（回补需循环调用）
- **可复现性**：raw_fetch_cache（原始响应 + SHA256，可从缓存重放；同日收盘前抓的盘中快照读侧视为未命中、写侧不落正式缓存，ADR-009）+ record_fixture 录制真实响应供离线测试；预测报告 sha256 回归红线**已失效**：旧值 `a61d026b…f4eb` 于 2026-09-15 被 `be9c343`/`015c466`（给 `evidence.inputs` 加 `mu_mode`/`sigma_mode`/`dist_mode` 回显）改变，现行实测 `b69afb711f60ec533882bcc98b42c32c0503717a33708d8e74555c6a1a910bcb`；`grep -rn a61d026b` 全仓无任何测试/脚本引用（该红线从来没人守），P25 在做「仓库内可自动校验的不变量」
- **踩过的坑**：SQLite `recursive_triggers` 默认 OFF，`INSERT OR REPLACE` 隐式删行绕过 append-only 触发器、会静默覆盖已落库数据（已在 `connect()` 开启并加测试钉住）；幂等判定须先过 `_canonical()`（内存 `None` vs 库里 `0` 曾被误判为内容不同）；`data/` 曾被 `.gitignore` 整体忽略导致文件从未提交（已改根锚定忽略）；实验决策 `experiment_decisions` 幂等键曾含整份报告哈希 `report_sha256`（改一句措辞即判成新决策），ADR-005 改为语义键（措辞/时间戳/排版/report_sha256 退出键，NULL 比较用 null-safe `IS`）；`insert_bars` 冲突时曾把 `amount`/`turnover` 重写为 excluded 值（次日重灌抹掉已回填成交额），已改 COALESCE 保留既有值；raw 缓存 key 只含锚点 `end`、不含 `start` 且首写永久保留、无时效 → 同日 00:21 抓的盘中快照冻结整条链（2026-09-15 当日 K 线不落库、`amount` 回填跳过，人工挪走 6 个缓存文件才补上），已由 P18/ADR-009 修复
- **构建事实**：`pyproject.toml` 无 `[build-system]`、无 `[dev]` extra → `pip install -e '.[dev]'` 不可用
- **进度（2026-09-17 15:48 实测）**：P1–**P35** 已建成，**P36-retry ✅已完工收尾**（3提交 `d52b25a`/`fdbb152`/`7b8c0dd`）。亲跑 **pytest 1648 passed全绿 + `verify.sh` ✅all + git干净**（基线1635→1648）。**自排期队列已实质排空**：等用户三选一（风险读数 / 6→20只 / 停开发攒数据），确认无任务在跑时**不硬凑任务**。收盘链8步全exit=0：asof=2026-09-17，当日bars 6根/快照6行，明日(09-18)预测2条，review `reports/2026-09-17-review.json`，paper到09-17共5行，日历3265行到2026-09-17。模拟盘09-17（account_id口径）：arm-hold/arm-now 19818.91；discipline-05 19809.41/-10 19809.91/-15 19804.57；均<120天只并列不推荐。线上老页面仍是旧版，P36产物未上线。**P33 ✅ schema前滚单一入口**（ADR-015）。**P30 ✅ 日历未来休市表**（ADR-013）。**P29估值/资金流样本外评测 ✅（P35收尾）**：V1/V2均`LOSE`不采纳（commit `a56c0e5`）。
- **构建/验证基线（2026-09-17 15:48）**：先清 `__pycache__` 再 `.venv/bin/python -m pytest`（**别加 `-q`**）；pytest 计数轨迹 = 1528(P25) / 1536(P26) / 1571(P28b) / 1591(P32) / 1599(P33) / 1635(P35) / **1648(P36-retry轮，当前)**；`bash scripts/verify.sh` → `✅ 验证通过: all`。CLI 一律 `.venv/bin/python -m stocklab.cli.main <cmd>`；`predict run` 默认只写 JSON；`session backfill-close` 需显式 `--date YYYY-MM-DD`
- **表名/文件实测口径**：`quote_snapshots`（时点 `YYYYMMDDHHMMSS` 字符串）、`predictions`、`paper_nav_daily`（主键 date，**分组列是 `account_id`、不是 `arm`**）；复盘只有 `reports/YYYY-MM-DD-review.{md,json}`；append-only 触发器拦 DELETE，构造测试场景须先 `db init --db /tmp/xxx.db`
- **决策层 `action`/`size_pct` 口径**：`size_pct = 100 × (1 − 不利方向的概率质量)`（`stocklab/predict/model.py`），不含成本/风险预算、不知持仓、不是可执行建议
- **用户新增三需求（2026-09-16，排队/进行中）**：① 减持类建议必须按整手可执行过滤；② 项目管理每日检查 + 排期；③ 智能体全链路用真实数据看准确率（仅个人学习）
- **P16–P19（2026-09-15~16）**：P16 前端改版预算触顶中断但验收自跑全绿；P17 ETF标的池+ETF成本口径 ✅（6提交，$5.69，ADR-008）；P18 raw_cache当日时效修复 ✅（ADR-009）；P19 模拟盘三臂对照 ✅（ADR-010）。P16+P17两轮 ≈$19（用户对开发花销敏感）
- **模拟盘口径（P19，已定死）**：起跑日 2026-09-15 收盘、初始 20,000 元；三臂并行——① 什么都不动 ② 复制实盘账本 ③ 按纪律分散；约束沿用单票 ≤40%、现金 45–60%、止损 85.00、现价 ≥87.00 禁补仓；扣真实成本（ETF免印花税）；三臂只并列，不许挑一个当推荐
- **待办清单**：文件 `docs/TODO.md` + 网页 `claude.spring-ai.top/todo/`（`/var/www/stocklab-todo/`）
- **待用户决策（截至 2026-09-17 18:41自动化暂停等规划）**：① 交易日历未来休市表收口；② `experiment_decisions` 4行历史重复是否清理；③ 门户合并入 stock-lab（P37优先）；④ 整手过滤模型层改动碰红线需先问、项目管理每日检查需用户定形态。已决：止损线口径＝「**小于**就算跌破」；并发每项目≤1个。用户已授权自主排期与自主定下一轮任务，仅口径/范围类需问
- **三站实测（2026-09-18 curl 200全活）**：8791/lab 18372B、8792/console 48049B、8793/home 16483B；部署 `127.0.0.1:8791→/lab/`（渲染 `stocklab/labweb/render.py`+`static/app.css`）、8792→/console/（单文件 `static/index.html`）、8793→/home/（`portal/render.py` 内嵌CSS），经 nginx :80 反代
- **lab持仓实测（2026-09-18 /tmp/lab.html）**：总资产19818.91/现金11314.91/市值8504/累亏181.09，美的100股持有不动；全清到手8490.42、费用9.33
- **P37未落地**：prompt `/tmp/stocklab-p37.txt`、结果 `/tmp/stocklab-p37.json`，是否用新模型重跑待定；P38只登记未起；线上 `/lab/` 仍P36前旧版；基线 `d52b25a` 干净 pytest1648
- **等用户三选一（2026-09-18已盘完，勿擅自开工）**：A持仓看板/B账房/C门户 + 手机还是电脑 + 最卡的具体操作，再按 claude-dev 起任务
