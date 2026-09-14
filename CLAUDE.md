# CLAUDE.md — stock-lab

> A股预测-验证-自优化闭环系统（每日预测、次日验证归因、回测模拟与实盘对比、策略自动迭代）

本项目遵循全局规约 `~/.claude/CLAUDE.md` 的「先验证，后交付」体系。以下为本项目补充说明。

## 项目概述

面向 A 股的**预测 → 验证 → 归因 → 自优化**闭环系统。每晚产出次日预测，次日收盘后用真实行情验证并归因，模拟盘与实盘对比，策略在统计纪律约束下迭代。

使用者：个人投资者（本项目作者）。持仓现状：000333 美的集团（约 50%）+ 现金（约 50%）。

## 准确率优先（用户明确的最高目标）

> 系统存在的意义是**预测得更准**，不是功能更多。
> 凡有取舍，以「能否提升**样本外**预测准确率」为第一标准；漂亮的功能不等于价值。

### 度量纪律（违反即视为无效结论，不得上报）

1. **样本外才算数**：策略/参数只有经过 walk-forward（滚动训练窗 → 验证窗）后的样本外结果才可上报。样本内表现仅用于调试。
2. **按日聚类**：A 股同涨同跌，20 标的同一天 ≠ 20 个独立样本。有效样本量按**交易日**计，统计须做日间聚类稳健处理。
3. **样本量门槛**：不足 **120 个交易日**的结论一律标注「样本不足，仅供观察」——不得用于选策略、不得写进优化理由。
4. **扣成本**：A 股最低佣金 5 元/笔 + 印花税 + 滑点，必须进净值曲线；回测不扣成本的成绩视为无效。
5. **基准对照**：任何策略必须与 `index_300` / `buy_and_hold` 比较，**跑不赢就明说**。
6. **失败是合法结论**：没有 edge 就写「无 edge」。禁止改口径、挑区间、换指标把结果“救”回来。
7. **单变量原则**：一次实验只改一个变量，否则无法归因。

### 优化循环（建成后不间断）

```
收集数据 → 预测 → 次日验证 → 归因 → 提假设 → 单变量实验
   ↑                                              ↓
   └──── walk-forward 评估：达标才改策略 ──── 不达标则记台账、保留原策略
```

- 每轮实验必须落 `docs/experiments/YYYY-MM-DD-<name>.md`：假设 / 数据区间 / 变量 / 结果 / 结论（**含否证结论**）。
- 实验台账 **append-only**，失败的实验也要留（否则会重犯同一个错）。
- 准确率指标**提前定死，事后不许换**：① 方向准确率 ② 相对沪深300 的超额 ③ 校准度（预测概率 vs 实际频率）。
- 每天定时采集（交易日收盘后）+ 预测 + 验证，由 nanobot 侧调度触发；**项目内不实现 cron/常驻进程**。

### 反过拟合红线

- ❌ 全样本调参后再报全样本成绩
- ❌ 因为某几天表现差就把样本删掉
- ❌ 参数搜索越过 walk-forward 训练窗
- ❌ 用「市场噪声」当万能解释（该结论必须有数据支撑，不是甩锅）

## 快速开始

```bash
# ① 建虚拟环境（系统 python3 是 3.14，套用 3.12 的解释器建）
/opt/nanobot-venv/bin/python -m venv .venv

# ② 装依赖（pyproject 当前**没有** [build-system]，所以 `pip install -e .` 不可用；
#    测试靠“在仓库根目录跑”把包路径接入 sys.path，不需要可安装安装）
.venv/bin/pip install requests pytest numpy pandas

# ③ 初始化数据库
.venv/bin/python -m stocklab.cli.main db init
```

> ⚠️ 不要写 `.venv/bin/pip install -e '.[dev]'` —— `pyproject.toml` 里既无 `[build-system]` 也无 `[dev]` extra，这条命令必然报错（P2 实测）。

## 核心铁律

> **未验证不交付 · 未记录不通过 · 同样的错不犯第二次**

主流程：`需求澄清 → 任务拆分 → 计划落盘 → 隔离工作区 → 实现(TDD) → 验证 → 文档同步 → 交付`

## 技能使用映射

| 阶段 | 技能 | 产物 |
|------|------|------|
| 需求澄清 / 设计 | `superpowers:brainstorming` | `docs/plans/` 设计稿 |
| 拆解为计划 | `superpowers:writing-plans` | `docs/plans/YYYY-MM-DD-<task>.md` |
| 隔离工作区 | `superpowers:using-git-worktrees` | 分支 / worktree |
| 实现 | `superpowers:test-driven-development` | 代码 + 测试 |
| 执行计划 | `superpowers:executing-plans` | 逐任务提交 |
| 调试 | `superpowers:systematic-debugging` | `docs/errors/ERROR_DIARY.md` |
| 复查 | `superpowers:requesting-code-review` | 复查意见 |
| 完成前验证 | `superpowers:verification-before-completion` | 验证证据 |
| 收尾 | `superpowers:finishing-a-development-branch` | merge / PR |
| 全流程编排 | 本地 `/task` | `docs/tasks/` |

## 工作流程

1. **需求澄清** — 用 `brainstorming` 把想法聊成设计，写到 `docs/plans/`
2. **任务拆分** — 拆成可独立验证的最小单元，用 TaskCreate，记录到 `docs/tasks/`
3. **查阅错误日记** ⚠️ — 每次开工前先读 `docs/errors/ERROR_DIARY.md`
4. **实现** — 按 TDD，一个任务一个任务来
5. **验证** — 每个任务写完立刻验证，未通过不进下一个
6. **文档同步** — 架构/决策/错误日记随改随更
7. **交付** — 只有全部验证通过、`scripts/verify.sh` 通过，才交付

## 目录结构

```
stock-lab/
├── CLAUDE.md               # 本文件
├── .gitignore
├── docs/
│   ├── README.md           # 文档索引
│   ├── architecture/       # 架构设计
│   ├── tasks/              # 任务拆分记录
│   ├── plans/              # 设计与实现计划
│   ├── decisions/          # 设计决策对比（ADR）
│   └── errors/             # 错误日记（防重犯）
├── skills/task/            # /task skill 源码（软链到 .claude/skills/）
├── templates/VERIFY.md     # 验证模板
└── scripts/verify.sh       # 验证辅助脚本
```

## 验证标准（DoD）

一个任务算“完成”，必须同时满足：

- [ ] 计划文件已存在且被遵守
- [ ] 测试/验证命令**实际执行过**，输出被贴出
- [ ] 边界与错误路径有覆盖
- [ ] 代码无调试残留、无死代码
- [ ] 相关文档已同步
- [ ] 有新教训已写入错误日记
- [ ] git 有清晰的提交信息

## Git 规范

- 新功能从 `main` 拉新分支（或 worktree）
- 提交信息清晰描述变更内容
- **提交前必须跑验证**（`scripts/verify.sh`）

## 测试命令

```bash
.venv/bin/python -m pytest -q     # 单元/集成测试（离线，禁止真实联网）
bash scripts/verify.sh            # 交付前必须通过
```

- **解释器固定 `.venv/bin/python`**（Python 3.12）；系统 `python3` 是 3.14，装了也没用。
- 测试默认走 fixture / raw 缓存重放，**离线必须全绿**；真实联网只允许出现在显式标注的录制脚本里。
