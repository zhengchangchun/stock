import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from stocklab.config.paths import SCHEMA_SQL
from stocklab.store.db import connect

TZ = timezone.utc

# ---------------------------------------------------------------------------
# P28：估值 / 资金流两表的「空表重定义」迁移。
#
# 背景：这两张表在 P28 之前已由旧 schema 建出（老 shape，0 行、无触发器），
# 而新 shape 与旧 shape 列集不同。`CREATE TABLE IF NOT EXISTS` 不会改已存在的
# 表，所以老库必须走一次 DROP+CREATE。**只允许在表为空时做** —— 一旦有数据，
# append-only 铁律要求绝不动历史行（DROP 会丢数据），故直接抛错拒绝。
# DDL 与 schema.sql 里的 `CREATE TABLE IF NOT EXISTS` **同文**（改一处须同步另一处）。
# ---------------------------------------------------------------------------
_MIGRATE_P28_DDL: dict[str, str] = {
    "money_flow_daily": """
CREATE TABLE money_flow_daily (
    code          TEXT NOT NULL,
    date          TEXT NOT NULL,
    close         REAL,
    change_ratio  REAL,
    turnover      REAL,
    main_net      REAL,
    xl_net        REAL,
    ratio_amount  REAL,
    source        TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    resp_sha256   TEXT NOT NULL,
    cache_key     TEXT,
    PRIMARY KEY (code, date)
)""",
    "valuation_daily": """
CREATE TABLE valuation_daily (
    code          TEXT NOT NULL,
    date          TEXT NOT NULL,
    pe_ttm        REAL,
    pb            REAL,
    ps_ttm        REAL,
    total_mv      REAL,
    total_shares  REAL,
    close_price   REAL,
    change_rate   REAL,
    source        TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    resp_sha256   TEXT NOT NULL,
    cache_key     TEXT,
    PRIMARY KEY (code, date)
)""",
}

#: 新 shape 的判定列：存在它即视为「已经是新 shape」，跳过迁移。
_MIGRATE_P28_MARKER = {"money_flow_daily": "resp_sha256",
                       "valuation_daily": "resp_sha256"}


def _table_columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()[0] > 0


def migrate_p28_valuation_moneyflow(conn) -> list[str]:
    """把空的旧-shape 估值/资金流表前滚到新 shape，返回被重建的表名列表。

    有数据的老表**不迁移**（append-only，历史不可丢），直接抛错 —— 让调用方停下来
    决定，而不是静默删数据。触发器在 `init_db` 的 executescript 里已重建（CREATE
    TRIGGER IF NOT EXISTS 幂等），故这里只负责表结构。
    """
    rebuilt: list[str] = []
    for table, ddl in _MIGRATE_P28_DDL.items():
        cols = _table_columns(conn, table)
        if _MIGRATE_P28_MARKER[table] in cols:
            continue                              # 已是新 shape
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if n:
            raise RuntimeError(
                f"{table} 是老 shape 且已有 {n} 行数据 —— 拒绝 DROP 重建"
                "（append-only：历史行不可丢）。请人工处理后再前滚。"
            )
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(ddl)
        rebuilt.append(table)
    return rebuilt


#: P32 迁移的判定列：存在它即视为「已有 origin 列」，跳过。
_MIGRATE_P32_MARKER = "origin"

#: 追加的列定义。与 schema.sql 里 predictions 的 `origin` 列定义**同文**
#: （改一处须同步另一处）。ALTER TABLE ADD COLUMN 追加到表尾，故 schema.sql 里
#: 也把 origin 放在 created_at 之后，保证新库/老库列序一致。
_MIGRATE_P32_ADD_COLUMN = (
    "ALTER TABLE predictions ADD COLUMN "
    "origin TEXT CHECK (origin IN ('live','replay'))"
)


def migrate_p32_predictions_origin(conn) -> list[str]:
    """给 `predictions` 增 `origin` 列（P32），返回变更列表。

    append-only 兼容：**只加列，不碰任何历史行**。`ALTER TABLE ADD COLUMN` 让所有
    老行 `origin` 为 NULL —— 这正是要的：历史行**不许回填**成 live/replay（那等于
    按推断结果冒充事实）。NULL 通过 `CHECK (origin IN ('live','replay'))`（`NULL IN
    (...)` 判 NULL 而非 FALSE），读取侧对 NULL 退回 `created_at[:10] == asof_date`
    推断（见 `chain.accuracy` / `session.review.classify`）。
    """
    cols = _table_columns(conn, "predictions")
    if _MIGRATE_P32_MARKER in cols:
        return []
    conn.execute(_MIGRATE_P32_ADD_COLUMN)
    return ["predictions.origin"]


# ---------------------------------------------------------------------------
# P37：`paper_accounts.arm` 的 CHECK 要放开到 'agent' / 'agent_random'。
#
# SQLite 改不了 CHECK 约束（只有 ADD COLUMN 是原地的），所以只能**重建表**。
# 而 `paper_accounts` 是 append-only 的，重建必须：
#   ① 在一个事务里做（失败就回滚，绝不留半张表）；
#   ② 逐行搬运且**校验行数**；
#   ③ 把随 DROP 一起消失的两个触发器重新建起来。
# 触发器文本与 schema.sql **同文**（两处都改才算改完）。
# 只动 `arm` 列的允许集合，不动任何历史行的值。
# ---------------------------------------------------------------------------
_MIGRATE_P37_MARKER = "agent_random"

_MIGRATE_P37_DDL = """
CREATE TABLE paper_accounts (
    account_id     TEXT PRIMARY KEY,
    arm            TEXT NOT NULL
                   CHECK (arm IN ('hold', 'now', 'discipline', 'agent', 'agent_random')),
    etf_target_pct REAL,
    start_date     TEXT NOT NULL,
    initial_cash   REAL NOT NULL,
    initial_positions_json TEXT NOT NULL,
    initial_nav    REAL NOT NULL,
    params_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL
)"""

_MIGRATE_P37_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS trg_paper_accounts_no_update"
    " BEFORE UPDATE ON paper_accounts"
    " BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END;\n"
    "CREATE TRIGGER IF NOT EXISTS trg_paper_accounts_no_delete"
    " BEFORE DELETE ON paper_accounts"
    " BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END;"
)

_MIGRATE_P37_COLUMNS = ("account_id", "arm", "etf_target_pct", "start_date",
                        "initial_cash", "initial_positions_json", "initial_nav",
                        "params_json", "created_at")


def paper_accounts_needs_agent_arms(conn) -> bool:
    """该表的 CHECK 还不允许 'agent_random' 吗？（只读探测，供 doctor 用）

    表不存在时返回 False：`executescript` 会按 schema.sql 的新 shape 直接建出，
    没有迁移可做也不该备份。
    """
    if not _table_exists(conn, "paper_accounts"):
        return False
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='paper_accounts'"
    ).fetchone()
    return row is not None and _MIGRATE_P37_MARKER not in (row[0] or "")


def migrate_p37_paper_accounts_agent_arms(conn) -> list[str]:
    """放开 `paper_accounts.arm` 的 CHECK（P37），返回变更列表。

    重建 + 搬行是这里唯一可行的手法（SQLite 不支持改 CHECK），所以它也是本项目里
    唯一会「重写」 append-only 表的迁移。为此：整段在一个事务里、搬完核对行数、
    核对不过就回滚报错，且**一行值都不改**（只换允许集合）。
    """
    if not paper_accounts_needs_agent_arms(conn):
        return []
    cols = ", ".join(_MIGRATE_P37_COLUMNS)
    rows = [tuple(r) for r in conn.execute(
        f"SELECT {cols} FROM paper_accounts ORDER BY account_id")]
    conn.execute("BEGIN")
    try:
        conn.execute("DROP TABLE paper_accounts")
        conn.execute(_MIGRATE_P37_DDL)
        conn.executemany(
            f"INSERT INTO paper_accounts ({cols})"
            f" VALUES ({', '.join('?' * len(_MIGRATE_P37_COLUMNS))})", rows)
        conn.executescript(_MIGRATE_P37_TRIGGERS)
        after = [tuple(r) for r in conn.execute(
            f"SELECT {cols} FROM paper_accounts ORDER BY account_id")]
        if after != rows:
            raise RuntimeError(
                f"paper_accounts 重建后内容不一致（{len(rows)} 行 → {len(after)} 行）"
                f"—— 已回滚，库未被改动")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ["paper_accounts.arm"]


# ---------------------------------------------------------------------------
# P44：`plugin_audit.action` 的 CHECK 要放开到 10 个事件（模块2 状态机）。
#
# 与 P37 同款：SQLite 改不了 CHECK，只能重建表；而 `plugin_audit` 是 append-only
# 的审计链，重建必须在一个事务里、逐行搬运并**校验行数**、把随 DROP 一起消失的
# 两个触发器与索引重新建起来。DDL / 触发器文本与 schema.sql **同文**
# （两处都改才算改完）。**只改允许集合，不动任何历史行的值。**
# ---------------------------------------------------------------------------
_MIGRATE_P44_MARKER = "start_validation"

_MIGRATE_P44_DDL = """
CREATE TABLE plugin_audit (
    audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id  INTEGER NOT NULL,
    action     TEXT NOT NULL CHECK (action IN
                 ('submit','sandbox_pass','sandbox_fail','approve','reject','archive',
                  'start_validation','finish_validation','freeze','unfreeze')),
    actor      TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL
)"""

_MIGRATE_P44_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS trg_plugin_audit_no_update"
    " BEFORE UPDATE ON plugin_audit"
    " BEGIN SELECT RAISE(ABORT, 'plugin_audit is append-only'); END;\n"
    "CREATE TRIGGER IF NOT EXISTS trg_plugin_audit_no_delete"
    " BEFORE DELETE ON plugin_audit"
    " BEGIN SELECT RAISE(ABORT, 'plugin_audit is append-only'); END;\n"
    "CREATE INDEX IF NOT EXISTS idx_plugin_audit_script"
    " ON plugin_audit (script_id, audit_id);"
)

_MIGRATE_P44_COLUMNS = ("audit_id", "script_id", "action", "actor", "reason",
                        "created_at")


def plugin_audit_needs_module2_events(conn) -> bool:
    """该表的 CHECK 还不能装模块2 的新事件吗？（只读探测，供 doctor 用）

    表不存在时返回 False：`executescript` 会按 schema.sql 的新 shape 直接建出。
    """
    if not _table_exists(conn, "plugin_audit"):
        return False
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='plugin_audit'"
    ).fetchone()
    return row is not None and _MIGRATE_P44_MARKER not in (row[0] or "")


def migrate_p44_plugin_audit_events(conn) -> list[str]:
    """放开 `plugin_audit.action` 的 CHECK（P44），返回变更列表。

    审计链重建是本项目里与 P37 并列的「重写 append-only 表」场景，纪律相同：
    整段在一个事务里、搬完核对内容逐行相等、核对不过就回滚报错，**一行值都不改**。
    """
    if not plugin_audit_needs_module2_events(conn):
        return []
    cols = ", ".join(_MIGRATE_P44_COLUMNS)
    rows = [tuple(r) for r in conn.execute(
        f"SELECT {cols} FROM plugin_audit ORDER BY audit_id")]
    conn.execute("BEGIN")
    try:
        conn.execute("DROP TABLE plugin_audit")
        conn.execute(_MIGRATE_P44_DDL)
        conn.executemany(
            f"INSERT INTO plugin_audit ({cols})"
            f" VALUES ({', '.join('?' * len(_MIGRATE_P44_COLUMNS))})", rows)
        conn.executescript(_MIGRATE_P44_TRIGGERS)
        after = [tuple(r) for r in conn.execute(
            f"SELECT {cols} FROM plugin_audit ORDER BY audit_id")]
        if after != rows:
            raise RuntimeError(
                f"plugin_audit 重建后内容不一致（{len(rows)} 行 → {len(after)} 行）"
                "—— 已回滚，库未被改动")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ["plugin_audit.action"]


# ---------------------------------------------------------------------------
# P52：`paper_agent_decisions` 加三列（操盘决策，D-34）。
#
# **只加列、不回填历史行** —— 与 P32 的 `predictions.origin` 同一手法。
# 这里不重建表：CHECK 是**列级**的，`ALTER TABLE ADD COLUMN` 能带上它
# （实测 SQLite 3.51 会把新列的 CHECK 一起建出来），因此历史行的一个字节都不用碰。
# 新列的默认值让 P37 的历史行自解释：它们全都是 `decision_kind='spec'`。
# ---------------------------------------------------------------------------
_MIGRATE_P52_COLUMNS: tuple[tuple[str, str], ...] = (
    ("decision_kind",
     "ALTER TABLE paper_agent_decisions ADD COLUMN decision_kind TEXT NOT NULL"
     " DEFAULT 'spec' CHECK (decision_kind IN ('spec','portfolio'))"),
    ("payload_json",
     "ALTER TABLE paper_agent_decisions ADD COLUMN payload_json TEXT NOT NULL"
     " DEFAULT '{}'"),
    ("pool_json",
     "ALTER TABLE paper_agent_decisions ADD COLUMN pool_json TEXT NOT NULL"
     " DEFAULT '{}'"),
)


def agent_decisions_need_portfolio_columns(conn) -> bool:
    """台账表还缺 P52 的列吗？（只读探测，供 doctor 用）

    表不存在时返回 False：`executescript` 会按 schema.sql 的新 shape 直接建出。
    """
    if not _table_exists(conn, "paper_agent_decisions"):
        return False
    cols = _table_columns(conn, "paper_agent_decisions")
    return any(name not in cols for name, _ in _MIGRATE_P52_COLUMNS)


def migrate_p52_agent_decisions_portfolio(conn) -> list[str]:
    """给决策台账加操盘载荷三列（只加列，不改历史行）。返回变更列表。"""
    if not agent_decisions_need_portfolio_columns(conn):
        return []
    cols = _table_columns(conn, "paper_agent_decisions")
    changed: list[str] = []
    conn.execute("BEGIN")
    try:
        for name, ddl in _MIGRATE_P52_COLUMNS:
            if name in cols:
                continue
            conn.execute(ddl)
            changed.append(f"paper_agent_decisions.{name}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return changed


# ---------------------------------------------------------------------------
# P53：把东财 `INDUSTRY_NAME` 回填进 `instruments.sector`。
#
# 背景：`build_ctx` 早已把 `instruments.sector` 喂给插桩0（评审 A7），但这一列
# **从来没人写过** —— 实测真库 21/21 全为 NULL，于是 `ctx["sector"]` 恒为 None，
# 行业判定永远退化成「按标的名称猜关键词」。采到的行业数据躺在
# `financial_reports.industry_name` 里没用上。
#
# 这是**数据回填**，不是结构迁移：`instruments` 没有 append-only 触发器
# （评审 A7 已确认），但改的是会进 `ctx`、进而进分数的列，所以必须**留痕** ——
# 每次回填写一条 `system_events`，记下改了哪些 code、从什么值改成什么值，
# 以便将来回答「这个 sector 是什么时候写进去的」。
#
# ⚠️ **不注册进 `_KNOWN_MARKERS`**：那份清单是「既有表缺列」的结构 marker，
# 由 `_pending_column_migrations`（只读探测）驱动。这里是补数据，不是补列，
# 塞进去会让 doctor 把「已回填」误报成结构迁移状态。
# ---------------------------------------------------------------------------
_SECTOR_BACKFILL_SQL = """
SELECT f.code, f.industry_name
FROM financial_reports f
JOIN (
    SELECT code, MAX(report_date) AS rd
    FROM financial_reports
    WHERE industry_name IS NOT NULL AND industry_name <> ''
    GROUP BY code
) latest ON latest.code = f.code AND latest.rd = f.report_date
WHERE f.industry_name IS NOT NULL AND f.industry_name <> ''
"""


def instruments_need_sector_backfill(conn) -> list[str]:
    """只读探测：哪些 `instruments.sector` 为空且库里有行业名可回填。

    供 doctor / 迁移前预检用，**不写任何东西**。
    """
    if not (_table_exists(conn, "instruments")
            and _table_exists(conn, "financial_reports")):
        return []
    known = {code: name for code, name in conn.execute(_SECTOR_BACKFILL_SQL)}
    rows = conn.execute("SELECT code, sector FROM instruments")
    return sorted(code for code, sector in rows
                  if code in known and not (sector or "").strip())


def migrate_instruments_sector(conn, *, now: str | None = None) -> list[str]:
    """按 `financial_reports.industry_name`（每 code 取最新报告期）**补空**的
    `instruments.sector`。返回 `["<code>: None -> '<new>', ...]`。

    - **只补空**：已有非空值的行**一律不动**（人工值、或上一轮回填的值）。
      回填的语义是「填缺口」，不是「刷新」—— 静默覆盖既有值等于用今天的
      口径改写当时的事实（与 `predictions.origin` 不许回填同一条纪律）。
    - **幂等**：补过一次后再调用返回 `[]`。
    - **留痕**：有变更时写一条 `system_events`（`module='migrate'`），
      含全部变更明细 —— 这一列会进 `ctx`、进插桩0 的判据，必须可追溯。
    - 同一 `report_date` 若命中多个行业名 → 取字典序最小者，保证**可复现**。
    """
    if not (_table_exists(conn, "instruments")
            and _table_exists(conn, "financial_reports")):
        return []
    latest: dict[str, str] = {}
    for code, name in conn.execute(_SECTOR_BACKFILL_SQL):
        cur = latest.get(code)
        if cur is None or name < cur:
            latest[code] = name

    changed: list[str] = []
    for code, sector in conn.execute("SELECT code, sector FROM instruments"):
        if (sector or "").strip():
            continue                      # 只补空，不动既有值
        want = latest.get(code)
        if want is None:
            continue                      # 没有行业名可补（如 ETF）
        changed.append(f"{code}: None -> {want!r}")
    if not changed:
        return []

    conn.execute("BEGIN")
    try:
        for item in changed:
            code = item.split(":", 1)[0]
            conn.execute("UPDATE instruments SET sector = ? WHERE code = ?",
                         (latest[code], code))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    from stocklab.store import repo
    repo.log_event(
        conn, "migrate", "info",
        f"p53: 回填 instruments.sector {len(changed)} 行"
        "（来源 financial_reports.industry_name，东财二级行业名；只补空）",
        context={"changes": changed}, now=now)
    return changed



# ---------------------------------------------------------------------------
# P56：给 AI 操盘手家族的账户补上 `params.executor = 'agent_decision'`（D-50）。
#
# 背景：P56 把 AI 臂的日终认领权从 `paper step` 划给 `paper agent run`。
# 认领关系是**账户行里的一个字段**（`params_json.executor`），而 P52 建这两条
# 账户时还没有这个键 —— 于是老库上它们会被 `paper step` 悄悄认领（写一条没有
# 成交的净值），那条臂就永远不下单，而症状会出现在净值表上。
#
# ⚠️ 这是一次**数据**迁移（不是加列），而 `paper_accounts` 上有 append-only
# 触发器（`trg_paper_accounts_no_update`）。做法与 P37 的处理同源：在一个事务里
# 暂时摘掉那道触发器、逐行**只加一个键**、再把触发器原样建回来，并核对
# 「行数不变 + 除新键外逐字段不变」，任何一步不符就回滚。
# 触发器 DDL 与 `schema.sql` **同文**（改一处须同步另一处）。
#
# 判定按 `paper_accounts.arm`（`agent` / `agent_random`）而**不是**按账户名前缀：
# `arm-agent-random` 与通路 A 的 `arm-agent-<版本>` 同前缀，靠名字猜迟早猜错
# ——「谁落这一天」本来就是一个显式字段。
# ---------------------------------------------------------------------------

_AGENT_ARM_KINDS: tuple[str, ...] = ("agent", "agent_random")
_P56_EXECUTOR_VALUE = "agent_decision"
_P56_EXECUTOR_KEY = "executor"


def agent_arms_need_executor(conn) -> list[str]:
    """只读探测：哪些 AI 操盘手账户还没有 `params.executor`。**不写任何东西**。

    表不存在 → `[]`（`executescript` 会按 schema.sql 直接建出新 shape）。
    """
    if not _table_exists(conn, "paper_accounts"):
        return []
    out: list[str] = []
    for row in conn.execute(
            "SELECT account_id, arm, params_json FROM paper_accounts ORDER BY account_id"):
        if str(row["arm"]) not in _AGENT_ARM_KINDS:
            continue
        try:
            params = json.loads(row["params_json"] or "{}")
        except ValueError:
            params = None
        if not isinstance(params, dict) or not params.get(_P56_EXECUTOR_KEY):
            out.append(str(row["account_id"]))
    return out


def migrate_p56_agent_arms_executor(conn) -> list[str]:
    """给 `arm in (agent, agent_random)` 的账户补 `executor='agent_decision'`。

    **可重入**：跑第二次返回 `[]`（键已在位即跳过）。
    **只加一个键**：其余键值逐字不变，行数不变；不符就回滚报错。
    返回 `["<account_id>: executor -> 'agent_decision'", ...]`。
    """
    targets = agent_arms_need_executor(conn)
    if not targets:
        return []
    rows = [dict(r) for r in conn.execute(
        "SELECT account_id, arm, params_json FROM paper_accounts ORDER BY account_id")]
    before = {str(r["account_id"]): str(r["params_json"]) for r in rows}
    n_before = len(rows)
    conn.execute("BEGIN")
    try:
        conn.execute("DROP TRIGGER IF EXISTS trg_paper_accounts_no_update")
        for r in rows:
            if str(r["account_id"]) not in targets:
                continue
            params = json.loads(r["params_json"] or "{}")
            params[_P56_EXECUTOR_KEY] = _P56_EXECUTOR_VALUE
            conn.execute("UPDATE paper_accounts SET params_json = ? WHERE account_id = ?",
                         (json.dumps(params, ensure_ascii=False, sort_keys=True),
                          str(r["account_id"])))
        conn.execute(_P56_TRIGGER_NO_UPDATE)
        after = [dict(r) for r in conn.execute(
            "SELECT account_id, arm, params_json FROM paper_accounts ORDER BY account_id")]
        if len(after) != n_before:
            raise RuntimeError(
                f"paper_accounts 行数变了（{n_before} → {len(after)}）—— 已回滚")
        for r in after:
            aid = str(r["account_id"])
            if aid not in targets:
                if str(r["params_json"]) != before[aid]:
                    raise RuntimeError(
                        f"迁移动了不该动的账户 {aid} —— 已回滚（只许补 executor 键）")
                continue
            old = json.loads(before[aid] or "{}")
            new = json.loads(r["params_json"] or "{}")
            old.pop(_P56_EXECUTOR_KEY, None)
            if new.pop(_P56_EXECUTOR_KEY, None) != _P56_EXECUTOR_VALUE:
                raise RuntimeError(f"账户 {aid} 的 executor 没写对 —— 已回滚")
            if old != new:
                raise RuntimeError(
                    f"账户 {aid} 除 executor 外的键值被改动了 —— 已回滚")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return [f"{aid}: {_P56_EXECUTOR_KEY} -> {_P56_EXECUTOR_VALUE!r}"
            for aid in targets]


#: 与 `schema.sql` 的 `trg_paper_accounts_no_update` **同文**（改一处须同步两处）。
_P56_TRIGGER_NO_UPDATE = (
    "CREATE TRIGGER IF NOT EXISTS trg_paper_accounts_no_update"
    " BEFORE UPDATE ON paper_accounts"
    " BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END"
)


# ---------------------------------------------------------------------------
# P69：把两条臂**显式停飞**（`params.live = false`）。
#
# 为什么是**逐条点名**而不是按 kind 批量：停飞是**每一条臂的账** —— 「这条臂还有没有
# 产出决策的通路」。`arm-agent` 是 P52 的占位臂（无预注册 ⇒ `paper agent decide`
# 按定义拒收它 ⇒ 永远不会有决策），`arm-agent-ds-v1` 的提示词 v1 有整手 bug
# （四个买入权重折成 99.x 股 ⇒ 一手都没买到），已被 v2 取代（D-48：保留不删）。
# 两条都**不再认领日终**，但历史台账与净值行一行不动。
#
# 只加一个键（`live`），其余键值逐字不变；可重入；行数与「没被点名的账户」都核对。

_P69_LIVE_KEY = "live"

#: 要停飞的两条臂（P69 §T2 拍板点 2 / 3）。**必须是显式名字** ——
#: 这里多一条就少一条在飞的臂，改这份清单要同时改任务书。
_P69_HALTED_ARMS: tuple[str, ...] = ("arm-agent", "arm-agent-ds-v1")


def agent_arms_need_p69_live(conn) -> list[str]:
    """只读探测：哪些**点名要停飞的**臂还没有 `params.live == false`。**不写任何东西**。

    表不存在 → `[]`。账户不在库里 → 跳过（这条臂还没建出来，没什么可停的）。
    """
    if not _table_exists(conn, "paper_accounts"):
        return []
    out: list[str] = []
    for row in conn.execute(
            "SELECT account_id, params_json FROM paper_accounts ORDER BY account_id"):
        aid = str(row["account_id"])
        if aid not in _P69_HALTED_ARMS:
            continue
        try:
            params = json.loads(row["params_json"] or "{}")
        except ValueError:
            params = None
        if not isinstance(params, dict) or params.get(_P69_LIVE_KEY) is not False:
            out.append(aid)
    return out


def migrate_p69_agent_arms_live(conn) -> list[str]:
    """给点名那两条臂补 `params.live = false`（P69 / T2）。

    **可重入**：跑第二次返回 `[]`（键已在位即跳过）。
    **只加一个键**：其余键值逐字不变，行数不变，别的账户逐字节不变；
    不符就回滚报错。返回 `["<account_id>: live -> False", ...]`。
    """
    targets = agent_arms_need_p69_live(conn)
    if not targets:
        return []
    rows = [dict(r) for r in conn.execute(
        "SELECT account_id, arm, params_json FROM paper_accounts ORDER BY account_id")]
    before = {str(r["account_id"]): str(r["params_json"]) for r in rows}
    n_before = len(rows)
    conn.execute("BEGIN")
    try:
        conn.execute("DROP TRIGGER IF EXISTS trg_paper_accounts_no_update")
        for r in rows:
            if str(r["account_id"]) not in targets:
                continue
            params = json.loads(r["params_json"] or "{}")
            params[_P69_LIVE_KEY] = False
            conn.execute("UPDATE paper_accounts SET params_json = ? WHERE account_id = ?",
                         (json.dumps(params, ensure_ascii=False, sort_keys=True),
                          str(r["account_id"])))
        conn.execute(_P56_TRIGGER_NO_UPDATE)
        after = [dict(r) for r in conn.execute(
            "SELECT account_id, arm, params_json FROM paper_accounts ORDER BY account_id")]
        if len(after) != n_before:
            raise RuntimeError(
                f"paper_accounts 行数变了（{n_before} → {len(after)}）—— 已回滚")
        for r in after:
            aid = str(r["account_id"])
            if aid not in targets:
                if str(r["params_json"]) != before[aid]:
                    raise RuntimeError(
                        f"迁移动了不该动的账户 {aid} —— 已回滚（只许给点名的两条加 live 键）")
                continue
            old = json.loads(before[aid] or "{}")
            new = json.loads(r["params_json"] or "{}")
            old.pop(_P69_LIVE_KEY, None)
            if new.pop(_P69_LIVE_KEY, None) is not False:
                raise RuntimeError(f"账户 {aid} 的 live 没写成 false —— 已回滚")
            if old != new:
                raise RuntimeError(
                    f"账户 {aid} 除 live 外的键值被改动了 —— 已回滚")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return [f"{aid}: {_P69_LIVE_KEY} -> False" for aid in targets]


# ---------------------------------------------------------------------------
# P58：插桩5 复盘台账 `plugin_reviews`（**新表**）。
#
# 新表**不需要数据迁移**：`schema.sql` 的 `CREATE TABLE IF NOT EXISTS` 会在下一次
# `init_db` / `ensure_schema` 时把它建出来 —— 老库与新库走的是同一条路径，所以
# 「对老库是空动作」这句话在这里是**字面成立**的（表不存在 ⇒ 不 pending ⇒ 不备份
# ⇒ executescript 直接建）。
#
# 本函数只在**结构漂移**时动手：表在、但 append-only 触发器不见了（被 DROP 过 /
# 有人手工建过同名表）。那种库看着「有这张表」，实际可以改历史行 —— 必须前滚。
#
# 判据刻意**不含「表不存在」**：既有约定是「表不存在 ⇒ 按新 shape 建出，无需迁移、
# 无需备份，不算 pending」（见 `_pending_column_migrations` 的 docstring）。把「表
# 不存在」也算成 pending，会让每一次写库入口都给老库做一次备份。
#
# 触发器文本与 `schema.sql` **同文**（改一处须同步两处）。
# ---------------------------------------------------------------------------

_P58_TABLE = "plugin_reviews"

_P58_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS trg_plugin_reviews_no_update"
    " BEFORE UPDATE ON plugin_reviews"
    " BEGIN SELECT RAISE(ABORT, 'plugin_reviews is append-only'); END;\n"
    "CREATE TRIGGER IF NOT EXISTS trg_plugin_reviews_no_delete"
    " BEFORE DELETE ON plugin_reviews"
    " BEGIN SELECT RAISE(ABORT, 'plugin_reviews is append-only'); END;"
)

_P58_TRIGGER_NAMES = ("trg_plugin_reviews_no_update", "trg_plugin_reviews_no_delete")


def _trigger_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,)).fetchone()[0] > 0


def plugin_reviews_needs_p58(conn) -> bool:
    """复盘台账在、但 append-only 触发器缺席吗？（只读探测，供 doctor 用）

    表**不存在**时返回 False —— 那不是「待迁移」，是「等着被建出来」。
    """
    if not _table_exists(conn, _P58_TABLE):
        return False
    return any(not _trigger_exists(conn, name) for name in _P58_TRIGGER_NAMES)


def migrate_p58_plugin_reviews(conn) -> list[str]:
    """补回复盘台账的 append-only 触发器（P58）。**可重入**、老库上零动作。"""
    if not plugin_reviews_needs_p58(conn):
        return []
    conn.executescript(_P58_TRIGGERS)
    return ["plugin_reviews.triggers"]


# ---------------------------------------------------------------------------
# P62：AI 臂**未结算**的净值行 —— 数据修复（不是结构迁移）。
#
# 缺陷是怎么产生的：`paper/agent_decide.py::execute_decision` 只生成订单、原样
# 返回入参状态，而 `engine._step_all` 拿着返回值直接写净值 ⇒ 净值行记的是
# **执行前**的现金（P62 任务书 §1/§2）。代码已修（同站的另一半），但真库里
# 那几行错数据还在。
#
# 为什么这里**只删不写**：`paper_nav_daily` 是 append-only（`no_update` /
# `no_delete`），改写历史行的值等于伪造账目。正确值**可以由代码重算**（修好后
# 的 `execute_decision` 会结算）⇒ 删掉点名的那一行，再由
# `paper agent run --asof <day>` 按同一个 `_step_all` 重写。成交（`paper_trades`）
# **一行都不删**：那一笔是真的。
#
# ## 为什么这个迁移**不**登记进 `_pending_column_migrations` / `_apply_schema`
#
# 其余迁移都是「结构前滚」，跑多少次都不动一行数据；本条**删数据行**。把它挂到
# 「任一写库入口都会跑」的位置，等于让某次 `paper predict` 顺手删历史净值 ——
# 而「删了此行」这件事只能由人对着探测清单拍板。所以：**显式调用**。
# 只登记进 `_KNOWN_MARKERS`（doctor 只读报告「还有没有未结算的 AI 臂净值行」）。
#
# ## 只删点名行
#
# 探测（`p62_defective_agent_nav_rows`）是**全表**扫自身成交驱动的臂。若缺陷行
# 里出现了点名清单之外的行 ⇒ 抛错拒绝，绝不「顺手一起修」。
# ---------------------------------------------------------------------------

_P62_NAMED_ROWS: tuple[tuple[str, str], ...] = (("arm-agent-ds-v1", "2026-09-23"),)
#: 「对得上」的容差：净值是「重放 + 收盘价」算出来的浮点数，逐位比较没有意义。
_P62_TOL = 1e-6
#: 「未结算」的现金判据（元）：P62 的签名差是**成交金额量级**（真库 ¥8,233.68 /
#: 一条 100 股卖单），而分币级漂移每笔 ≤ ¥0.005 ⇒ 取 ¥1 一刀切开两者，既不会被
#: 舍入噪声误触（10 笔/天也只到 ¥0.05），也不会放过任何「结算漏了一笔」的账。
#: 持仓**集合**不等则是精确判据（真库那行的 `{"000333": 0}` vs 空仓）。
_P62_SETTLEMENT_BAR = 1.0
#: 与 `schema.sql` 的触发器**同文**（改一处须同步两处）。
_P62_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS trg_paper_nav_daily_no_update"
    " BEFORE UPDATE ON paper_nav_daily"
    " BEGIN SELECT RAISE(ABORT, 'paper_nav_daily is append-only'); END;\n"
    "CREATE TRIGGER IF NOT EXISTS trg_paper_nav_daily_no_delete"
    " BEFORE DELETE ON paper_nav_daily"
    " BEGIN SELECT RAISE(ABORT, 'paper_nav_daily is append-only'); END;"
)

_P62_TRIGGER_NAMES = ("trg_paper_nav_daily_no_update", "trg_paper_nav_daily_no_delete")


def p62_defective_agent_nav_rows(conn) -> dict:
    """只读探测：哪些净值行与「重放 + mark-to-market」对不上（P62 §3.2 的不变量）。

    判据：`paper_nav_daily` 的一行必须等于
    **`initial_cash` + 全部 `<= asof` 的自身成交按 `_fee_parts` 口径重放
    + 当日收盘 mark-to-market**。两半都用既有实现（`engine.arm_state_for` →
    内部就是 `_ledger_arm_state`，市值走 `engine.mark_to_market`）——
    这里不另算一遍净值，否则「判据」与「被测对象」会一起漂。

    ## 三分类（只有第一类归 P62）

    - `defective`（**未结算**，P62 的靶子）：持仓集合与重放不同，或现金差
      **超过 ¥1.00**（`_P62_SETTLEMENT_BAR`）。「现金是执行前的 + 持仓是执行后的」
      那种混合状态落这里。
    - `cent_drift`：现金与重放差 **≤ ¥0.01** —— `Decision.amount` 先四舍五入
      到分、重放不四舍五入造成的**既有**分币级漂移（真库上落在
      `arm-discipline-*`）。**只报告，不动**（改它 = 改主干口径，另一件事）。
    - `valuation`：现金与持仓都与重放一致，只有净值差 ⇒ 市值口径/价格源在
      写入之后漂移过（真库 2026-09-22 那三行，差 ¥1.70–4.60）。**只报告，不动**。

    **范围**：只判「自身成交驱动」的臂（`engine.replays_own_trades`）。`arm-hold`
    是冻结快照、`arm-now` 读实盘账本，它们的净值本来就不由自身成交推出 ——
    它们进 `skipped` 并**写明原因**（「判不了」与「判过」必须能分开）。

    重放不出来（持仓缺价、账户不在）→ 进 `skipped`，**不算合格**。
    """
    from stocklab.paper import engine          # 懒 import：避免 store/paper 成环

    empty = {"checked": 0, "defective": [], "cent_drift": [], "valuation": [],
             "skipped": [], "named": [f"{a}/{d}" for a, d in _P62_NAMED_ROWS],
             "note": "表不存在 ⇒ 没比对过，不是「全部干净」"}
    if not (_table_exists(conn, "paper_nav_daily")
            and _table_exists(conn, "paper_accounts")):
        return empty
    accounts = {str(r["account_id"]): dict(r) for r in
                conn.execute("SELECT * FROM paper_accounts")}
    checked = 0
    defective: list[dict] = []
    cent: list[dict] = []
    valuation: list[dict] = []
    skipped: list[dict] = []
    for r in conn.execute("SELECT * FROM paper_nav_daily ORDER BY account_id, date"):
        row = dict(r)
        aid, date = str(row["account_id"]), str(row["date"])
        account = accounts.get(aid)
        if account is None:
            skipped.append({"account_id": aid, "date": date,
                            "reason": "净值行没有对应的账户行 —— 重放没有起点"})
            continue
        if not engine.replays_own_trades(account):
            skipped.append({"account_id": aid, "date": date,
                            "reason": f"臂 {account['arm']!r} 的状态不由自身成交推出"
                                      f"（arm-hold 冻结快照 / arm-now 实盘账本）"
                                      f"—— 不在本判据范围内"})
            continue
        try:
            state = engine.arm_state_for(conn, aid, date)
        except Exception as exc:                       # noqa: BLE001 —— 判不了就报出来
            skipped.append({"account_id": aid, "date": date,
                            "reason": f"重放失败（{type(exc).__name__}: {exc}）"
                                      f"—— 判不了 ≠ 判过"})
            continue
        if state is None:
            skipped.append({"account_id": aid, "date": date,
                            "reason": "账户不存在 —— 重放没有起点"})
            continue
        expected_cash = round(float(state["cash"]), 4)
        expected_pos = {str(c): int(q) for c, q in state["positions"].items()}
        expected_nav = round(expected_cash + float(state["market_value"]), 4)
        got_pos = {str(p["code"]): int(p["qty"])
                   for p in json.loads(row["positions_json"] or "[]")}
        dcash = float(row["cash"]) - expected_cash
        dnav = float(row["nav"]) - expected_nav
        checked += 1
        item = {"account_id": aid, "date": date,
                "d_cash": round(dcash, 4), "d_nav": round(dnav, 4),
                "named": (aid, date) in _P62_NAMED_ROWS}
        if got_pos != expected_pos or abs(dcash) > _P62_SETTLEMENT_BAR:
            item["diffs"] = {
                "cash": {"got": float(row["cash"]), "replay": expected_cash},
                "positions_json": {"got": got_pos, "replay": expected_pos},
                "nav": {"got": float(row["nav"]), "replay": expected_nav}}
            defective.append(item)
        elif abs(dcash) > _P62_TOL:
            item["note"] = ("现金与重放差 ≤ ¥0.01：`Decision.amount` 先舍到分、重放不舍"
                            "—— **既有**的分币级口径差，不属 P62（只报告）")
            cent.append(item)
        elif abs(dnav) > _P62_TOL:
            item["note"] = ("现金与持仓都与重放一致、只有净值差 ⇒ 写入之后市值口径/"
                            "价格源漂移过 —— 不属 P62（只报告）")
            valuation.append(item)
    return {
        "checked": checked, "defective": defective, "cent_drift": cent,
        "valuation": valuation, "skipped": skipped,
        "named": [f"{a}/{d}" for a, d in _P62_NAMED_ROWS],
        "note": ("零缺陷与零比对是两件事：`checked` 为 0 时说明**没有比对过**，"
                 "不是「全部干净」"),
    }


def migrate_p62_agent_nav_settlement(conn) -> list[str]:
    """删掉**点名的那一行**未结算的 AI 臂净值行，等 `paper agent run` 重写。

    **可重入**：第二次跑返回 `[]`（那一行已经不在了 ⇒ 探测不到 ⇒ 无事可做）。
    **只删点名行**：探测出的**未结算**行里有点名清单之外的 ⇒ **抛错拒绝**（人来
    拍板）。分币级漂移 / 估值漂移两类**只报告不动**（它们不属 P62，见探测器 docstring）。
    事务内摘/挂触发器（照 `migrate_p56_agent_arms_executor` 的先例）；其余行逐字节
    不变、行数只少点名的那几行；事后触发器仍在。台账记进 `system_events`。
    """
    probe = p62_defective_agent_nav_rows(conn)
    named = set(_P62_NAMED_ROWS)
    outsiders = [d for d in probe["defective"]
                 if (d["account_id"], d["date"]) not in named]
    if outsiders:
        raise RuntimeError(
            "探测到**点名清单之外**的未结算净值行：" +
            "、".join(f"{d['account_id']}/{d['date']}" for d in outsiders) +
            f"（点名清单：{sorted(named)}）—— 本迁移只删点名行，"
            f"其余的一行都不动。要先拍板：是补进清单，还是另有原因。**未做任何改动**")
    targets = [d for d in probe["defective"]
               if (d["account_id"], d["date"]) in named]
    # 另外两类**不是本站的靶子**，但必须让人看见（否则「零改动」会被读成「全干净」）。
    report_only = [f"⚠️ 只报告（不属 P62，未改动）：{d['account_id']}/{d['date']} "
                   f"class=cent_drift d_cash={d['d_cash']} d_nav={d['d_nav']}"
                   for d in probe["cent_drift"]] + [
        f"⚠️ 只报告（不属 P62，未改动）：{d['account_id']}/{d['date']} "
        f"class=valuation d_cash={d['d_cash']} d_nav={d['d_nav']}"
        for d in probe["valuation"]]
    if not targets:
        return report_only

    before = {(_key(r)): dict(r) for r in
              conn.execute("SELECT * FROM paper_nav_daily")}
    n_before = len(before)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    changes: list[str] = []
    conn.execute("BEGIN")
    try:
        for name in _P62_TRIGGER_NAMES:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for d in targets:
            row = before[(d["account_id"], d["date"])]
            conn.execute("DELETE FROM paper_nav_daily WHERE account_id = ? AND date = ?",
                         (d["account_id"], d["date"]))
            conn.execute(
                "INSERT INTO system_events (ts, module, level, message, context_json)"
                " VALUES (?,?,?,?,?)",
                (now, "paper_nav_daily", "warn",
                 f"P62 修复：删除未结算的净值行 {d['account_id']}/{d['date']}"
                 f"（旧 nav={row['nav']}、cash={row['cash']}、"
                 f"positions={row['positions_json']}）—— 待 `paper agent run --asof "
                 f"{d['date']}` 按修好的 execute_decision 重写",
                 json.dumps({"task": "P62", "account_id": d["account_id"],
                             "date": d["date"], "old": {
                                 k: row[k] for k in
                                 ("cash", "positions_json", "market_value", "nav",
                                  "cum_cost", "cum_return")},
                             "diff": d["diffs"],
                             "basis": "docs/tasks/2026-09-23-p62-AI臂净值未结算修复.md"},
                            ensure_ascii=False, sort_keys=True)))
            changes.append(f"{d['account_id']}/{d['date']}: 删除未结算净值行"
                           f"（nav {row['nav']} → 待重写）")
        conn.executescript(_P62_TRIGGERS)
        after = {(_key(r)): dict(r) for r in
                 conn.execute("SELECT * FROM paper_nav_daily")}
        gone = {k for k in before if k not in after}
        if gone != {(d["account_id"], d["date"]) for d in targets}:
            raise RuntimeError(f"删掉的行不是点名的那几行：{sorted(gone)} —— 已回滚")
        if len(after) != n_before - len(targets):
            raise RuntimeError(f"净值行数 {n_before} → {len(after)}，"
                               f"与「只少 {len(targets)} 行」不符 —— 已回滚")
        for k, row in after.items():
            if row != before[k]:
                raise RuntimeError(f"迁移动了不该动的净值行 {k[0]}/{k[1]} —— 已回滚")
        missing_trg = [n for n in _P62_TRIGGER_NAMES if not _trigger_exists(conn, n)]
        if missing_trg:
            raise RuntimeError(f"append-only 触发器没挂回去：{missing_trg} —— 已回滚")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return [*changes, *report_only]


def _key(row) -> tuple[str, str]:
    return (str(row["account_id"]), str(row["date"]))


#: 已知迁移 marker 清单：doctor 逐个报告在位与否（只读，不迁移）。#: (name, table, 判据)。判据是列名（str）或一个只读探测函数。
#: 新增迁移时必须在这里登记，否则 doctor 看不出来。
_KNOWN_MARKERS: list[tuple[str, str, object]] = [
    ("p28_resp_sha256_valuation", "valuation_daily", "resp_sha256"),
    ("p28_resp_sha256_moneyflow", "money_flow_daily", "resp_sha256"),
    ("p32_predictions_origin", "predictions", "origin"),
    ("p37_paper_accounts_agent_arms", "paper_accounts",
     lambda conn: not paper_accounts_needs_agent_arms(conn)),
    ("p44_plugin_audit_events", "plugin_audit",
     lambda conn: not plugin_audit_needs_module2_events(conn)),
    ("p52_agent_decisions_portfolio", "paper_agent_decisions",
     lambda conn: not agent_decisions_need_portfolio_columns(conn)),
    ("p56_agent_arms_executor", "paper_accounts",
     lambda conn: not agent_arms_need_executor(conn)),
    ("p58_plugin_reviews", "plugin_reviews",
     lambda conn: not plugin_reviews_needs_p58(conn)),
    # P62 是**数据**修复（删未结算的净值行），不挂 `_pending_column_migrations` /
    # `_apply_schema` —— 挂上去等于让任一写库入口顺手删历史净值。这里只让 doctor
    # 只读报告「还有没有与重放对不上的 AI 臂净值行」。
    ("p62_agent_nav_settled", "paper_nav_daily",
     lambda conn: not p62_defective_agent_nav_rows(conn)["defective"]),
    # P69 是**显式停飞**（数据变更），与 P62 同理**不挂** `_pending_column_migrations`
    # / `_apply_schema`：挂上去等于让任一写库入口（`paper init` / `db init`）
    # 顺手停飞两条臂，而「停飞哪几条」是要拍板的账。这里只让 doctor 只读报告
    # 「点名的那两条臂停飞了没有」。真库落位由 nanobot 显式调
    # `migrate_p69_agent_arms_live`（§5 的备份 → 副本 → 核对纪律）。
    ("p69_agent_arms_live", "paper_accounts",
     lambda conn: not agent_arms_need_p69_live(conn)),
]


def _marker_present(conn, table: str, check: object) -> bool:
    if not _table_exists(conn, table):
        return False
    if callable(check):
        return bool(check(conn))
    return check in _table_columns(conn, table)


def _pending_column_migrations(conn) -> list[str]:
    """只读探测：哪些「改既有表结构」的前滚还没做（不含建新表）。

    只统计**表已存在但缺 marker** 的情况 —— 表不存在时 `executescript` 会按
    schema.sql 的新 shape 直接建出（无需迁移、无需备份），不算 pending。
    """
    pending: list[str] = []
    for table, marker in _MIGRATE_P28_MARKER.items():
        if _table_exists(conn, table) and marker not in _table_columns(conn, table):
            pending.append(f"p28:{table}")
    if (_table_exists(conn, "predictions")
            and _MIGRATE_P32_MARKER not in _table_columns(conn, "predictions")):
        pending.append("p32:predictions.origin")
    if paper_accounts_needs_agent_arms(conn):
        pending.append("p37:paper_accounts.arm")
    if plugin_audit_needs_module2_events(conn):
        pending.append("p44:plugin_audit.action")
    if agent_decisions_need_portfolio_columns(conn):
        pending.append("p52:paper_agent_decisions.portfolio")
    if agent_arms_need_executor(conn):
        pending.append("p56:paper_accounts.params.executor")
    if plugin_reviews_needs_p58(conn):
        pending.append("p58:plugin_reviews.triggers")
    return pending


def schema_status(conn) -> dict:
    """只读报告各已知迁移 marker 在位与否，供 doctor 显式告警（**不迁移**）。

    doctor 是只读入口：发现缺失只报告、让调用方决定（跑任一写库入口或 `db init`
    都会前滚），绝不擅自改库。
    """
    markers = {}
    for name, table, check in _KNOWN_MARKERS:
        markers[name] = {
            "table": table,
            "column": check if isinstance(check, str) else "（结构判据）",
            "present": _marker_present(conn, table, check),
        }
    return {
        "markers": markers,
        "ok": all(m["present"] for m in markers.values()),
        "note": "doctor 只报告、不迁移；缺失请跑任一写库入口或 `db init` 前滚。",
    }


def _apply_schema(conn, sql: str) -> list[str]:
    """executescript 建表 + 历史前滚，返回实际结构变更列表。"""
    conn.executescript(sql)
    changes: list[str] = []
    changes += migrate_p28_valuation_moneyflow(conn)
    changes += migrate_p32_predictions_origin(conn)
    changes += migrate_p37_paper_accounts_agent_arms(conn)
    changes += migrate_p44_plugin_audit_events(conn)
    changes += migrate_p52_agent_decisions_portfolio(conn)
    changes += migrate_p56_agent_arms_executor(conn)
    changes += migrate_p58_plugin_reviews(conn)
    return changes


def _now_tag() -> str:
    return datetime.now(TZ).strftime("%Y%m%dT%H%M%SZ")


def backup_db(db_path: Path, backup_dir: Path, tag: str) -> Path:
    """把现有 DB 复制到 backup_dir。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{db_path.stem}.{tag}.{_now_tag()}.db"
    # 先落盘 WAL 内容，保证备份文件自洽（否则最近事务可能只在 -wal 里）
    src = connect(db_path)
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        src.close()
    shutil.copy2(db_path, dest)
    return dest


def init_db(db_path: Path, *, backup_dir: Path | None = None,
            schema_path: Path | None = None) -> Path | None:
    """建库 / 应用 schema。

    前滚迁移策略（评审 D4）：不做 down migration，
    若 DB 已存在则先备份，再幂等应用 schema。
    返回备份文件路径；首次创建返回 None。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if db_path.exists():
        bdir = backup_dir or (db_path.parent / "backups")
        backup = backup_db(db_path, bdir, "premigrate")

    sql = (schema_path or SCHEMA_SQL).read_text(encoding="utf-8")
    conn = connect(db_path)
    try:
        # P28：空表重定义（老库的 valuation/money_flow 是旧 shape，见模块 docstring）。
        # 必须在 executescript 之后跑：触发器已在上面重建，这里只动表结构。
        # P32：给 predictions 增 origin 列（只加列、不回填历史行）。
        _apply_schema(conn, sql)
        conn.commit()
    finally:
        conn.close()
    return backup


def ensure_schema(db_path: Path | str) -> list[str]:
    """幂等前滚 schema —— 写库入口的统一守护（P33）。

    与 `init_db` 的分工：`init_db` 每次显式调用都先备份（`db init` 的语义是
    「我要动库，留退路」）；`ensure_schema` 是**每次写之前**自动调用的守护，
    必须廉价且幂等 —— 只在「确有结构变更」时备份一次（备份的是变更前那份），
    没有变更则零写、零备份（否则每次 `predict run` 都产一份备份文件）。

    真失败要炸、不许静默：老 shape 表有数据（`migrate_p28` 抛 `RuntimeError`）、
    `ALTER TABLE` 失败、IO 错误，一律向上抛 —— 宁可让写入口停下来报错，
    也不要在坏 schema 上写进口径错误的数据。

    返回本次实际结构变更列表（列级迁移的变更；`executescript` 建新表不计入）。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    conn = connect(db_path)
    try:
        if _pending_column_migrations(conn):
            backup_db(db_path, db_path.parent / "backups", "premigrate")
        changes = _apply_schema(conn, sql)
        conn.commit()
        return changes
    finally:
        conn.close()
