# 调度链（P11）：各时点跑什么、失败怎么办、怎么补跑

> **本项目不实现任何 cron / 常驻进程**（ADR-001 D-05）。
> 这里定义的是一批**一次性命令**，由 nanobot 侧的调度器在指定时点调用；
> 命令本身不含定时逻辑，跑完即退。调度器怎么挂不在本项目范围内。

## 1. 时点表

| 时点（Asia/Shanghai） | 命令 | 干什么 |
|---|---|---|
| 交易日 09:35 / 11:35 / 13:35 | `session tick` | 抓快照 → 落库（幂等）→ 验证**已到期**的预测 → 滚动准确率 |
| 交易日 15:05 | `session tick` | 同上，且此时 `cutoff` = 今天 → 回填当日 `amount`/`turnover` + 验证 `target_date == 今天` 的预测 |
| 交易日 15:30 | `review daily` | 生成 `reports/YYYY-MM-DD-review.md` + `.json`（只读，不写任何数据表） |
| 交易日 15:30（`review daily` **之后**） | `paper step --asof <最新交易日>` | 推进模拟盘一天：三臂记净值 → `reports/paper/<asof>-paper.md` + `.json`（幂等，见 §1.1） |
| 每 2 小时（nanobot 开发巡检） | **不在本项目内** | nanobot 自己的开发流程巡检（总纲 §「每 2 小时开发巡检」），与本项目代码无关 |

**为什么盘中每 2 小时一次**：A 股同涨同跌，日内多次采集拿到的是**同一交易日的多个截面**，
用来留证「当天盘中看到过什么」；准确率的有效样本单位始终是**交易日**，不是采集次数。

### tick 的判定链

```
now ─► 读日历（可能为空，不报错）─► 抓快照 ─► 四态落库
                                      │
                                      ├─► 对每个「已收盘」的 captured_trade_date：回填 amount/turnover
                                      └─► 验证 target_date <= cutoff 的最近 --window 个到期日
                                              └─► 滚动准确率（按日聚类 + CI + LIVE/REPLAY 分列）
```

**到期判据（tick 不误打未到期预测）**：

```
cutoff = max{ d ∈ 日历交易日 : d < today，或 d == today 且本地时刻 ≥ 15:00 }
```

盘中跑时 `cutoff` = 上一交易日 → `target_date == 今天` 的预测**一根都不碰**。
拿盘中半截 bar 去给今天的预测打分，会写下一行「预测错了」而它根本不成立。

日历为空（未 `ingest index`）→ 摘要里报 `calendar.error` 并**不猜**：采集照做
（快照的身份键含源站 `ts`，猜错也不会造假行），但依赖日历的 `cutoff` / 回填准入
会显式降级为「跳过」并写进摘要。

## 1.1 模拟盘（P19）：`paper step` 进 15:30 链

```bash
cd /root/.nanobot/workspace/projects/stock-lab
.venv/bin/python -m stocklab.cli.main paper step --asof <最新交易日>
# 报告：reports/paper/<asof>-paper.md（+ .json）；退出码 0=完成（含幂等命中）/ 2=用法或库的问题
```

**前置条件（缺一不可）**

1. **当日 bars 已入库**：`step` 只用 `bars_daily`（`adj_mode='none'`）里 `date <= asof`
   的收盘价 → 必须排在 **`ingest bars` 之后**。取不到价会**退出 2 且一行净值都不写**
   （不落半截状态）。⚠️ 见 ADR-009：15:30 抓日K 可能命中当天盘中冻结的缓存 → 当日 bar 缺失
   → 本步失败，需按该 ADR 的排查命令处理后再补跑。
2. `--asof` 取**最新交易日**（不是自然日；非交易日跑会把净值记到不存在的交易日上）。
3. 建臂只跑一次：`paper init`（幂等）；全新库先 `stocklab db init` 前滚 schema。

**补跑安全**：`step` 幂等（同日重跑不重复下单、stdout 与报告逐字节一致），
落库前先补 `ingest bars` 即可。只读查看用 `paper show`
（不带 `--asof` 时：今天没净值会**回落**到最新净值日，并在 `asof_source` /
`disclosure` 里写明 —— ADR-010 D7）。

口径与已知限制见 **ADR-010**（禁方向择时 / 三臂并列 / 成本 / PIT / append-only /
整手约束下的 `[FAIL]`）。

---

## 2. 退出码

| 退出码 | 含义 | 出现在 |
|---|---|---|
| `0` | 做完了（**包含幂等命中**：`identical` 不重复写也算成功） | 全部三条命令 |
| `1` | 出事了，要人来看 —— 明细在 stdout JSON 的 `anomalies` / `skipped_*` 里 | `session tick`、`session backfill-close` |
| `2` | 用法或库的问题：库文件不存在、schema 未前滚（缺 `quote_snapshots`）、参数错误 | 全部三条命令 |

`session tick` 触发 `1` 的异常种类：`collect_failed`（接口失败）、`snapshot_field_error`
（源站字段不可用）、`snapshot_conflict`（同键不同内容）、`snapshot_missing_codes`
（请求了但源站没回行）、`verify_no_predictions`、`verification_conflict`。

> ⚠️ 已知口径：`session tick` 里回填的 `skipped_missing_bar`（有快照、无当日 bar）
> **不**计入 `anomalies`，所以**不会**让 tick 退出 1 —— 但它在摘要 `backfill[].skipped_missing_bar`
> 里看得见，并在 `system_events` 留一条 `warn`。手动补跑用 `session backfill-close`，
> 那条命令**会**退出 1。
>
> `review daily` 只有 `0` / `2`：它是只读报告，没有「部分失败」这个态。

## 3. 失败怎么办

### 3.1 采集失败（断网 / DNS / 超时 / 源站改格式）

表现（全部同时成立）：

- stdout 的 JSON 里 `collect.error` 非空、`ok=false`、`exit_code=1`；stderr 有 `⚠️ collect_failed: ...`；
- `system_events` 落一条 `module='session'`、`level='error'` 的记录；
- `job_runs` 本次作业以 `status='failed'` 收尾；
- **零行写入**：本次采集不写任何快照行（拒绝用旧数据/空数据顶替）。

**验证部分照跑**：采集只读网络、验证只读库，两者互不依赖 —— 「今天行情没采到」
不该连带把「昨天的预测也没验证」变成既成事实。

### 3.2 快照冲突（同 `(code, trade_date, ts)` 不同内容）

**不覆盖、不改写**已有行，计入 `conflicts` + `anomalies` + `system_events` error，退出 1。
这表示源站对「同一时刻的截面」给过两种说法 —— 需要人看，不是能自动收敛的状态。

### 3.3 任务根本没跑起来（进程被 kill / 调度器没触发）

命令在开跑时就 `job_runs` 记一行 `status='running'`（立即 commit）。若看到长期停在
`running` 的行，说明那次调用**中途死了** —— 这是「失败不留痕」的兜底探针。

### 3.4 库是旧的（缺 `quote_snapshots`）

退出 2，stderr 明确提示跑 `stocklab db init` 前滚 schema（不抛栈）。

## 4. 如何补跑

> **「幂等」的准确含义：不重复写同一份数据，而不是「行数不变」。**
> 快照的身份是 `(code, trade_date, ts)`，`ts` 是**源站自报的成交时刻**。
> 于是：
> - **同一刻的市场**重放（同 `ts`）→ `identical`，**零行新增**；
> - **盘中重跑** → 源站的 `ts` 已经前进，那是**另一个市场时刻** → 正常新增行，
>   这是设计如此，不是重复写（实测两次 tick 相隔 22 秒，`ts` 从 `20260915135046`
>   前进到 `20260915135110`，各得一行）；
> - **非交易日重跑** → 源站返回的仍是上一交易日的最后一条 tick，`ts` 不变 →
>   零行新增（这条性质是白送的，也是「今天是不是交易日」猜错也不造假的底气）。
>
> 验证一侧同理：四态里 `identical` 表示「这条已经验证过且内容一致」，**不重复写**。

「补跑」= 原样再跑一次，不会重复写同一份数据：

| 场景 | 补跑方式 |
|---|---|
| 某个时点的 tick 没跑成 | 直接重跑 `session tick`（快照按 `(code, trade_date, ts)` 去重，验证按四态去重） |
| 收盘那次 tick 没跑成，但快照已落库 | `session tick --no-capture`（**离线**：跳过联网，只做回填 + 验证）；或只补回填用 `session backfill-close --date D` |
| 只想补某一天的 `amount`/`turnover` | `session backfill-close --date YYYY-MM-DD` |
| 补出来的报告 | `review daily --date YYYY-MM-DD`（默认今天） |
| 测试/复盘时想假装某个时刻 | `--now 2026-09-15T15:05:00+08:00`（仅供测试/补跑） |

```bash
# 典型：收盘链补跑（离线，不联网）
.venv/bin/python -m stocklab.cli.main session tick --no-capture
# 典型：只补某日回填
.venv/bin/python -m stocklab.cli.main session backfill-close --date 2026-09-15
# 典型：补当天的复盘报告
.venv/bin/python -m stocklab.cli.main review daily
```

### `session backfill-close` 的语义（写死，不会越界）

`amount`/`turnover` 是**当日累计**量，来源是快照（`ingest bars` 的腾讯日K 响应里
**没有**这两个字段，它只能写 NULL）。回填遵守五条：

1. **只补那一天**：SQL 是 `WHERE code=? AND date=?` —— 语句层面够不到别的日期；
2. **只补 NULL**：`COALESCE(amount, :amount)` —— 已有值的行原样不动；
3. **只用当日最后一条快照**：中途那条是「半天成交额」，数值合法、含义全错；
4. **`adj_mode != 'none'` 硬拒绝**：与 `repo.insert_bars` 同一把闸（不复权是
   `bars_daily` 的唯一合法口径）；
5. **缺数据不静默**：当日没有 bar 行 → `skipped_missing_bar` + `system_events` 留痕 + 退出 1；
   快照里该字段为空 → `skipped_no_value`，**不填 0、不插值**。

摘要里另有两个 `reason`（都是「什么都没做」但要看得见）：该日**没有任何快照** →
`reason: "no_snapshots"`，**退出码 0**（本来就没东西可补，不是异常）。

### 「历史 NULL 不碰」的语义

`bars_daily` 里 **2026-09-15 回填上线之前**的 `amount`/`turnover` 全部是 NULL
（实测 14164 行恒 NULL），这个 NULL 的语义是**「当日未采集」**，不是 0、不是待插值。
回填**永不触碰**它们：作用域锁死在当日，且只补 NULL。

机器可读证据是 `review daily` 报告里的 `gaps.amount_first_date` —— 它要么是 `null`
（还没有任何一天有值），要么是一个**不早于回填上线日**的日期。

> 附带的双保险：`repo.insert_bars` 的 `ON CONFLICT DO UPDATE` 已改成
> `COALESCE(excluded.amount, bars_daily.amount)`。原先写 `excluded.amount` 时，
> 因为源站日K 里没有 amount 字段，**每天重跑 `ingest bars` 都会把前一天刚回填好的
> `amount` 抹回 NULL** ——「源站没给」不等于「源站说要清空」。详见 ERROR_DIARY。

## 5. 其它不变量

- **不做滚动清理**：任何表（含 `quote_snapshots`）历史全留，没有「只保留近 30 天」这种删除路径；
- **`review daily` 是只读**：跑完前后所有表行数逐个不变，`experiment_decisions` 一行都不写；
- **报告可复现**：同输入两次运行 → md 与 json **逐字节一致**（正文不含生成时刻）；
- **口径不许漂**：报告把准确率拆成 `LIVE（实盘累计）` / `REPLAY（PIT 历史回放）` 两桶分列，
  判据是 `created_at 日期 == asof_date`。LIVE 桶为空时报告明写「窗口内 0 行」，
  **不得**把回放数字称为「实盘表现」。

## 6. 相关文件

| 内容 | 文件 |
|---|---|
| 设计稿 | `docs/plans/2026-09-15-p11-调度链.md` |
| 任务拆分 | `docs/tasks/2026-09-15-p11-调度链-task45-52.md` |
| 快照抓取 | `stocklab/data/fetch.py` |
| 快照领域层 / 四态落库 | `stocklab/session/quotes.py`、`stocklab/session/store.py` |
| 收盘回填 | `stocklab/session/close.py` |
| tick 编排 | `stocklab/session/tick.py` |
| 复盘报告 | `stocklab/session/review.py` |
| CLI 接线 | `stocklab/cli/main.py`（`cmd_session_tick` / `cmd_session_backfill_close` / `cmd_review_daily`） |
| 测试 | `tests/test_session_quotes.py`、`tests/test_session_store.py`、`tests/test_session_close.py`、`tests/test_session_tick.py`、`tests/test_session_review.py`、`tests/test_cli_session.py` |
