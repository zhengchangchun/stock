# 实验：resid-quantile（区间构造改取「训练窗标准化残差经验分位」，**预注册 · 待跑**）

> **本文件在跑数据之前写死并 commit。** 第 1–4 节是假设与判据，不是结果的复述；
> 第 5 节起才是跑完之后贴回来的**真实 CLI 原始输出**。
> 判据一旦落到这里，事后**不许改**（红线 R7 / 台账规则 6）。

- **日期**：2026-09-15 ｜ **提出人**：user（P10-a）
- **状态**：**预注册 · 待跑**（跑完按判据写 `falsified` 或 `WIN`）
- **口径版本**：`p8-metrics-v1`（比较前先 `assert_metric_version`，不一致直接报错）
- **预注册文件**：本文件；**计划**：`docs/plans/2026-09-15-p10a-残差分位区间.md`
- **冻结项**（本实验**不许**改）：`FLAT_BAND=0.005`、`WINDOW=60`、`LEVEL_WINDOW=20`、
  `SIGMA_SCALE_MIN/MAX=0.5/1.5`（**本轮根本不走缩放**）、`sigma_mode="const"`、
  `mu_mode="sample_mean"`、标的集合 `{000333, 600690}`、成本模型 `CostModel`、
  本金 1,000,000、样本区间与三段切分（train 0.60 / validate 0.20 / test 0.20）。

## 0. 这条轴与「已被否证的缩放轴」的区别，以及多重比较声明

**已否证的是 `sigma_mode` 的缩放轴**（`vol_z` / `rv_pct` / `index_rv_pct`，
见 `docs/experiments/2026-09-15-sigma-*.md`）：三个变体 Δbrier 一致**显著为正**
（过度自信）。本轮：

- **不重跑缩放**：不改 `SIGMA_SCALE_MIN/MAX`、不换 `WINDOW`、`sigma_mode` 保持 `const`；
- **改形状来源**：`mu ± z_gaussian·sigma` → `mu ± q_p(残差分布)`，其中 `q_p` 是
  **训练窗内**标准化残差 `(r_t − mu_hat_t)/sigma_hat_t` 的经验分位。

本轮**只跑 1 个变体**（`resid-quantile`）→ 无多重比较问题（与 P9-a 一轮三变体不同）。

**test 段封存**：本轮预注册声明「即使 validate `WIN` 也不打开 test」
（CLI `--keep-test-sealed`）→ `test_evaluated` 必须为 `false`。
若 validate `WIN`，记 `inconclusive`（「值得复现的信号」），须下一轮同变体、同判据、
**不调参**重跑复现后才谈晋级评审。理由：这是形状轴上的第一个变体，
单次 validate 胜利不足以支撑对封存段的唯一一次读取。

## 1. 假设

> 基线把次日收盘收益的分布**假设**成 `Normal(mu, sigma)`，它的 `range_80` 与三分类概率
> 全部是解析分位。这个**形状假设**从未被检验过。把形状来源换成**训练窗内标准化残差**
> `(r_t − mu_hat_t)/sigma_hat_t` 的**经验分位**（分位只由 train 段拟合，apply 到 validate），
> 应当改善样本外 **Brier（校准度）**，且 `range_80` 的覆盖率**不劣化**。

**失效条件**（提前写死，事后不许改）：

- **主指标 (a) Brier**：validate 段按日聚类的配对 Δ（变体 − 基线）须 **Δ < 0 且 CI 上界 < 0**。
  两个 CI **同时**要满足：(a1) CLI gate 的 `ci95`（正态近似，`p8-metrics-v1` 口径）；
  (a2) 预注册的**按日 bootstrap CI**（重采样单位是**交易日**，2000 次，seed 固定）。
  两者结论若不一致 → **保守取值记 `falsified`**，并把分歧原样贴进 §5。
- **副指标 (b) 覆盖率**：`|cov_variant − 0.80| <= |cov_baseline − 0.80|`，
  两侧都用 `summary.range.coverage_daily.mean`（**按日聚类**，不是行级）。
- **副指标 (c) 方向**：**不为显著负** —— 即不满足「Δdirection < 0 且 CI 上界 < 0」。
- **WIN** 需 (a) ∧ (b) ∧ (c) **同时**成立。
- **LOSE / `falsified`**：任一不满足 → 台账写 `falsified`，
  **绝不许打开 test 段**，**绝不许**回调参数（换分位估计量 / 平滑经验分布 / 换窗）
  再试同一变体 —— 那是在 validate 上做参数搜索（红线 R7）。
- **样本量**：validate 段有效交易日 < **120** → 直接写「样本不足，实验作废」并停。

**可比对手**：同一份报告里、**同一次取数**重算的基线 `pit-rw-v1.0.1`
（`dist_mode="gaussian"`），以及 P7 报告内的常数基准（`always_up` 等）与 `index_300`
—— 后者只作背景，不作判据。

**已知风险 / 反方预期（事先写下，不许事后当成解释）**：

1. 经验分布的中心质量通常**比同 `sigma` 的高斯更窄**（尾部更厚）→ 0.10/0.90 分位
   可能落在 `±1.2816` **内侧** → 覆盖率**下降**。基线 81.68% 本就略过覆盖，
   下降可能恰好逼近 0.80，也可能击穿到 0.80 以下 → 判据 (b) 失败。
2. 经验 CDF 在中心更陡 → `p_flat` 被推高；若真实 flat 频率没那么高，Brier 变差。
3. 训练残差池给出的是**常数形状**，不随状态变化。本轮只检验「形状」，
   **不**检验「状态条件化」（那正是 P9-a 已被否证的缩放轴）。
   **不得**因 (3) 的观察去加状态条件化重跑 —— 那是第二个变量。

## 2. 变量（只改一个）

| 项 | 基线值 | 实验值 |
|---|---|---|
| `ForecastSpec.dist_mode` | `gaussian` | `resid_emp` |

`mu_mode` / `sigma_mode` / `WINDOW` / `LEVEL_WINDOW` / 关键位 / `action` / `cost`
**逐行未动**。相对基线改掉的字段 = `["dist_mode"]`（注册表校验器强制恰好一个）。

### 2.1 变量的精确边界（**必须先读这一条**）

「区间构造」这个名字**窄于**实际改动。次日的 `direction` 三分类概率与 `range_80`
是**同一个预测分布**的两个泛函：分布的 CDF 与分位数。只换 `range_80` 的构造、
把 `direction` 留给高斯 CDF，得到的**不是一个分布**（CDF 与分位互相矛盾），
而且**主指标 Brier 会恒等于基线**（Δ ≡ 0）→ 实验在结构上不可能 WIN，等于没做。

所以本轮的变量精确定义为：**次日收盘收益预测分布的「形状来源」**
（`gaussian` → `resid_emp`）。`direction` 与 `range_80` 是这**一个**变量的两个泛函，
**同时且只能同时**改变。这是**一次**比较，不是两次。

**明确不在变量内**（冻结、逐行不动）：`p_touch` / `key_levels` / `invalidate_if`。
`p_touch` 是**日内路径**量的近似（用实测影线均值估「够不够得到某价位」），
它的定义域不是收盘收益分布，保持高斯。

### 2.2 残差的定义与拟合（PIT）

对 train 段每个交易日 `t`（`asof = t 的上一交易日`）：

```
mu_hat_t    = fmean(最近 WINDOW=60 个对数收益)          # 与 compute_forecast 逐位同式
sigma_hat_t = stdev(同上)                                # 同上
resid_t     = (log(close_t / close_asof) − mu_hat_t) / sigma_hat_t
```

残差池 = 全部 train 交易日的全部标的的 `resid_t`，**只在 train 段拟合**，
apply 到 validate（以及将来可能打开的 test）。`sigma_hat_t <= 0`、
`asof` 不是紧邻交易日、K 线不足 `max(WINDOW, LEVEL_WINDOW)+1` → 该 `(日, 标的)`
**剔除并计数**，不填 0。

应用（validate 的某天，`z = (log(1±FLAT_BAND) − mu)/sigma`）：

```
p_down = F(z_lo);  p_up = 1 − F(z_hi);  p_flat = F(z_hi) − F(z_lo)   # ≥ 0（单调性保证）
range_80 = [close·exp(mu + sigma·Q(0.10)),  close·exp(mu + sigma·Q(0.90))]
```

其中 `F` = 残差池的 ECDF（`#{v ≤ z} / n`），`Q` = 其逆（`min{v : F(v) ≥ p}`）。
`Q` 是**非对称**的 —— 残差分布偏斜时左右分位天然不同，这正是「形状」要检验的东西。
分位估计量与 `F` 的定义**写死在这里**：`p=0.10/0.90`、`n=len(resid 池)`，
不做任何平滑、不插值、不加先验。

## 3. 数据

- 区间：`2013-04-16` → `2026-09-14`；切分 train 0.60 / validate 0.20 / test 0.20
  （连续不重叠，与前几轮逐日同轴）
- 标的：`000333`、`600690`（**既有池子，未增删**）；`sh000300` 仅作背景（本变体不用指数）
- 数据快照：`data/stocklab.db`（`bars_daily` 至 2026-09-14，14164 行）
- 选择依据：`--selection-split validate`（默认值）

## 4. 判据摘要（预注册）

| 判据 | 阈值 | 怎么读 |
|---|---|---|
| (a) Brier | `Δ < 0` 且 CI 上界 `< 0`，gate `ci95` 与 bootstrap CI **都要**满足 | `splits.validate.paired.brier` |
| (b) 覆盖率 | `\|cov_variant − 0.80\| <= \|cov_baseline − 0.80\|` | `summary.range.coverage_daily.mean` 两侧 |
| (c) 方向 | 不显著负 | `splits.validate.paired.direction` |
| 样本量 | validate ≥ 120 有效交易日 | `pair_rows` 的 `n_days` |
| 晋级 | 本轮**不晋升**（见 §0）：validate `WIN` 也只记待复现信号，test 不打开 | `test_evaluated` 必须 `false` |

首次运行前先确认 validate 段「按日聚类 + bootstrap」有 ≥120 有效交易日；
不足 → 写「样本不足，实验作废」并停。

## 5. 真实输出（跑完后贴回）

命令（在工作区根目录；**唯一**的区别是本轮多跑了一次 bootstrap 辅助判据）：

```bash
.venv/bin/python -m stocklab.cli.main experiment run \
  --variant residual-quantile-interval --from 2013-04-16 --to 2026-09-14 --keep-test-sealed
```

**三段切分**：train `2013-04-16`→`2021-04-29`（1955）/ validate `2021-04-30`→`2024-01-03`
（**651**）/ test `2024-01-04`→`2026-09-14`（653，**未评估**）。
`test_evaluated = false`；封存原因（机器原文）：`validate 段 gate=FLAT（未达 WIN）→ 封存段不打开`
+ 「本轮预注册也声明了即使 WIN 也封存」。变体 `changed_fields = ["dist_mode"]`。

### 5.1 判据 (a) 主指标 Brier（两个估计量都要满足）

| 估计量 | Δ 均值 ± 标准误 [95% CI] | 有效交易日 | `Δ<0` | `CI 上界<0` |
|---|---|---|---|---|
| (a1) gate `ci95`（正态近似，`p8-metrics-v1`） | **-0.0017 ± 0.0016** [-0.0048, **+0.0014**] | 651 | ✅ | ❌ |
| (a2) 按日聚类 bootstrap（2000 次，seed `20260915`，重采样单位=**交易日**） | **-0.001693** [-0.004901, **+0.001392**] | 651 | ✅ | ❌ |

**两个估计量结论一致**（都在 0 两侧、上界都 > 0）→ §1 的「两者不一致则保守取值」条款
**未触发**，不存在需要贴出的分歧。点估计**符号是对的**（Brier 变小 0.0017），但
**未达显著** → **判据 (a) 不成立**。

### 5.2 判据 (b) 副指标 `range_80` 覆盖率（按日聚类 `coverage_daily.mean`）

| 口径 | 覆盖率 | \|cov − 0.80\| |
|---|---|---|
| 基线 `pit-rw-v1.0.1` | 0.819508 | **0.019508** |
| 变体 `…+residual-quantile-interval` | **0.800307** | **0.000307** |

变体远更接近标称 → **判据 (b) 成立**。注意这正是 §1 风险 1 预写的方向：
训练残差分位 `q_0.10=-1.196708` / `q_0.90=+1.246752` **都落在高斯 `±1.2816` 内侧**
（区间更窄）→ 覆盖率**下降**；下降的落点几乎正好是标称值（80.03%），
把基线的**过覆盖**（81.95%）修掉了。

### 5.3 判据 (c) 副指标方向

| 指标 | 变体 | 基线 | Δ [95% CI]（按日聚类） |
|---|---|---|---|
| 方向准确率（`accuracy_daily.mean`） | 0.371736 | 0.368664 | **+0.0031 ± 0.0090** [-0.0145, +0.0206] |
| bootstrap 方向 Δ | — | — | +0.00307 [-0.01536, +0.02074] |

「显著负」不成立 → **判据 (c) 成立**。

### 5.4 行级 / 配对计数与拟合溯源

- 配对：`n_pairs = 1302`、`baseline_rows = 1302`、`variant_rows = 1302`、
  `keys_baseline_only = 0`、`keys_variant_only = 0`、`unscorable_either = 0`；
  全报告 `baseline_rows = 4898` / `variant_rows = 4898`（train + validate 两侧同数）。
- 残差池（**只在 train 段拟合**）：`n = 3581`、`n_days = 1943`、`n_skipped = 328`、
  `window = 60`、train 区间 `2013-04-16`→`2021-04-29`、`mean = +0.005341`、
  `q_50 = -0.017452`（中位数略偏负 = 轻微左偏，形状确实不居中）。
  跳过项已**显式计数**（区间起点无上一交易日 / `asof` 当日无 K 线），未填 0。

### 5.5 gate 与预注册判据的措辞差异（如实贴出，不调口径）

| 来源 | 记的是什么 | 原文 |
|---|---|---|
| 机器 gate（`metrics.gate`） | `FLAT` | 「符号对但未达显著 —— 『差一点』不是结论」 |
| 机器结论（`metrics.decide`） | `inconclusive` | 「validate 段未达显著 → 不打开 test；『差一点』不是结论，也不许挪口径」 |
| **本台账（预注册 §4 判据表）** | **`falsified`** | §1 写死：「WIN 需 (a) ∧ (b) ∧ (c) 同时成立；**LOSE / `falsified`：任一不满足**」 |

三者**没有互相矛盾的数字**，差异只在「非 WIN 叫什么」：机器的 `FLAT` / `inconclusive`
描述的是**显著性档位**，本台账按预注册的**更严口径**记 `falsified`（假设未被证实）。
按用户指令「口径不一致时保守取值」，最后一行（`falsified`）为**本实验的正式结论**；
三个词都不等于「采纳」，`test` 段均不打开。train 段 gate 同为 `FLAT`。

## 6. 结论

**`falsified` —— 假设未被证实，不采纳，基线 `pit-rw-v1.0.1` 保持不表。**
test 段**未打开**（`test_evaluated = false`），且按 §0 预注册**不开**（即使 WIN 也不开）。

**判据逐条**：`(a)` ❌ 未达（两个 CI 上界都 > 0，点估计符号对但落在噪声内）｜
`(b)` ✅ 成立（覆盖率 80.03%，\|cov−0.80\| 从 0.0195 降到 0.0003）｜
`(c)` ✅ 成立（方向 Δ=+0.0031，不显著负）｜样本量 ✅（651 ≥ 120）。

**学到了什么（两条，都不是「差一点」的借口）**：

1. **形状轴确实能修校准的「幅度」，但没有带来准确率**。换上训练窗经验分位后
   `range_80` 覆盖率几乎正中标称（81.95% → 80.03%），这是本轮**唯一扎实**的观察：
   基线的过覆盖来自高斯尾部太厚，经验分位把它修平了。
2. 但**主指标 Brier 没有显著改善**（Δ=-0.0017，CI 跨 0），方向也没有
   （Δ=+0.0031，CI 跨 0）。所以「把形状假设换掉能提升样本外准确率」这个假设
   **未被证实** —— 修好区间的宽度 ≠ 修好概率的准确度。

**红线遵守**：按 §1，**不许**回去换分位估计量 / 平滑经验分布 / 换窗再试同一变体
（那是用 validate 做参数搜索，R7）；**不得**因观察 (1) 或 §1 风险 3 去加状态条件化
重跑（那是第二个变量）。若要再走形状轴，须**另开**预注册。

## 7. 收尾

- 决策落库：本轮由既有 CLI 走 `experiment run` 写报告并落 `experiment_decisions`
  （`variant_id = residual-quantile-interval`，`split = train/validate`，
  `metric = direction/brier`，`gate_status = FLAT`），行数与 `inserted/identical`
  原始输出见下（**落库后追加**，不改上文字）。
- 生产表**一行未写**：`predictions` / `verifications` 跑前跑后同为 `6055 / 6055`；
  `experiment_decisions` 由 `20` → `24`（本轮 4 条，append-only）。
- 回归哨兵：`predict run --asof 2026-09-14` 报告 sha256 仍为 `a61d026b…f4eb`。

### 7.1 CLI 原始输出（决策落库）

```text
（跑完后贴回 —— 见下方追加块）
```

### 7.2 复现

```bash
# 主跑（报告 + 落库；内存评估，不写生产表）
.venv/bin/python -m stocklab.cli.main experiment run \
  --variant residual-quantile-interval --from 2013-04-16 --to 2026-09-14 --keep-test-sealed
```

按日聚类 bootstrap（判据 a2）不是 CLI 的一个开关，而是
`stocklab.experiments.metrics.bootstrap_daily_ci`：它对**同一批配对行**
（`_summarize_split` 收到的那两份，n=1302 行 / 651 天）重采样 2000 次、seed 固定，
所以与 (a1) 是同一份数据上的两个估计量，可复现。
