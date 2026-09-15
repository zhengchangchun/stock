# 实验：sigma-rv-pct（自身 20 日 RV 分位条件化 sigma，**只预注册、未运行**）

> 本文件在**跑之前**写死。第 1–4 节是假设与判据，不是结果的复述；
> 第 5 节起才是跑完之后贴回来的真实输出（`<!-- RUN-OUTPUT -->` 之后）。

- **日期**：2026-09-15 ｜ **提出人**：claude（P9-a）
- **状态**：**已预注册 · 未运行**
- **口径版本**：`p8-metrics-v1`（比较前先 `assert_metric_version`，不一致直接报错）
- **完整报告**：`reports/2026-09-15-exp-sigma-rv-pct.{md,json}`
- **冻结项**（本实验**不许**改）：标签带 `FLAT_BAND=0.005`、`WINDOW=60`、
  `LEVEL_WINDOW=20`、`SIGMA_SCALE_MIN/MAX=0.5/1.5`、标的集合 `{000333, 600690}`、
  成本模型 `CostModel`、本金 1,000,000、样本区间与三段切分。变体 `ForecastSpec`
  里**没有**这些旋钮。

## 0. 多重比较声明（**预注册，不许事后补**）

本轮同时跑 **3 个变体**（`sigma-vol-z` / `sigma-rv-pct` / `sigma-index-rv-pct`），
它们：

- 共享**同一段 validate**（同交易日、同标的、同标签带）；
- 改的是**同一个字段** `sigma_mode` 的三个取值 → 三次比较彼此**不独立**。

处理方式（选一种并写死）：**不做 Bonferroni 校正，任何变体的 validate `WIN`
一律不直接晋级**。理由：n=3 次非独立比较下名义 95% CI 的覆盖不足，
校正又会把本来就稀疏的 651 个交易日压到没有鉴别力。

因此：

1. 本轮 `test` 段**一律不打开**（CLI 用 `--keep-test-sealed`），即使出现 `WIN`；
   `decide()` 记 `inconclusive` 并把理由写成「validate WIN，test 按预注册封存」；
2. 任何 `WIN` 只是「值得复现的信号」，须**下一轮用同一变体、同一判据、不调参**
   重跑复现后才谈晋级评审；
3. 报告如实写 `test_evaluated: false`。

## 1. 假设

> 波动率聚集意味着「20 日已实现波动率在**自身**过去 250 日 RV 中的分位」携带了 60 日无条件标准差丢掉的状态信息。用该分位缩放 sigma 应当改善样本外**校准度**，方向准确率不劣化。

**失效条件**（提前写死，事后不许改）：

- **方向**：validate 段 `Δdirection`（变体 − 基线，按日聚类）95% CI **下界 ≤ 0** → 本假设不成立。
- **校准**：validate 段 `Δbrier` 的 95% CI **上界 ≥ 0** → 本假设不成立。
- 两条**同时**满足才算 `WIN`；符号错记 `LOSE`，符号对但不显著记 `FLAT`；
  有效交易日 < 120 记 `INSUFFICIENT`（不算数）。validate 不到 `WIN` → 不开 `test`。
- **子预测（也提前写死，可被单独否证）**：预期 `Δbrier` 显著 < 0，且**改善幅度应大于** `sigma-vol-z` 与 `sigma-index-rv-pct`（自身 RV 是这条信息链上最直接的量）。若 `Δbrier` 显著 ≥ 0 → 否证。

**已知风险 / 反方预期**：基线的 `sigma` 是 60 日窗，本身已是波动率的平滑估计；
RV20 分位的增量可能整个落在噪声里（`FLAT`）。若 `Δbrier` 显著为正（校准**变差**，
过度自信）记 `LOSE` —— 那就是**否证**，不许去找个更小的 `SIGMA_SCALE_MAX` 再来一次
（那是全样本调参，红线 R7）。

## 2. 变量（只改一个）

| 项 | 基线值 | 实验值 |
|---|---|---|
| `sigma_mode` | `const`（`sigma = sigma_base`，不缩放） | `rv_pct` |

`mu_mode` / `WINDOW` / `LEVEL_WINDOW` / 关键位 / `action` / `cost` **逐行未动**。
`factor = 0.5 + 1.0 × q`，`q` = 20 日 RV 在自身过去 250 期 RV 中的分位（含当期）。
相对基线改掉的字段 = `["sigma_mode"]`（注册表校验器强制恰好一个）。

## 3. 数据

- 区间：`2013-04-16` → `2026-09-14`；切分 train 0.60 / validate 0.20 / test 0.20
  （连续不重叠；validate = 2021-04-30 → 2024-01-03，651 个交易日）
- 标的：`000333`、`600690`（**既有池子，未增删**）；`sh000300` 仅作无关（本变体不用指数）
- 数据快照：`data/stocklab.db`（`bars_daily` 至 2026-09-14；`amount`/`turnover` 全 NULL，**不可用**）
- 选择依据：`--selection-split validate`（默认值）

## 4. 判据摘要（预注册）

| 判据 | 阈值 |
|---|---|
| 方向 | `Δdirection` 的 95% CI 下界 > 0 |
| 校准 | `Δbrier` 的 95% CI 上界 < 0 |
| 样本量 | validate ≥ 120 有效交易日 |
| 晋级 | 本轮**不晋升**（见 §0）：validate `WIN` 也只记待复现信号，test 不打开 |

## 5. 真实输出（跑完后贴回）

<!-- RUN-OUTPUT -->

命令（在工作区根目录）：

```bash
.venv/bin/python -m stocklab.cli.main experiment run \
  --variant sigma-rv-pct --from 2013-04-16 --to 2026-09-14 --keep-test-sealed
```

## 6. 结论

<!-- VERDICT -->
