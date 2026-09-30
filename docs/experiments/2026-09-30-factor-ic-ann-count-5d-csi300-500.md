# 实验：factor-ic · 候选信号源 #4 `ann_count_5d`（5 交易日公告条数）· csi300-500

- **日期**：2026-09-30
- **提出人**：nanobot（依据 `docs/plans/2026-09-27-p95-新信号源-设计稿.md` §5.4 的 **EV-A** 与 §8 的 P98 顺序
  第 4 项；用户 2026-09-24 「只要能提高 ai 模拟的准确率，就都按照你想的优化」的常驻授权）
- **状态**：**预注册（跑之前落盘）** —— 本文件在度量**之前**提交；跑完**不许改**，要改只能追加新实验
- **上游**：
  `docs/tasks/2026-09-30-p102-ann-count-5d研究侧取值器.md`（研究侧取值器 `ann_count_5d`，P102 交付，§10 已
  独立复核 ⇒ **本实验零代码**）、
  `docs/plans/2026-09-27-p95-新信号源-设计稿.md` §5.4（EV-A 定义与覆盖实测）、
  `docs/experiments/2026-09-29-factor-ic-ep-ttm-csi300-500.md` §7（候选 #3 判决：CI 跨 0 ⇒ 不采纳
  ⇒ 顺序推进到 #4）

## 0. 预注册参数（机器可读；命令按此逐字段 fail-closed 校验）

```json
{"experiment": "factor-ic", "pool": "short", "start": "2015-01-01", "universe": "csi300-500",
 "factors": ["mom20", "vr15", "ann_count_5d"],
 "secondary_factors": ["roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"],
 "ic_type": "spearman", "n_layers": 5, "horizon": 5,
 "min_periods": 120, "min_xsec_n": 30, "bootstrap_n": 2000, "bootstrap_seed": 20260918,
 "rule": "逐因子独立判：验证段 2000 次按日重采样 95% CI 不含 0 ⇒ IC_SIGNIFICANT（IC 为负也算 significant）；含 0 ⇒ IC_NOT_SIGNIFICANT；验证段有效日期数 < MIN_VALID_PERIODS(120) ⇒ INCONCLUSIVE。次因子只报读数、不作判据。另有预注册冗余门：验证段逐调仓日的截面 Spearman(|rho|) 均值 —— max(|rho_mom20|,|rho_vr15|) > 0.5 ⇒ 冗余淘汰（即使 IC_SIGNIFICANT 也不采纳）；0.3 < max ≤ 0.5 ⇒ 需另立残差 IC 实验；≤ 0.3 ⇒ 视为独立候选。"}
```

> 窗口 / 池 / 宇宙 / 前向收益口径 / 判据常量 / bootstrap 次数与种子，与
> `2026-09-26-factor-ic-short-csi300-500.md` §0 **逐字段相同**。**唯一变量 = 主读数名单**换为
> `ann_count_5d`（`mom20` / `vr15` 同表并排、同一个 verdict 词汇，**不许挑一个当推荐**）。

## 1. 假设

`ann_count_5d`（P95 §5.4 EV-A）＝ 窗口 `notice_date ∈ (d₋₅, d₀]` 内的 `announcements` **全类型**公告条数，
`d₀ = asof`、`d₋₅` = `trading_calendar` 里 `<= asof` 的第 **6** 个交易日（左端**开**、右端**闭**的
**日期区间**读法，不是「最近 5 个交易日的闭集」——`announcements` 实测 **19.5%** 的行 `notice_date`
落在周六/周日，闭集读法会静默丢弃这 1/5）。
PIT 锚 = `notice_date`；`display_time` **永不参与筛选**。走 `research/factor.py` 的 `_ann_count_5d_map`
（P102 交付），**不过 `title` / `column_name` 任何筛**（`ev_回购` 是另一个尚未立项的因子）。

**假设**：公告密度对下一个调仓周期（5 交易日）的前向收益**有可测的横截面排序能力**。

**方向不预设**（CLAUDE.md 度量纪律 6）：只量化 IC 与分层。常见的两种相反叙事都存在
（披露密度高 ⇒ 信息更新快 / 或有负面事件；无公告 ⇒ 安静、或有长期停牌类风险），
本实验**不据此预设符号**（负 IC 同样算 `IC_SIGNIFICANT`）。

**头号风险（本节提前写死）**：**分辨率粗糙**。零值是真值（见 §2）⇒ 早期年份「多数标的是 0」：
`2016-03-15` 截面零值占比 **0.626**、`2020-07-01` `0.369`、`2024-09-02` `0.156`、`2026-09-24` `0.404`
（P102 §9.4 / §10.2 我自跑实测，两方逐格一致）。并列（tie）比例同步高 ⇒
**Spearman 的名次信息会被大量并列压扁**。

- 这**不是**取值器的缺陷，是数据本身的形状；**不许**用「加噪声抖开并列 / 二值化 / 取对数 /
  只保留有公告的标的」等手法掩盖。
- ⇒ 本实验**必须同时报** `zero_ratio_p50` / `tie_ratio_p50`（P102 新增的两个只增键），
  与 IC / CI 并排读；**这两个键不参与 verdict**（判据一个都不许动）。
- **`LOW_COVERAGE` 闸门对本因子永不触发**（每个宇宙成员都有值，没公告就是 `0.0`）⇒
  判读**只认** IC / CI / t，**不得**把「覆盖 100%」当质量证据。

## 2. 失效条件（提前写死）

- 某日截面**同时有因子值与有效前向收益**的标的数 < `MIN_XSEC_N = 30` ⇒ 该日 IC 记 `None`
  并计入 `n_dates_skipped`，**不许**用 0 顶替。
- 验证段有效日期数 < `MIN_VALID_PERIODS = 120` ⇒ `INCONCLUSIVE`（＝「测不出」≠「没效果」）。
- **缺失语义（本档与其它研究侧因子不同，必须点名）**：窗口内**没有公告 ⇒ `0.0`，不是 `None`**
  —— 数量确实是 0，不是「算不出」；宇宙每个成员都有值（库外 code 也是 `0.0`）。
  ⇒ `value_coverage_p50` 恒等于宇宙成员数、`LOW_COVERAGE` 永不触发（见 §1 末）。
- **空窗**（`asof` 前不足 6 个交易日）⇒ 该日窗口算不出（**不拿「有几日算几日」顶替**，与 MF-A 同规）。
  本实验窗从 2015-01-01 起、`trading_calendar` 覆盖 2007-01-15 起 ⇒ 不会触发。
- 复权不可用 ⇒ 前向收益回退未复权价并计 `n_fwd_fallback`（P78 D4 同口径，**不抛**）；裸 `AdjustError` 原样抛。
- 取值走研究侧取值器 `_research_map(conn, asof, "ann_count_5d", member_codes)`（P102）⇒
  **不许**另立价格口径，**不许**把它注进 `payloads_by_mark[*]["feats"]`（伪造 `ctx` 形状，会让覆盖度读数说谎）。

## 3. 数据与非 PIT 披露

- 区间 `2015-01-01 → 2026-09-24`、`pool=short`、`universe=csi300-500`（800 只**现成分、非 PIT**）。
- 三条已知非 PIT 项同 `2026-09-25-xsec-topn-csi300-500.md`（ST 取当前名 / sector 取当前值 / 宇宙事后挑选）。
- `notice_date` 的 PIT 锚 = 公告日（P88 D2 / P96 §7.7）；该表按日、收盘后可得。
- **源数据起止披露**：`announcements` 实测 `COUNT(*) = 1,128,199`、`MIN(notice_date) = 2014-12-19`
  （P96 首采 `--days 4300` 的 cutoff）、`MAX = 2026-09-29`、宇宙 800/800 覆盖、周末行 220,271（19.52%）。
  ⇒ **窗口左端对 2015-01-01 起的首几个 `asof` 会被截断**（2014-12 末的几天），
  影响仅限全窗最左侧、**不改可判定性**；解读时须报出这一截断。
- 因子值**不来自打分用的 `ctx`**（`source=research`，P83 之后第四次口径扩增，ADR-045 追加段）⇒
  读数只能说「这个定义在这段历史上与收益的关系是这样」，**不能说「这就是插桩用的因子」**。
- 只读、零写入：走 `--db` 指向的**副本**；不新表、不迁移、不 regen 红线、不改任何插桩。

## 4. 判据（跑完照此写结论）

1. **主读数**：`ann_count_5d` 的全窗 / 训练段 / 验证段 IC 均值、95% CI、IR、t、分层（5 层）spread、
   单调步数、`n_dates_skipped`、`n_fwd_fallback`、覆盖度（`value_coverage_p50` 与当日截面 P50）；
   **并排报** `zero_ratio_p50` / `tie_ratio_p50`（§1 头号风险）；`mom20` / `vr15` 同表并排（同样出 verdict）。
2. **冗余门（预注册，跑完只许照此判）**：`ρ̄` = 验证段逐调仓日的**截面 Spearman**均值 ——
   分别对 `ann_count_5d` 与 `mom20`、`ann_count_5d` 与 `vr15`，在「当日两个因子都有效」的**共同代码集合**上计算。
   - `max(|ρ̄_mom20|, |ρ̄_vr15|) > 0.5` ⇒ **冗余淘汰**（即使 IC_SIGNIFICANT 也不采纳）；
   - `0.3 < max ≤ 0.5` ⇒ 需另立「残差 IC」单变量实验后才谈采纳；
   - `≤ 0.3` ⇒ 视为独立候选（仍要过下面的 IC 与折扣）。
   **度量路径与已知缺口**：harness **不出**该读数（`research factor-ic` 无此输出，候选 #2/#3 §7 已披露）；
   P98 的只读探针 `/tmp/p98b/rho_probe.py` 存在，本实验**若 verdict 为 `IC_SIGNIFICANT` 则必须先用它补齐
   该门**（探针 sha256 与输出 json 回填 §7），否则按「缺口未执行、不改判」如实披露。
3. **折扣（只用于解读，不改代码门槛）**：
   - 有效样本折扣：`ann_count_5d` 是**日频**（不是季频）⇒ 相邻调仓日仍有重叠（5 日持有期 ⇒ 1 倍重叠）
     ⇒ 若结论要采纳，须另立**按日聚类 / 换窗**的稳健性实验；本档只**报**「重叠倍数 = 1.0」这一事实。
   - 族大小折扣：本实验的假设来源是 P95 §5 的候选池（`gm_yoy_pp`、`mf_ratio_5d`、`ep_ttm` 已否、
     本档是第 4 个）⇒ 报出 `1 − 0.95^k`（k = 4 ⇒ **18.5%**）供解读，**不作判据**。
4. **结论词汇只许四个**（`IC_SIGNIFICANT` / `IC_NOT_SIGNIFICANT` / `INCONCLUSIVE` / `LOW_COVERAGE`）；
   **不得**写因果推断，**不得**据此直接改打分函数或权重。

## 5. 后果与后续

- `IC_SIGNIFICANT` 且过冗余门（`max|ρ̄| ≤ 0.5`）⇒ `ann_count_5d` 获得「进池 / 进打分」的候选资格，
  下一步是**新预注册的单变量实验**（一次只动一处）。
- `IC_NOT_SIGNIFICANT` / `INCONCLUSIVE` / 冗余淘汰 ⇒ 写成「**在现有证据下不足以采纳**」，
  按 P95 §8 的顺序推进下一候选（`pe_pct_756` / `mf_net_surprise_20d`，需先补各自的研究侧取值器
  ⇒ 另立任务书）。
- 无论结果如何，本实验**不改**任何真库数据、**不 regen** 红线基线、**不改**任何插桩脚本。

## 6. 执行记录

- **本站＝nanobot 自己执行（零代码改动、零 CC 站）**：命令只需 P102 已交付的
  `research factor-ic --factor ann_count_5d`。
- 命令（真库只读；为避开 15:30 收盘链写入，走与候选 #1/#2/#3 **同一个** APFS 克隆副本
  `/tmp/p98/copy.db`（2026-09-29 14:00 克隆）⇒ marks/周期数与
  `2026-09-29-factor-ic-ep-ttm-csi300-500.md` **逐字段可比**）：

```bash
.venv/bin/python -m stocklab.cli.main research factor-ic \
  --pool short --start 2015-01-01 --end 2026-09-24 \
  --universe csi300-500 --factor ann_count_5d \
  --prereg docs/experiments/2026-09-30-factor-ic-ann-count-5d-csi300-500.md \
  --out reports/research/ --db /tmp/p98/copy.db
```

- 预期时长 ≈ **3.1–3.3 h CPU**（同口径实测候选 #1 = 11615.5 s、#2 = 11883.3 s、#3 = 43451.3 s 含挂起；
  本因子的窗口查询计划是 `SCAN announcements USING COVERING INDEX idx_announcements_code_notice`、
  实测 ≈0.76 s/`asof` ⇒ 571 日外推 ≈7.2 min，**相对主开销可忽略、不构成新的瓶颈**）；
  产物名带因子 tag（`…-factor-ic-csi300-500-ann_count_5d.{json,md}`）⇒ **不会覆盖**任何既有产物。
- 长跑防挂起：`/usr/bin/caffeinate -w <pid>` 跟随进程（候选 #2/#3 都吃过 Deep Idle 挂起的亏）。
- 读数回填见 `docs/experiments/README.md` 台账 ＋ 本文件 §7。

## 7. 读数

（跑完由 nanobot 回填）
