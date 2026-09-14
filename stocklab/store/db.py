import sqlite3
from contextlib import contextmanager
from pathlib import Path


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """打开连接并设置所有必需 PRAGMA。read_only 不影响文件创建。

    isolation_level=None 关闭 sqlite3 的隐式事务管理：连接处于 autocommit，
    只有显式 ``transaction()`` 才开启事务。否则一条被触发器 ABORT 的语句会
    留下未关闭的隐式事务，导致下一次 BEGIN 报
    「cannot start a transaction within a transaction」。
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    # append-only 的**结构性**防线要靠这一行（P6 实测发现）。
    # SQLite 默认 recursive_triggers=OFF，而 `INSERT OR REPLACE` 解决唯一冲突的
    # 方式是**隐式删行再插入** —— 隐式 DELETE **不触发** DELETE 触发器，
    # 于是 `RAISE(ABORT, 'xxx is append-only')` 根本不跑，静默覆盖成功：
    # 实测 predictions 的 pred_id 从 1 变成 2、未列出的列被重置为 NULL。
    # 打开后，隐式 DELETE 会触发 append-only 触发器并 ABORT。
    conn.execute("PRAGMA recursive_triggers = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """显式事务；异常回滚并重抛。禁止嵌套（嵌套说明调用方漏了 commit/rollback）。"""
    if conn.in_transaction:
        raise RuntimeError("已有未结束的事务，禁止嵌套 transaction()")
    try:
        conn.execute("BEGIN")
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
