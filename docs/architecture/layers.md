# 分层与依赖方向（P1–P4 交付时点）

> 本文回答「**谁可以依赖谁**」。新增模块时先对照本图，
> 反向依赖（下层 import 上层）一律视为设计错误。

## 分层图

```
                              ┌──────────────────────────────┐
   L5  入口 / 编排            │  cli/main.py（argparse 子命令）│
                              └──────────────┬───────────────┘
                                             │ 只向下调用，不做业务计算
                          ┌──────────────────┴──────────────────┐
                          │                                     │
              ┌───────────▼───────────┐            ┌────────────▼─────────────┐
              │ L4 评估/决策           │            │ L4 特征                  │
              │ backtest/             │            │ features/               │
              │  engine / portfolio   │            │  snapshot / indicators  │
              │  metrics / benchmark  │            │  regime / registry      │
              │  strategies           │            └────────────┬─────────────┘
              │  walkforward（纯函数） │                         │
              └───────────┬───────────┘                         │
                          │                                     │
┌─────────────────────────▼─────────────────────────────────────▼───────────────┐
│ L3 数据访问 / 口径      data/adjust.py（复权读取层，唯一价格出口）             │
│                        data/ingest.py（唯一写入口）· calendar/                │
└─────────────────────────────────┬─────────────────────────────────────────────┘
                                  │
┌─────────────────────────────────▼─────────────────────────────────────────────┐
│ L2 存储                 store/repo.py · store/migrate.py · store/db.py        │
│                        store/schema.sql（DDL + append-only 触发器）           │
└─────────────────────────────────┬─────────────────────────────────────────────┘
                                  │
┌─────────────────────────────────▼─────────────────────────────────────────────┐
│ L1 基础设施             config/(paths, settings, costs, universe) · data/models│
│                        data/http.py · data/raw_cache.py · data/errors.py       │
│                        quality/checks.py（**纯校验内核**：只 import L1 数据类，│
│                                          无 I/O；L2 与 L3 都调它）             │
└───────────────────────────────────────────────────────────────────────────────┘
```

> `quality/checks.py` 放在 L1 而不是 L4，是**按实测依赖定的**，不是按目录名：
> `store/repo.py` 与 `data/ingest.py` 都 import 它（写库前的最后一道校验）。
> 若把它算作上层，就等于宣称「L2/L3 依赖 L4」，那是假的；
> 它实质是一个「只报告不修复」的纯函数库，与 `config/` 同级。
> 核对命令：`grep -n "^from stocklab" stocklab/quality/checks.py` → 只有 `data.models`。

## 依赖方向（硬约束）

| 层 | 可以依赖 | **禁止**依赖 |
|---|---|---|
| L1 基础设施（含 `quality/checks.py`） | 仅标准库 / 第三方 | 任何上层 |
| L2 存储 | L1 | L3–L5 |
| L3 数据访问 | L1、L2 | L4–L5 |
| L4 评估 · 特征 | L1–L3 | L5、**L4 内部不互相收编**（backtest 不 import features 的快照构建器，反之亦然；只通过数据库与 dataclass 交互） |
| L5 CLI | L1–L4 | — |

**验证方式**（两条都可直接复制执行）：

```bash
# ① backtest 不得依赖 cli / features（P4 实测：无输出 = 通过）
grep -rn "from stocklab" stocklab/backtest/ | grep -E "cli|features"
# ② 特征层的跨层依赖必须落在 L1–L3 内（P4 实测：只有 data.models / store / 自身）
grep -rn "from stocklab" stocklab/features/
```

`stocklab.config` / `stocklab.data.models` 出现在 L4 是**允许**的（属 L1：纯 dataclass 与常量，不含 I/O）。

## L4 内部结构（backtest 包）

```
strategies.py ──(Signal)──▶ engine.py ──▶ portfolio.py（成交/T+1/涨跌停/成本）
                              │                 ▲
                              │                 │ CostModel（L1 config/costs.py）
                              ▼
                          metrics.py ◀── benchmark.py（基准 NAV 与超额）
                              ▲
                              │ 只有报告，不参与交易
                        walkforward.py（折分 + 样本量，纯函数，零 I/O）
```

- `engine.py` 是**唯一**把 bar 序列变成成交与净值的地方；策略只产出 `Signal`，不得直接改仓位。
- `walkforward.py` 刻意做成**纯函数**（输入会话轴 → 输出折分），不碰数据库：
  这样「同一输入 → 同一折分」是结构保证，而不是靠约定（见 `tests/test_backtest_walkforward.py`
  的 `test_build_report_is_reproducible`）。

## 三条「唯一出口」原则（跨层）

1. **价格唯一出口**：`data/adjust.py:load_bars_adjusted`。任何回测都不得直接读 `bars_daily`。
   理由：不复权价在除权日有假跌幅，直接读会让因子链失效且**不报错**。
2. **写库唯一入口**：`data/ingest.py`（抓取 → 落库）。回测/特征层**只读**。
3. **样本外唯一出口**：`backtest/walkforward.py`。任何「策略表现」必须在它的折分上产出；
   它同时负责给出样本量（交易日口径），不足 120 交易日时不得用于选策略。

## 已知取舍 / 技术债

- **L4 之间靠数据库耦合**（features 写 `features_daily`，backtest 将来读快照）：
  好处是复现性强（每行带 `feature_version` + hash），代价是没有进程内的类型约束。
- `walkforward.py` 目前只切**会话轴**，未与特征/策略执行器串联 —— P5 注册策略后接通。
- `quality/checks.py` 尚未进入 CLI 的日常链路（`doctor` 只做计数）。

## 变更记录

- 2026-09-15: P4 收尾（Task 24/25）建立本文；新增 `backtest/walkforward.py`（L4，纯函数）。
