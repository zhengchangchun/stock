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
