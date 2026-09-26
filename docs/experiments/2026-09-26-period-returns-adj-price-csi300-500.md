# 实验：xsec-topn · 组合回放价格口径（未复权价 → PIT 复权价）· csi300-500

- **日期**：2026-09-26
- **提出人**：nanobot（用户 2026-09-24 授权「只要能提高 ai 模拟的准确率，就都按照你想的优化」）
- **状态**：**预注册（跑之前落盘）** —— 本文件在回放**之前**提交；跑完**不许改**，要改只能追加新实验
- **上游**：`docs/decisions/2026-09-26-ADR-030-信号有效性度量口径.md` §已知遗留、
  `docs/tasks/2026-09-25-p77-候选池打分价格口径.md` §0.3、`docs/experiments/2026-09-25-xsec-topn-csi300-500.md`

## 0. 预注册参数（机器可读；命令按此逐字段 fail-closed 校验）

```json
{"experiment": "xsec-topn", "pool": "short", "start": "2015-01-01", "universe": "csi300-500",
 "topn": 6, "hold_arm": "topn", "hold_control": "all-eligible", "min_periods": 120,
 "bootstrap_n": 2000, "bootstrap_seed": 20260918, "benchmark": "sh000300",
 "rule": "WIN = CI 下界 > 0；CI 跨 0 ⇒ 如实写「无可测的选股增量」"}
```

> 与 `2026-09-25-xsec-topn-csi300-500.md` **逐字段相同**（同窗口、同池、同 N、同判据、
> 同 bootstrap 次数与种子、同基准、同宇宙）。**唯一变量 = `candidate/replay.py::period_returns`
> 的收益价格口径**。

## 1. 假设

`period_returns` 的周期毛收益是 `p1 / p0 − 1`，`p0/p1` 直读 `bars_daily.close`；
而 `bars_daily` 全表 `adj_mode='none'` ⇒ **未复权价**（ADR-030 §已知遗留；P77 §0.3 把
「所有其它价格路径都走 `load_bars_adjusted`」列了一遍时**漏了 `replay.py` 这一条**）。
除权日的价格跳空因此被算成**组合亏损**。

> **假设**：把 `p0 / p1` 换成 **PIT 复权价**（`adjust.load_bars_adjusted(conn, code,
> as_of=该日, start=chain.usable_from)`）后，两臂的周期收益序列与 Δ 都会变。

**方向与显著性不预设**：本实验只**量化影响**（CLAUDE.md 度量纪律 6：不许改口径救结果，
也不许预设结论方向）。

## 2. 失效条件（提前写死）

- 复权不可用（`MissingFactor` / `EtfChainUnsupported` / 该日无 bar）⇒ 该只该期**回退未复权价**
  并计数 `n_adj_fallback`（按 `(code, 周期)` 去重）；**不抛、不静默**（与 ADR-030 D4 同口径）。
- 验证段周期数 < `min_periods`(120) ⇒ 仍按既有 `_verdict` 输出 `INCONCLUSIVE`，
  不得读成「没效果」。
- **默认路径零变化**：`--price-mode raw`（默认）必须与 `reports/research/2026-09-24-xsec-topn-csi300-500.json`
  逐字段一致，差异只许出现在 `elapsed_*` / 时间戳 / 本次新增键上。若 raw 臂出现数值差异
  ⇒ **本实现破坏了默认路径**，先修再谈结论。

## 3. 数据与非 PIT 披露

- 窗口 `2015-01-01 → 2026-09-24`、`pool=short`、`universe=csi300-500`（800 只**现成分、非 PIT**）、
  基准 `sh000300`、成本口径照旧（ADR-017 D-13/D-14，**本站不动**）。
- 三条已知非 PIT 项同 `2026-09-25-xsec-topn-csi300-500.md`（ST 取当前名 / sector 取当前值 /
  种子宇宙事后挑选）。
- **新增披露**：复权链覆盖 —— 池内 800 只里 `adj_factors` 非空的只数、`corp_actions` 事件总数，
  以及「窗口内至少一只池内成员发生除权的调仓周期数」（＝本口径的**影响上限**）。
- **AS-OF 语义**：`load_bars_adjusted(as_of=d1)` 只累乘 `cqr ≤ d1` 的事件 ⇒ 无未来函数；
  `d0` 侧另有既有 `guard_pit_prices` 守卫，**不许绕过**。

## 4. 判据（跑完照此写结论）

1. **raw 臂** vs `2026-09-24-xsec-topn-csi300-500.json`：逐字段一致（除 `elapsed`/新键）
   ⇒ 默认路径零变化。
2. **adj 臂**：两臂各段（全窗 / 训练 / 验证）收益、Δ 与 95% CI、`verdict`，与 raw **并排**列出。
3. `n_adj_fallback` ＋ §3 的影响上限读数。
4. 结论词汇**只许**这三种：`SAME_VERDICT`（verdict 未变）/ `VERDICT_FLIPPED`（翻转）/
   `INCONCLUSIVE`。**不得**写「口径修好了所以策略变好了」——**采纳动作另派任务书**。

## 5. 后果与后续

- 若 Δ 与 verdict **显著变化** ⇒ 另开任务书决定是否把 `period_returns` 的默认口径切到复权价
  （那会同时动 `plugin/sandbox.py` 经注入调用的回放路径 ⇒ 属**第二个变量**，必须单独预注册）。
- 若**几乎不变** ⇒ 记一笔「已量化、影响可忽略」，ADR-030 的遗留降级为「已知无害差异」。
- 本实验**不改**任何真库数据、**不 regen** 红线基线。
