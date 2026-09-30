# 实验：factor-ic · 候选信号源 #5 `mf_net_surprise_20d`（主力资金净流入 20 日时序 z 分数）· csi300-500

- **日期**：2026-09-30
- **提出人**：nanobot（依据 `docs/plans/2026-09-27-p95-新信号源-设计稿.md` §5.1 的 **MF-B** 与 §8 的 P98 顺序
  第 5 项；用户 2026-09-24 「只要能提高 ai 模拟的准确率，就都按照你想的优化」的常驻授权）
- **状态**：**预注册（跑之前落盘）** —— 本文件在度量**之前**提交；跑完**不许改**，要改只能追加新实验
- **上游**：
  `docs/tasks/2026-09-30-p103-mf-net-surprise-20d研究侧取值器.md`（研究侧取值器 `mf_net_surprise_20d`，
  P103 交付，§10 已独立复核 ⇒ **本实验零代码**）、
  `docs/plans/2026-09-27-p95-新信号源-设计稿.md` §5.1（MF-B 定义与覆盖实测）、§5「通用约定」（缺失一律
  `None` 不补 0；rank IC **不做 winsorize**）、
  `docs/experiments/2026-09-30-factor-ic-ann-count-5d-csi300-500.md` §7（候选 #4 判决：CI 跨 0 ⇒ 不采纳
  ⇒ 顺序推进到 #5）

## 0. 预注册参数（机器可读；命令按此逐字段 fail-closed 校验）

```json
{"experiment": "factor-ic", "pool": "short", "start": "2015-01-01", "universe": "csi300-500",
 "factors": ["mom20", "vr15", "mf_net_surprise_20d"],
 "secondary_factors": ["roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"],
 "ic_type": "spearman", "n_layers": 5, "horizon": 5,
 "min_periods": 120, "min_xsec_n": 30, "bootstrap_n": 2000, "bootstrap_seed": 20260918,
 "rule": "逐因子独立判：验证段 2000 次按日重采样 95% CI 不含 0 ⇒ IC_SIGNIFICANT（IC 为负也算 significant）；含 0 ⇒ IC_NOT_SIGNIFICANT；验证段有效日期数 < MIN_VALID_PERIODS(120) ⇒ INCONCLUSIVE。次因子只报读数、不作判据。另有预注册冗余门：验证段逐调仓日的截面 Spearman(|rho|) 均值 —— max(|rho_mom20|,|rho_vr15|) > 0.5 ⇒ 冗余淘汰（即使 IC_SIGNIFICANT 也不采纳）；0.3 < max ≤ 0.5 ⇒ 需另立残差 IC 实验；≤ 0.3 ⇒ 视为独立候选。"}
```

> 窗口 / 池 / 宇宙 / 前向收益口径 / 判据常量 / bootstrap 次数与种子，与
> `2026-09-26-factor-ic-short-csi300-500.md` §0 **逐字段相同**。**唯一变量 = 主读数名单**换为
> `mf_net_surprise_20d`（`mom20` / `vr15` 同表并排、同一个 verdict 词汇，**不许挑一个当推荐**）。

## 1. 假设

`mf_net_surprise_20d`（P95 §5.1 MF-B）＝ **时序标准化**的主力资金净流入异动：

```
( main_net[d₀] − mean(main_net 最近 20 个交易日) ) / stdev(main_net 最近 20 个交易日)
```

- `d₀` = `money_flow_daily.date <= asof` 的**最后一个交易日**（`asof` 本身可以不是交易日，
  与候选 #1–#4 同规）；`μ`/`σ` 取**同一 20 个交易日**（**含** `x_now` 自身）。
- `stdev` 用 **`statistics.stdev`（样本标准差，`ddof=1`）** —— 与仓内既有四处先例一致
  （`research/signal.py:484`、`verify/report.py:56`、`experiments/metrics.py:85`、`experiments/residuals.py:222`，
  无一处 `pstdev`）；20 个样本上二者差约 2.6%。
- PIT 锚 = `money_flow_daily.date`（收盘后可得）；**只读 `main_net` 一列**
  （`ratio_amount` 是候选 #2 MF-A 的输入；`close` / `turnover` / `xl_net` 本因子一律不读）。
- **窗口序口径**（P103 §10.4 裁定）：`_recent_trading_dates` 返回 `ORDER BY date DESC`（最近的在前）⇒
  取值器把它**翻成升序**后取 `vals[-1]`，语义 = P95 §5.1 逐字公式的 `main_net[asof]`。
  本实验按该裁定执行（若照字面用倒序索引，会读到 20 个交易日之前的值、公式反了）。
- **`main_net` 与价格无关** ⇒ 与 `mom20`/`vr15` 的分子**没有结构性重叠**（候选 #2 的冗余风险来自
  `ratio_amount` 与 `vr15` 的分子窗重叠，本档不同）；但**冗余门仍必须量测**（见 §4.2）。

**假设**：资金流的**自身异动**（相对过去 20 日的偏离程度）对下一个调仓周期（5 交易日）的前向收益
**有可测的横截面排序能力**。

**方向不预设**（CLAUDE.md 度量纪律 6）：只量化 IC 与分层。两种相反叙事都存在
（资金突然放量流入 ⇒ 情绪/基本面改善；或 ⇒ 拉高出货/接盘），本实验**不据此预设符号**
（负 IC 同样算 `IC_SIGNIFICANT`）。

**头号风险（本节提前写死）**：

1. **单点极值主导**。`main_net` 的日内分布长尾严重，`(x_now − μ)/σ` 在 `x_now` 极端时会被放大；
   P95 §5 已写死「rank IC **不做 winsorize**」⇒ 本档**不许**加 winsorize / 截尾 / 对数化 / 二值化，
   也**不许**剔掉极值标的。⇒ 必须并排报 harness 自带的裁剪诊断（`ratio_p50` / `n_clipped_p50` /
   `xsec_p50`，只报不判）作为「极值有多重」的旁证。
2. **`σ == 0` ⇒ 静默退出截面**。连续 20 日 `main_net` 完全不动（长期停牌 / 一字板 / 数据停更）的标的
   按缺失处理（**不返回 0.0**）。这不是缺陷，但会让截面在特定年份**系统性偏向「有波动的标的」**
   ⇒ 本档必须报 `value_coverage_p50` / `n_dates_skipped` / `xsec_size_p50`，并与 IC 并排读。
3. **窗口含 `x_now` 自身**。`μ`/`σ` 里含当日值 ⇒ 分母被当日放大、比值天然被压向 (−1, +1) 附近，
   属于定义的一部分（P95 §5.1 逐字），**不改**；但解读时不得把它当成「标准化后的 t 统计量」。

## 2. 失效条件（提前写死）

- 某日截面**同时有因子值与有效前向收益**的标的数 < `MIN_XSEC_N = 30` ⇒ 该日 IC 记 `None`
  并计入 `n_dates_skipped`，**不许**用 0 顶替。
- 验证段有效日期数 < `MIN_VALID_PERIODS = 120` ⇒ `INCONCLUSIVE`（＝「测不出」≠「没效果」）。
- **缺失语义（本档与候选 #4 相反，必须点名）**：窗内任一 `main_net` 为 `NULL`、或该 (code, date)
  行**缺失**、或该 code 在窗内行数 < 20、或 `σ == 0` ⇒ 该标的**不进结果集**（＝ `None`，**绝不补 0**）。
  库外 code（非宇宙成员）不进截面。
- **不足 20 个交易日**（`asof` 太早）⇒ 该日窗口算不出（**不拿「有几日算几日」顶替**，与候选 #2 MF-A 同规）。
  `money_flow_daily` 实测起 `2010-03-01`、`trading_calendar` 覆盖 2007-01-15 起 ⇒ 窗口 2015-01-01 起
  **不会触发**（首几个 `asof` 的 20 日窗全部落在库内）。
- 复权不可用 ⇒ 前向收益回退未复权价并计 `n_fwd_fallback`（P78 D4 同口径，**不抛**）；裸 `AdjustError` 原样抛。
- 取值走研究侧取值器 `_research_value(conn, asof, "mf_net_surprise_20d", member_codes)`（P103）⇒
  **不许**另立价格口径，**不许**把它注进 `payloads_by_mark[*]["feats"]`（伪造 `ctx` 形状，会让覆盖度读数说谎）。

## 3. 数据与非 PIT 披露

- 区间 `2015-01-01 → 2026-09-24`、`pool=short`、`universe=csi300-500`（800 只**现成分、非 PIT**）。
- 三条已知非 PIT 项同 `2026-09-25-xsec-topn-csi300-500.md`（ST 取当前名 / sector 取当前值 / 宇宙事后挑选）。
- **源数据披露（P95 §5.1 ＋ P103 §10.2 我自跑实测）**：`money_flow_daily` 全史 **2,455,257** 行
  （副本 `2,455,236`）、起 **2010-03-01**、`main_net` **零 NULL**、`MAX(date) = 2026-09-29`；
  `asof = 2026-09-24` 时**当日有行的标的 805 只、窗内满 20 个交易日的标的 = 805 只** ⇒
  **本因子不存在结构性覆盖缺口**，验证段 n **预期 = 171**（与候选 #1/#2/#4 同窗；**不像候选 #3
  `ep_ttm` 那样退到 n=127**）。缺失主要来自「新上市 / 停牌导致的**行缺失**」与 `σ==0`，不是 `NULL`。
- 因子值**不来自打分用的 `ctx`**（`source=research`，P83 之后第五次口径扩增，ADR-045 追加段）⇒
  读数只能说「这个定义在这段历史上与收益的关系是这样」，**不能说「这就是插桩用的因子」**。
- 与候选 #2 `mf_ratio_5d` **同数据类不同口径**（#2 = `ratio_amount` 的 5 日均值、**横截面量级**；
  本档 = `main_net` 的 20 日**时序 z 分数**）⇒ 两者的 IC 只可**同表并排读**，**不可当同一因子的两次实现**横比。

## 4. 判据（跑完照此写结论）

1. **主读数**：`mf_net_surprise_20d` 的全窗 / 训练段 / 验证段 IC 均值、95% CI、IR、t、分层（5 层）spread、
   单调步数、`n_dates_skipped`、`n_fwd_fallback`、覆盖度（`value_coverage_p50`、`xsec_size_p50`）；
   **并排报**裁剪诊断（`ratio_p50` / `n_clipped_p50`，§1 头号风险 1）与 `zero_ratio_p50` / `tie_ratio_p50`
   （只报不判，用于说明分辨率）；`mom20` / `vr15` 同表并排（同样出 verdict）。
2. **冗余门（预注册，跑完只许照此判）**：`ρ̄` = 验证段逐调仓日的**截面 Spearman**均值 ——
   分别对 `mf_net_surprise_20d` 与 `mom20`、`mf_net_surprise_20d` 与 `vr15`，在「当日两个因子都有效」的
   **共同代码集合**上计算。
   - `max(|ρ̄_mom20|, |ρ̄_vr15|) > 0.5` ⇒ **冗余淘汰**（即使 IC_SIGNIFICANT 也不采纳）；
   - `0.3 < max ≤ 0.5` ⇒ 需另立「残差 IC」单变量实验后才谈采纳；
   - `≤ 0.3` ⇒ 视为独立候选（仍要过下面的 IC 与折扣）。
   **度量路径与已知缺口**：harness **不出**该读数（`research factor-ic` 无此输出，候选 #2/#3/#4 §7 已披露）；
   P98 的只读探针 `/tmp/p98b/rho_probe.py` 存在，本实验**若 verdict 为 `IC_SIGNIFICANT` 则必须先用它补齐
   该门**（探针 sha256 与输出 json 回填 §7），否则按「缺口未执行、不改判」如实披露。
   P95 判本因子相关风险**低-中**（时序标准化已消掉规模项）—— 但**不许**据此跳过量测。
3. **折扣（只用于解读，不改代码门槛）**：
   - 有效样本折扣：本因子是**日频**（不是季频）⇒ 相邻调仓日窗口仍重叠（20 日窗 + 5 日持有期
     ⇒ 窗口重叠 ≈ 4 倍、持有期重叠 1 倍）⇒ 若结论要采纳，须另立**按日聚类 / 换窗**的稳健性实验；
     本档只**报**「有效独立样本 ≈ 调仓日数 / 4」这一事实。
   - 族大小折扣：本实验的假设来源是 P95 §5 的候选池（`gm_yoy_pp`、`mf_ratio_5d`、`ep_ttm`、`ann_count_5d`
     已否，本档是第 5 个）⇒ 报出 `1 − 0.95^k`（k = 5 ⇒ **22.6%**）供解读，**不作判据**。
4. **结论词汇只许四个**（`IC_SIGNIFICANT` / `IC_NOT_SIGNIFICANT` / `INCONCLUSIVE` / `LOW_COVERAGE`）；
   **不得**写因果推断，**不得**据此直接改打分函数或权重。

## 5. 后果与后续

- `IC_SIGNIFICANT` 且过冗余门（`max|ρ̄| ≤ 0.5`）⇒ `mf_net_surprise_20d` 获得「进池 / 进打分」的候选资格，
  下一步是**新预注册的单变量实验**（一次只动一处）。
- `IC_NOT_SIGNIFICANT` / `INCONCLUSIVE` / 冗余淘汰 ⇒ 写成「**在现有证据下不足以采纳**」，
  按 P95 §8 的顺序推进**最后一个候选** `pe_pct_756`（VAL-B，需先补研究侧取值器 ⇒ 另立任务书）。
- 无论结果如何，本实验**不改**任何真库数据、**不 regen** 红线基线、**不改**任何插桩脚本。

## 6. 执行记录

- **本站＝nanobot 自己执行（零代码改动、零 CC 站）**：命令只需 P103 已交付的
  `research factor-ic --factor mf_net_surprise_20d`。
- 命令（真库只读；走与候选 #1–#4 **同一个** APFS 克隆副本 `/tmp/p98/copy.db`（2026-09-29 14:00 克隆）
  ⇒ marks/周期数与 `2026-09-30-factor-ic-ann-count-5d-csi300-500.md` **逐字段可比**）：

```bash
.venv/bin/python -m stocklab.cli.main research factor-ic \
  --pool short --start 2015-01-01 --end 2026-09-24 \
  --universe csi300-500 --factor mf_net_surprise_20d \
  --prereg docs/experiments/2026-09-30-factor-ic-mf-net-surprise-20d-csi300-500.md \
  --out reports/research/ --db /tmp/p98/copy.db
```

- 预期时长 ≈ **3.1–3.3 h CPU**（同口径实测候选 #1 = 11615.5 s、#2 = 11883.3 s、#4 = 11749.8 s；
  本因子的窗口查询是 `money_flow_daily` 的 20 个交易日的 `IN` 白名单，**`date` 无索引**
  ⇒ 每 `asof` 2 条 SQL、P103 §9 实测单 `asof` **0.21–1.28 s**（覆盖 507–805 只）
  ⇒ 571 日外推 ≈2–12 min，**相对主开销（`scan_s` ≈11.7 ks）可忽略、不构成新的瓶颈**）；
  产物名带因子 tag（`…-factor-ic-csi300-500-mf_net_surprise_20d.{json,md}`）⇒ **不会覆盖**任何既有产物。
- 长跑防挂起：`/usr/bin/caffeinate -w <pid>` 跟随进程（候选 #2/#3 都吃过 Deep Idle 挂起的亏）。
- 读数回填见 `docs/experiments/README.md` 台账 ＋ 本文件 §7。

## 7. 读数

（待跑。跑完由 nanobot 回填：`rc` / `elapsed_s` / `marks` / `periods` / `fwd_asof` / `members_sha256`
/ 各段 IC 与 CI / 分层 / 覆盖与裁剪诊断 / 同表并排的 `mom20`、`vr15` / 冗余门读数或缺口披露 / 折扣 / 结论。）
