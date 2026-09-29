# 实验：factor-ic · 候选信号源 #2 `mf_ratio_5d`（5 日主力资金净流入占比）· csi300-500

- **日期**：2026-09-29
- **提出人**：nanobot（依据 `docs/plans/2026-09-27-p95-新信号源-设计稿.md` §5.1 的 **MF-A** 与 §8 的 P98 顺序
  第 2 项；用户 2026-09-24 「只要能提高 ai 模拟的准确率，就都按照你想的优化」的常驻授权）
- **状态**：**预注册（跑之前落盘）** —— 本文件在度量**之前**提交；跑完**不许改**，要改只能追加新实验
- **上游**：
  `docs/tasks/2026-09-29-p97-因子度量扩展.md`（`--factor` ＋ 研究侧因子注册表 `RESEARCH_FACTORS`，
  `mf_ratio_5d` 的取值器由 P97 交付 ⇒ **本实验零代码**）、
  `docs/plans/2026-09-27-p95-新信号源-设计稿.md` §5.1（MF-A 定义、覆盖实测 800/800 只、1,921,737 行 100% 非空）、
  `docs/experiments/2026-09-29-factor-ic-gm-yoy-pp-csi300-500.md` §7（候选 #1 的判决：折扣后不成立 ⇒ 顺序推进到 #2）

## 0. 预注册参数（机器可读；命令按此逐字段 fail-closed 校验）

```json
{"experiment": "factor-ic", "pool": "short", "start": "2015-01-01", "universe": "csi300-500",
 "factors": ["mom20", "vr15", "mf_ratio_5d"],
 "secondary_factors": ["roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"],
 "ic_type": "spearman", "n_layers": 5, "horizon": 5,
 "min_periods": 120, "min_xsec_n": 30, "bootstrap_n": 2000, "bootstrap_seed": 20260918,
 "rule": "逐因子独立判：验证段 2000 次按日重采样 95% CI 不含 0 ⇒ IC_SIGNIFICANT（IC 为负也算 significant）；含 0 ⇒ IC_NOT_SIGNIFICANT；验证段有效日期数 < MIN_VALID_PERIODS(120) ⇒ INCONCLUSIVE。次因子只报读数、不作判据。另有预注册冗余门：验证段逐调仓日的截面 Spearman(|rho|) 均值 —— max(|rho_mom20|,|rho_vr15|) > 0.5 ⇒ 冗余淘汰（即使 IC_SIGNIFICANT 也不采纳）；0.3 < max ≤ 0.5 ⇒ 需另立残差 IC 实验；≤ 0.3 ⇒ 视为独立候选。"}
```

> 窗口 / 池 / 宇宙 / 前向收益口径 / 判据常量 / bootstrap 次数与种子，与
> `2026-09-26-factor-ic-short-csi300-500.md` §0 **逐字段相同**。**唯一变量 = 主读数名单**多写一个
> `mf_ratio_5d`（`mom20` / `vr15` 同表并排、同一个 verdict 词汇，**不许挑一个当推荐**）。

## 1. 假设

`mf_ratio_5d`（P95 §5.1 MF-A）＝ `asof` 前 **5 个交易日** `money_flow_daily.ratio_amount` 的均值，
即「主力资金净流入占成交额的比例」的 5 日平均。现役短池打分插桩（`script_id=7`，v1.1.0）**没有用它**。

**假设**：`mf_ratio_5d` 对下一个调仓周期（5 交易日）的前向收益**有可测的横截面排序能力**。

**方向不预设**（CLAUDE.md 度量纪律 6）：只量化 IC 与分层，不预设正负。

**头号风险（P95 §5.1 已点名）**：`mf_ratio_5d` 的窗口（最近 5 日）与 `vr15` 的分子窗（`V[-5:]`）**完全重叠**，
且动量与资金流经济上同源 ⇒ **必须在解读前量横截面相关性**，故本次把它写成**预注册的冗余门**（§4.2），
而不是跑完再看着办。

## 2. 失效条件（提前写死）

- 某日截面**同时有因子值与有效前向收益**的标的数 < `MIN_XSEC_N = 30` ⇒ 该日 IC 记 `None`
  并计入 `n_dates_skipped`，**不许**用 0 顶替。
- 验证段有效日期数 < `MIN_VALID_PERIODS = 120` ⇒ `INCONCLUSIVE`（＝「测不出」≠「没效果」）。
- `mf_ratio_5d` 的 5 日窗内任一 `ratio_amount` 为 `NULL`/缺失 ⇒ 该只当日 **`None`**（**不补 0**），
  不进截面（P95 §5.1 的取值规则，取值器已按此实现）。
- 复权不可用 ⇒ 前向收益回退未复权价并计 `n_fwd_fallback`（P78 D4 同口径，**不抛**）；裸 `AdjustError` 原样抛。
- 取值走研究侧取值器 `_research_map(conn, asof, "mf_ratio_5d")`（P97 L5）⇒ **不许**另立价格口径，
  **不许**把它注进 `payloads_by_mark[*]["feats"]`（伪造 `ctx` 形状，会让覆盖度读数说谎）。

## 3. 数据与非 PIT 披露

- 区间 `2015-01-01 → 2026-09-24`、`pool=short`、`universe=csi300-500`（800 只**现成分、非 PIT**）。
- 三条已知非 PIT 项同 `2026-09-25-xsec-topn-csi300-500.md`（ST 取当前名 / sector 取当前值 / 宇宙事后挑选）。
- `ratio_amount` 的 PIT 锚 = `money_flow_daily.date <= asof`（该表按日、T-1 可得）。
- 因子值**不来自打分用的 `ctx`**（`source=research`，P83 之后第二次口径扩增，ADR-045）⇒ 读数只能说
  「这个定义在这段历史上与收益的关系是这样」，**不能说「这就是插桩用的因子」**。
- 只读、零写入：走 `--db` 指向的**副本**；不新表、不迁移、不 regen 红线、不改任何插桩。

## 4. 判据（跑完照此写结论）

1. **主读数**：`mf_ratio_5d` 的全窗 / 训练段 / 验证段 IC 均值、95% CI、IR、t、分层（5 层）spread、
   单调步数、`n_dates_skipped`、`n_fwd_fallback`、覆盖度（含 `value_coverage_p50` 与并列比例）；
   `mom20` / `vr15` 同表并排（同样出 verdict）。
2. **冗余门（预注册，跑完只许照此判）**：`ρ̄` = 验证段逐调仓日的**截面 Spearman**均值 ——
   分别对 `mf_ratio_5d` 与 `mom20`、`mf_ratio_5d` 与 `vr15`，在「当日两个因子都有效」的**共同代码集合**上计算。
   - `max(|ρ̄_mom20|, |ρ̄_vr15|) > 0.5` ⇒ **冗余淘汰**（即使 IC_SIGNIFICANT 也不采纳）；
   - `0.3 < max ≤ 0.5` ⇒ 需另立「残差 IC」单变量实验后才谈采纳；
   - `≤ 0.3` ⇒ 视为独立候选（仍要过下面的 IC 与折扣）。
   **度量路径**：harness **不出**该读数（`research factor-ic` 无此输出），故由 nanobot 的**只读探针**
   `/tmp/p98b/rho_probe.py` 计算：**复用** `stocklab.research.factor` 的纯函数 `mom20` / `vr15` 与
   `_research_map`（不另写公式）、取数复用 `candidate/run.py` 的打分侧口径，marks / 代码集合与本次 IC
   **同一口径**；探针 sha256 与输出 json 一并回填 §7。探针只读副本、零写库。
3. **折扣（只用于解读，不改代码门槛）**：
   - 有效样本折扣：`mf_ratio_5d` 是**日频**（不是季频）⇒ 相邻调仓日仍有重叠（5 日窗 vs 5 日持有期，
     1 倍重叠）⇒ 若结论要采纳，须另立**按日聚类 / 换窗**的稳健性实验；本档只**报**「重叠倍数 = 1.0」这一事实。
   - 族大小折扣：本实验的假设来源是 P95 §5 的候选池（`gm_yoy_pp` 已否、本档是第 2 个）⇒ 报出
     `1 − 0.95^k`（k = 2 ⇒ 9.8%）供解读，**不作判据**。
4. **结论词汇只许四个**（`IC_SIGNIFICANT` / `IC_NOT_SIGNIFICANT` / `INCONCLUSIVE` / `LOW_COVERAGE`）；
   **不得**写因果推断，**不得**据此直接改打分函数或权重。

## 5. 后果与后续

- `IC_SIGNIFICANT` 且过冗余门（`max|ρ̄| ≤ 0.5`）⇒ `mf_ratio_5d` 获得「进池 / 进打分」的候选资格，
  下一步是**新预注册的单变量实验**（一次只动一处）。
- `IC_NOT_SIGNIFICANT` / `INCONCLUSIVE` / 冗余淘汰 ⇒ 写成「**在现有证据下不足以采纳**」，
  按 P95 §8 的顺序推进下一候选（`ep_ttm`，2018+ 覆盖、训练段丢 147 周期须在预注册里写死）。
- 无论结果如何，本实验**不改**任何真库数据、**不 regen** 红线基线、**不改**任何插桩脚本。

## 6. 执行记录

- **本站＝nanobot 自己执行（零代码改动、零 CC 站）**：命令只需 P97 已交付的 `research factor-ic --factor`。
- 命令（真库只读；为避开 15:30 收盘链写入，走与候选 #1 **同一个** APFS 克隆副本 ⇒ marks/周期数与
  `2026-09-29-factor-ic-gm-yoy-pp-csi300-500.md` **逐字段可比**）：

```bash
# 复用候选 #1 的同一个副本（2026-09-29 14:00 克隆，避开当日 15:30 收盘链写入）
.venv/bin/python -m stocklab.cli.main research factor-ic \
  --pool short --start 2015-01-01 --end 2026-09-24 \
  --universe csi300-500 --factor mf_ratio_5d \
  --prereg docs/experiments/2026-09-29-factor-ic-mf-ratio-5d-csi300-500.md \
  --out reports/research/ --db /tmp/p98/copy.db
```

- 预期时长 ≈ **3.2 h**（同口径实测候选 #1 = 11615.5 s）；产物名带因子 tag
  （`…-factor-ic-csi300-500-mf_ratio_5d.{json,md}`）⇒ **不会覆盖**任何既有产物。
- 读数回填见 `docs/experiments/README.md` 台账 ＋ 本文件 §7。

## 7. 读数（2026-09-29 20:43 跑完；nanobot 自己执行，未派 CC 站）

**命令实跑与 §6 逐字段相同**（副本 `/tmp/p98/copy.db`、真库零写入）。`rc=0`、`elapsed_s=11883.30`（≈3.30 h，
与预期 3.2 h 相符）、`n_marks=571` / `n_periods=570`、验证段 `n=170`。产物：
`reports/research/2026-09-24-factor-ic-csi300-500-mf_ratio_5d.{json,md}`（gitignore，未提交；**不覆盖** P83 产物）。

### 7.1 主判决

| 项 | 读数 |
|---|---|
| `verdict` | **`IC_NOT_SIGNIFICANT`** |
| `mf_ratio_5d` 验证段 IC 均值 | **+0.006996**（95% CI **[−0.014533, +0.028797]（跨 0）**、IR +0.0477、t **+0.6219**、n=170） |
| 训练段（n=395） | 均值 **−0.007266**、IR −0.0518、t −1.0290 |
| 全窗 | 均值 −0.002975、std 0.14230、有效日 565 |
| 分层 5 层（均值/层） | 1: +0.004887 / 2: +0.003498 / 3: +0.004478 / 4: +0.003483 / 5: +0.003138 |
| spread(1−5) | **+0.001749**、CI **[−0.000257, +0.003676]（跨 0）**、单调步 3/4、n_days=565 |
| 同表并排（主读数） | `mom20` −0.01732 CI[−0.05080,+0.01493] t −1.048；`vr15` −0.01202 CI[−0.03138,+0.00709] t −1.207 —— **都不显著** |
| 覆盖 | `value_coverage_p50=688`、`xsec_size_p50=388`（min 0 / max 533）、`n_dates_skipped=5`、`coverage_validate_below_min=1` |

**判决＝不采纳**：CI 跨 0 ⇒ 在现有证据下 `mf_ratio_5d` **没有可测的横截面排序能力**，
与候选 #1 `gm_yoy_pp`（折扣后不成立）**同结论**。⇒ 按 §5 推进 P95 §8 顺序的下一候选（`ep_ttm`）。

### 7.2 两条披露（如实记，不改判）

1. **预注册的冗余门没有落读数**：`mf_ratio_5d` 与 `mom20`/`vr15` 的验证段逐调仓日截面 Spearman 均值
   （`ρ̄`）**在产物 JSON 里不存在**，`stocklab/research/` 全目录也没有实现这段横截面相关性（`grep -rn "rho"`
   只命中 `spearman_ic` 的单因子实现）⇒ §4.2 的门**本次未被执行**。
   影响面：该门只在 `IC_SIGNIFICANT` 时才决定是否「冗余淘汰」，本次 verdict 已是 `IC_NOT_SIGNIFICANT`
   ⇒ 对判决**不构成必要条件、不改判**；但**若将来某因子显著，必须先补此门**（否则「独立候选」的说法没有依据）。
2. **`mf_ratio_5d` 的取值来源是 `source=research`**（P83 之后第二次因子值不来自打分 `ctx`，ADR-045）
   ⇒ 本读数只能说「这个定义在这段历史上与收益的关系是这样」，**不能说「这就是插桩用的因子」**。

### 7.3 纪律核对

- 只读副本、**真库 sha 未变**（本实验与候选 #1 共用同一 `/tmp/p98/copy.db`）；未新表、未迁移、
  **未 regen 红线**、零改动插桩/打分/权重（`git status --porcelain` 只有本文件与 `docs/experiments/README.md`）。
