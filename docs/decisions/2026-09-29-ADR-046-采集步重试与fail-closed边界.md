# ADR-046 采集步重试与 fail-closed 的边界（P100）

日期：2026-09-29 ｜ 状态：已实施 ｜ 上游：P100 任务书
（[`docs/tasks/2026-09-29-p100-收盘链抗瞬时故障.md`](../tasks/2026-09-29-p100-收盘链抗瞬时故障.md)）
｜ 事故复盘：[`docs/ops/2026-09-28-收盘链瞬时故障与补跑.md`](../ops/2026-09-28-收盘链瞬时故障与补跑.md)

## 背景

2026-09-28 15:30 那一刻 DNS 挂了 ⇒ `ingest index` exit 1 ⇒ **交易日历没有前滚**
（`ingest index` 是日历的唯一来源，ADR-001 B4）⇒ `predict run` 被
`predict/service.py::assert_session` 判「非交易日」拒绝 ⇒ 收盘链停在 9/14 步：
当天的 live 预测、验证行、复盘报告、模拟盘推进全部没跑，18:30 的 AI 操盘手日更
也因此拿到 `unknown` 而**零决策**。次日早上巡检自愈了日历、补出了预测行与报告，
但**那一天的实时口径与决策时段永久缺失**。

要修的不是「DNS 会挂」（修不了），而是「**一次瞬时失败就判死一整天、整条链一次重试
都没有**」。

## 决定

### 1. 重试是**链声明**的，不是执行器的默认（`ops/runner.py`）

`run_steps` 新增可选 `retry_plan: Mapping[str, int] | None = None` 与
`retry_delay_s: float = 15.0`；新增常量 `RETRY_DELAY_S = 15.0`、`RETRY_ATTEMPTS = 3`。

- **缺省 `None` ⇒ 完全走原来的路径**：每步一次、不 sleep、结果里**一个新键都没有**。
  这一条是硬约束：巡检侧与全部既有调用方逐字节不变，重试只能由链自己声明。
- 声明过的步骤在「**失败形状**」时可再试，上限 `retry_plan[name]`（`1` ≡ 缺省）。
  失败形状 = 退出码非 0 **或** `exit_code is None`（子进程没起来）。
- **`timeout` 不重试**：超时说明预算/挂死，重试只会更糟。
- **等待必须装得进整轮 deadline**：`remaining <= 0` 或 `retry_delay_s >= remaining`
  ⇒ 不重试，按既有 `budget_exhausted` 路径收尾（`worst_code` ⇒ 2，不许报绿）。
- 结果**只增**三键：`attempts` / `retried` / `exit_codes`；`exit_code` 仍是**最后一次**
  尝试的值（`worst_code`、回执摘要、`bad=` 的既有语义因此一字不变）；
  `duration_s` 改为**全部尝试的累计**（写进 docstring，别被读成最后一次）。

**为什么是 3 次 × 15 s**：这类瞬时故障（resolver 抖动 / 代理重启 / 网关重置）的恢复
时间在「几秒到十几秒」；间隔太短会在同一个坏窗口里连撞三次（等于只试了一次），太长的
话两次重试的开销（30 s）会吃掉收盘链 900 s 预算的可观比例。三次全失败基本可判定为
真故障 —— 再试只是把预算耗在一件已确定失败的事上。

### 2. 只有 `ingest *` 可重试（`ops/chain.py`）

`CLOSE_RETRY_PLAN` / `MONTHLY_RETRY_PLAN` 用同一条规则取
（`_ingest_retry_plan`：步骤 argv 的第一段是 `ingest`），**不手抄命令名清单** ——
清单漏跟的那一步会静默地没有重试，而下一次瞬时故障那天才看得出来。

- **可重试**：收盘链 6 条 ＋ 月度链 5 条 `ingest *`。它们**幂等**（首写保留 /
  `INSERT OR IGNORE` / 按 `--days` 增量）且**联网络** —— 事故的形状正是网络瞬时失败。
- **不重试**：`session_*` / `predict_run` / `verify_pending` / `review_daily` /
  `paper_step` / `m2_daily` / `doctor`。它们不是网络故障的形状（失败说明库或口径上
  有事），重试只会**掩盖**真问题；`predictions` 还是 append-only，盲目重试撞冲突只会
  多几条噪声。
- `weekly` / `quarterly` 两条链**本档不加**重试计划（季度链也有一条 `ingest_financials`）：
  P100 的范围锁定在收盘链＋月度链，动它们＝多一处行为变化；要做另立任务书。
  （周/季度链的重试缺席**不静默**：它们的回执里没有 `retried_steps`，因为没有计划。）

### 3. 不静默：重试必须看得见（`ops/chain.py`）

- `anomalies` 新增 `kind="step_retried"`（文案含 step 名与每次退出码）——哪怕它最后
  **成功**。重试后成功的步 `exit_code=0`，整链**不**因此变红（既有 `worst_code` 语义），
  但「今天这轮抖了一下」不能从回执里消失。
- 回执新增顶层键 `retried_steps`（`[{name, attempts, exit_codes}]`，无重试 ⇒ `[]`）。
- 摘要行只在**真的重试过**时追加 ` retried=N`（没重试 ⇒ 逐字节不变）。
- 新增 `kind="calendar_not_forward_rolled"`：收尾体检发现「asof 不在 `trading_calendar`」
  或「日历 max < asof」时，**收盘链自己的回执**点名那句因果 ——
  「`ingest_index` 是交易日历的唯一来源（ADR-001 B4），它失败就会连带 `predict_run`
  判非交易日」。巡检侧的同义检查（`patrol.plan`：日历 STALE ⇒ 补 `ingest_index`）**不动**；
  这一条补的是「只看收盘链回执的读者」看不见因果的缺口。

## 为什么不放开 `assert_session` 的 fail-closed

`predict run --asof X` 在 X 不被交易日历覆盖时 exit 2，是**刻意**的（ADR-001 / P46）：

1. **日历是判据，不是提示**。prediction 是 append-only 的：允许「日历没覆盖也照样跑」
   等于允许在**不确定**的日子里写一批退不回来的行。而「日历没覆盖」最常见的成因恰恰是
   「采集没跑成」（本次事故）—— 那时真正该做的是**补采集**，不是绕过判据。
2. **绕过它会把「链没跑完」改写成「链跑完了」**。这正是 ERROR_DIARY #54 那一族形状
   （预算用尽却报 0）：launchd 侧只看退出码，一个被绕过的判据会让红灯变绿灯，
   第二天的自愈就再也不会被触发。
3. **代价不对称**：重试只花 30 s 且**幂等**；绕过判据换来的是错口径的数据。
   ⇒ P100 选的是「让瞬时失败重试」，不是「让不确定性通过」。

由此引申出本档的**边界**：`predict/service.py::assert_session`、`bars_finalized_on`、
`worst_code` 的三条停止线、`cross_db_refusal` **一个字都不动**（L1）。

## 取舍记录（两处，都写在这里以免下次被当成 bug）

1. **`exit_code is None`（子进程没起来）算失败形状 ⇒ 会重试**（P100 §1 的定义），
   而任务书 §4.6 给的缺省倾向是「不重试」。取「重试」的理由：起不来在 15:30 那种并发
   压力下（fork / FD 耗尽）同样是**可能瞬时**的，而重试的代价被 deadline 与尝试上限
   双重封顶；真要一直起不来，三次之后照旧如实报 `step_error` 断链。
2. **`test_ops_close.py::test_monthly_a_blocking_step_failure_is_still_fatal` 被改了**
   （任务书 §4 的「旧用例不改」与本档 L3「月度链 `ingest_*` 要重试」不能同时成立：
   那条用例直接断言「失败的那一步只被调用一次」）。改法是**保住牙齿**：仍然断言
   「链停在这一步、退出码 2、后面的步一步没跑」，只把「一步 = 一次调用」换成
   「一步 = `RETRY_ATTEMPTS` 次调用」，并把重试间隔打桩成 0（否则那条用例会真睡 30 s）。
   硬约束（L3）优先于措辞（「旧用例不改」）。
