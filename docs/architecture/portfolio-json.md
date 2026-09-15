# 组合视图 JSON 接口（P13 页面消费）

> 命令：`stocklab portfolio show --asof DATE --json`
> 契约由 `tests/test_portfolio_view.py::test_json_schema_is_pinned` 钉住 ——
> 改动字段名/层级会让该测试变红。**这是刻意的**：悄悄改名的代价是页面静默渲染出空格子，
> 而不是报错。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 一切正常 |
| `1` | **要人来看**：有标的缺现价（`missing_price_codes` 非空），或有纪律条目 `FAIL` |
| `2` | 用法错误 / 库不存在 / schema 未前滚 |

视图在退出码 1 时**照样完整打印** —— 1 不是"命令失败"，它让调度器能机械地发现异常，
而不必去解析人看的文本。`WARN` / `UNDETERMINED` **不**触发退出码 1（见下）。

## 顶层字段（17 个，字段集合精确相等）

| 字段 | 类型 | 含义 |
|---|---|---|
| `asof` | str | 估值日 YYYY-MM-DD。全部取数按 `date <= asof` |
| `cash` | float | 现金（元，2 位小数） |
| `cash_breakdown` | obj | `{net_deposits, trade_cash, other_cash}` |
| `market_value_priced` | float | 已定价持仓市值合计 |
| `market_value_missing` | float | 缺现价的持仓市值（恒为 0.0，占位用于说明"被排除"这件事） |
| `total_assets` | float | `cash + market_value_priced` |
| `net_invested` | float | 累计净投入 = Σdeposit + Σwithdraw |
| `total_pnl_incl_fee` | float | `total_assets − net_invested` |
| `total_return_incl_fee` | float\|null | `total_pnl / net_invested`；**净投入为 0 时为 `null`**（收益率无定义，不是 0） |
| `positions` | array | 持仓明细，见下 |
| `missing_price_codes` | array[str] | 缺现价的标的（有序，可空） |
| `discipline` | array | 纪律检查条目，见下 |
| `advisory` | array | 动作建议（纪律换算成股数） |
| `warnings` | array[str] | 人读的告警（`[FAIL]` / `[WARN]` / `[未判定]` 前缀） |
| `ledger` | obj | `{trades, cash_flows}`：**计入本视图的**行数（已按 asof 截断） |
| `price_policy` | str | 现价口径说明书 |
| `cost_policy` | str | 成本口径说明书 |

## `positions[]`（19 个字段）

| 字段 | 说明 |
|---|---|
| `code` / `name` / `qty` | 标的、名称、股数 |
| `avg_cost_incl_fee` | 剩余持仓均价（**含费**，4 位小数） |
| `avg_cost_excl_fee` | 剩余持仓均价（不含费） |
| `cost_basis_incl_fee` / `cost_basis_excl_fee` | 剩余持仓总成本，两口径 |
| `price` | 现价；`null` = 缺现价 |
| `price_source` | `"snapshot"` \| `"bars"` \| `null` |
| `price_asof` | **价格实际所属日期**（停牌回落时会早于 `asof`） |
| `price_detail` | snapshot 的 `ts` / bars 的日期，供追溯 |
| `market_value` | `price × qty`；缺现价时 `null`（**不是 0，不是成本价**） |
| `float_pnl_incl_fee` / `float_pnl_excl_fee` | 浮动盈亏两口径；缺现价时 `null` |
| `weight_of_total_assets` | 占总资产百分比（分母是 `total_assets`，不是已定价市值） |
| `realized_pnl_incl_fee` / `realized_pnl_excl_fee` | 已实现盈亏（加权均价结转） |
| `fees_paid` | 该标的累计费用 |
| `status` | `"ok"` \| `"missing_price"` |

## `discipline[]`（5 个字段）

`{check, subject, status, detail, numbers}`

`status ∈ {PASS, WARN, FAIL, UNDETERMINED}`：

| 状态 | 含义 | 是否触发退出码 1 |
|---|---|---|
| `PASS` | 满足 | 否 |
| `WARN` | 偏离目标带 / 触发了禁止动作 | **否** |
| `FAIL` | 破了硬约束 | **是** |
| `UNDETERMINED` | **数据不足，不猜** | **否** |

`UNDETERMINED` **不是** PASS：没有现价就没有权重，把它当 0% 会自动 PASS 掉一条本该报警的规则。
`WARN` 不触发退出码 1 也是刻意的 —— 否则「偏离目标带」与「破硬约束」共用一个信号，报警就失去分辨力。

六条 check（顺序固定）：

| `check` | 判据 | 失败态 |
|---|---|---|
| `single_position_max_40pct` | 单票市值/总资产 ≤ 40% | 超 → FAIL |
| `cash_band_45_60pct` | 现金/总资产 ∈ [45%, 60%] | 带外 → WARN |
| `stop_loss_close_85` | 日**收盘** ≥ 85.00 | 破 → FAIL |
| `stop_loss_weekly_83_25` | 周线（本周 ≤asof 最后收盘）≥ 83.25 | 破 → FAIL |
| `no_add_above_87` | 现价 < 87.00 | ≥ 87 → WARN |
| `cash_per_trade_max_5pct` | 单次动用现金 ≤ 总资产 5% | 给额度，恒 PASS |

后三条的价格线是**按标的**配置的（`PER_CODE_LINES`）。未配线的标的判 `UNDETERMINED`
—— 85.00 是围绕 000333 的 86.80 入场价定的，不是全市场常数。

## `advisory[]`（5 个字段）

`{rule, code, pct, shares, note}` —— `rule ∈ {trim_light_10pct, trim_on_break_20pct}`。
纪律是百分比，人要的是股数。

## 现价口径（`price_policy`）

```
同日 quote_snapshots（ORDER BY ts DESC LIMIT 1）
  → bars_daily 最近 date<=asof 且 adj_mode='none' 的收盘
  → null（status=missing_price，排除出市值合计）
```

- 快照**必须同日**，不是「找最近一条」——拿 09-15 的快照给 09-14 估值就是用未来信息。
- 只认 `adj_mode='none'`：复权价是「为算收益调整过的序列」，不是「今天这只票值多少钱」。
- **不用成本价冒充、不插值。**

## 最小消费示例（P13）

```python
view = json.loads(subprocess.check_output([...  "portfolio", "show", "--asof", d, "--json"]))
for p in view["positions"]:
    if p["status"] == "missing_price":
        render_placeholder(p["code"], "无现价")     # 不要 fallback 到成本价
    else:
        render(p["code"], p["price"], p["market_value"], p["weight_of_total_assets"])
for c in view["discipline"]:
    render_badge(c["status"], c["detail"])
```
