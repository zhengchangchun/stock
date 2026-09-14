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
| 2026-09-14 | `.gitignore` 裸 `data/` 吞掉源码包，P1 有两个文件从未提交 | 忽略规则必须根锚定 `/data/` | ✅ |
| 2026-09-14 | 离线守卫把 `socket.socket` 换成函数，`ssl` 导入即崩 | 替换类要用子类，函数不能当基类 | ✅ |
| 2026-09-14 | 离线守卫只拦连接不拦 DNS，测试变成环境巧合 | 守卫要覆盖全部出网路径，并在 `unshare -n` 下复验 | ✅ |
| 2026-09-14 | 测试替身 `sleep` 让时钟静止，限流与退避共用通道互相污染 | 单测只测一个机制，其余显式关掉 | ✅ |
| 2026-09-14 | 解析器「取不到就回退」把复权价标成不复权 | 口径不匹配就该取不到数据，禁止静默降级 | ✅ |
| 2026-09-14 | 计划里的 fixture 形状与解析器守卫互斥（第二次踩） | 真实响应先探针、再写测试 | ✅ |

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

---

## 2026-09-14: `.gitignore` 的裸 `data/` 吞掉 Python 包 `stocklab/data/`

### 错误描述
P2 要提交 `stocklab/data/http.py`，`git add` 报
`The following paths are ignored by one of your .gitignore files: stocklab/data`。
进一步检查发现：P1 的 `stocklab/data/errors.py` 与 `stocklab/data/__init__.py`
**从来没有进过版本库**（`git ls-files stocklab` 里没有它们）。
后果：仓库处于「测试能跑、但新克隆一定挂」的状态 ——
`stocklab/config/universe.py` 第 5 行 `from stocklab.data.errors import HostNotAllowed`
在新克隆里必然 `ModuleNotFoundError`。P1 的 73 个测试全绿也**完全掩盖**了这个问题，
因为它们跑的是本地未提交的工作区。

### 根本原因
`.gitignore` 写的是 `data/`（本意是项目数据目录），而 gitignore 的 `data/` 会匹配
**任意层级**名为 `data` 的目录 —— 包括 Python 包 `stocklab/data/`。
另外「本地全绿」与「仓库自洽」是两件事，只跑测试不检查 `git ls-files` 就发现不了。

### 教训
1. 忽略规则要么根锚定（`/data/`），要么写清层级。
2. **「测试全绿」不等于「仓库完整」**：测试跑的是工作区，仓库缺文件照样全绿。
3. 提交前用 `git status --ignored` / `git ls-files <包名>` 核对一次源码是否真的入库。

### 检查清单（下次必查）
- [ ] `.gitignore` 里凡是要忽略的目录，都加根锚定前缀 `/`
- [ ] 新增 Python 包后，`git ls-files <pkg>/` 必须列出该包所有 `.py`
- [ ] 阶段性验收时额外跑一次 `git ls-files | grep <新目录>` 抽查

---

## 2026-09-14: 离线守卫把 `socket.socket` 换成函数 → `import ssl` 直接崩

### 错误描述
为实现「测试禁止联网」，conftest 里写了
`monkeypatch.setattr(socket, "socket", _boom)`（`_boom` 是函数）。
结果 `tests/test_data_raw_cache.py` 大面积报
`TypeError: function() argument 'code' must be code, not str`，
栈顶落在 `ssl.py:955` 的 `class SSLSocket(socket):`。

### 根本原因
`ssl` 模块在**导入时**执行 `class SSLSocket(socket)`，即把 `socket.socket` 当**基类**用。
基类必须是类，函数不行。P1 的日历测试没踩到，只是因为那些测试运行时 `ssl`
早就被 `requests` 间接导入过了 —— **同一个守卫，踩不踩得到取决于导入顺序**。

### 教训
替换一个「会被继承的类」时，必须替换成**子类**而不是函数；
子类既能被继承，又能通过覆写 `connect` / `connect_ex` 拦住真实连接。
另外：模块级副作用型缺陷，只在「首次导入」时暴露 —— 测试必须能复现首次导入，
否则修了也证明不了（本次用子进程重跑一次新解释器来钉死）。

### 补充（同日复踩一半）：守卫只拦 socket，不拦 DNS

修好子类问题后，在**无网络命名空间**（`unshare -n`）里跑全量测试，
`test_requests_cannot_reach_network` 反而失败了：只拦 `socket.socket` /
`create_connection` 时，DNS 不可用的环境里 urllib3 会先在 `getaddrinfo`
抛 `gaierror`，**永远走不到我们的守卫**。
于是「守卫有效」的测试实际在断言「本机 DNS 恰好可用」——
换个环境就变味，这正是守卫要防的那类问题。
→ 守卫必须**同时**拦住 `socket.getaddrinfo`；测试也跟着补一条解析阶段的断言。

### 检查清单（下次必查）
- [ ] monkeypatch 替换 `socket.socket` / `ssl.SSLSocket` 这类可能被继承的对象时，用子类
- [ ] 「导入期行为」的修复，必须补一个**子进程/新解释器**的回归测试
- [ ] 全局 autouse 守卫上线后，跑一次全量测试确认没有模块被它弄崩
- [ ] 网络守卫要覆盖**全部**出网路径：DNS 解析 + 连接（+ 代理），缺一层就只剩环境巧合
- [ ] 「能拦住」的测试，要在**真的没有网**的环境里再跑一遍（`unshare -n`）才算证明

---

## 2026-09-14: 测试替身 `sleep` 让时钟静止，限流与退避互相污染

### 错误描述
Task 8 的退避重试测试断言 `slept == [1.0, 2.0, 4.0]`，实际得到
`[1.0, 0.35, 2.0, 0.35, ...]` —— 多出来的 0.35 是**限流间隔**的 sleep。

### 根本原因
`HttpClient` 用同一个 `sleep` 通道做两件事：限流等待（`min_interval`）与退避重试。
测试把 `sleep` 换成假函数后，**真实时间不再流逝**（假 sleep 立即返回），
于是第二次请求时 `now - last ≈ 0 < min_interval` → 每次重试都额外触发一次限流等待。
即：替身让时钟静止 → 限流逻辑在测试里比生产环境更频繁地生效。

### 教训
一个测试只验证一个机制。测退避就把限流显式关掉（`min_interval=0.0`），
测限流就固定 `clock` 并让重试不发生。**不要靠"环境恰好不会触发另一条分支"**。

### 检查清单（下次必查）
- [ ] 同一函数里有多条「等待/重试」逻辑时，单测逐条隔离验证
- [ ] 替换 `sleep`/`clock` 的测试，先想清楚「时间静止后哪些分支会变成必然触发」
- [ ] 断言「睡了多久」时，用 `min_interval=0` 或显式 clock 隔离无关等待

---

## 2026-09-14: 解析器「取不到就回退」，把复权价标成不复权

### 错误描述
腾讯日K 适配器最初写成：
`keys = ("qfqday", "day") if adj_mode == "qfq" else ("day", "qfqday")` ——
即「要 day 拿不到就退回 qfqday」。于是一个只含 `qfqday` 的响应，
会在 `adj_mode="none"` 下被解析出来，**价格是前复权的，标签却是不复权**。
更糟的是我自己写的测试把这个行为当成期望值断言了下来。

### 根本原因
「宽松回退」看着像健壮性，实则是在**复权口径**这种不可混淆的语义上做静默降级。
本项目铁律①要求「抓取层绝不下发复权后的价格」，而回退恰好制造了这种数据。
下游拿到的是「贴了错误标签的价格」，比拿不到数据危险得多 ——
因为它会一路算进因子链和回测，且没有任何报错。

### 教训
口径/单位/币种这类**语义契约**不允许 fallback：不匹配就返回空，
让调用方看到「0 根」而不是「一堆贴错标签的价格」。
**写测试时如果发现「当前实现这么写也能过」，要反问一句：这是我要的行为吗？**

### 检查清单（下次必查）
- [ ] 解析/取值路径里不许有跨口径、跨单位、跨币种的 fallback
- [ ] 语义不匹配时返回空/抛错，并让调用方留痕
- [ ] 复权、单位、时区相关的分支，必须有「反向测试」：不匹配时必须取不到数据
