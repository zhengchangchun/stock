---
name: task
description: >
  This skill should be used when the user starts a new development task, feature request, bug fix, code change, or any work item that requires implementing something in the codebase.
  它把「先验证，后交付」的全流程编排起来，并把各阶段映射到 superpowers 技能：brainstorming → writing-plans → using-git-worktrees → test-driven-development → systematic-debugging → requesting-code-review → verification-before-completion → finishing-a-development-branch。
  Invoke with /task <description> to run any development requirement through this disciplined workflow.
---

# 🚀 「先验证，后交付」工作流编排器

> **核心铁律**: 未验证不交付，未记录不通过，同样的错不犯第二次。

本 skill 是**外层编排**：它决定「现在处于哪个阶段、产物写到哪」；
每个阶段**具体怎么做**交给对应的 superpowers 技能。两者不冲突。

## 阶段 → 技能映射

| 阶段 | 调用的技能 | 产物 |
|------|-----------|------|
| 1. 需求澄清 | `superpowers:brainstorming` | 澄清后的需求 + 设计稿 |
| 2. 计划落盘 | `superpowers:writing-plans` | `docs/plans/YYYY-MM-DD-<task>.md` |
| 3. 隔离工作区 | `superpowers:using-git-worktrees` | 分支 / worktree |
| 4. 实现 | `superpowers:test-driven-development` | 代码 + 测试 |
| 5. 调试 | `superpowers:systematic-debugging` | 根因 + 错误日记 |
| 6. 复查 | `superpowers:requesting-code-review` | 复查意见 |
| 7. 验证 | `superpowers:verification-before-completion` | **验证证据** |
| 8. 收尾 | `superpowers:finishing-a-development-branch` | merge / PR |

## 工作流程

收到 `/task <描述>` 后按阶段顺序执行：

### 📋 阶段 1：需求澄清 + 任务拆分
1. 用 `brainstorming` 把需求聊清楚（目标、范围、非目标、验收标准）
2. 拆成**可独立验证的最小任务单元**，每个任务含：描述 / 实现步骤 / 验证方式
3. `TaskCreate` 建列表，记录到 `docs/tasks/YYYY-MM-DD-<task-name>.md`

### ⚠️ 阶段 2：查阅错误日记（每次必做）
1. 读 `docs/errors/ERROR_DIARY.md`
2. 检查是否有相关教训，在任务里标注风险点

### 🧭 阶段 3：计划落盘
用 `writing-plans` 把方案写成 `docs/plans/YYYY-MM-DD-<task>.md`，再动代码。

### 🛠️ 阶段 4：逐步实现
每个任务按顺序：
1. **实现**（TDD：先写失败测试）
2. **生成验证方案**
3. **执行验证**，贴出命令与输出
4. 记录决策 → `docs/decisions/`；变更架构 → `docs/architecture/`

> 一个任务未验证通过，不进下一个任务。

### 🔍 阶段 5：全局验证
- [ ] 所有任务完成
- [ ] 每个任务实测通过（有证据）
- [ ] 边界与错误路径已覆盖
- [ ] 无调试残留 / 死代码
- [ ] 文档已同步
- [ ] 错误日记已更新
- [ ] `scripts/verify.sh` 通过

### 📝 阶段 6：错误记录
发现教训立即写入 `docs/errors/ERROR_DIARY.md`（错误描述 / 根本原因 / 如何避免 / 检查清单）。

### 📬 阶段 7：交付
只有全部验证通过后才交付。交付内容须含：**变更摘要 · 验证证据 · 遗留问题**。

## 验证输出格式

```markdown
## 验证: [任务名]

### 执行步骤
1. `命令` → ✅ 通过（贴关键输出）
2. `命令` → ❌ 失败（贴报错）

### 结果
✅ 验证通过 / ❌ 验证失败，需修复
```

## 参考资源

- `references/workflow-guide.md` — 详细工作流指引
- `templates/VERIFY.md` — 验证模板
- `docs/errors/ERROR_DIARY.md` — 错误日记（每次必查）
- `~/.claude/CLAUDE.md` — 全局规约与技能映射
