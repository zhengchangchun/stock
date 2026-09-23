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
