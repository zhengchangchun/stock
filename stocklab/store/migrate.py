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


#: 已知迁移 marker 清单：doctor 逐个报告在位与否（只读，不迁移）。
#: (name, table, 判据)。判据是列名（str）或一个只读探测函数。
#: 新增迁移时必须在这里登记，否则 doctor 看不出来。
_KNOWN_MARKERS: list[tuple[str, str, object]] = [
    ("p28_resp_sha256_valuation", "valuation_daily", "resp_sha256"),
    ("p28_resp_sha256_moneyflow", "money_flow_daily", "resp_sha256"),
    ("p32_predictions_origin", "predictions", "origin"),
    ("p37_paper_accounts_agent_arms", "paper_accounts",
     lambda conn: not paper_accounts_needs_agent_arms(conn)),
    ("p44_plugin_audit_events", "plugin_audit",
     lambda conn: not plugin_audit_needs_module2_events(conn)),
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
