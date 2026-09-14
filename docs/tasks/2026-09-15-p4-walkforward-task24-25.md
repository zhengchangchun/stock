# P4 收尾：Task 24（Walk-forward 切分器）+ Task 25（全量回归与交付验证）

- 日期：2026-09-15
- 计划依据：`docs/plans/2026-09-14-phase1-4-implementation.md` Task 24 / Task 25
- 起点基线：`pytest` = **419 passed**（离线 `unshare -n` 同为 419），`scripts/verify.sh` ✅，工作区干净
- 预算：$6；优先级 测试全绿 > 提交干净 > 报告落盘

## 一、目标与非目标

**目标**

1. 切分器：按时间前移、**绝不 shuffle**；train/test 之间 **purge/embargo**；数据不足时**显式报错**。
2. 样本量单位 = **交易日**（按日聚类），报告同时给出「交易日数」与「标的-日行数」，禁止拿行数当样本量。
3. 真实数据跑一次完整 walk-forward，落盘 `reports/`，并**明确判断**样本外交易日是否达到 **120 交易日**硬门槛；不够就直说，并给出「需要多长历史 / 多少标的」的算术。

**非目标（明确不做）**

- 不注册新策略（P5）、不做预测/验证命令（P6/P7）。
- 不产出任何策略绩效结论；不写 `docs/experiments/`（用户自有台账）。
- 本任务不需要「跑赢基准」的结论。

## 二、与计划的两处偏离（必须留痕）

| # | 计划原文 | 实际做法 | 原因 |
|---|---------|---------|------|
| D1 | `test_insufficient_data_returns_empty`：`split_walk_forward(sessions(5), train=5, test=3) == []` | 改为 `pytest.raises(InsufficientData)` | 用户验收标准明确要求「数据太短时**显式报错而非静默少跑**」。静默返回 `[]` 会让调用方把「0 折」当成「跑完了、没结论」，这正是本项目最怕的「静默降级」（ERROR_DIARY 2026-09-14 腾讯复权回退、2026-09-15 上游静默截断同源）。 |
| D2 | `Fold(index, train_start, train_end, test_start, test_end, ...)` 把区间端点列为构造参数 | `Fold(index, train_dates, test_dates, purged_dates=())`，端点改为 `@property` | 端点与日期元组是**同一个信息的两种表示**，同时作为入参就有「不一致」的自由度（可构造出 `train_start` 与 `train_dates[0]` 不同的 Fold）。派生为 property 后该类不变量由类型消除。 |

两处都**不放宽断言**：D1 让测试更严（报错 > 静默），D2 让类型更强。

## 三、任务拆分

| # | 任务 | 验证方式 |
|---|------|---------|
| 24a | `stocklab/backtest/walkforward.py`：`Fold` / `split_walk_forward` / 样本量报告 | `tests/test_backtest_walkforward.py` 全绿 |
| 24b | purge/embargo + 不 shuffle 的**反证测试**（变异法：打红 → 还原绿，贴两次真实输出） | 变异后必须变红 |
| 24c | CLI `backtest walkforward` → 真实数据报告落盘 `reports/` | 贴真实输出 |
| 25  | 全量回归（在线 + `unshare -n` 离线 + `verify.sh`） | 贴汇总行 |

## 四、验收清单

- [x] `pytest` ≥ 419 且全绿（**472 passed**）；`unshare -n` 全绿（472）；`verify.sh` ✅；工作区干净、已提交
- [x] 切分器单测覆盖：不 shuffle、purge/embargo 生效、折数随数据长度变化的边界、数据太短显式报错
- [x] 真实数据 walk-forward 报告落盘（`reports/2026-09-15-walkforward*.md|json`）+ 贴真实输出
- [x] Task 25 回归清单 + 「仍未验证/未覆盖」清单
- [x] 收尾 ≤40 行（见文末）

---

## 五、执行结果

### 24a 切分器 + 报告（`stocklab/backtest/walkforward.py`）

纯函数、零 I/O。折数公式 `floor((N - train - test) / step) + 1`。

### 24b 反证测试（变异法：先打红 → 还原绿）

注入的真实 bug 与实测结果（每条跑完立即用 `cp` 还原，并比对 sha256）：

| 变异 | 注入的 bug | 实测 |
|---|---|---|
| M1 | `if embargo:` → `if False:`（关掉 purge） | **2 failed**, 6 passed |
| M2 | `_validate_axis(sessions)` → `sorted(sessions)`（偷偷排序接受 shuffle） | **3 failed** |
| M3 | `raise InsufficientData(...)` → `return []`（不足时静默返回空表） | **2 failed**, 2 passed |

还原后逐条复跑：`8 passed` / `3 passed` / `4 passed`，
且 `sha256sum` 与注入前一致（`bab6150369a6f822...`），`diff` 逐字节相同。

### 24c 真实数据 walk-forward（报告落盘 `reports/`）

命令（可复现：同一输入 → 同一折分）：

```bash
.venv/bin/python -m stocklab.cli.main backtest walkforward \
    --code 000333 --code 600690 --start 2001-01-15 \
    --out reports/2026-09-15-walkforward-2codes.md
```

实测输出（原文）：

```
折数: 131
会话轴: 2013-10-08 ~ 2026-09-14（3007 个交易日）
样本外交易日: 2751 | 标的-日行数: 5502 | 标的数: 2
门槛 120 交易日: ✅ 达标（差 0 日 / 还需 0 个交易日 ≈ 0.00 年）
跳过（复权链拒绝，不回退）: 无
```

单标的默认跑（600690 被复权链拒绝、**不回退**到不复权）：

```
折数: 135
会话轴: 2013-09-18 ~ 2026-09-14（3095 个交易日）
样本外交易日: 2835 | 标的-日行数: 2835 | 标的数: 1
门槛 120 交易日: ✅ 达标
跳过（复权链拒绝，不回退）: {'600690': "MissingFactor: 600690 窗口 [1993-11-19, 2026-09-14]
  跨越了无法定价的除权事件 ['1996-05-02','1997-10-21','1999-08-12','2001-01-15'] ...
  请显式传 start >= 2001-01-15（chain.usable_from）"}
```

折分细节（双标的）：131 折，每折 `n_train=245`（250 位 − 5 purge）、`n_test=21`、`n_purged=5`；
首折 train `2013-10-08 ~ 2014-10-16` → test `2014-10-24 ~ 2014-11-24`；
末折 train `2025-07-28 ~ 2026-07-30` → test `2026-08-07 ~ 2026-09-04`；
轴末尾 **6 个交易日**（2026-09-07 ~ 2026-09-14）未被任何折用到 —— **如实报出**，未静默截断。

数据太短时的真实报错（退出码 2，且**不留半成品报告**）：

```
$ .venv/bin/python -m stocklab.cli.main backtest walkforward --train 3000 --test 200
❌ 会话数不足，无法切出任何一折：现有 3095 个交易日，至少需要 train(3000) + test(200) = 3200 个，
   还差 105 个。（本模块不会静默返回空表——0 折不是「没有结论」，是「没跑」）
退出码=2
```

### 核心结论：样本量够不够？

**够，而且远超门槛。** 实测样本外 **2751 个交易日**（双标的）/ **2835**（单标的），
门槛 120 个交易日 → 达标，超出 2631 个交易日。

**「需要多长历史」的具体算术**（本参数下 `train=250 / test=21 / step=21`）：

- 最少折数 = `ceil(120 / 21) = 6` 折（6 × 21 = 126 ≥ 120）
- 最少历史 = `250 + 21 + (6 - 1) × 21 = **376 个交易日**` ≈ **1.55 年**（按 243 交易日/年）
- 现有 3007 个交易日 → 富余 2631 个交易日 ≈ 10.8 年

**「需要多少标的」的答案：标的数不解决样本量问题。**
按日聚类（A 股同涨同跌），有效样本量只能是**交易日数**：
000333 + 600690 两个标的把行数从 2751 顶到 5502（×2），
但有效样本量仍是 2751，**一分没多** —— 两个标的的同一天是两个高度相关的行，不是一个独立样本。

**必须说清的两点保留**：

1. 2751 个样本外交易日**互不重叠**（测试窗逐日铺开，每折 test 不重叠，报告自查 `overlap_detected=false`），
   但 **131 折彼此并不独立**：相邻折的训练窗高度重叠，所以「131 折」不能当成 131 次独立实验。
   做显著性检验时必须按**日**聚类，且要意识到折间的相关性。
2. 本结论只针对**样本量**，不针对任何策略 —— 本任务没有评估任何策略，
   也没有产生任何 in-sample 或样本外的绩效数字。

### Task 25 全量回归

| # | 回归项 | 命令 | 结果 |
|---|--------|------|------|
| 1 | 全量测试（在线） | `.venv/bin/python -m pytest` | **472 passed in 3.34s** |
| 2 | 全量测试（离线） | `unshare -n .venv/bin/python -m pytest` | **472 passed in 3.36s** |
| 3 | 项目验证脚本 | `bash scripts/verify.sh` | ✅ 验证通过（`472 passed in 3.34s` 汇总行可见） |
| 4 | 端到端 doctor | `stocklab.cli.main doctor` | 退出码 0；`bars_daily=14164`、`features_daily=4`、`open_issues=0` |
| 5 | 端到端 features | `features build --date 2026-09-11` | 退出码 0；`written=2, conflicts=[]` |
| 6 | 端到端 backtest | `backtest run --start 2024-01-01 --end 2026-09-11` | 退出码 0；基准 `status=OK` |
| 7 | 依赖方向体检 | `grep -rn "from stocklab" stocklab/backtest/ \| grep -E "cli\|features"` | 无输出（无反向依赖） |
| 8 | 工作区 | `git status` | 干净（提交后） |

基线 419 → 472（+53：`test_backtest_walkforward.py` 49 条 + `test_cli_backtest.py` 4 条），**未删改任何既有断言**。

**仍未验证 / 未覆盖（不掩盖）**

1. **未跑 win rate / 校准度**：本任务只有样本量，没有预测，无法计算方向准确率与校准度。
2. **未接通策略执行器**：切分器只切「会话轴」，尚未把某折的 train/test 喂给 `engine.run_backtest`；
   这个串联属于 P5（注册策略时）。当前报告**不能**用于评价任何策略。
3. **未做统计显著性**：报告给的是样本量与折分，没有置信区间 / p 值 / 聚类稳健标准误。
4. **组合口径只覆盖 2 只股票**：公共会话轴交集让 000333 被裁 88 天、600690 被裁 3055 天；
   被裁的是**停牌等无行情日**（已抽查 2013-12-06、2014-04-11、2015-10-19 前后确有价格跳跃），
   但「停牌日该如何在组合里计价」这一口径**未定义**（P9 模拟盘再议）。
5. **`reports/` 未入库**：该目录被 `.gitignore` 忽略，且 `scripts/verify.sh` 第 4b 步**要求**它被忽略
   （防把数据/产物提交进仓库）。因此报告实体只在磁盘上，**可复现证据靠上面的命令 + 本节数字**。
   如果希望报告进版本库，需要另建 `docs/reports/` 之类的路径 —— 本任务未擅自改这条既有约定。
6. **`--embargo` 默认值 5 是本任务选的**（≈1 周），依据是「防标签跨切分点泄漏」；
   P6 定下预测视界（如 T+1 vs T+5）后应把 embargo 与之对齐，届时默认值可能需要调整。

---

## 收尾（P4 Task 24/25）

**① 变更文件**

- 新增 `stocklab/backtest/walkforward.py`（切分器 + 样本量报告 + Markdown 渲染，纯函数零 I/O）
- 新增 `tests/test_backtest_walkforward.py`（49 条）、`tests/test_cli_backtest.py` +4 条
- 改 `stocklab/cli/main.py`：新增 `backtest walkforward` 子命令（只读库、不产生策略结论）
- 改 `scripts/verify.sh`：去掉重复的 `-q`（`addopts` 已有），让 `N passed` 汇总行可见
- 新增 `docs/architecture/layers.md`（分层 + 依赖方向 + 三条「唯一出口」）
- 新增 `docs/tasks/2026-09-15-p4-walkforward-task24-25.md`（本文件）、`docs/errors/ERROR_DIARY.md` +3 条教训
- 落盘（**未入库**，`reports/` 被 gitignore 且 verify.sh 要求其被忽略）：
  `reports/2026-09-15-walkforward{,-2codes}.md|json`

**② 验证证据**

- `pytest` **472 passed**（基线 419，+53）；`unshare -n` 同为 **472 passed**；`verify.sh` ✅
- 变异反证 3 条（关 purge / 偷偷排序 / 静默返回空表）→ 分别 2、3、2 条测试变红，还原后逐字节一致
- 真实数据：131 折 / 2751 样本外交易日 / 门槛 120 → **达标**
- 数据不足路径实测退出码 **2** 且不留半成品报告

**③ 遗留问题（含样本量结论）**

- **样本量结论：够。** 2751 个**交易日**（非行数）≥ 120 硬门槛；达标所需最少历史 376 交易日 ≈ 1.55 年。
  但「131 折」不是 131 次独立实验（训练窗高度重叠），做检验必须按日聚类。
- 加标的**不**增加有效样本量（2 标的把行数顶到 5502，有效 N 仍是 2751）。
- 切分器尚未与 `engine.run_backtest` 串联（P5）；无显著性检验；停牌日计价口径未定义（P9）。
- `reports/` 不在版本库中 —— 报告实体只在磁盘，复现靠命令 + 本文数字。
- `--embargo` 默认 5 是暂定值，P6 定下预测视界后需对齐。
