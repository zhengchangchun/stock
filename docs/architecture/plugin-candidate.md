# 插桩执行模型与状态机设计说明

> 本文描述 `stocklab/plugin/` 子系统与候选池流水线的接驳方式，
> 供后来者理解「为什么这样设计」，而不只是「这是什么」。

## 插桩是什么

「插桩（plugin）」是一段存在**数据库**里的 Python 脚本（`plugin_scripts.source_text`），
不是文件系统里的 `.py`。每段脚本暴露一个 `run(ctx) -> dict` 函数。

目的：让策略逻辑可以在运行时被替换、审计、回滚，而**不需要重新部署代码**。
副作用：版本、审批、归档的状态全部是数据，用 SQL 可查，无 hidden state。

## 六个插桩编号的分工

| 编号 | 职责 | 池 | 本轮实现状态 |
|------|------|-----|-------------|
| 0 | 行业特殊排雷（pre-screen 之后） | 全池通用 | 量价近似，非 PIT |
| 1 | 短期量价打分 | 短期池 | 真实现（纯量价） |
| 2 | 中期景气 & 财务打分 | 中期池 | **占位**（财报未接） |
| 3 | 长期护城河打分 | 长期池 | **占位**（财报未接） |
| 4 | 风险权重修正（step 8） | 全池通用 | 真实现 |
| 5 | AI 优化子流程 / 复盘 | 离线批量 | **占位**（子流程未实现） |

## 状态由事件流推导

`plugin_scripts` 只存「已发生了什么」，当前状态通过回放事件流（`plugin_audit`）得到：

```
submitted → sandbox_pass/fail → pending_review → approved → active
                                                          ↗ 新版本 approved 时旧版自动 archived
                                  → rejected
```

**为什么不存一个 `status` 字段？**

1. `status` 字段会出现「更新」——更新是可修改的；事件流是 append-only 的。
2. 审计链完整性由 `trg_plugin_audit_no_update / no_delete` 触发器保证。
3. 任何时间点的历史状态都可通过截断事件流还原，不丢信息。

## 沙盒 verdict 的含义

`sandbox.run_sandbox` 会返回三种 verdict：`WIN` / `LOSE` / `INCONCLUSIVE`。

- **`INCONCLUSIVE`（样本不足或首版无 baseline）→ `pending_review`**：
  不足以作出对比结论不等于脚本有问题，不应因此直接驳回。
  人工审核者可以在 `INCONCLUSIVE` 的情况下 approve，但**必须清楚这不是「经过验证」**。
- **`LOSE` → `pending_review`**（有证据表明更差）：
  同样进入人工审核，拒绝权在人，不在系统。
- 本轮空数据库下所有 submit 的 verdict 均为 `INCONCLUSIVE`——
  这是 fail-closed 设计的预期行为，不是 bug。

## 候选池流水线的 12 步与插桩的接驳点

```
step 1-2  seeds + screen (pre_screen, ST/停牌过滤)
step 3    pre_screen 固定规则 (screen.py)
step 4    插桩0 行业特殊排雷      ← lifecycle.call_active("0", ctx)
step 5-6  三池分流 + 补 K 线
step 7    插桩1/2/3 按池打分      ← lifecycle.call_active(pool_plugin_id, ctx)
step 8    插桩4 风险权重修正      ← lifecycle.call_active("4", ctx)
step 9    快照落库
step 10   Markdown 报告
step 11   触发 AI 优化子流程标志  ← 插桩5（本轮空实现）
step 12   完成
```

`lifecycle.call_active(conn, plugin_id, ctx)` 在没有 active 版本时抛 `NoActivePlugin`，
主流程捕获后记录淘汰原因 `no_active_plugin`，继续处理下一只标的。

## 财务数据占位说明

插桩 2/3 的 `risk_list` 中有「财务因子未接」标记。这条标记：

1. 会随 `ScoreOutcome.risk_list` 进入 `candidate_items` 表（字段 `risk_list_json`）；
2. 会在 Markdown 报告的「已知限制」段被统计；
3. 在测试 `test_score_plugins_declare_financial_data_is_stubbed` 中被断言。

因此财务占位不会悄悄消失——任何读到该候选记录的人都能看到这条标记。

## 初始版本的存储方式

`stocklab/candidate/builtin/` 把 6 个脚本的源文本以字符串常量导出（`BUILTIN_PLUGINS`）。
首次上线时由运维人员（或验收脚本）调用 `plugin submit` 写入数据库。
字符串格式使测试可以不依赖文件系统直接验证脚本的可执行性与契约合规性。
