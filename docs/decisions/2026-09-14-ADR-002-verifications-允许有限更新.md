# ADR-002. verifications 允许有限更新（身份列不可变 + 禁删）

- 日期：2026-09-14
- 状态：已接受
- 实现：`stocklab/store/schema.sql` 的 `trg_verifications_no_update_identity` / `trg_verifications_no_delete`
- 测试：`tests/test_store_append_only.py`

> 命名说明：实现计划里把本 ADR 写作「ADR-001-verifications-允许更新」，
> 但 `ADR-001` 已被 `2026-09-14-ADR-001-评审决策.md` 占用，故顺延为 ADR-002。

## 背景

系统设计总纲第 6 节把 `verifications` 列入 append-only 集合（禁止 UPDATE/DELETE）。
但 P0 评审的 A5 决策要求「两级归因」：`attribution_manual`（`SIGNAL`/`STRATEGY`/`MODEL`/`NOISE`）
是**人工标注**，必须在验证结果落库之后由人回填。二者直接冲突。

## 备选方案

1. **全表 append-only** —— 人工归因只能靠再插一行新 `verifications`。
   缺点：同一 `pred_id` 出现多行，统计时要去重，且「当前归因是什么」不明确。
2. **完全放开 UPDATE** —— 简单，但等于放弃 R8，任何人可改写历史验证结果。
3. **只放开指定列（本决策）** —— 身份列（`verification_id` / `pred_id` / `target_date` / `created_at`）
   设置为不可变，其余结果列与 `attribution_manual` 可更新；`DELETE` 全禁。

## 决策

采用方案 3。

## 后果

- 好处：人工归因可回填；`pred_id`/`target_date`/`created_at` 一旦落库无法被改写，
  即无法通过「把验证挪到别的预测上」或「改目标日」来美化结果；无法删行。
- 代价：`verifications` 失去严格意义上的 append-only 保证（评审已确认为刻意例外）。
- 补偿手段：
  1. 身份列由触发器强制不可变（有测试 `test_verifications_identity_columns_immutable`）；
  2. `created_at` 落库后不变，报告中同时展示标注时间；
  3. `attribution_auto`（程序判定）与 `attribution_manual`（人工标签）分列，
     人工标注不得覆盖程序判定。
- 注意：SQLite 触发器无法按「列白名单」放开，只能按「列黑名单」拒绝，
  故新增需保持不可变的列时必须同步更新 `trg_verifications_no_update_identity`。
