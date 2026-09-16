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


#: 已知迁移 marker 清单：doctor 逐个报告在位与否（只读，不迁移）。
#: (name, table, column)。新增迁移时必须在这里登记，否则 doctor 看不出来。
_KNOWN_MARKERS: list[tuple[str, str, str]] = [
    ("p28_resp_sha256_valuation", "valuation_daily", "resp_sha256"),
    ("p28_resp_sha256_moneyflow", "money_flow_daily", "resp_sha256"),
    ("p32_predictions_origin", "predictions", "origin"),
]


def _pending_column_migrations(conn) -> list[str]:
    """只读探测：哪些「改既有表结构」的前滚还没做（不含建新表）。

    只统计**表已存在但缺 marker 列**的情况 —— 表不存在时 `executescript` 会按
    schema.sql 的新 shape 直接建出（无需迁移、无需备份），不算 pending。
    """
    pending: list[str] = []
    for table, marker in _MIGRATE_P28_MARKER.items():
        if _table_exists(conn, table) and marker not in _table_columns(conn, table):
            pending.append(f"p28:{table}")
    if (_table_exists(conn, "predictions")
            and _MIGRATE_P32_MARKER not in _table_columns(conn, "predictions")):
        pending.append("p32:predictions.origin")
    return pending


def schema_status(conn) -> dict:
    """只读报告各已知迁移 marker 在位与否，供 doctor 显式告警（**不迁移**）。

    doctor 是只读入口：发现缺失只报告、让调用方决定（跑任一写库入口或 `db init`
    都会前滚），绝不擅自改库。
    """
    markers = {}
    for name, table, column in _KNOWN_MARKERS:
        present = (_table_exists(conn, table)
                   and column in _table_columns(conn, table))
        markers[name] = {"table": table, "column": column, "present": present}
    return {
        "markers": markers,
        "ok": all(m["present"] for m in markers.values()),
        "note": "doctor 只报告、不迁移；缺失请跑任一写库入口或 `db init` 前滚。",
    }


def _apply_schema(conn, sql: str) -> list[str]:
    """executescript 建表 + P28/P32 前滚，返回实际结构变更列表。"""
    conn.executescript(sql)
    changes: list[str] = []
    changes += migrate_p28_valuation_moneyflow(conn)
    changes += migrate_p32_predictions_origin(conn)
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
