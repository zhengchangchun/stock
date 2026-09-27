# 实验：研究侧默认复权口径（`research xsec-topn` 的 `--price-mode` 默认 raw → adj）

- **日期**：2026-09-27
- **提出人**：nanobot（P94 任务书）
- **状态**：已采纳（**口径采纳，不是策略改动**）
- **上游**：ADR-030 §已知遗留 ／ P82（[`docs/tasks/2026-09-26-p82-组合回放复权价口径.md`](../tasks/2026-09-26-p82-组合回放复权价口径.md)）§7.2 D7「不做采纳、另立任务书」 ／ 本变更任务书 [`docs/tasks/2026-09-27-p94-研究侧默认复权口径.md`](../tasks/2026-09-27-p94-研究侧默认复权口径.md)
- **预注册**：本文（**变更前写定**，判据见 §5）

## 1. 假设

> 把 `research xsec-topn` 的收益侧默认口径从**未复权价**切成 **PIT 复权价**，
> 能让「除权日跳空被算成组合亏损」这条**已知偏差**从默认路径上消失；
> 并且它**不**改变任何既有结论的方向。

**这不是「策略会变好」的假设。** 采纳理由只有一条：不留一个已知的口径偏差在默认路径上。
报告/文档里**不得**出现「切了 adj 所以策略更好」这类表述。

**失效条件**（提前写死）：
- 若两口径的 verdict 出现**分叉**（一档 `LOSE`、另一档 `WIN`）或任一读数越出 P82 已披露的 CI
  ⇒ 采纳撤回，回落到开关默认 `raw`。
- 若契约层出现**任何逐位变化** —— `candidate/replay.py::period_returns` 的形参默认
  （`price_mode: str = DEFAULT_PRICE_MODE`）、`DEFAULT_PRICE_MODE` 常量语义、或
  `plugin/sandbox.py` 的调用点 ⇒ 本变更作废（D2／D8）。

## 2. 变量（只改一个）

| 项 | 基线值 | 实验值 |
|---|---|---|
| `research xsec-topn` 的 `--price-mode` **argparse 默认值** | `raw` | `adj` |
| `cli/research.py` 的 `getattr(args, "price_mode", …)` 兜底常量 | `PRICE_MODE_RAW` | `PRICE_MODE_ADJ` |

其余一律不变：窗口 `2015-01-01~2026-09-24`、池 `short`、宇宙 `csi300-500`（800 只）、
topn 6、`MIN_VALID_PERIODS=120`、bootstrap n=2000 seed=20260918、成本模型、
涨跌停判定（未复权 `prev_close`）、`period_returns` 的**函数默认** `DEFAULT_PRICE_MODE = "raw"`、
`plugin/sandbox.py` 的调用点（不传 `price_mode` ⇒ 自动走 raw）。

**契约层（沙盒）不进本实验**：沙盒读数是插件版本之间的对照基线，默认口径一变，
历史 `source_sha256` 的对照关系就漂。本站只改「谁在调」（CLI 默认值），不改「函数怎么默认」。

## 3. 数据

- 区间：`2015-01-01 → 2026-09-24`（**全窗，P82 已跑，本站不重跑**）
- 股票池 / 标的：`csi300-500`（800 只，`members_sha256 eff7b478ea9a…`），topn 6
- 数据快照版本：真库 `data/stocklab.db`（全程**只读**，sha 跑前 == 跑后）
- **唯一可引版本**（不许重算、不许改写）：
  `reports/research/2026-09-24-xsec-topn-csi300-500-adj.json`
  （`--price-mode both`，571 marks / 570 periods）

## 4. 结果（样本外）

| 收益侧口径 | 验证段 Δ/期 | 95% CI | verdict | n_validate |
|---|---|---|---|---|
| `raw`（未复权） | **+0.3025%** | `[-0.5076%, +1.1480%]` | **LOSE** | 171 |
| `adj`（PIT 复权） | **+0.2757%** | `[-0.5401%, +1.1038%]` | **LOSE** | 171 |

- 两口径 verdict **都是 `LOSE`**；CI 都跨 0；两档点估计相差 **0.027pp**（远小于 CI 宽度）。
- **样本量是否 ≥ 120 交易日**：是（`n_validate = 171`）。
- 按日聚类的显著性：CI 由 2000 次按日重采样得出（`seed 20260918`）。
- 影响面（P82 §7.5，只读）：全窗 **453 / 570 = 79.5%** 的调仓周期里至少一只成员发生除权。
- `n_adj_fallback = 5171`（复权不可用而回退未复权的 `(code, 周期)` 数）。
- 判据 `WIN = CI 下界 > 0`；两档都没到。

## 5. 结论

- **采纳**：`research xsec-topn` 的 `--price-mode` 默认 `raw → adj`；`raw` / `both` 仍可显式指定
  （取值集合不变，`choices` 三档不动）。
- 理由（一句话）：**两口径 verdict 都是 `LOSE`，采纳 `adj` 只是为了不留已知偏差** —— 不是「策略变好」。
- **不重跑历史实验作为判据**：既有 `LOSE` 结论在 `adj` 口径下同样成立，证据就是 §4 的
  P82 全窗读数（本站**不重算**、不改写）。
- **验收判据只看逐位不改性**（D8）：
  ① 不传 `price_mode` 的 `period_returns` 调用**逐位不变**（`tests/test_replay_price_mode.py::test_default_mode_is_raw_and_bitwise_identical` 继续绿）；
  ② `plugin/sandbox.py` 的调用点**不传** `price_mode` ⇒ 走函数默认 `raw`（源码扫描 ＋ AST 两档钉住）。
- 若否证 —— 学到了什么：不适用（本档判据是「不改性」，不是统计增量）。

## 6. 复现

```bash
# 默认口径（不传 --price-mode ⇒ adj；产物名带 -adj）
.venv/bin/python -m stocklab.cli.main research xsec-topn \
    --pool short --start 2016-01-01 --end 2016-12-31 --universe csi300-500 \
    --prereg <start=2016-01-01 的预注册> --out /tmp/p94/ --db <真库克隆副本>

# 对照（显式 raw；产物名无后缀）
.venv/bin/python -m stocklab.cli.main research xsec-topn \
    --pool short --start 2016-01-01 --end 2016-12-31 --universe csi300-500 \
    --price-mode raw \
    --prereg <同一份预注册> --out /tmp/p94/ --db <真库克隆副本>
```

> `--prereg` 的 `start` 必须与命令行 `--start` 逐字一致（fail-closed）；
> 短窗只为证明「开关与默认真的接通」，**短窗数字不是结论**。
