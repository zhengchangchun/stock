import sqlite3
from contextlib import contextmanager
from pathlib import Path


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """打开连接并设置所有必需 PRAGMA。read_only 不影响文件创建。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """显式事务；异常回滚并重抛。"""
    try:
        conn.execute("BEGIN")
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
