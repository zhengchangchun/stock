# 实验：factor-ic · 把 `gm_yoy_pp` **升格为主读数**再跑一次单变量度量 · csi300-500

- **日期**：2026-09-29
- **提出人**：nanobot（依据 `docs/plans/2026-09-27-p95-新信号源-设计稿.md` §9 拍板点 1 与 §8 的 P98 占位；
  用户 2026-09-24 「只要能提高 ai 模拟的准确率，就都按照你想的优化」的常驻授权）
- **状态**：**预注册（跑之前落盘）** —— 本文件在度量**之前**提交；跑完**不许改**，要改只能追加新实验
- **上游**：
  `docs/tasks/2026-09-26-p83-因子级IC分解.md`（`gm_yoy_pp` 的唯一正读数出处：次读数 IC **+0.013433**、
  95% CI **[+0.0013, +0.0254]**、t 2.111）、
  `docs/tasks/2026-09-29-p97-因子度量扩展.md`（`--factor` 升格开关 ＋ `PROMOTABLE_FACTORS`，本实验的**前置工具**）、
  `docs/plans/2026-09-27-p95-新信号源-设计稿.md` §3.4（为什么它卡在「不足以采纳、也不足以忽略」）

## 0. 预注册参数（机器可读；命令按此逐字段 fail-closed 校验）

```json
{"experiment": "factor-ic", "pool": "short", "start": "2015-01-01", "universe": "csi300-500",
 "factors": ["mom20", "vr15", "gm_yoy_pp"],
 "secondary_factors": ["roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"],
 "ic_type": "spearman", "n_layers": 5, "horizon": 5,
 "min_periods": 120, "min_xsec_n": 30, "bootstrap_n": 2000, "bootstrap_seed": 20260918,
 "rule": "逐因子独立判：验证段 2000 次按日重采样 95% CI 不含 0 ⇒ IC_SIGNIFICANT（IC 为负也算 significant）；含 0 ⇒ IC_NOT_SIGNIFICANT；验证段有效日期数 < MIN_VALID_PERIODS(120) ⇒ INCONCLUSIVE。次因子只报读数、不作判据。"}
```

> 窗口 / 池 / 宇宙 / 前向收益口径 / 判据常量 / bootstrap 次数与种子，与
> `2026-09-26-factor-ic-short-csi300-500.md` §0 **逐字段相同**。**唯一变量 = 主读数名单**
> （`mom20` / `vr15` 之外**多写一个 `gm_yoy_pp`** ⇒ 它从「次读数、只报不判」升格为「主读数、出 verdict」）。
> —— 这就是 P97 的 `--factor` 语义（`PREREG_FIELDS` 一字未动，被选因子名必须已在 `factors` 里）。

## 1. 假设

`gm_yoy_pp`（毛利率同比变化，百分点）在当前打分管线的 `ctx["features"]` 里**已经存在**
（`candidate/run.py::_factor_payload` 的 5 个财务因子之一），只是现役短池打分插桩
（`script_id=7`，v1.1.0）**一个都没用**。

**假设**：`gm_yoy_pp` 对下一个调仓周期（5 交易日）的前向收益**有可测的横截面排序能力**
—— 且这个能力大到「换掉 `mom20`/`vr15` 这套输入」值得。

**方向不预设**（CLAUDE.md 度量纪律 6）：只量化 IC 与分层，不预设正负，也不预设它是「好因子」。

**为什么必须重跑、不能直接引 P83 的次读数**：P83 那次它是**次读数**（`exploratory=true`），
与 `roe` / `gross_margin` / `inv_days` / `fcf_margin` **同族比较**了 5 次；把它当结论需要：
① 用**主读数**的同一套 verdict 词汇判定；② 显式扣掉多重比较与有效样本折扣（见 §4.3）。
不重跑就引它 = 事后从 5 个数字里挑最大的那个当结论。

## 2. 失效条件（提前写死）

- 某日截面**同时有因子值与有效前向收益**的标的数 < `MIN_XSEC_N = 30` ⇒ 该日 IC 记 `None`
  并计入 `n_dates_skipped`，**不许**用 0 顶替。
- 验证段有效日期数 < `MIN_VALID_PERIODS = 120` ⇒ `INCONCLUSIVE`（＝「测不出」≠「没效果」）。
- 非空覆盖度在验证段里 `< MIN_XSEC_N` 的日子占比 > 50% ⇒ 标 `LOW_COVERAGE`、**不出 verdict**
  （财务因子在短池上很可能就是这种；如实报，不看图说话）。
- 复权不可用 ⇒ 前向收益回退未复权价并计 `n_fwd_fallback`（P78 D4 同口径，**不抛**）；裸 `AdjustError` 原样抛。
- `gm_yoy_pp` 的取值器走既有 `secondary_value`（P97 L3/L5）⇒ **不许**另建价格/财务口径，
  也不许把它注进 `payloads_by_mark[*]["feats"]`（伪造 `ctx` 形状，会让覆盖度读数说谎）。

## 3. 数据与非 PIT 披露

- 区间 `2015-01-01 → 2026-09-24`、`pool=short`、`universe=csi300-500`（800 只**现成分、非 PIT**）。
- 三条已知非 PIT 项同 `2026-09-25-xsec-topn-csi300-500.md`（ST 取当前名 / sector 取当前值 / 种子宇宙事后挑选）。
- 因子值取自**打分用的同一个 ctx**；`bars` 只放 `date <= asof`，财报只放 `notice_date <= asof`。
- **AS-OF**：前向收益用 `load_bars_adjusted(conn, code, as_of=d1)`（只累乘 `cqr ≤ d1`）；P94 起默认复权价口径。
- 只读、零写入：真库走 `mode=ro`；本实验**不新表、不迁移、不 regen 红线、不改任何插桩**。

## 4. 判据（跑完照此写结论）

1. **主读数**：`gm_yoy_pp` 的全窗 / 训练段 / 验证段 IC 均值、95% CI、IR、t、分层（5 层）spread、
   单调步数、`n_dates_skipped`、`n_fwd_fallback`、覆盖度；`mom20` / `vr15` 同表并排（同样升格为主读数、
   同一个 verdict 词汇，**不许挑一个当推荐**）。
2. **次读数**（`exploratory=true`，只报不判）：`roe` / `gross_margin` / `inv_days` / `fcf_margin`。
3. **折扣（P95 §9 拍板点 1 已写死，不是跑完才想）**：
   - **有效样本折扣** `t_eff ≈ 0.63`（财务因子的季频更新 ⇒ 相邻调仓日不独立）；
   - **族大小折扣** `1 − 0.95⁵ = 22.6%`（同一份 P83 报告里比较了 5 个财务因子 ⇒ 假阳性率抬到 22.6%）。
   - 两条折扣**只用于解读**，不改代码门槛：报告里 `verdict` 仍是机器的 `IC_SIGNIFICANT` /
     `IC_NOT_SIGNIFICANT` / `INCONCLUSIVE` / `LOW_COVERAGE`，折扣写在本实验的**结论段**里。
4. **与 P83 的关系**：必须并排列出 P83 的次读数（IC +0.013433、CI [+0.0013, +0.0254]、t 2.111）
   与本次主读数 —— 若两者不同，先解释差异（窗口/口径/被选因子是否改变了 `factors` 名单带来的行为），
   再谈结论。
5. 结论词汇只许上述四个；**不得**写「因子 X 有效」之外的因果推断，**不得**据此直接改打分函数或权重。

## 5. 后果与后续

- `IC_SIGNIFICANT` 且过折扣（两条折扣后仍稳）⇒ `gm_yoy_pp` 获得「**进池 / 进打分**」的候选资格，
  下一步是**新预注册的单变量实验**（一次只动一处：先做「进候选池打分」，再做「进 AI 操盘决策上下文」）。
- `IC_NOT_SIGNIFICANT` 或 `INCONCLUSIVE` ⇒ 写成「**在现有证据与折扣下不足以采纳**」，
  回到 P95 §5 的另外三类候选（资金流 `mf_ratio_5d` / 估值 `ep_ttm` / 公告 `ann_count_5d`）各自单独立项。
- 无论结果如何，本实验**不改**任何真库数据、**不 regen** 红线基线、**不改**任何插桩脚本。

## 6. 执行记录

- **本站＝nanobot 自己执行（零代码改动、零 CC 站）**：命令只需 P97 已交付的 `research factor-ic --factor`。
- 命令（真库只读；为避开 15:30 收盘链写入，走 APFS 克隆副本）：

```bash
cp -c data/stocklab.db /tmp/p98/copy.db
.venv/bin/python -m stocklab.cli.main research factor-ic \
  --pool short --start 2015-01-01 --end 2026-09-24 \
  --universe csi300-500 --factor gm_yoy_pp \
  --prereg docs/experiments/2026-09-29-factor-ic-gm-yoy-pp-csi300-500.md \
  --out reports/research/ --db /tmp/p98/copy.db
```

- 预期时长 ≈ **3.7 h**（同口径全窗实测 `elapsed_s = 13485`，P83 那次）；产物名带因子 tag
  （P97 L8：`…-factor-ic-csi300-500-gm_yoy_pp.{json,md}`）⇒ **不会覆盖** P83 的产物。
- 读数回填见 `docs/experiments/README.md` 台账 ＋ 本文件 §7。

## 7. 结果与判决（2026-09-29 14:02 起 → 17:15 止；nanobot 自跑）

### 7.1 跑的条件

- pid **8707**、日志 `/tmp/p98/run.log`、`--db /tmp/p98/copy.db`（14:00 的 APFS 克隆副本，避开 15:30 收盘链写入）、
  `prereg_sha256 = dc471e85…32cd9`（fail-closed 逐字段通过）、`n_marks = 571 / n_periods = 570`。
- `elapsed_s = 11615.5`（逐调仓日扫描 11597.5 ＋ 前向收益载入 13.1）、**rc = 0**。
- 产物：`reports/research/2026-09-24-factor-ic-csi300-500-gm_yoy_pp.{json,md}`（gitignore、未提交；**未覆盖** P83 产物）。
- 真库：全程只读副本 ⇒ **零写入**、未 regen 红线、未改任何插桩/阈值/权重。

### 7.2 主读数（三因子同表，同一个 verdict 词汇）

| 因子 | 验证段 n | 验证段 IC 均值 | 95% CI | IR | t | 过拟合标记 | verdict |
|---|---|---|---|---|---|---|---|
| `mom20` | 171 | −0.01732 | [−0.05080, +0.01493] | −0.080 | −1.048 | `None` | `IC_NOT_SIGNIFICANT` |
| `vr15` | 171 | −0.01202 | [−0.03138, +0.00709] | −0.092 | −1.207 | `suspected` | `IC_NOT_SIGNIFICANT` |
| **`gm_yoy_pp`** | 171 | **+0.01343** | **[+0.00132, +0.02545]** | **+0.161** | **+2.111** | `None` | **`IC_SIGNIFICANT`** |

- `gm_yoy_pp` 全窗 +0.012544、训练段（n=399）+0.012163（t +2.729）、验证段 std 0.083211；覆盖度日 P50 = 344.0、跳过日 0。
- 分层（降序、第 1 层 = 最高分）：L1 +0.004988 / L2 +0.004235 / L3 +0.003718 / L4 +0.003675 / L5 +0.003213
  ⇒ `spread = layer1 − layer5` = **+0.001775**、95% CI **[+0.000527, +0.002987]（不含 0）**、单调步数 **4/4**。
  ⚠️ 这是**层间**平均前向收益差（5 交易日 ≈ **+0.18%/期**、**未扣成本**）—— 只能读成「有可测的横截面排序信息」，**不是**可交易收益。
- Pearson 次读数 +0.011911（不作判据）。次读数（只报不判）同 P83：`gross_margin` −0.01105、`inv_days` −0.00880、`fcf_margin` +0.01579、`roe` `LOW_COVERAGE`。

### 7.3 与 P83 §4.4 的比对

**逐位相同**（+0.013433 / CI [+0.001323, +0.025446] / t 2.111）⇒ 「升格」（`kind` 由 `secondary` 换 `main`）**没有改变读数**，差异 = 0，无须解释差异来源。

### 7.4 折扣（§4.3 预先写死；只用于解读、不改代码门槛）

- **有效样本折扣**：验证段 ≈ 3.5 年、财务因子季频更新 ⇒ 独立观测 ≈ **15 个季度状态**
  ⇒ `t_eff = t·√(n_eff/n) = 2.111·√(15/171) = **0.63**`（乐观取 `n_eff = 25` ⇒ 0.81）⇒ 远不到显著。
- **族大小折扣**：假设来源是 P83 同一份报告里比较过的 **5 个财务因子** ⇒ `1 − 0.95⁵ = **22.6%**`。
  如实记录：本次运行的机器 `family_size = 1`（代码未做校正），这条折扣只出现在本节的解读里。
- **Bonferroni 99% CI**（P95 §3.3 预先算过、本次逐位复现）：**[−0.0030, +0.0298] 含 0**。

### 7.5 判决

- 机器 verdict = `IC_SIGNIFICANT`（CI 不含 0）；但 §5 第一支要求「`IC_SIGNIFICANT` **且**过折扣」⇒ **不成立**。
- 结论（用既有词汇）：**在现有证据与预注册折扣下不足以采纳** `gm_yoy_pp` —— 与 P95 §9 拍板点 1 的结论一致，本次没有把它从「不足以采纳」推到「可采纳」。
- ⇒ **P99（进池站）不立**（前置条件不成立；不预判方向，不做任何策略改动）。
- 不采纳的后果：**不改**打分函数 / 权重 / `m2_a1` 插桩；`gm_yoy_pp` 退回 P95 §5 的候选池，与 `mf_ratio_5d` / `ep_ttm` / `ann_count_5d` 并列，按 P95 §8 的顺序逐个另立单变量实验。
- 词义纪律：`IC_SIGNIFICANT` 只说「95% CI 不含 0」，**不等于可交易**、不等于「打分函数有效」、也不外推到全市场。

