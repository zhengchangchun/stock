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

开工 HEAD：`a3609e3`（与任务书写的 `a08991f` 相差 1 个 commit —— `a3609e3` 是本任务书
自己的 docs 提交，代码侧无差异）。真库 sha 开工时 = `0f8231f0…`，与 §0.3 一致。

### 7.1 落点（含行数）

| 文件 | 落点 | 行 |
|---|---|---|
| `stocklab/candidate/run.py` | 模块 docstring：改对「`write_snapshot` 内部 commit」这句假描述（autocommit 下是空操作）＋新增「拒绝行按 `(code, stage)` 去重（在**写路径**上）」一节 | 18–21、23–30 |
| `stocklab/candidate/run.py` | `score_pipeline` docstring：补一句「`rejects` 无损，折叠在写路径」 | 279–282 |
| `stocklab/candidate/run.py` | **新增** `_dedup_rejects(rejects) -> (kept, dropped)`：按 `(code, stage)` 去重、保留流水线顺序第一条 | 375–399 |
| `stocklab/candidate/run.py` | `run_candidate`：`_dedup_rejects(pipe.rejects)` ＋ `params = {**pipe.params, "n_reject_dups_dropped": n}`（420–423）；同一份 dict 落库（424–426）与返回（436） | 420–426、436 |
| `stocklab/candidate/snapshot.py` | `from stocklab.store.db import transaction` | 28 |
| `stocklab/candidate/snapshot.py` | `write_snapshot`：三条 INSERT 包进 `with transaction(conn):`（90），删掉裸 `conn.commit()`；docstring 写明原子性与「不能用 `with conn:`」 | 65–112 |
| `tests/test_candidate_run_p90.py` | **新增文件**（223 行），9 条用例 | 全档 |
| `tests/test_candidate_snapshot.py` | 新增 P90 段 4 条用例（原子性 ×2、事务不悬挂、三种 run_kind 并存） | 102–176 |
| `tests/test_candidate_run.py` | `test_score_pipeline_matches_run_candidate_members` 的 rejects 断言改为「内核产物**按主键投影后**」 | 318–323 |
| `tests/test_candidate_run_p77.py` | `test_p77_snapshot_params_has_two_new_keys_and_history_untouched` 的「老键」排除集合 +1 个新键（判据本身仍是「老键逐位未变」） | 254–258 |
| `docs/decisions/2026-09-27-ADR-040-*.md` + `README.md` | 新增 ADR-040 ＋ 索引一行 | — |

一份合同证据：**唯一**的 `write_snapshot` 调用方是 `run_candidate`（`run.py:424`）与
`tests/test_labweb_candidate.py:115` 的 `_snap`，两者都**没有**先开事务 ⇒
D3 的「若调用方已在事务里就停下」不成立、`transaction()` 不会嵌套。

### 7.2 TDD 红 → 绿

RED 用**最终那套用例**对**修前的两个源文件**跑：只把这两份恢复成 HEAD
（`git stash push -- stocklab/candidate/run.py stocklab/candidate/snapshot.py`），
测试文件保持现状；跑完 `git stash pop` 复原，`git diff --stat`
（`run.py` 55 / `snapshot.py` 52 行改动）前后逐位一致。

```
$ .venv/bin/python -m pytest -o addopts="" -q tests/test_candidate_run_p90.py tests/test_candidate_snapshot.py
FAILED tests/test_candidate_run_p90.py::test_kernel_keeps_both_rows_and_write_path_folds_them
FAILED tests/test_candidate_run_p90.py::test_dedup_count_reaches_run_params
FAILED tests/test_candidate_run_p90.py::test_dedup_count_is_zero_without_duplicates
FAILED tests/test_candidate_run_p90.py::test_dedup_is_per_code - AttributeErr...
FAILED tests/test_candidate_run_p90.py::test_other_stages_are_not_dropped - A...
FAILED tests/test_candidate_run_p90.py::test_run_candidate_writes_deduped_rejects
FAILED tests/test_candidate_run_p90.py::test_run_candidate_dedup_count_reaches_snapshot_params
FAILED tests/test_candidate_snapshot.py::test_mid_write_failure_rolls_back_everything
FAILED tests/test_candidate_snapshot.py::test_failure_leaves_no_fake_completed_snapshot
9 failed, 11 passed in 2.78s
```

失败原因分档（**没有一条**是「断言写错」）：

- `test_dedup_is_per_code` / `test_other_stages_are_not_dropped`：`AttributeError`
  —— 修前根本没有 `_dedup_rejects`；
- 其余 5 条 p90 用例：`KeyError: 'n_reject_dups_dropped'` / 计数对不上；
- 2 条原子性用例：`{'candidate_snapshots': 1} != {…: 0}`、`assert 1 is None`
  —— **半截快照与「假已完成」原样复现**（修前 autocommit 下 `conn.commit()`
  是空操作，抛错也不 ROLLBACK）。

端到端那一条是**真 bug 原样复现**（同一 `(code, stage)` 两行被送进 `write_snapshot`）：

```
tests/test_candidate_run_p90.py:190: in test_run_candidate_writes_deduped_rejects
    result = candidate_run.run_candidate(conn, asof=ASOF, run_kind="weekly", now=NOW, universe=(BANK,))
stocklab/candidate/run.py:379: in run_candidate
    snapshot_id = snapshot.write_snapshot(
rejects = [RejectRow(code='600036', stage='score', reason='mid池打分未通过：可用财务因子不足 2 个', plugin_id='2'),
           RejectRow(code='600036', stage='score', reason='long池打分未通过：可用财务因子不足 2 个', plugin_id='3')]
```

GREEN（全部用例，含被波及的既有用例）：

```
$ .venv/bin/python -m pytest -o addopts="" -q tests/test_candidate_run_p90.py \
    tests/test_candidate_snapshot.py tests/test_candidate_run.py \
    tests/test_candidate_run_p77.py tests/test_research_factor.py \
    tests/test_research_signal.py tests/test_candidate_replay.py
156 passed in 9.73s
```

### 7.3 G1–G9 原始输出

**G1 全量 pytest**（基线与修后，两次都真跑）

```
$ .venv/bin/python -m pytest -o addopts="" -q          # 开工前（基线，代码未改）
3704 passed, 2 skipped in 244.95s (0:04:04)

$ .venv/bin/python -m pytest -o addopts="" -q          # 修后
3717 passed, 2 skipped in 243.18s (0:04:03)
rc=0
```

新增用例 = 3717 − 3704 = **13** 条（`tests/test_candidate_run_p90.py` 9 条 ＋
`tests/test_candidate_snapshot.py` 的 P90 段 4 条），≥ 8 ✓。

**G2 `bash scripts/verify.sh`**（代码改完跑一次、文档写完再跑一次，两次都 rc=0；下面贴交付态那次）

```
$ bash scripts/verify.sh
--- 4. 项目测试 ---
  ▶ .venv/bin/python -m pytest
3717 passed, 2 skipped in 236.63s (0:03:56)
--- 4c. 回归红线 ---
  ✅ predict_synthetic / predict_real_2026-09-14 / backfill_real_2013-12-23_2026-09-14
✅ 回归红线全部成立
--- 5. git 工作区状态 ---
  ⚠️  有未提交改动：            ← 本轮提交前，预期
✅ 验证通过: all
rc=0
（同一次末尾的 `shasum -a 256 data/stocklab.db` =
 0f8231f03dc3ca80b3b46cd13e16885a456a4536b835ab426f0a5c5231ae33f4）
```

**G3 `scripts/check_redlines.py`（未加 `--regen`）**

```
$ .venv/bin/python scripts/check_redlines.py
  ✅ predict_synthetic
       sha256 0acf35c90f54ab6a… == 基线；叶子 175 项一致（leaves_sha256 a59e8c1906bc4ce2…）
  ✅ predict_real_2026-09-14
       sha256 73ffbfb136e8620d… == 基线；叶子 1330 项一致（leaves_sha256 3af6008903703950…）
  ✅ backfill_real_2013-12-23_2026-09-14
       sha256 ddac0a889c623d75… == 基线；叶子 171 项一致（leaves_sha256 06bf461ef1234b4e…）
✅ 回归红线全部成立
rc=0
```

三目标 sha 前缀与叶子数与判据给的 `0acf35c9…(175)` / `73ffbfb1…(1330)` /
`ddac0a88…(171)` 逐位相符。

**G4 真库零写入**

```
跑前 / 跑后（G3 前后各一次、全部实测结束后再一次）：
0f8231f03dc3ca80b3b46cd13e16885a456a4536b835ab426f0a5c5231ae33f4  data/stocklab.db
```

**G5 端到端（`/tmp/p90` 副本，真跑）**

```
$ cp -c data/stocklab.db /tmp/p90/copy_g5b.db
$ .venv/bin/python -m stocklab.cli.main candidate run --run-kind weekly --asof 2026-09-24 --db /tmp/p90/copy_g5b.db
snapshot_id=3 asof=2026-09-24 kind=weekly
入池：短期 6 / 中期 8 / 长期 5；淘汰 4
rc=0                                        ← 修前 rc=1（IntegrityError）
```

落库读回：

```
members=19
rejects=4
distinct_keys=4
code    stage       reason_head                                        plugin_id
------  ----------  -------------------------------------------------  ---------
002032  pre_screen  consecutive_limit_down
600036  score       mid池打分未通过：可用财务因子不足 2 个（缺：opera  2
601318  score       mid池打分未通过：可用财务因子不足 2 个（缺：opera  2
601398  score       mid池打分未通过：可用财务因子不足 2 个（缺：opera  2
600036|1   601318|1   601398|1        ← 三家银行各恰好一行（D1 顺序判据：mid 在前）
params_json：
{"members_sha256": "b77cd49e…", "n_adj_fallback": 9, "n_reject_dups_dropped": 3,
 "scoring_price_mode": "adjusted_factor_side+raw_screen", "seed_count": 21,
 "topn": {"long": 5, "mid": 8, "short": 6}, "universe_id": "seed21"}
```

幂等重跑：

```
$ shasum -a 256 /tmp/p90/copy_g5b.db
4208e95d6c4ede743b6e804b03fee169435e50d7e41f60d801525b3a297c4fa4
$ .venv/bin/python -m stocklab.cli.main candidate run --run-kind weekly --asof 2026-09-24 --db /tmp/p90/copy_g5b.db
⏭ 快照已存在（snapshot_id=3），跳过重跑        ← 走的是「已存在」路径
snapshot_id=3 asof=2026-09-24 kind=weekly
入池：短期 6 / 中期 8 / 长期 5；淘汰 4
rc=0
$ shasum -a 256 /tmp/p90/copy_g5b.db
4208e95d6c4ede743b6e804b03fee169435e50d7e41f60d801525b3a297c4fa4   ← 逐位不变
snapshot_id=3  members=19  rejects=4  snapshots_total=3               ← 行数不变、id 不变
```

**G6 原子性（另开一张干净副本，真制造一次写 members 中途的失败）**

失败点刻意选 **`pool` 撞 `candidate_members.pool` 的 CHECK**，不用 `status` 非法 ——
后者被 `write_snapshot` 的 pre-flight 在**开事务之前**拦掉，只能证明 pre-flight、
证不到回滚（任务书 §G6 的原话）。探针脚本 `/tmp/p90/g6_probe.py`：先 `[GOOD, BAD]`
两名成员（第 1 个入得了、第 2 个炸），再打印读数。

```
$ cp -c data/stocklab.db /tmp/p90/copy_g6b.db
$ .venv/bin/python /tmp/p90/g6_probe.py /tmp/p90/copy_g6b.db
✅ 已制造失败：sqlite3.IntegrityError: CHECK constraint failed: pool IN ('short','mid','long')
   失败前：snapshots(2026-09-24,weekly)=0 members=0 rejects=0  [全表 members=38]
   失败后：snapshots(2026-09-24,weekly)=0 members=0 rejects=0  [全表 members=38]
   find_snapshot=None  conn.in_transaction=False
probe_rc=0

$ .venv/bin/python -m stocklab.cli.main candidate run --run-kind weekly --asof 2026-09-24 --db /tmp/p90/copy_g6b.db
snapshot_id=3 asof=2026-09-24 kind=weekly
入池：短期 6 / 中期 8 / 长期 5；淘汰 4
rc=0
snapshots=3
members(2026-09-24,weekly)=19      ← 与 G5 读数逐位相同（不留「假已完成」）
rejects(2026-09-24,weekly)=4
params_json：… "n_reject_dups_dropped": 3 …
```

**G7 三种 `run_kind` 互不覆盖**（同一张副本，同 `asof`）

```
=== before ===            3|weekly|19|4
$ … candidate run --run-kind light     --asof 2026-09-24 --db /tmp/p90/copy_g5b.db
snapshot_id=4 asof=2026-09-24 kind=light      rc=0
$ … candidate run --run-kind quarterly --asof 2026-09-24 --db /tmp/p90/copy_g5b.db
snapshot_id=5 asof=2026-09-24 kind=quarterly  rc=0
=== after ===             3|weekly|19|4
                          4|light|19|4
                          5|quarterly|19|4
distinct_ids=3
```

**G8 反目标**

```
$ git diff --name-only
stocklab/candidate/run.py
stocklab/candidate/snapshot.py
tests/test_candidate_run.py
tests/test_candidate_run_p77.py
tests/test_candidate_snapshot.py
（＋新增未跟踪：tests/test_candidate_run_p90.py、docs/decisions/2026-09-27-ADR-040-*.md）
$ git diff -- stocklab/store/schema.sql
（空输出）
$ git diff --stat -- stocklab/store/db.py stocklab/candidate/screen.py \
      stocklab/candidate/score.py stocklab/candidate/risk_adjust.py \
      stocklab/candidate/pools.py stocklab/candidate/report.py
（空输出）
```

打分循环里两处调用**逐字未变**（`git diff` 里没有任何一行触及它们）：

```
$ grep -n "score\.score_pool(\|risk_adjust\.adjust(" stocklab/candidate/run.py
346:            outcome = score.score_pool(conn, inst, pool, pool_ctx,
354:            final, risks = risk_adjust.adjust(conn, outcome, pool_ctx,
```

`candidate_rejects` 的 DDL（主键 / CHECK）与两条 append-only 触发器：`schema.sql`
整文件 diff 为空（见上）。

**G9 真库现状核实（只读）**

开工时**按判据原命令**跑过一次，输出就是判据给的 `2` / `0`：

```
$ sqlite3 'file:/…/data/stocklab.db?mode=ro' \
    'SELECT count(*) FROM candidate_snapshots; SELECT count(*) FROM candidate_rejects;'
2
0
```

全部实测结束后再跑**同一条命令**却退出 14：

```
Error: in prepare, unable to open database file (14)
```

**这是环境行为、不是库被改动**：真库是 WAL 模式，`mode=ro` 且**不存在 `-shm`**
时 SQLite 拒绝开库（只读连接不允许创建 `-shm`）。开工那一刻有别的进程持有
`-shm` 所以能开；现在没有就开不了（`/tmp/p90/copy.db` 上同一个现象、同一条命令
同样 `rc=14`）。改走 `immutable=1`（只读、不建 `-shm`、不加锁）读回同一答案，
并以 sha 钉住库未被改动：

```
$ sqlite3 'file:/…/data/stocklab.db?mode=ro&immutable=1' \
    'SELECT count(*) FROM candidate_snapshots; SELECT count(*) FROM candidate_rejects;'
2
0
$ shasum -a 256 data/stocklab.db
0f8231f03dc3ca80b3b46cd13e16885a456a4536b835ab426f0a5c5231ae33f4   （== 基线）
```

结论：`candidate_snapshots` 2 行（`snapshot_id=1,2`，`candidate_members` 38 行 = 19×2）、
`candidate_rejects` 0 行，与 §0.3 的只读复核一致，**无异常**。

### 7.4 `git diff --stat`

分两笔提交（**代码/测试**一笔、**docs** 一笔；只用显式路径，未用 `git add -A`）：

```
$ git show --stat --oneline 8d73a86          # 代码 + 测试
 stocklab/candidate/run.py        |  55 +++++++++-
 stocklab/candidate/snapshot.py   |  52 +++++----
 tests/test_candidate_run.py      |   6 +-
 tests/test_candidate_run_p77.py  |   5 +-
 tests/test_candidate_run_p90.py  | 223 +++++++++++++++++++++++++++++++++++++++
 tests/test_candidate_snapshot.py |  82 ++++++++++++++
 6 files changed, 395 insertions(+), 28 deletions(-)

$ git show --stat --oneline 8e39d43          # docs
 docs/decisions/2026-09-27-ADR-040-*.md                    | 110 +++++++
 docs/decisions/README.md                                  |   1 +
 docs/tasks/2026-09-27-p90-candidate-run…化.md             | 363 ++++++++++++-
 3 files changed, 473 insertions(+), 1 deletion(-)

$ git diff --stat a3609e3 HEAD                # 合计（相对开工 HEAD）
 9 files changed, 868 insertions(+), 29 deletions(-)
```

提交后 `git status --porcelain` 为空，`data/stocklab.db` sha 仍是 `0f8231f0…`。

### 7.5 坑

1. **`pytest -q` 与 `addopts` 叠加**会把汇总行吃掉（`verify.sh` 里已有注释）；
   本站一律用 `-o addopts=""` 明确关掉，读数才可比。
2. **`| tail` 会吃掉退出码**：`.venv/bin/python -m pytest … | tail -6` 的 `$?`
   是 `tail` 的。凡要报 rc 的地方一律重定向到文件再 `echo rc=$?`。
3. **`mode=ro` 在这台机器上对 WAL 库不可靠**（见 G9）：判据里那条命令今天能跑、
   明天可能 `rc=14`。要么用 `immutable=1`，要么先确认有别的进程持有 `-shm`。
4. **`sqlite3` 的 `count(*)` 要按 `snapshot_id` 过滤**：`candidate_members` 没有
   `asof` 列，直接 `count(*)` 会把老快照的 38 行算进来（我用 `JOIN candidate_snapshots`）。

### 7.6 偏离与未决

**偏离 1（需要裁决，最重要）：D1 的去重落在了写路径 `run_candidate`，没有进内核
`score_pipeline`。**

任务书有两处硬约束在这里**互相矛盾**：

- D2 要求把 `n_reject_dups_dropped` 写进快照 `params`；而该键只可能在
  `score_pipeline` 里算出来（`params` 是它构造的）。
- 但 P83 的三条**既有红线**把内核 `params` 的键集/digest 逐一钉死：
  `test_research_factor.py::test_params_keys_unchanged`（`set(res.params)` 精确相等）、
  `test_research_factor.py::test_default_path_digest_is_unchanged_from_head`
  （五字段 digest == 常量）、`test_research_signal.py::
  test_pipeline_result_exposes_scored_without_changing_derivation`（同键集）。
  键进内核 ⇒ 三条必红；要让它们绿就得改这两个文件，而它们**不在 §1 的允许改动面**
  （「超出即停」）。
- 另外 `PipelineResult` 的字段列表也被 `test_research_factor.py::
  test_pipeline_result_field_order_and_defaults_are_only_additive` 精确钉死
  ⇒ **不能**只增一个「丢弃数」字段把计数从内核递出来。

按「先找同时满足的落点」的办法，唯一能同时满足 D1/D2/G1/§1 的落点是：内核**保持
无损**（同一 `(code, stage)` 有几行就是几行，`params` 键集真的没变 —— 三条红线
**仍然有效，不是被放宽**），折叠与计数放在写路径 `run_candidate`。
代价如实记：内核 rejects（真库 7 行）与落库 rejects（4 行）在有重复时**不等**。
若派单人更想要「内核即落库形状」（代价＝改上述三个测试文件、并放宽 P83 红线的
键集断言），请在 §8 裁决，本站按裁决改。

**偏离 2（在允许面内，但改了既有用例）**：`tests/test_candidate_run.py::
test_score_pipeline_matches_run_candidate_members` 的 rejects 断言改为
「内核产物**按主键投影后**」与落库行相等（`_dedup_rejects(pipe.rejects)`）。
该用例的保证（回测跑的就是生产逻辑）不变，只是把「按主键投影」这一步显式化。

**偏离 3（在允许面内）**：`tests/test_candidate_run_p77.py` 的「老键」排除集合
+1 个新键。该用例的判据（`scoring_price_mode`/`n_adj_fallback` 之外的老键逐位未变）
未被放宽，注释写明了原因。

**偏离 4（口径解释）**：§1 写「`snapshot.py`（`write_snapshot` 原子化 ＋ 去重入参/计数）」。
本站把「去重入参/计数」实现为「计数随**已有的** `params` 入参进快照」，
`write_snapshot` 的**形参一个没加**。理由：若在 `write_snapshot` 内部去重并把计数
注入 `params_json`，则 `RunResult.params` 的首跑路径（`= pipe.params`，无该键）会与
幂等重跑路径（`= 读回快照`，有该键）**不一致**。`write_snapshot` 保持「写什么就是
什么」的纯写入语义。

**未决 1**：**错误日记**。CLAUDE.md 的 DoD 要求「有新教训已写入错误日记」，
但本任务书 §1 的允许改动面**不含 `docs/errors/`**（「超出即停」）⇒ 本轮**未写**。
两个根因都属 ERROR_DIARY #48 同型的「文档/结构在说谎」类教训
（① 主键意图是「一个 `(code, stage)` 一行」，产出侧却产两行；
② autocommit 连接下 `conn.commit()` 是空操作、`with conn:` 同理，
`run.py` 的 docstring 还写着「`write_snapshot` 内部 commit」）。
建议 nanobot 在 §8 决定是否补一条（或授权本站补）。

**未决 2**：`G5` 判据里的 `rejects=4` 与 D1 的「丢的是『另一个池也拒了它』」——
若将来报告/labweb 需要「同一标的被几个池拒了」，现在这条信息只在
`n_reject_dups_dropped`（一个总数）里，**不再能按标的还原**。本档按 D1/D2 只增
计数键，不新增明细字段（那要动 schema 或再加键）。

**未决 3**：G9 的 `mode=ro` 环境问题（见 7.3/G9）。本站没有为了让那条命令成功而
在真库旁边创建 `-shm`（那是对真库目录的写副作用），改用 `immutable=1` ＋ sha 对账。

---

## 8. nanobot 独立复核

**结论：通过**（G1–G9 逐条自跑复现；4 条偏离全部接受；1 条未决由我补做）。
复核时间 2026-09-27 13:00–13:40，复核基线 HEAD `db93013`、工作区 clean。

### 8.1 我自跑的读数（唯一可引版本）

| 判据 | 我的实测 | 判定 |
|---|---|---|
| G1 全量 pytest | **3717 passed / 2 skipped in 240.05s**，rc=0（基线 3704/2 ⇒ **+13**，任务书要求 ≥8） | ✅ |
| G2 `verify.sh` | `✅ 验证通过: all`，rc=0（末节「✅ 工作区干净」） | ✅ |
| G3 `check_redlines.py`（未 regen） | rc=0；`0acf35c9…`(175) / `73ffbfb1…`(1330) / `ddac0a88…`(171) 逐位 == 基线 | ✅ |
| G4 真库零写入 | 跑前 == 跑后 `0f8231f03dc3ca80b3b46cd13e16885a456a4536b835ab426f0a5c5231ae33f4`（全量 pytest ＋ verify ＋ 红线之后复读） | ✅ |
| G5 端到端（`/tmp/p90rev/copy.db`） | **rc=0**（修前 rc=1）；`snapshot_id=3`「入池：短期 6 / 中期 8 / 长期 5；淘汰 **4**」；落库 `candidate_members`=**19**、`candidate_rejects`=**4**；`params_json` 含 `"n_reject_dups_dropped": 3`；三家银行各**恰好一行**且 `reason` 以 `mid池打分未通过` 开头 | ✅ |
| G5 幂等 | 同命令再跑 ⇒ 打印「⏭ 快照已存在（snapshot_id=3），跳过重跑」，rc=0；`members 19 / rejects 4 / snapshots 3` 逐位不变 | ✅ |
| G6 原子性（`/tmp/p90rev/copy2.db`） | 在 members 插入中途真抛 `IntegrityError`（第 2 行 `risk_json` NOT NULL，**发生在 snapshot 行已 INSERT 之后**）⇒ `candidate_snapshots where asof='2026-09-24'` = **0**、该快照下 members = **0**、总快照数仍 **2**、`conn.in_transaction` = **False**；紧接重跑正常路径 ⇒ `snapshot_id=3`、members=1 | ✅ |
| G8 反目标 | `git diff --name-only a3609e3..HEAD` = 9 文件（2 源 + 4 测试 + 3 docs），**全部落在 §1 允许面内**；`stocklab/store/` 与 `candidate/{screen,score,risk_adjust,pools,report}.py` **零改动**；打分循环里 `score_pool`/`risk_adjust` 调用行**无任何 ±** | ✅ |
| G9 真库现状（只读） | `candidate_snapshots` = **2** 行（id 1 `2026-09-18` weekly / id 2 `2026-06-15` weekly）、`candidate_rejects` = **0**、`candidate_members` = 38。**与任务书 §0.3 一致，真库未被污染、我没有清理任何东西** | ✅ |

### 8.2 偏离裁决（§7.6 四条）

1. **偏离 1（D1 去重落在写路径 `run_candidate`、没进内核 `score_pipeline`）——接受，且我确认这是硬约束下的唯一解。**
   站的推理我逐条复核过：`params` 是 `score_pipeline` 构造的，键只可能在核心里算出来；而 P83 的三条既有红线
   （`test_research_factor.py::test_params_keys_unchanged` / `test_default_path_digest_is_unchanged_from_head` /
   `test_research_signal.py::test_pipeline_result_exposes_scored_without_changing_derivation`）对内核 `params` 键集
   **做精确相等断言**，`PipelineResult` 字段表也被 `…field_order_and_defaults_are_only_additive` 钉死，
   而这三个文件**不在 §1 的允许改动面**。⇒ 让内核保持无损、把折叠放在唯一写路径上，是**同时满足
   D1/D2/G1/§1** 的落点；代价（内核 7 行 ≠ 落库 4 行）已如实记进 §7.6 与模块 docstring，**不是被藏起来**。
   我另核了 `_dedup_rejects` 的实现在真库形状上给出 D1 要的顺序判据（保 `mid` 那条），
   且 `transaction()` 在本轮候选链上**只在 `write_snapshot` 一处被用**（`grep` 全仓：`candidate/` ＋ `cli/main.py` 无第二处），
   不存在嵌套 ⇒ D3 的「必须最外层」成立。
2. **偏离 2（`test_candidate_run.py` 的 rejects 断言改成「按主键投影后相等」）——接受**。
   该用例的保证（回测跑的就是生产逻辑）没被放宽，只是把「映射到表结构」这一步显式化。
3. **偏离 3（`test_candidate_run_p77.py` 的老键排除集合 +1）——接受**。判据（其余老键逐位未变）未被放宽。
4. **偏离 4（`write_snapshot` 形参一个没加，计数随已有 `params` 入参进快照）——接受**。
   站给的理由成立：若在 `write_snapshot` 内部注入，首跑（`params = pipe.params`）与幂等重跑（读回快照）两条路径的
   `RunResult.params` 会**不一致**；保持「写什么就是什么」的纯写入语义是对的。我实测首跑与重跑读回的 `params` 逐位相同。

### 8.3 未决处置（§7.6 三条）

| # | 处置 |
|---|---|
| 1 错误日记（§1 允许面不含 `docs/errors/` ⇒ 站未写） | **由我补做**：已写 ERROR_DIARY **#84**（见下方 §8.4 的两条根因）。
| 2 `n_reject_dups_dropped` 只有总数、不能按标的还原「被几个池拒了」 | **接受，记账**。要还原得动 schema 或再加键，超出本档允许面；列入候选，等报告/labweb 真需要时另立任务书。
| 3 G9 的 `mode=ro` 在本机对 WAL 库不可靠 | **接受**。我复核时用 `immutable=1` ＋ sha 对账，读数与站一致（2 / 0）；站没有为让命令成功而在真库目录造 `-shm`，这点尤其对。

### 8.4 我补做的落位

- `docs/errors/ERROR_DIARY.md` 新增 **#84**：① 主键意图是「一个 `(code, stage)` 一行」，产出侧却
  在多个池各产一行（同 #48 同型的「文档/结构在说谎」）；② `isolation_level=None` 的 autocommit 连接下
  `conn.commit()` 与 `with conn:` **都是空操作**，`run.py` 的 docstring 还写着「`write_snapshot` 内部 commit」。
- 本档 §8（本文件）。

### 8.5 待办：P89 的 weekly 链现在可用了吗

**可用，且已在真库语义上验过**：G5 的 `asof=2026-09-24 / run_kind=weekly` 正是 2026-10-05 16:30
那轮的形状（全新 `(asof, run_kind)`、首次执行），实测 **rc=0**。⇒ 「weekly 第 1 步必红」这条阻塞级缺陷**已解除**。
下一步（巡检业务化 B5+B7）照审计 §6 派。
