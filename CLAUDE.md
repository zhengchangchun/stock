# CLAUDE.md — stock-lab

> A股预测-验证-自优化闭环系统（每日预测、次日验证归因、回测模拟与实盘对比、策略自动迭代）

本项目遵循全局规约 `~/.claude/CLAUDE.md` 的「先验证，后交付」体系。以下为本项目补充说明。

## 项目概述

<!-- 一两句话说明这个项目是做什么的、给谁用 -->

## 快速开始

```bash
# 安装依赖
# 运行
# 测试
```

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

<!-- 填上本项目的实际测试命令，例如: npm test / pytest -q -->
