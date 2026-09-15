"""仓储层（Task 14）：**唯一写入口**。

纪律：除 `stocklab/store/repo.py` 外，任何模块不得直接 INSERT/UPDATE 业务表。
`bars_daily` 是本项目 append-only 规则的**刻意例外**（源站会修订历史行情），
理由见 `docs/decisions/2026-09-14-ADR-001-评审决策.md` §8.1。
"""

import sqlite3

import pytest

from stocklab.config.universe import Instrument
from stocklab.data.adjust import build_chain
from stocklab.data.models import Bar, CorpAction
from stocklab.quality.checks import Issue
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T19:00:00+08:00"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    # P17：复权链的写入口按 `instruments.type` 判口径（白名单），未登记一律拒绝 ——
    # 本文件的标的一律按股票口径登记。
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES ('000333','美的集团','sz','main','stock',?)", (NOW,))
    c.commit()
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


# ---------- ADR-004：除权事件写入（幂等 + 修订留痕 + first_seen 不可重置） ----------

def action(cqr="2026-06-29", content="10派38元", fh_sh=38.0, djr="2026-06-26"):
    return CorpAction(code="000333", cqr=cqr, djr=djr, content=content, fh_sh=fh_sh)


def test_insert_corp_actions_idempotent(conn):
    """重复采集同一批事件：不新增行、`first_seen` 不被重置、修订数为 0。"""
    assert repo.insert_corp_actions(conn, [action()], now=NOW) == (1, 0)
    assert repo.insert_corp_actions(conn, [action()], now="2026-09-15T19:00:00+08:00") \
        == (1, 0)
    rows = conn.execute("SELECT * FROM corp_actions").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_seen"] == NOW                  # 首次入库时间不可被覆盖


def test_insert_corp_actions_counts_restatements(conn):
    """源站改条款 → 更新且**计数**：改口径会改变复权价，下游必须看得见。"""
    repo.insert_corp_actions(conn, [action(fh_sh=38.0, content="10派38元")], now=NOW)
    written, restated = repo.insert_corp_actions(
        conn, [action(fh_sh=36.1, content="10派38元")], now=NOW)
    assert (written, restated) == (1, 1)
    row = conn.execute("SELECT * FROM corp_actions").fetchone()
    assert row["fh_sh"] == pytest.approx(36.1)


def test_insert_corp_actions_allows_null_fh_sh(conn):
    """送转-only 事件的 `fh_sh` 是空串 → 如实存 NULL，条款靠原文。"""
    repo.insert_corp_actions(conn, [action(cqr="1994-04-04", content="10送3股",
                                           fh_sh=None)], now=NOW)
    row = conn.execute("SELECT * FROM corp_actions").fetchone()
    assert row["fh_sh"] is None and row["content"] == "10送3股"


def test_insert_adj_factors_writes_explicit_ones_for_no_event_stock(conn):
    """ADR-001：无事件的标的也必须显式写 `factor = 1`，禁 NULL。"""
    bars = [bar("2026-09-10"), bar("2026-09-11")]
    chain = build_chain(bars, [])
    n = repo.insert_adj_factors(conn, "000333", chain, source="test", now=NOW)
    assert n == 2
    rows = conn.execute("SELECT date, factor FROM adj_factors ORDER BY date").fetchall()
    assert [(r["date"], r["factor"]) for r in rows] == [
        ("2026-09-10", 1.0), ("2026-09-11", 1.0)]


def test_insert_adj_factors_is_overwritable(conn):
    """因子是纯函数：重算必须能修正（append-only 会让错误因子永久留库）。"""
    bars = [bar("2026-09-10"), bar("2026-09-11")]
    repo.insert_adj_factors(conn, "000333", build_chain(bars, []),
                            source="test", now=NOW)
    chain = build_chain(bars, [action(cqr="2026-09-11", content="10派2元")])
    repo.insert_adj_factors(conn, "000333", chain, source="test", now=NOW)
    rows = dict(conn.execute("SELECT date, factor FROM adj_factors").fetchall())
    assert rows["2026-09-10"] == pytest.approx(1.0)
    assert rows["2026-09-11"] < 1.0


# ---------- P17：标的池扩展不得动既有行 ----------

def test_upsert_extended_universe_leaves_existing_rows_column_identical(conn):
    """**append-only 的硬证据**：把 ETF 加进标的池后，000333/600690 的每一列都不许变。

    逐列比较（含 `added_at`）—— 「只更新 name/market/board」这种说法要靠断言钉住，
    不能靠读代码确认。
    """
    from stocklab.config.universe import DEFAULT_UNIVERSE

    old = tuple(i for i in DEFAULT_UNIVERSE if i.code in ("000333", "600690"))
    repo.upsert_instruments(conn, old, now="2026-09-01T00:00:00+08:00")
    before = {r["code"]: dict(r) for r in conn.execute(
        "SELECT * FROM instruments WHERE code IN ('000333','600690')")}
    assert len(before) == 2

    repo.upsert_instruments(conn, DEFAULT_UNIVERSE, now="2026-09-15T00:00:00+08:00")
    after = {r["code"]: dict(r) for r in conn.execute(
        "SELECT * FROM instruments WHERE code IN ('000333','600690')")}
    assert before == after, "既有标的的行被改动了（append-only 纪律）"


def test_upsert_writes_real_asset_type(conn):
    """`type` 列必须写标的自己的口径 —— 曾经硬编码成 'stock'（P17 修正）。"""
    from stocklab.config.universe import DEFAULT_UNIVERSE

    repo.upsert_instruments(conn, DEFAULT_UNIVERSE, now=NOW)
    got = {r["code"]: (r["type"], r["market"], r["board"]) for r in conn.execute(
        "SELECT code, type, market, board FROM instruments")}
    assert got["510300"] == ("etf", "sh", "main")
    assert got["510880"] == ("etf", "sh", "main")
    assert got["512890"] == ("etf", "sh", "main")
    assert got["518880"] == ("etf", "sh", "main")
    assert got["000333"] == ("stock", "sz", "main")
    assert got["600690"] == ("stock", "sh", "main")


def test_upsert_does_not_duplicate_or_delete_rows(conn):
    """重复 upsert 不新增、不删除（仍幂等于 6 行）。"""
    from stocklab.config.universe import DEFAULT_UNIVERSE

    repo.upsert_instruments(conn, DEFAULT_UNIVERSE, now=NOW)
    repo.upsert_instruments(conn, DEFAULT_UNIVERSE, now=NOW)
    n = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
    assert n == len(DEFAULT_UNIVERSE) == 6
