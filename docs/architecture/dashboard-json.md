# 看板摘要 JSON（`dashboard build` / `/api/summary` 的契约）

> 契约由 `tests/test_dashboard_summary.py::test_top_level_fields_are_pinned` 钉住
> （顶层字段集合**精确相等**），字段增删必须同时改三处：`summary.TOP_LEVEL_FIELDS`
> / `summary.SCHEMA_VERSION` / 本文件。
>
> 📌 本文件在 P50（2026-09-23）才落盘：`summary.py` 的 docstring 从 P14 起就指向
> 「`docs/architecture/dashboard-json.md`」，但那个文件一直不存在 —— 属于「文件在说谎」
> （审计项 A6）。本轮补上，并把它与代码的对应关系写清楚。

## 顶层字段（9 个）

| 字段 | 来源 | 说明 |
|---|---|---|
| `schema_version` | 常量 | 当前 `2`（P50 新增 `m2` 时 1→2） |
| `service` | 常量 | `stocklab-dashboard` |
| `asof` | 入参 | 取数一律 `date <= asof` |
| `portfolio` | `portfolio.view.build_portfolio` | 组合视图（ADR-006 口径） |
| `freshness` | `session.review.freshness` + 全库最新快照 | 数据新鲜度 |
| `accuracy` | `session.review.rolling_accuracy` | 滚动准确率（LIVE / REPLAY **分列**） |
| `risk` | 调用方注入 | `null` = 风险面板**没接入**，不是「风险为零」 |
| `m2` | `labweb.m2_charts.chart_panel`（P50） | 模块2 的三条曲线：净值 / 回撤 / 命中率 |
| `alarms` | `summary.build_alarms` | 需要人来看的事（**不返回「一切正常」**） |

## `m2` 这一段的口径（P50 §3）

- **与 `/lab/m2` 页面同源**：同一份 `m2_charts.chart_panel(conn, asof)` 载荷、
  同一个渲染函数 `m2_render.charts_section` —— 两处渲染出的 HTML **逐字节相同**
  （`tests/test_p50_config_charts.py::test_t5_*` 钉住）。
- **图只有一处实现**：绘图是 `paper_render.race_svg`，本模块只组装 `races`；
  缺数据时 `svg` 为 `None` + `reason`，**不画空曲线、不写 0**。
- **老库容忍**：库里没有模块2 的表（未前滚）时，这一段是
  `{"available": False, "reason": "…"}` + 空 `blocks`，报告**不报 500、不编数**。

## 确定性

同一份库 + 同一个 `asof` ⇒ 同一串 JSON（`sort_keys=True`）。页面上的「生成时刻」
由 `html.render_html(built_at=…)` 单独加，**不进 payload** —— 否则
`/api/summary` 每次请求都在变，既钉不住测试也 diff 不了。
