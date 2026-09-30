# P103 任务书 ｜ 候选 #5 `mf_net_surprise_20d` 的研究侧取值器（P95 §5.1 MF-B）

**站点**：Claude Code（代码开发一律交 CC；nanobot 只出任务书 ＋ 独立复核 ＋ 自己跑全窗实验）
**分支**：`feat/financials`
**上游依据**：`docs/plans/2026-09-27-p95-新信号源-设计稿.md` **§5.1 MF-B**（逐字公式／PIT 锚／`std==0` 例外／覆盖实测）、§5「通用约定」（缺失一律 `None` 不补 0；rank IC 不做 winsorize）、§8（P98 单因子预注册实验）；`docs/decisions/2026-09-29-ADR-045-研究侧因子注册表与factor开关.md`（研究侧注册表 ＋ `--factor` 开关 ＋ P102 追加段）；`docs/tasks/2026-09-30-p102-ann-count-5d研究侧取值器.md` §10（候选 #4 判 `IC_NOT_SIGNIFICANT` ⇒ 按顺序推进到候选 #5）
**开工基线**：HEAD `f5367d0`，工作区干净（`git status --porcelain` 0 行）
**测试基线**：**3956 passed / 2 skipped, rc=0**（nanobot 于 2026-09-30 P102 复核时自跑；其后 `c0164fe`(预注册) 与 `f5367d0`(台账回填) 两笔**只动 `docs/`**（`git show --stat` 已核）⇒ 测试读数不变）；`bash scripts/verify.sh` 同基线 exit 0。站收工读数必须 ≥ 基线，逐字节口径见 §10。

---

## §0 背景（要解决的一个具体问题）

1. **候选 #5 进不了 harness**。`research factor-ic --factor mf_net_surprise_20d` **当前 exit 2**：
   `RESEARCH_FACTORS = ("mf_ratio_5d", "ep_ttm", "ann_count_5d")` 里没有它，
   `selected_factors()` 对未登记名字 fail-closed 拒跑（`factor.py:634`）。
2. **它不是「换数据类别」，是同一数据类别的第二个口径**。P94 的方向结论是「合成分数与输入因子均无 IC
   ⇒ 换信号源（新数据类别），不在原因子上调权重」；`mf_net_surprise_20d` 用的仍是 `money_flow_daily`，
   **但**它的构造在两点上与前四个候选都不同：① **时序标准化**（只与自身历史比，不做横截面归一）
   ⇒ 规模项在「同一只标的的分子分母」里自动约掉；② 不依赖 `valuation_daily`（不受 2018 起点的限制），
   **可用窗口保持 2010+**。P95 §5.1 把它的相关风险判为**低-中**（比 MF-A 的「高」低一档）。
3. **本档的缺失语义与候选 #4 相反**：候选 #4 是「0 是真值、永不 `None`」；本档是**标准的 `None` 规则**
   （20 日窗内任一 `NULL` 或行缺失 ⇒ `None`；`std == 0` ⇒ `None`，**不给 0** —— 给 0 会被读成
   「无异动」，而真实语义是「分母为 0、比值无定义」）。**不许**照抄 P102 的补 0 形状。
4. **本站不跑实验**：全窗 `factor-ic` ≈3.1–3.3 h、纯本地 CPU、零 token ⇒ **由 nanobot 在 `/tmp` 副本上
   自己跑**（复用既有副本 `/tmp/p98/copy.db`），预注册也由 nanobot 写。站只交付「取值器 ＋ 登记 ＋
   用例 ＋ 口径披露」。

---

## §0.5 拍板点（**已由 nanobot 锁定，站不逐项问、不另立方案**）

| # | 锁定项 | 内容 |
|---|---|---|
| L1 | 改动面 | **只动** `stocklab/research/factor.py` ＋ `tests/`（新增 `tests/test_research_factor_p103.py`；**并允许**同步 `tests/test_research_factor_p97.py` / `tests/test_research_factor_ep_ttm.py` / `tests/test_research_factor_p102.py` 里把 `RESEARCH_FACTORS` **集合内容写死**的那几处断言与参数表 —— 见 L9）＋ `docs/`（ADR-045 追加段 ＋ `docs/architecture/ai-pipeline.md` ＋ 本任务书 §7/§9）。`candidate/` `plugin/` `predict/` `paper/` `m2/` `ops/` `data/` `config/` `store/` `cli/` `research/signal.py` `candidate/replay.py` **零改动** |
| L2 | 窗口（**关键口径，逐字**） | `W` = `_recent_trading_dates(conn, asof=asof, n=20)`（**交易日**，不是自然日）。`window[-1]`（= 最靠 asof 的那个交易日）的 `main_net` 记作 `x_now`，`mean(W)` 记作 `μ`、`stdev(W)` 记作 `σ`。**不足 20 个交易日 ⇒ 该 `asof` 返回空映射 `{}`**（不拿「有几日算几日」顶替，与 `_mf_ratio_5d_map` 同规）。 |
| L3 | 逐字公式 | `mf_net_surprise_20d = (x_now − μ) / σ`，`x_now` = `main_net[date == window[-1]]`，`μ`/`σ` 取**同一 20 个交易日**（**含** `x_now` 自身）的 `main_net`。单位是「元」，除法消掉量纲。 |
| L4 | `std` 的自由度（**本档新锁的口径**） | 用 **`statistics.stdev`（样本标准差，`ddof=1`，分母 `n−1`）** —— 与仓内既有约定一致（`research/signal.py:484`、`verify/report.py:56`、`experiments/metrics.py:85`、`experiments/residuals.py:222` 全是 `stdev`，无一处 `pstdev`）。**不许**改 P95 §5.1 的公式字样，但**必须**把这条选择写进 ADR 追加段与 nanobot 的预注册（口径披露，不是判据）。 |
| L5 | 缺失语义 | **20 日窗内任一 `main_net` 为 `NULL`、或该 (code, date) 行缺失、或该 code 在窗内的行数 < 20 ⇒ 该标的 `None`（不进结果集，不补 0）**；**`σ == 0`（含浮点 0.0 与 −0.0）⇒ `None`**，**不许**返回 `0.0`。⇒ 返回值类型＝`dict[str, float]`，缺的标的就是**没有键**（与 `_mf_ratio_5d_map` / `_ep_ttm_map` 同规，**不是** P102 的「每个成员都给值」形状）。 |
| L6 | PIT 锚 | `money_flow_daily.date`，一律 `date <= asof`；本因子**只读 `main_net` 一列**（`ratio_amount` 是 MF-A 的输入，别混用）；**不许**读 `close`/`turnover`/`xl_net` |
| L7 | 批量友好 | 每个 `asof` 只发 **2 条 SQL**（日历 1 条 ＋ 窗口 1 条），一次覆盖全市场标的；**禁止**逐标的一条 SQL。复杂度写进 docstring（`O(窗口行数)` ＋ `O(标的数)` 归并） |
| L8 | 入参与形状 | `_mf_net_surprise_20d_map(conn, asof) -> dict[str, float]`（**与 `_mf_ratio_5d_map` 同签名，不接 `members`**）；单只入口 `mf_net_surprise_20d(conn, code, asof) -> float | None`。`_research_map(conn, asof, name, members=None)` 新增分支时**忽略 `members`**（该参数只对 `ann_count_5d` 有意义），**不许**改动既有三个分支的签名/行为；`_research_value` 同样新增一支（**保持 `float | None` 返回注解**） |
| L9 | 登记与「写死集合」的对齐 | `RESEARCH_FACTORS: tuple[str, ...] = ("mf_ratio_5d", "ep_ttm", "ann_count_5d", "mf_net_surprise_20d")`；`FACTOR_SOURCES` 由既有推导自动带上（**不许**手抄第二份名单）；`_research_map()` 新增分支并**保留**末尾 `PreregError` 兜底。**既有测试里把 `RESEARCH_FACTORS` 集合内容写死的断言（`test_research_factor_p97.py:141`、`test_research_factor_ep_ttm.py:207`、`test_research_factor_p102.py` 内同名断言若存在）必须同步**，并在 §7/§9 逐处点名 —— 这类断言与「新增登记」**不可能同时成立**（P102 §10.4-1 已裁过一次），**同步它们不算越界**；但**不许**借此放松任何**规则类**断言（重叠规则、fail-closed、兜底分支） |
| L10 | 「缺省逐位不变」 | 不传 `--factor` 时，`research factor-ic` 产物必须与 P102 交付后**逐字节相同**（多一个登记名**不得**改变缺省路径任何字段/顺序）。用 `tests/test_research_factor_p97.py` 里那条自证用例的**同一手法**再钉一次（临时 `TMPDIR` 产物 ＋ sha256 对比） |
| L11 | 不碰判据 | 门槛 / 切分（`split_train_validate`）/ CI / `MIN_XSEC_N` / `MIN_VALID_PERIODS` / `N_LAYERS` / verdict 词汇表 / bootstrap 次数与种子 **一个都不许动**；**不做参数搜索**（20 / 756 / 6 这类常数只能照抄 P95，**不许试别的**）；**不改** `PREREG_FIELDS` |
| L12 | 只读与零写入 | 真库**只读**（`mode=ro`）；**不新增表/列/索引、不改 `store/schema.sql`、不跑迁移**；**不跑全窗实验**（可跑 ≤6 个月短窗冒烟，且**必须在 `/tmp` 副本**上）；站收工后真库 sha 必须跑前==跑后或差异**逐条归因** |
| L13 | 口径披露 | 在 **ADR-045 里追加一段**（公式 / 20 交易日窗 / PIT 锚 / `std==0 ⇒ None` / **`stdev` ddof=1 的选择** / 「缺失＝不进截面，与 P102 的『0 是真值』相反」），并在 `docs/architecture/ai-pipeline.md` 的研究侧因子那句里补上第四个名字。**不新开 ADR 编号** |
| L14 | 预注册与实验 | 站**不写、不改**任何实验文档；预注册 `docs/experiments/2026-09-30-factor-ic-mf-net-surprise-20d-csi300-500.md` 由 **nanobot 自己写**，全窗实验也由 nanobot 跑 |

---

## §0.6 为什么必须锁 `stdev`、以及窗内行数不足为什么算缺失

- **`stdev` vs `pstdev` 对 20 个样本差 2.6%**（`1/√(1−1/20) = 1.0259`）。这不是噪声级别的小事：
  rank IC 在**横截面内**比较，若同一次实验里一半标的上限被 2.6% 缩放、另一半没有，名次的相对顺序
  **不会**变（同一窗口下 `σ` 的缩放对**该标的**是常数）；但**不同 `asof` 之间的值域会漂**，
  而这正是 `zero_ratio_p50` / `tie_ratio_p50` 这类分辨率读数的输入。**锁一次，写进 ADR，别留给后人猜。**
- **窗内行数 < 20 为什么算缺失、而不是「有几日算几日」**：`main_net[asof]` 与 `μ`/`σ` 必须来自
  同一段历史；缺一天就把 `μ` 拉向「有数据的那几天」，两只标的的 `σ` 分母不同 ⇒ 截面内**不同规**。
  仓内既有 `_mf_ratio_5d_map` 就是这么判的（`len(by_date) == MF_WINDOW_TRADING_DAYS` 才保留），
  本档照抄该规。
- **实测覆盖**（nanobot 2026-09-30 只读：`money_flow_daily` 2,455,257 行、`main_net` **零 NULL**、
  `MAX(date) = 2026-09-29`）⇒ 缺失主要来自「新上市 / 停牌导致的**行缺失**」，不是 NULL ⇒ **两种缺失
  都必须处理**（`L5` 里已并列写明）。**不要在代码里 `assert` 任何覆盖数字。**

---

## §1 T1 取值器（`stocklab/research/factor.py`）

新增（放在 `_mf_ratio_5d_map` 一族**之后**、`_ep_ttm_map` 之前 —— 保持「同数据类的两个口径相邻」）：

```python
def _mf_net_surprise_20d_map(conn: sqlite3.Connection,
                             asof: str) -> dict[str, float]:
    """`{code: (main_net[窗口末] − mean(窗)) / stdev(窗)}`（P95 §5.1 MF-B）。

    …（窗口 / 逐字公式 / `stdev` ddof=1 / 缺失与 `σ==0` / 复杂度 / 出处）…
    """

def mf_net_surprise_20d(conn: sqlite3.Connection, code: str,
                        asof: str) -> float | None:
    """单只标的的 `mf_net_surprise_20d`（`None` = 算不出）。"""
```

要点：
- 常数：`SURPRISE_WINDOW_TRADING_DAYS = 20`（放在既有 `MF_WINDOW_TRADING_DAYS` 旁，注释给出 P95 §5.1 出处）。
- 日历那条 SQL 复用 `_recent_trading_dates(conn, asof=asof, n=SURPRISE_WINDOW_TRADING_DAYS)`；
  `len(window) < SURPRISE_WINDOW_TRADING_DAYS` ⇒ `{}`。
- 窗口那条 SQL：`SELECT code, date, main_net FROM money_flow_daily WHERE date IN (<20 个占位符>)`
  —— 与 `_mf_ratio_5d_map` **同一姿势**（`IN` 白名单 ＋ Python 归并），**不要**改成 `BETWEEN`/`>=`（那会
  把 20 个交易日之间的**非交易日**行也带进来，口径就变了）。`main_net IS NULL` 的行**跳到但不计该日**
  （等于「这一天没有数据」）。
- 归并：`acc[code][date] = float(v)`；只保留 `len(by_date) == SURPRISE_WINDOW_TRADING_DAYS` 的 code；
  `vals = [by_date[d] for d in window]`（**按 `window` 的顺序取**，别用 dict 的插入序 —— 最后一个
  元素必须是 `window[-1]`）；`σ = statistics.stdev(vals)`；`σ == 0 ⇒ 跳过`（**不给 0.0**）；
  否则 `out[code] = (vals[-1] − statistics.fmean(vals)) / σ`。
  `fmean` 而非 `mean`：与 `experiments/residuals.py` 的既有取值一致，且少一次中间舍入。
- docstring 里给出出处（P95 §5.1 MF-B）与 §0.6 的实测读数（**只作注释，不作断言**）。

## §2 T2 登记与分派

1. `RESEARCH_FACTORS: tuple[str, ...] = ("mf_ratio_5d", "ep_ttm", "ann_count_5d", "mf_net_surprise_20d")`
   （注释写「本档补 P95 §5.1 MF-B」）。
2. `_research_map(conn, asof, name, members=None)`：
   - `name == "mf_net_surprise_20d"` ⇒ `return _mf_net_surprise_20d_map(conn, asof)`
     （**忽略 `members`**，与另外两个行情类分支同形）；
   - 其余分支签名/行为**一字不动**；末尾 `raise PreregError(...)` 保留。
3. `_research_value(conn, code, asof, name, members=None)`：`name == "mf_net_surprise_20d"` 走
   `mf_net_surprise_20d(conn, code, asof)`（返回注解**保持 `float | None`**）；其余分支不动。
4. `run_factor_ic` 的调用点**不动**（P102 已改成 `_research_map(conn, d0, name, member_codes)`，
   本档新分支忽略该参数即可 —— **不许**把 `member_codes` 透传删掉，那会改 `ann_count_5d` 的行为）。

## §3 T3 诊断键

`zero_ratio_p50` / `tie_ratio_p50` 由 P102 的 `zero_tie_diag()` **通用实现**，对**所有** `source=research`
的因子自动生效（`factor.py:960` 那段 `if FACTOR_SOURCES[name] == "research"` 循环）⇒
**本档 T3 无需新增任何键、零代码改动**。站只需在 §7 里**明确记一句**「已核对：新因子自动拿到这两个
诊断键，未新增/未改名」，并在用例里钉一条（C8）。

## §4 T4 用例（新增 `tests/test_research_factor_p103.py`）

用**临时 sqlite**（`tmp_path`）自建最小表（`trading_calendar` / `money_flow_daily`），**不碰真库**。
至少覆盖（每条给出「输入 → 期望」）：

| # | 用例 | 期望 |
|---|---|---|
| C1 | 窗口边界：`window[-1]`（第 20 个交易日）**有**行、`window[0]`（第 20 个的前一个交易日，**窗外**）**有**行且值极大 | 窗外的极值**不影响** `μ`/`σ`；手算值对拍 |
| C2 | 手算对拍（构造 20 个已知值） | `(vals[-1] − fmean(vals)) / stdev(vals)` 与返回**逐位相同**（用 `==`） |
| C3 | 窗内任一 `main_net IS NULL` | 该 code **不在**结果里（`code not in got`） |
| C4 | 窗内缺**一行**（20 个交易日里少 1 行） | 同上，**不进结果**（不是「有几日算几日」） |
| C5 | `σ == 0`（20 个值完全相同） | **不进结果**（**不是** `0.0`） |
| C6 | 交易日不足 20 个（`trading_calendar` 只放 19 天） | 返回 `{}` |
| C7 | 单只入口与批量映射一致 ＋ 缺值时返回 `None` | `mf_net_surprise_20d(c, code, a) == _mf_net_surprise_20d_map(c, a)[code]`；缺值那只 `is None` |
| C8 | 诊断键自动生效（§3） | 端到端跑一次小 `factor-ic`（或直接断言 `zero_tie_diag` 对新因子有值）⇒ 新型的因子块里同时有 `zero_ratio_p50` / `tie_ratio_p50` |
| C9 | 未登记名仍 fail-closed ＋ 兜底分支 | `selected_factors(["mf_net_surprise_20dx"])` 抛 `PreregError`；`monkeypatch` 加一个无分支的登记名后 `_research_map` 仍抛 `PreregError`（照抄 `test_research_factor_ep_ttm.py:224-227` 的手法） |
| C10 | **缺省逐位不变**（L10） | 不传 `--factor` 的产物 sha256 == 基线产物 sha256（同一手法见 `tests/test_research_factor_p97.py`） |
| C11 | 其它三个因子的行为**逐位不变** | `_mf_ratio_5d_map` / `_ep_ttm_map` / `_ann_count_5d_map` 在小表上的返回值与新增分支前一致（可用直接断言锁住形状，不必跑旧代码） |

## §5 T5 文档

1. ADR-045 追加段（**不新开编号**）：公式、20 交易日窗、PIT 锚、`std==0 ⇒ None`、**`stdev` ddof=1**、
   与 P102「0 是真值」的对照一句、深度/覆盖说明（`money_flow_daily` 全史 2010+，`main_net` 零 NULL，
   缺失来自行缺失）。
2. `docs/architecture/ai-pipeline.md`：研究侧因子那句补第四个名字。
3. 本任务书 §7（实施记录）＋ §9（偏离/未决，逐条点名）。

## §6 反目标（**做了就是越界**）

1. 不跑全窗实验（>6 个月的 `factor-ic`）；不在真库上跑任何写库命令；不在真库上跑 `--factor
   mf_net_surprise_20d` 冒烟。
2. 不改 `store/schema.sql`、不新增表/列/索引、不跑迁移；**不加任何索引**（`WHERE date IN (…)` 已有
   既有查询计划，站若实测慢也**不许**动 schema）。
3. 不动 `candidate/` / `plugin/` / `predict/` / `paper/` / `m2/` / `ops/` / `cli/` / `research/signal.py`。
4. 不改门槛、切分、CI、`MIN_*`、`N_LAYERS`、verdict 词汇表、bootstrap；不做参数搜索（不许试
   10/30/60 日窗或 `pstdev` 对照）；不写任何「因子有效」的结论。
5. 不改 `announcements` / `money_flow_daily` 的 DDL 与 append-only 触发器；不采新数据。
6. 不写实验文档 / 预注册（L14）；不动 `docs/experiments/README.md`。

## §7 交付物与进度

| 交付物 | 路径 |
|---|---|
| 本任务书 §7（实施记录）＋ §9（偏离/未决） | `docs/tasks/2026-09-30-p103-mf-net-surprise-20d研究侧取值器.md` |
| 取值器 ＋ 登记 ＋ 分派 | `stocklab/research/factor.py` |
| 用例 | `tests/test_research_factor_p103.py`（＋ L9 允许的既有断言同步） |
| 口径披露 | ADR-045 追加段 ＋ `docs/architecture/ai-pipeline.md` |

| # | 事项 | 状态 |
|---|---|---|
| T1 | 取值器 `_mf_net_surprise_20d_map` / `mf_net_surprise_20d` | ☐ |
| T2 | 登记 + `_research_map`/`_research_value` 分派 | ☐ |
| T3 | 核对诊断键自动生效（零改动，§3） | ☐ |
| T4 | 用例 C1–C11 | ☐ |
| T5 | ADR-045 追加段 + ai-pipeline + §7/§9 | ☐ |
| T6 | 自跑全量 pytest ＋ `scripts/verify.sh` ＋ 真库 sha 跑前/跑后 | ☐ |

## §8 风险（预登记）

- **机时**：全窗实验（nanobot 跑）≈3.1–3.3 h CPU；不是本站的事，站若擅自跑全窗会挤占。
  冒烟窗口 ≤6 个月、且在 `/tmp` 副本上 ⇒ 成本 <5 min。
- **相关风险（实测 ρ̄ 由 nanobot 在预注册与报告里处理，站不许下结论）**：P95 §5.1 判 MF-B 与
  `mom20`/`vr15` 的相关风险「低-中」（时序标准化消掉规模项）⇒ 预注册里仍需报 ρ̄ 并套
  「|ρ̄| > 0.5 淘汰 / 0.3–0.5 必须报正交化残差 IC」的统一判据。
- **取数成本**：`money_flow_daily` 245 万行；`WHERE date IN (<20 个占位符>)` 走的是 `date` 上的
  现有索引/扫描路径，nanobot 未实测本因子单 `asof` 计时 ⇒ 若站实测单 `asof` >2 s，**只报读数、
  不许优化 schema**（反目标 2）。
- **语义重复的自我检查**：本因子与 `mf_ratio_5d`（候选 #2）**同源不同口径**，两者 IC 可并排读，
  但**不许**把本档写成「#2 的结论被推翻/被证实」——#2 测的是 5 日占比均值，本档测的是 20 日
  自身历史异动，是两个独立假设。

## §9 实施记录（站填）

（站收工时填写：改动文件清单（显式路径）／代码位置表／用例数与实测耗时／偏离任务书处逐条点名／
提交（分笔、显式路径、`git commit -F`、消息无反引号）／收工 `git status --porcelain` 为空。）

## §10 复核（nanobot 独立复核，不采信自报）

（由 nanobot 在站收工后填写：自跑判据表／独立口径对拍／真库写入归因／偏离裁决／结论。）
