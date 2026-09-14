# 🚫 错误日记

> 每次犯错后记录，防止再次出错。
> **规则**: 每次开始新任务前，先查阅本日记。

## 记录时机

- 花费超过 15 分钟定位的 Bug
- 重复出现两次以上的问题
- 改了一处却导致另一处出错（耦合教训）
- 文档和代码不一致导致的问题

## 格式

```markdown
## YYYY-MM-DD: [错误简述]

### 错误描述
发生了什么？

### 根本原因
为什么会发生？

### 教训
以后如何避免？

### 检查清单（下次必查）
- [ ] ...
```

---

## 错误索引

| 日期 | 错误 | 教训 | 已修复 |
|------|------|------|--------|
| 2026-09-14 | 非交互 `acceptEdits` 不批准 Bash，会话空转 | 无人值守必须 `bypassPermissions` | ✅ |
| 2026-09-14 | Bash 工具 PATH 被插件覆盖，`git` 找不到 | 项目 `.claude/settings.json` 固定 PATH | ✅ |
| 2026-09-14 | 实现计划里的断言量级写错，测试必失败 | 计划不是圣经，测试必须真跑 | ✅ |
| 2026-09-14 | 触发器 ABORT 后隐式事务残留，下次 BEGIN 报错 | `isolation_level=None` + 显式事务 | ✅ |
| 2026-09-14 | 计划的 DDL 与它自己的测试互斥 | DDL 与测试必须同源校验 | ✅ |

---

## 2026-09-14: 初始化注意事项（防坑指南）

### 常见陷阱
1. **忘记查错误日记就开工** → 每次任务拆分前必须先查阅本文件
2. **实现完不验证就交付** → 必须先生成验证方案 → 执行验证 → 通过后才交付
3. **修改代码不同步文档** → 修改和文档更新要在同一任务中完成
4. **任务拆分太粗** → 每个任务应该是可独立验证的最小单元
5. **计划只存在脑子里** → 必须先落盘到 `docs/plans/` 再动代码

### 检查清单（每次任务前）
- [ ] 已查阅 ERROR_DIARY.md
- [ ] 需求已澄清并拆分为具体任务
- [ ] 计划已写入 `docs/plans/`
- [ ] 每个任务有明确的验证方式
- [ ] 相关文档已确认需更新

---

## 2026-09-14: 非交互调用时 `--permission-mode acceptEdits` 不批准 Bash

### 错误描述
上一次 P1 会话以 `--permission-mode acceptEdits` 启动，Claude Code 可以编辑文件，
但**每一次 Bash 调用都需要人工批准**。无人值守场景下没人按「允许」，
于是整个会话无法执行任何命令（建 venv、跑 pytest、git 提交全部做不了），最终空转失败。

### 根本原因
`acceptEdits` 只自动批准**文件编辑**类工具，不覆盖 Bash。
把它当成「无人值守模式」是错的。

### 教训
非交互（nanobot 驱动）时，Bash 必须可用，否则等于没开工。

### 检查清单（下次必查）
- [ ] 非交互会话使用 `IS_SANDBOX=1` + `--permission-mode bypassPermissions`
- [ ] 开工第一件事：跑一条无害命令（`git status`）确认 Bash 真能用
- [ ] 沙箱环境**不要**用 `--dangerously-skip-permissions`

---

## 2026-09-14: Claude Code Bash 工具的 PATH 被插件覆盖

### 错误描述
Bash 已获批准，但 `git`、`python3` 等命令找不到（command not found），
原因是插件（superpowers 等）注入的 PATH 覆盖了系统 PATH。

### 根本原因
Claude Code 的 Bash 工具使用 settings 里 `env.PATH`；
若该值由插件缓存路径拼成而不含 `/usr/bin`，基础命令全部失效。

### 教训
PATH 必须显式写死并保留系统基础目录，不依赖继承。

### 检查清单（下次必查）
- [ ] 项目 `.claude/settings.json` 的 `env.PATH` 含 `/usr/bin`、`/bin`、`/usr/local/bin`
- [ ] 新项目初始化后立即验证 `git --version` / `python3 -V`

---

## 2026-09-14: 实现计划里的断言量级写错，照着抄测试必失败

### 错误描述
`docs/plans/2026-09-14-phase1-4-implementation.md` Task 3 的
`test_small_trade_costs_more_than_proportional` 断言
`small > large * 100`。按同一份计划里的成本模型实算：
小额费率 = 5.01/1000 = 0.501%，大额费率 = 260/1e6 = 0.026%，
比值 **19.27x**，永远不可能 >100x —— 该测试**必然失败**。

### 根本原因
计划是手写的，没有跑过；量级（100x）是凭感觉写的，未代入实测算过。
如果实现时「测试写完后没看它失败就改成能过的样子」，就会把真实费率搞错。

### 教训
**计划文件不是可执行真源**。测试写完必须真跑，看到它因「功能缺失」失败；
如果失败原因是「断言本身不成立」，改的是断言，但必须在记录里写明为什么改、实测值是多少。

### 检查清单（下次必查）
- [ ] 从计划里抄测试时，先心算/实算一遍断言是否成立
- [ ] 每个「改计划里写法」的地方，都在 docs/tasks 记录里列明原因与实测值
- [ ] 禁止为了让测试通过而放宽断言而不留痕

---

## 2026-09-14: 触发器 ABORT 后隐式事务残留，下一次 BEGIN 报错

### 错误描述
append-only 表被 UPDATE 时应抛 `IntegrityError` —— 这没问题。
但抛错之后，再执行 `with transaction(conn): INSERT ...` 会报
`sqlite3.OperationalError: cannot start a transaction within a transaction`。
计划里的测试 `test_insert_still_works_after_failed_update` 正好抓到了这个真实缺陷。

### 根本原因
Python `sqlite3` 默认是 legacy 事务控制（`isolation_level=""`）：
DML 语句会**隐式**开启事务。被 `RAISE(ABORT)` 中止的语句只回滚该语句本身，
**显式事务之外的这个隐式事务仍然开着**，于是下一条 `BEGIN` 失败。
（注意 ABORT ≠ ROLLBACK：ABORT 只撤销当前语句。）

### 教训
不要依赖驱动层的隐式事务；显式打开、显式关闭，语义才可预测。

### 检查清单（下次必查）
- [ ] `sqlite3.connect(..., isolation_level=None)`，让连接处于 autocommit
- [ ] 所有写入包在显式 `transaction()` 里，并在其中检测嵌套（`conn.in_transaction`）
- [ ] 任何「约束/触发器抛错」的测试，后面要再跟一条写入，验证连接仍可用

---

## 2026-09-14: 计划里的 DDL 与它自己的测试互斥

### 错误描述
Task 4 的 `schema.sql` 把 `created_at` / `params_hash` / `data_version` /
`source` / `fetched_at` 定义为 `NOT NULL`（无默认值），
但同一 Task 的测试却按 5 列、8 列插入，必然触发 `NOT NULL constraint failed`。
DDL 与测试互相矛盾，谁先跑谁错。

### 根本原因
DDL 和测试是两处手写产物，没有同源校验；写计划时只想着「字段要全」，
写测试时只想着「断言要短」。

### 教训
遇到这种冲突，**不要把 NOT NULL 放宽成可空来迁就测试** ——
那会削弱可复现性（append-only 表的 `created_at`/`params_hash` 丢了就再也补不回来）。
正确做法是补齐测试数据，并记录这是「测试服从 DDL」。

### 检查清单（下次必查）
- [ ] 新增 NOT NULL 列时，同步更新所有 INSERT 测试
- [ ] 冲突时先问「哪个意图更强」，而不是「哪个改起来方便」
- [ ] 记录偏离：在 docs/tasks 里写明改了哪几条测试、为什么
