# 实验：factor-ic · 短池打分的**输入因子**逐个算 rank IC（因子级 IC 分解）· csi300-500

- **日期**：2026-09-26
- **提出人**：nanobot（用户 2026-09-26 拍板「先做 a吧」＝因子级 IC 分解；授权同 2026-09-24）
- **状态**：**预注册（跑之前落盘）** —— 本文件在度量**之前**提交；跑完**不许改**，要改只能追加新实验
- **上游**：`docs/experiments/2026-09-26-rank-ic-short-csi300-500.md`（合成分数的 IC 已测：`IC_NOT_SIGNIFICANT`）、
  `docs/decisions/2026-09-26-ADR-030-信号有效性度量口径.md`、`docs/tasks/2026-09-26-p83-因子级IC分解.md`

## 0. 预注册参数（机器可读；命令按此逐字段 fail-closed 校验）

```json
{"experiment": "factor-ic", "pool": "short", "start": "2015-01-01", "universe": "csi300-500",
 "factors": ["mom20", "vr15"],
 "secondary_factors": ["roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"],
 "ic_type": "spearman", "n_layers": 5, "horizon": 5,
 "min_periods": 120, "min_xsec_n": 30, "bootstrap_n": 2000, "bootstrap_seed": 20260918,
 "rule": "逐因子独立判：验证段 2000 次按日重采样 95% CI 不含 0 ⇒ IC_SIGNIFICANT（IC 为负也算 significant）；含 0 ⇒ IC_NOT_SIGNIFICANT；验证段有效日期数 < MIN_VALID_PERIODS(120) ⇒ INCONCLUSIVE。次因子只报读数、不作判据。"}
```

> 窗口 / 池 / 宇宙 / 前向收益口径 / 判据常量 / bootstrap 次数与种子，与
> `2026-09-26-rank-ic-short-csi300-500.md` **逐字段相同**。**唯一变量 = 度量对象**
> （从「合成后的 `adj_score`」换成「构成它的输入因子各自」）。

## 1. 假设

现役短池打分插桩（`plugin_scripts.script_id=7`，v1.1.0）的分数是**两个输入因子的线性组合 + 裁剪**：

```
score = clip(50 + 150 × mom20 + 30 × (vr15 − 1), 0, 100)
mom20 = (C[-1] − C[-20]) / C[-20]            # 20 日动量（C = PIT 复权收盘价，P77 口径）
vr15  = mean(V[-5:]) / mean(V[-20:-5])       # 5/15 量比
```

> **假设**：`mom20` 与 `vr15` 中**至少一个**对下一个调仓周期的前向收益有可测的横截面排序能力
> （逐因子独立判 95% CI 是否不含 0）。

**方向不预设**：本实验只**量化**每个因子的 IC 与分层，不预设正负，也不预设「哪个因子更强」
（CLAUDE.md 度量纪律 6）。

**为什么值得测**：`rank-ic` 已证「合成分数无 IC」。合成分数 = 两个因子 + 裁剪，所以三种可能
都被这一次分解区分开：① 两个因子都没 IC ⇒ **这套输入没有 edge**，方向应转向换信号源（新数据），
不是继续调权重；② 某因子有 IC 但合成分数没有 ⇒ **组合/裁剪把信息毁了**，下一步是改合成方式；
③ 两个因子都有 IC ⇒ 与合成分数的读数矛盾，说明合成或裁剪有问题，先查实现。

## 2. 失效条件（提前写死）

- 某日截面**同时有因子值与有效前向收益**的标的数 < `MIN_XSEC_N = 30` ⇒ 该因子该日 IC 记 `None`
  并计入 `n_dates_skipped`，**不许**用 0 顶替。
- 验证段有效日期数 < `MIN_VALID_PERIODS = 120` ⇒ 该因子 `INCONCLUSIVE`（＝「测不出」≠「没效果」）。
- 复权不可用（`MissingFactor` / `EtfChainUnsupported` / `StaleFactorTable`）⇒ 前向收益回退未复权价
  并计 `n_fwd_fallback`（与 P78 D4 同口径，**不抛**）；裸 `AdjustError` 原样抛。
- 某因子的**非空覆盖度**在验证段里 `< MIN_XSEC_N` 的日子占比 > 50% ⇒ 该因子标 `LOW_COVERAGE`、
  **不出 verdict**（财务因子在短池上很可能就是这种，如实报）。
- **`mom20` / `vr15` 的取值必须与插桩看到的一致**：同一 ctx 上
  `clip(50 + 150×mom20 + 30×(vr15−1))` 必须等于插桩实际返回的 `score`（一致性用例钉住）。
  对不上 ⇒ 本实验的因子不是插桩的因子，**先修再谈结论**。

## 3. 数据与非 PIT 披露

- 区间 `2015-01-01 → 2026-09-24`、`pool=short`、`universe=csi300-500`（800 只**现成分、非 PIT**）。
- 三条已知非 PIT 项同 `2026-09-25-xsec-topn-csi300-500.md`（ST 取当前名 / sector 取当前值 / 种子宇宙事后挑选）。
- 因子值取自**打分用的同一个 ctx**（`candidate/run.py::_load_bars_adjusted` + `_cross_section_map`
  + `score.build_ctx`），**不另建价格口径**；`bars` 只放 `date <= asof`，财报只放 `notice_date <= asof`。
- **AS-OF**：前向收益用 `load_bars_adjusted(conn, code, as_of=d1)`（只累乘 `cqr ≤ d1`）。

## 4. 判据（跑完照此写结论）

1. **主读数**：`mom20` / `vr15` 各自的全窗 / 训练段 / 验证段 IC 均值、95% CI、IR、t、分层 spread、
   单调步数、`n_dates_skipped`、`n_fwd_fallback`；逐因子 verdict。
2. **次读数（只报，不作判据，`exploratory=true`）**：`roe` / `gross_margin` / `gm_yoy_pp` /
   `inv_days` / `fcf_margin` 在同一截面上的 IC 与覆盖度（含 `LOW_COVERAGE` 标记）。
3. **裁剪诊断**：逐日「被打分插桩裁到 0 或 100 的标的本数 / 当日截面数」的 P50 与极值
   —— 量化裁剪对 rank 分辨率的破坏（**这是读数，不是判据**）。
4. 与 `rank-ic` 的合成分数读数**并排**列出（合成分数 IC −0.0158、CI [−0.0458, +0.0143]）。
5. 结论词汇只许：`IC_SIGNIFICANT` / `IC_NOT_SIGNIFICANT` / `INCONCLUSIVE` / `LOW_COVERAGE`。
   **不得**写「因子 X 有效/无效」之外的因果推断，也不得据此直接改打分函数。

## 5. 后果与后续

- 有因子显著 ⇒ 该因子获得「作为改打分函数的**候选**」资格，下一步是**新预注册的单变量实验**
  （改合成方式/c权重，一次只动一处）。
- 全都不显著 ⇒ 写成「**这套输入在这段历史上没有可测的横截面信息**」，方向转为**换信号源**
  （新数据类别），而不是继续在现有因子上调权重。
- 本实验**不改**任何真库数据、**不 regen** 红线基线，也**不改**任何插桩脚本。
