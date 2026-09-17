# P36-retry：labweb 展示补齐（模拟盘多臂 + 「能不能动」+ 风险摘要）实现计划

> 上游取证（nanobot 2026-09-17）已直接给出，本轮**不重查链路**。
> 只动 `stocklab/labweb/` 展示层与 `tests/`，不碰任何口径与数字。

## 0. 范围与非目标

**做**
1. 总览新增「模拟盘」段：读 `paper_nav_daily` 最新一交易日（`date <= asof`）全部臂。
2. 总览渲染 `summary["actions"]`（每只持仓「能不能动」结论 + 全清试算）。
3. 「怎么做」段：折算股数不可执行的减仓说法标注「该笔执行不了」。
4. `Lab.overview()` 复用 `Lab.risk()` 同源逻辑算 `risk_block`，总览风险摘要不再「没算」。
5. 新增 labweb 用例覆盖以上四点。

**不做（硬约束）**
- 不改 `stocklab/portfolio/decision.py`、`stocklab/paper/`、`stocklab/risk/`、`stocklab/backtest/`；
- 不改 `stocklab/labweb/app.py` 路由；不改预测/验证/复盘链路；
- `docs/experiments/` 只读；`docs/decisions/` 不新增 ADR。

## 1. 关键口径决定（都不产生新数）

| 决定 | 依据 |
|---|---|
| 模拟盘取数 `WHERE date = (SELECT MAX(date) ... WHERE date <= asof)` | 与 `build_summary` 的「取数一律 `date <= asof`」同一条 PIT 纪律；写死 `MAX(date)` 全表会让历史 `--asof` 截图展示未来数据 |
| 多臂**只并列**：无排名/推荐字样 | `chain/accuracy.py::PAPER_NO_PICK` 同一纪律（页面不另立一套文案，但仍不复制其长文，只保留「只并列、不挑推荐」的约束句） |
| 不可执行判据 `shares % LOT_SIZE != 0`（含 `<=0`），`LOT_SIZE` 从 `portfolio.decision` **导入** | 不复制 100 到 render；「卖出须 100 股整数倍」全项目只有一个常数来源 |
| 整手原因**原样**展示 `position_decision` 的 `because` 文本 | `whole_lot_reason()` 的句子已在 `because` 里，render 不重写、不复制阈值 |
| `risk_block` 来源与 `Lab.risk()` 逐字相同（`risk_subject(view)` → `build_risk_block`） | 同一函数、同一受试标的；不许第二套风险数 |

## 2. 任务分解（每节：红测试 → 实现 → 验证 → commit）

### T1 总览风险摘要不再「没算」
- 测试：`Lab.overview()["risk"] is not None`，且与 `Lab.risk()["risk"]` 相等（同源）；
  渲染出的总览 HTML 不含「没算」。
- 实现：`overview()` 先 `build_portfolio` → `risk_subject` → `build_risk_block` → `build_summary(risk_block=...)`。
- 验证：`.venv/bin/python -m pytest tests/test_labweb_overview.py -q`

### T2 模拟盘段
- 测试：有数据 → 页面含每个臂的 `account_id` 与 nav 数字，且**无**「建议/推荐/应该」；
  无数据 → 「暂无模拟盘记录」。
- 实现：`Lab._paper(conn)` 取数 + `render._paper_block()` 渲染；接进 `overview_page`。
- 验证：同上。

### T3 「能不能动」段
- 测试：页面含 `HEADLINE[stop]`（「全清或不动」）与 `decision.whole_lot_reason(100)` 的首句
  （从函数取，不抄字符串）；state=unknown 时页面含「判不了」的理由。
- 实现：`render.actions_block()`，逐行渲染 headline / because / options / 全清试算。
- 验证：同上。

### T4 「怎么做」段去害
- 测试：100 股持仓下，页面出现「10 股」时必须同段出现「执行不了」。
- 实现：`render.advisory_list()` 用导入的 `LOT_SIZE` 判 `shares % LOT_SIZE != 0` 并标注。
- 验证：同上。

### T5 全局
- `find . -name __pycache__ -type d -prune -exec rm -rf {} +` → `.venv/bin/python -m pytest` 全绿（计数 ≥ 1635）；
- `bash scripts/verify.sh` → `✅ 验证通过: all`；
- `make_server(port=0)` 实测总览页，贴三段原始 HTML 证据。

## 3. 风险点（来自错误日记）

- **#41 迁移/写库**：本轮不新增写库入口、不碰 schema → 不适用；但 `_paper` 只读，确认无写。
- **#40 类（测试断言无区分力）**：每条新断言都要在「摘掉实现」后变红 —— T1/T2/T3/T4 的断言
  分别对着 `risk_block` 注入、`_paper` 接线段、`actions_block` 渲染、标注分支。
- **#42 判据歧义**：本轮无判据表。
- 二次转义：`glance()` 才走 `rich()`；新写的 `because` 直接 `rich()` 一次，不重复转义。
