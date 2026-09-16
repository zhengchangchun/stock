# 计划：趋势状态标签正式版验证（预注册 `trend-state-hit-rate`）

- **日期**：2026-09-16
- **唯一口径来源**：`docs/experiments/2026-09-15-trend-state-hit-rate.md`（**只读、不可改**）
- **本计划的作用**：把预注册的口径**翻译成可执行的结构**，不新增任何口径。
  凡预注册没写死、而实现必须选一个的地方，全部列在 §4「口径落地说明」，
  **在跑正式版之前定死**，跑完不许改。

## 1. 交付物

| 文件 | 内容 |
|---|---|
| `stocklab/trend/state.py` | 状态标签**纯函数**（PIT）+ 前向收益/命中 + PIT 自证工具 |
| `stocklab/trend/evaluate.py` | 取数、行构造、M1/M2、按日聚类 bootstrap、F1/F2/F3 判定、报告渲染 |
| `stocklab/cli/main.py` | 新子命令 `stocklab trend evaluate`（风格与 `experiment run` 一致） |
| `tests/test_trend_state.py` `tests/test_trend_evaluate.py` `tests/test_cli_trend.py` | TDD 测试 |
| `reports/2026-09-16-trend-state-hit-rate.{md,json}` | 产物（`reports/` 被 gitignore） |

**不写**：任何生产表（`predictions` / `verifications` / `bars_daily` … 只读）、
`docs/experiments/`（nanobot 维护）。

## 2. 任务拆分（每个任务独立验证）

- **T1**：`state.py` 状态标签 + 前向收益 —— 测试：等号边界、不足 60 根、PIT 截断自证、与
  `risk.rules.trend_state` 逐点一致（同一口径的两份实现必须相等）。
- **T2**：取数 + 行构造（PIT：`state_t` 只用 `≤ t` 收盘；行按**实现日 `t+N`** 归属分段）。
- **T3**：M1 条件命中率（UP/DOWN）vs 按日配对的无条件基线 + 按日聚类 bootstrap CI。
- **T4**：M2 延续率 `P(state_{t+5}=state_t)` vs 该状态无条件频率 + 按日聚类 CI。
- **T5**：F1/F2/F3 + 达标规则 → 判定；test 段默认封存、达标才打开一次。
- **T6**：渲染 + 落盘（md/json，含种子、数据快照哈希、报告 sha256、两段日期范围）+ 稳健性附表。
- **T7**：CLI 子命令 + 拒收预注册外标的。
- **T8**：跑正式版，贴数字，写报告。

## 3. 复用的既有框架

- `experiments/split.py`：`split_days` / `boundaries`（60/20/20 按日期连续切）
- `experiments/metrics.py`：`daily_stats`（按日均值 + 正态近似 CI）、
  `BOOTSTRAP_N=2000` / `BOOTSTRAP_SEED=20260915`（种子写死 → 可复现）
- `verify/report.MIN_DAYS = 120`（F3 门槛同源同值，不另写一个 120）
- `verify/replay.session_dates`：交易日轴 = 日历 ∩ 行情

## 4. 口径落地说明（预注册未写死、实现必须选一的点；**跑之前定死**）

| # | 预注册原文 | 落地选择 | 理由 |
|---|---|---|---|
| L1 | 「未来 5 个交易日收益方向」 | `hit = 1 ⟺ close_{t+5} > close_t`（不复权），无 FLAT 带 | 预注册「无 FLAT 阈值」只提状态定义；读数 A 的基线 ≈ 0.51 与之相符。平局（价格完全相等）计 0 并**单独计数**上报 |
| L2 | 「按日聚类」 | 同一交易日的**所有标的/状态**进同一簇；bootstrap **按日期**重采样 | 总纲；同一天多标的不是独立样本 |
| L3 | F1 的 Δ | 日级配对：`Δ_d = mean(hit \| state=S, 日 d) − mean(hit \| 全部行, 日 d)`，再对 `Δ_d` 做日聚类 bootstrap | 与 `metrics.paired_daily_delta` 同一纪律：比较这一步也要按日聚类。基线用**当日同一批行**（与读数 A「基线与状态同集合」一致） |
| L4 | F2 的「该状态的无条件频率」 | `freq_S(d) =` 当日 `state=S` 的行占比；`Δ_d = mean(same \| S, 日 d) − freq_S(d)` | F2 未说用哪个状态的频率；与 F1 对称取 UP/DOWN 两侧，FLAT 只登记 |
| L5 | F2 的判定范围 | (b) 成立 ⟺ **UP 与 DOWN 两侧**延续率差值 CI 下界 **均 > 0** | 与 F1 的「两侧都要」对称（读数 B 三态都 +17~45pp，该选择不改变结果方向） |
| L6 | 「沿用 P8 框架的切分」 | 行按**实现日 `t+N`** 归段（P8 按 `target_date` = 打分日归段，这里打分日 = `t+N`） | 避免 validate 的标签吃掉 test 段的价格（跨段标签泄漏）；等价于天然 embargo |
| L7 | 「标的严格 000333/600690/sh000300」 | CLI 硬编码预注册标的，传入其它代码**直接拒收** | 扩大样本量属于改设计，需另立预注册 |

## 5. 判定规则（照抄预注册 §1）

```
有效交易日 < 120          → inconclusive，test 不打开
判据 (a) 或 (b) 至少一条成立 → validate 达标 → 打开 test 复核一次
两条皆不成立               → falsified，test 不打开
```
- (a) 成立：UP 与 DOWN 两侧 `Δ` 的日聚类 95% CI **下界均 > 0**
- (b) 成立：UP 与 DOWN 两侧延续率差值的日聚类 95% CI **下界均 > 0**
- 打开 test 后：validate 成立的那条判据**在 test 上同样成立** → `WIN`；否则 `falsified`
  （沿用 `metrics.decide` 的纪律：样本外的胜利没能复现 → 不许嘴硬）

## 6. 稳健性附表（不得据以改判据）

`N ∈ {1,10,20}`、`MA(10,30)`、`MA(5,20)` 的 Δ 点估计 + CI，**只进附表**。

## 7. 验证

```bash
.venv/bin/python -m pytest -q          # 基线 1390 collected，只增不减，全绿
bash scripts/verify.sh                 # 必须 ✅
.venv/bin/python -m stocklab.cli.main trend evaluate --report reports/2026-09-16-trend-state-hit-rate.md
git log --oneline -5
```
