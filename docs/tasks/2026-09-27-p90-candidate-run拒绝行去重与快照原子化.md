# P90｜`candidate run` 拒绝行去重 ＋ 快照写入原子化

创建：2026-09-27 ｜ 派单人：nanobot ｜ 开工基线 HEAD：`a08991f`（P89 §8 复核后）
上游：`docs/tasks/2026-09-27-p89-每周与季度调度.md` §8.5（复核发现的阻塞级缺陷）；
`stocklab/candidate/snapshot.py` 的幂等键设计（`(asof, run_kind)`）；
`stocklab/store/db.py::transaction()`。

---

## 0. 背景：为什么这条必须在 2026-10-05 之前修掉

P89 给 `WEEKLY_STEPS` 的第 1 步就是 `candidate run --run-kind weekly --asof {asof}`（阻断步）。
2026-10-05（周一）16:30 是它**第一次**真跑。而它在**全新 `(asof, run_kind)`** 上**首次执行就失败**：

```
$ cp -c data/stocklab.db /tmp/p89rev/copy.db
$ .venv/bin/python -m stocklab.cli.main candidate run --run-kind weekly --asof 2026-09-24 --db /tmp/p89rev/copy.db
❌ IntegrityError: UNIQUE constraint failed: candidate_rejects.snapshot_id, candidate_rejects.code, candidate_rejects.stage
rc=1（耗时 1 s —— 是首次跑，不是重跑）
```

### 0.2 两个根因（2026-09-27 12:0x 复核，只读定位）

**根因 A：同一个 `(code, stage)` 被流水线产出两行。**
`stocklab/candidate/run.py` 的打分循环对每个 `pool ∈ pools.eligible_pools(inst)`
（`ALL_POOLS = (short, mid, long)`）各判一次，拒绝时 `stage` **恒为 `'score'`、不带池名**：

```python
for pool in pools.eligible_pools(inst):
    ...
    if not outcome.pass_flag:
        rejects.append(snapshot.RejectRow(code=inst.code, stage="score", ...))
```

`stocklab/store/schema.sql:1004-1012` 的主键是 `PRIMARY KEY (snapshot_id, code, stage)`，
而 `stage` 的 CHECK 只有 `('pre_screen','industry_screen','score')`。⇒ 一只标的若在
`mid` 与 `long` 两池都打分不通过，就写出两行同 `(code, stage)` ⇒ 撞主键。

**现场实测（21 只种子池，`asof=2026-09-24`）**：`rejects` 共 **7** 行，其中
`(code, stage)` 重复 **3 组**，全是银行股 —— `600036` / `601318` / `601398`，
在 `mid` 与 `long` 两池各被拒一次（原因都是「可用财务因子不足 2 个：金融股报表结构无营业成本/存货」）。
**种子里永远有这 3 家** ⇒ 这条路径不是偶发。

**根因 B：写快照不是原子的，失败后留下半截快照，且再跑会被当成「已完成」。**
`stocklab/store/db.py::connect()` 用 `sqlite3.connect(..., isolation_level=None)`
⇒ 连接处于 **autocommit**，`snapshot.py::write_snapshot` 末尾的 `conn.commit()` 是**空操作**，
每条 `INSERT` 立即提交；抛错时**没有 ROLLBACK**。实测副本里留下了
`candidate_snapshots.snapshot_id=3`（`asof=2026-09-24, run_kind=weekly`）＋ **2 行**
`candidate_rejects`，而该快照本应有 19 members ＋ 4 rejects。更糟的是：**再跑一次**会被
`find_snapshot` 命中并**静默**返回这份**残缺**快照（`skipped=True`、`load_snapshot` 读不全、
不报错）—— 这是一条「静静地产出错数据」的路径，比直接报错危险得多。

另外 `stocklab/candidate/run.py:19` 的模块 docstring 写着「快照写在一个事务边界内
（`write_snapshot` 内部 commit）」—— 这句话在 autocommit 连接下**是假的**，属
ERROR_DIARY #48 同型的「文档在说谎」，本档一并改对。

### 0.3 真库未被污染（复核过，勿动）

- `data/stocklab.db`（sha `0f8231f03dc3ca80…`）：`candidate_snapshots` 只有 `snapshot_id=1,2`
  （`2026-09-18`/`2026-06-15` 的 `weekly`，创建于 2026-09-20），`candidate_rejects` **0 行**。
- `CLOSE_STEPS`(14) / `MONTHLY_STEPS`(8) 里**没有** `candidate_run` ⇒ 日常链从未碰到它。
- **本档一律不写真库**；所有写实测只在 `/tmp` 副本上跑（`cp -c data/stocklab.db /tmp/p90/copy.db`）。

---

## 0.5 锁定口径（D1–D8；要改先回来说）

**D1｜去重规则＝`(code, stage)` 唯一，保留流水线顺序里的第一条。**
- 依据：`candidate_rejects` 的**主键就是** `(snapshot_id, code, stage)` —— schema 的意图
  本来就是「一个 `(code, stage)` 一行」；修的是**产出侧**在说谎，不是 schema。
- 策略语义不受影响：该标的在任一池打分不通过 ⇒ 就不会进那一池的 `scored`；
  丢掉的第二行只丢「另一个池也拒了它」这条信息，不丢任何买卖决策。
- 顺序必须**确定**：按流水线顺序（`eligible_pools` 的返回顺序，`short→mid→long`）
  取第一条 ⇒ 3 家银行保留的是 **`mid` 池**那条 `reason`。
- **不改 `reason` 文案**、**不改 `stage` 词汇**（不许把池名塞进 `stage`：那要动
  schema 的 CHECK 与报告/labweb 口径，代价远大于收益）。

**D2｜丢弃数必须可见（只增键）。** 新增计数写进快照的 `params`：
`"n_reject_dups_dropped": <int>`（键名固定用这个；沿用 P77/P83 的「只增键」惯例，
不得删改既有键）。`candidate_snapshots.params_json` 与报告里的 params 段都能看到它。

**D3｜`write_snapshot` 必须原子。** 快照行 ＋ members ＋ rejects **在同一个事务里**：
用**已存在的** `stocklab.store.db.transaction(conn)` 上下文管理器包住（它内部
`BEGIN` / `commit()` / 异常 `rollback()` 并重抛；在 `isolation_level=None` 连接上有效）。
- ⚠️ **不许用 `with conn:`** —— 在 `isolation_level=None` 下它是空操作（同一类坑）。
- ⚠️ `transaction()` **禁止嵌套**（`conn.in_transaction` 为真时抛 `RuntimeError`）
  ⇒ `write_snapshot` 必须是事务的**最外层**；调用方（`run_candidate`）不得自己先开事务。
  若确有调用方已在事务里，**停下写进 §7**，不许自己改 `transaction()`。
- 保留原有早退语义：已存在同 `(asof, run_kind)` ⇒ **直接返回既有 id、零写入**。

**D4｜`store/db.py` 与 `store/schema.sql` 都不许改。**
不许靠「把连接改成非 autocommit」来绕过（那会影响全库所有调用点）。
主键、CHECK、append-only 触发器**一个都不许动**。

**D5｜失败后必须干净、且不得留下「假已完成」。**
抛错后：`candidate_snapshots` **没有**该 `(asof, run_kind)` 行、
`candidate_members` / `candidate_rejects` **零残留**；随后再跑一次能**正常成功**。

**D6｜打分/分流口径一字不动。** `screen.py` / `score.py` / `risk_adjust.py` /
`pools.py` / `report.py` 零改动；不得改池定义、TOPN、打分公式、排序键。

**D7｜幂等语义保持不变。** 同 `(asof, run_kind)` 再跑 ⇒ `skipped=True`（既有行为）、
exit 0、行数不变；`light`/`weekly`/`quarterly` 三种 `run_kind` 互不覆盖。

**D8｜红线不 regen、真库零写入、不在真库上跑任何写库命令。**

---

## 1. 允许改动面（超出即停，写进 §7）

- `stocklab/candidate/run.py`（去重 ＋ 改对 docstring）
- `stocklab/candidate/snapshot.py`（`write_snapshot` 原子化 ＋ 去重入参/计数）
- 新增/修改 `tests/test_candidate_run*.py`、`tests/test_candidate_snapshot.py`
- `docs/tasks/2026-09-27-p90-*.md`（本档 §7）
- **允许新增** 一份 ADR（`docs/decisions/2026-09-27-ADR-040-*.md`）＋ `docs/decisions/README.md`
  索引一行 —— 只在你认为「主键语义 vs 产出侧去重」值得留档时写；不写也不算未达成。

**禁改**：`store/db.py`、`store/schema.sql`、`candidate/{screen,score,risk_adjust,pools,report}.py`、
`ops/`、`m2/`、`paper/`、`predict/`、`verify/`、`plugin/`、`data/`、`config/`。

---

## 2. 判据（G1–G9，逐条给命令与原始输出）

**G1 全量 pytest**：`.venv/bin/python -m pytest -o addopts="" -q` ⇒ rc=0；
用例数 **≥ 3704 passed / 2 skipped**（基线），新增用例 **≥ 8** 条。

**G2 `bash scripts/verify.sh`** ⇒ rc=0、`✅ 验证通过: all`。

**G3 `scripts/check_redlines.py`**（**不 regen**）⇒ rc=0、三目标逐位 == 基线：
`0acf35c9…`(175) / `73ffbfb1…`(1330) / `ddac0a88…`(171)。

**G4 真库零写入**：`shasum -a 256 data/stocklab.db` **跑前 == 跑后**
（本轮基线 `0f8231f03dc3ca80b3b46cd13e16885a456a4536b835ab426f0a5c5231ae33f4`）。

**G5 端到端（副本，必须真跑）**：

```
$ cp -c data/stocklab.db /tmp/p90/copy.db
$ .venv/bin/python -m stocklab.cli.main candidate run --run-kind weekly --asof 2026-09-24 --db /tmp/p90/copy.db
```

- **rc 必须 0**（修前 rc=1）。
- 落库读回：`candidate_members` = **19** 行、`candidate_rejects` = **4** 行
  （修前 7 行含 3 组重复）、`params_json` 含 `"n_reject_dups_dropped": 3`。
- 4 行 rejects 的 `(code, stage)` 两两不同；`600036`/`601318`/`601398` 各**恰好一行**、
  其 `reason` 以 `mid池打分未通过` 开头（D1 的顺序判据）。
- **幂等**：同命令再跑一次 ⇒ rc=0、`skipped`（输出里能看出是「已存在」路径）、
  两张表的行数**逐位不变**、`snapshot_id` 不变。

**G6 原子性（副本，必须真的制造一次失败）**：让 `write_snapshot` 在写 members 的
中途抛错（最简做法：monkeypatch `snapshot.write_snapshot` 里 members 插入的
`conn.execute` 计数到第 N 次抛异常；或用一个 `status` 非法的 `MemberRow` —— 但注意
现有 pre-flight 会在开事务前就拦掉它，**不能**用它当证据，免得证明的是 pre-flight
而不是回滚）。抛错后断言：

- `SELECT count(*) FROM candidate_snapshots WHERE asof=? AND run_kind=?` = **0**；
- 该 `asof` 的 `candidate_members` / `candidate_rejects` 残留 = **0**；
- 紧接着用**正常路径**再跑一次 ⇒ 成功、行数 = G5 的读数（不留「假已完成」）。

**G7 三种 `run_kind` 互不覆盖**（既有行为的回归）：同 `asof` 跑 `light` 与 `weekly`
⇒ 两个不同 `snapshot_id`、各自行数正确、互不删改（沿用 `tests/test_candidate_snapshot.py`
既有断言）。

**G8 反目标（给命令与输出）**：
- `git diff --name-only` 只落在 §1 的允许面内；
  `store/db.py`、`store/schema.sql`、`candidate/{screen,score,risk_adjust,pools,report}.py` 零改动。
- `candidate_rejects` 的 DDL（主键/CHECK）与两条 append-only 触发器**逐字未变**
  （贴 `git diff -- stocklab/store/schema.sql` 的空输出）。
- `candidate/run.py` 的打分循环里 `score.score_pool` / `risk_adjust.adjust` 的调用形参逐字未变。

**G9 真库现状核实（只读，发现异常就停下报告，不许自己清理真库）**：

```
$ sqlite3 'file:/Users/zhengchangchun/Documents/Workspace/stock/data/stocklab.db?mode=ro' \
    'SELECT count(*) FROM candidate_snapshots; SELECT count(*) FROM candidate_rejects;'
2
0
```

---

## 3. 纪律

1. **不许在真库上跑任何写库命令**；`ingest *` / `candidate run` / 迁移只在 `/tmp` 副本上跑。
2. 提交**只用显式路径**（禁 `git add -A`）；`git commit -m` 里**禁反引号**，长消息用 `-F <file>`。
3. 测试**不许真起子进程**（`conftest` 已 autouse 禁真实联网/禁子进程副作用）。
4. 判据说「不知道」的地方**不许自己拍**：停下、写进 §7 的偏离表，按最保守选项做。
5. 任务书 §7 逐条填：落点（含行数）、TDD 红→绿、G1–G9 原始输出、`git diff --stat`、坑、偏离与未决。
   **§8 留给 nanobot**（独立复核），不要代写。

---

## 5. 交付

代码 ＋ 测试 ＋ 本档 §7。完成后由 nanobot 独立复核（自跑 G1–G4 ＋ 在副本上复现 G5/G6），
再决定是否把 `candidate run` 标成「weekly 链可用」。

---

## 7. 实施记录（站填）

（站填）

---

## 8. nanobot 独立复核

（我填）
