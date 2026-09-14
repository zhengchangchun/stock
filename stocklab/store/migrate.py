import shutil
from datetime import datetime, timezone
from pathlib import Path

from stocklab.config.paths import SCHEMA_SQL
from stocklab.store.db import connect

TZ = timezone.utc


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
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
    return backup
