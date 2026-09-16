# 项目文档

本项目采用「先验证，后交付」的开发工作体系。每次任务都遵循：**需求澄清 → 任务拆分 → 计划 → 实现 → 验证 → 交付**。

## 目录结构

```
docs/
├── README.md              # 本文档索引
├── architecture/          # 架构文档（代码设计思路）
├── tasks/                 # 任务拆分与跟踪
├── plans/                 # 设计与实现计划（superpowers 产物）
├── decisions/             # 设计决策记录（ADR）
└── errors/                # 错误日记
    └── ERROR_DIARY.md     # 错误与教训记录
```

## 关键文档索引

### 调度与运维

- [`scheduling.md`](scheduling.md) — **调度链（P11）**：各时点跑什么（09:35/11:35/13:35/15:05
  `session tick`、15:30 `review daily`）、退出码含义、失败怎么办、如何补跑
  （含 `backfill-close` 用法与「历史 NULL 不碰」的语义）

### 实盘账本与组合视图（P12）

- [`architecture/portfolio-json.md`](architecture/portfolio-json.md) — **P13 页面的接口契约**：
  `portfolio show --json` 的全部字段（顶层 17 / positions 19 / discipline 5 / advisory 5）、
  退出码语义（0/1/2）、`WARN` 与 `UNDETERMINED` 为什么不触发报警
- [`decisions/2026-09-15-ADR-006-实盘账本口径.md`](decisions/2026-09-15-ADR-006-实盘账本口径.md) —
  **口径决策**：成本含费为默认（同时输出不含费）、现金是推导量、总资产只算有现价的持仓、
  无保证金、改错只能冲正、**为什么不用唯一约束防重复录入**
- [`decisions/2026-09-16-ADR-010-模拟盘口径.md`](decisions/2026-09-16-ADR-010-模拟盘口径.md) —
  **模拟盘口径**：禁方向择时（模型方向能力 ≈0：行级 38.14% / 按日聚类 0.3819±0.0070 /
  Brier 0.6581）、三臂并列不挑推荐、成本口径（ADR-008）、PIT 禁未来函数、
  append-only 不加滚动清理、起点 2026-09-15 + LIVE=0 + <120 交易日不算结论，
  以及**已知限制**：整手约束下 000333 一手即 ≈43.5%，`[FAIL]` 是数据不是建议
- [`plans/2026-09-15-p19-模拟盘骨架.md`](plans/2026-09-15-p19-模拟盘骨架.md) —
  P19 计划 + **§9 任务记录**（`paper show` 默认 asof 回落缺陷的改前/改后真实输出）
- [`plans/2026-09-15-p12-实盘账本与组合视图.md`](plans/2026-09-15-p12-实盘账本与组合视图.md) —
  P12 设计稿
- [`tasks/2026-09-15-p12-task53-60.md`](tasks/2026-09-15-p12-task53-60.md) — P12 任务拆分与验收清单
- [`tasks/2026-09-15-p15-task66-70.md`](tasks/2026-09-15-p15-task66-70.md) — P15 持仓管理 Web 应用（路由/写入路径/测试/启动与 nginx 反代）\n
- [`plans/2026-09-15-p16-UI设计.md`](plans/2026-09-15-p16-UI设计.md) — P16 前端 UI 设计计划（对账单色板 / 字体角色 / 布局线框 / AI 默认脸逐条自检）
- [`tasks/2026-09-15-p16-ui.md`](tasks/2026-09-15-p16-ui.md) — P16 任务记录（静态资产本地化 / 局部更新 / 已知缺陷）
```bash
# 录入（append-only；改错只能用 reverse 冲正）
stocklab cash add --date 2026-09-14 --kind deposit --amount 20000 --idempotency-key principal-20260914
stocklab trade add --date 2026-09-14 --code 000333 --side buy --price 86.80 --qty 100 --fee 5.09
stocklab trade reverse 1 --reason "录错券商"          # 冲正，不改原行
stocklab portfolio show --asof 2026-09-15            # 表；退出码 1 = 要人来看
stocklab portfolio show --asof 2026-09-15 --json     # P13 消费的稳定接口
```

### 计划与任务（当前轮）

- [`plans/2026-09-15-p11-调度链.md`](plans/2026-09-15-p11-调度链.md) — P11 设计稿：
  快照幂等键、收盘回填、LIVE/REPLAY 口径分列
- [`tasks/2026-09-15-p11-调度链-task45-52.md`](tasks/2026-09-15-p11-调度链-task45-52.md) —
  P11 任务拆分与验收清单

## 工作流程

1. **需求分析** — 理解需求，明确目标与验收标准
2. **任务拆分** — 将需求拆解为可执行的任务列表
3. **文档先行** — 记录设计思路和决策依据
4. **逐步实现** — 每个任务逐一完成
5. **验证通过** — 先验证，确认无误
6. **交付反馈** — 交付给用户确认

> ❗ 核心原则：**未验证不交付，未记录不通过。**
