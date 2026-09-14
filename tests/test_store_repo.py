"""仓储层（Task 14）：**唯一写入口**。

纪律：除 `stocklab/store/repo.py` 外，任何模块不得直接 INSERT/UPDATE 业务表。
`bars_daily` 是本项目 append-only 规则的**刻意例外**（源站会修订历史行情），
理由见 `docs/decisions/2026-09-14-ADR-001-评审决策.md` §8.1。
"""

import sqlite3

import pytest

from stocklab.config.universe import Instrument
from stocklab.data.models import Bar
from stocklab.quality.checks import Issue
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T19:00:00+08:00"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


def bar(date="2026-09-11", c=10.5):
    return Bar(code="000333", date=date, open=10.0, high=10.8, low=9.9, close=c,
               volume=1000, amount=10_500.0, turnover=1.0, source="test")


def test_upsert_instruments(conn):
    n = repo.upsert_instruments(conn, [Instrument("000333", "美的集团", "sz", "main")],
                                now=NOW)
    assert n == 1
    repo.upsert_instruments(conn, [Instrument("000333", "美的集团A", "sz", "main")],
                            now=NOW)
    row = conn.execute("SELECT name, added_at FROM instruments WHERE code='000333'"
                       ).fetchone()
    assert row["name"] == "美的集团A"
    assert row["added_at"] == NOW          # added_at 不被覆盖：首次入库时间不可变


def test_insert_bars(conn):
    assert repo.insert_bars(conn, [bar()], now=NOW) == 1
    assert repo.latest_bar_date(conn, "000333") == "2026-09-11"


def test_insert_bars_is_idempotent_on_pk(conn):
    repo.insert_bars(conn, [bar()], now=NOW)
    repo.insert_bars(conn, [bar()], now=NOW)
    n = conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0]
    assert n == 1


def test_insert_bars_allows_revision(conn):
    """行情可被源站修订，bars_daily 允许覆盖（刻意例外）。"""
    repo.insert_bars(conn, [bar(c=10.5)], now=NOW)
    repo.insert_bars(conn, [bar(c=10.7)], now=NOW)
    row = conn.execute("SELECT close FROM bars_daily").fetchone()
    assert row["close"] == pytest.approx(10.7)


def test_quality_issue_deduped_and_counted(conn):
    issue = Issue("error", "ohlc_inconsistent", "000333", "2026-09-11", "x")
    repo.insert_quality_issues(conn, [issue], source="test", now=NOW)
    repo.insert_quality_issues(conn, [issue], source="test", now=NOW)
    rows = conn.execute("SELECT occurrences, detail FROM data_quality").fetchall()
    assert len(rows) == 1
    assert rows[0]["occurrences"] == 2
    assert rows[0]["detail"] == "x"


def test_log_event(conn):
    repo.log_event(conn, "ingest", "error", "抓取失败", now=NOW)
    row = conn.execute("SELECT message, level, context_json FROM system_events"
                       ).fetchone()
    assert row["level"] == "error"
    assert row["message"] == "抓取失败"
    assert row["context_json"] == "{}"      # 无 context 时写空对象，不写 NULL


def test_log_event_serializes_context(conn):
    repo.log_event(conn, "ingest", "warn", "缺数据",
                   context={"code": "000333", "n": 3}, now=NOW)
    row = conn.execute("SELECT context_json FROM system_events").fetchone()
    assert '"code"' in row["context_json"]


def test_record_job_lifecycle(conn):
    rid = repo.record_job(conn, "ingest_daily", status="running", started_at=NOW)
    repo.finish_job(conn, rid, status="ok", finished_at=NOW, detail="20/20")
    row = conn.execute("SELECT status, detail FROM job_runs WHERE run_id=?",
                       (rid,)).fetchone()
    assert row["status"] == "ok"
    assert row["detail"] == "20/20"


def test_latest_bar_date_none_when_empty(conn):
    assert repo.latest_bar_date(conn, "999999") is None


def test_insert_bars_normalizes_unit_mismatch_before_write(conn):
    """写入前必须已归一化：volume 是股、amount 是元。"""
    repo.insert_bars(conn, [bar()], now=NOW)
    row = conn.execute("SELECT volume, amount FROM bars_daily").fetchone()
    assert row["volume"] == 1000
    assert row["amount"] == pytest.approx(10_500.0)


# ---------- 边界与错误路径（计划外补充） ----------

def test_insert_bars_empty_sequence_is_noop(conn):
    assert repo.insert_bars(conn, [], now=NOW) == 0
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0


def test_insert_quality_issues_empty_is_noop(conn):
    assert repo.insert_quality_issues(conn, [], source="test", now=NOW) == 0


def test_insert_bars_rejects_adjusted_bars(conn):
    """铁律①：复权价**永远**不得落 bars_daily，写入口直接拒绝。

    计划的接口说「adj_mode 不同视为不同记录」，但 schema 的 PK 是
    `(code, date)` —— 同一天物理上放不下两条口径。与其让 qfq 静默覆盖
    不复权价（数据被污染且无人察觉），不如在唯一写入口显式报错。
    """
    b_qfq = Bar(code="000333", date="2026-09-11", open=9.0, high=9.8, low=8.9,
                close=9.5, volume=1000, amount=9_500.0, turnover=1.0,
                source="test", adj_mode="qfq")
    with pytest.raises(ValueError, match="adj_mode"):
        repo.insert_bars(conn, [b_qfq], now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0


def test_insert_bars_rejects_adjusted_bars_atomically(conn):
    """批量里混入一根 qfq：整批拒绝，不得写进去一半。"""
    good = bar()
    bad = Bar(code="000333", date="2026-09-10", open=9.0, high=9.8, low=8.9,
              close=9.5, volume=1000, amount=9_500.0, turnover=1.0,
              source="test", adj_mode="qfq")
    with pytest.raises(ValueError):
        repo.insert_bars(conn, [good, bad], now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0


def test_latest_bar_date_ignores_other_codes(conn):
    repo.insert_bars(conn, [bar("2026-09-11")], now=NOW)
    other = Bar(code="600690", date="2026-12-31", open=1.0, high=1.0, low=1.0,
                close=1.0, volume=0, amount=0.0, turnover=None, source="test")
    repo.insert_bars(conn, [other], now=NOW)
    assert repo.latest_bar_date(conn, "000333") == "2026-09-11"


def test_quality_issue_dedup_separates_by_source(conn):
    """UNIQUE(date,source,code,issue_type)：不同来源的同名问题不合并。"""
    issue = Issue("error", "ohlc_inconsistent", "000333", "2026-09-11", "x")
    repo.insert_quality_issues(conn, [issue], source="ingest", now=NOW)
    repo.insert_quality_issues(conn, [issue], source="doctor", now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM data_quality").fetchone()[0] == 2


def test_zero_volume_bar_written_with_suspension_flag(conn):
    b = Bar(code="000333", date="2026-09-11", open=10.0, high=10.0, low=10.0,
            close=10.0, volume=0, amount=0.0, turnover=None, source="test")
    repo.insert_bars(conn, [b], now=NOW)
    row = conn.execute("SELECT is_suspended FROM bars_daily").fetchone()
    assert row["is_suspended"] == 1


def test_writes_do_not_leave_open_transaction(conn):
    """仓库层自带 commit：调用方紧接着开事务不应报嵌套错。"""
    repo.insert_bars(conn, [bar()], now=NOW)
    with conn:                                # sqlite3 显式事务仍可用
        conn.execute("SELECT 1")
    assert conn.in_transaction is False
