"""P88 T5：两个写函数（幂等 / 首写保留 / `conflicts` / `resp_sha256`）。

与 `ingest_financial_reports` 同一条纪律：唯一键命中即跳过、**首写保留**、
值变了计 `conflicts` 不覆盖；空输入是合法空，不是错误。
"""

from __future__ import annotations

import pytest

from stocklab.data.ingest import (ingest_announcements,
                                  ingest_northbound_holdings)
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-27T10:00:00+08:00"
SHA = "c" * 64


def _ann(art: str = "AN1", date: str = "2026-09-15", *, title: str = "美的集团:公告",
         **over) -> dict:
    row = {"code": "000333", "art_code": art, "notice_date": date,
           "display_time": f"{date} 19:35:36:855", "title": title,
           "column_name": "调研活动", "ann_type": "A",
           "source": "eastmoney-ann", "resp_sha256": SHA}
    row.update(over)
    return row


def _nb(date: str = "2026-06-30", *, shares: int = 893478198, **over) -> dict:
    row = {"code": "000333", "trade_date": date, "hold_shares": shares,
           "hold_market_cap": 67484408294.94, "a_shares_ratio": 12.83,
           "hold_shares_ratio": 12.83, "free_shares_ratio": 13.0201,
           "close_price": 75.53, "frequency": "quarterly",
           "source": "eastmoney-northbound", "resp_sha256": SHA}
    row.update(over)
    return row


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    yield c
    c.close()


# ---------- 公告 ----------


def test_ann_writes_row_with_all_provenance(conn):
    r = ingest_announcements(conn, [_ann()], now=NOW)
    assert r.ok and r.rows_written == 1 and r.conflicts == 0
    row = conn.execute("SELECT * FROM announcements").fetchone()
    assert row["code"] == "000333"
    assert row["art_code"] == "AN1"
    assert row["notice_date"] == "2026-09-15"
    assert row["display_time"] == "2026-09-15 19:35:36:855"
    assert row["column_name"] == "调研活动"
    assert row["source"] == "eastmoney-ann"
    assert row["fetched_at"] == NOW and row["created_at"] == NOW
    assert row["resp_sha256"] == SHA


def test_ann_is_idempotent(conn):
    ingest_announcements(conn, [_ann(), _ann("AN2", "2026-09-10")], now=NOW)
    r2 = ingest_announcements(conn, [_ann(), _ann("AN2", "2026-09-10")], now=NOW)
    assert r2.rows_written == 0
    assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 2


def test_ann_first_write_wins_and_counts_conflict(conn):
    """同 `(code, art_code)` 重采值变了 ⇒ 保留旧值 + `conflicts += 1`，不覆盖。"""
    ingest_announcements(conn, [_ann()], now=NOW)
    r2 = ingest_announcements(conn, [_ann(title="改过的标题")], now=NOW)
    assert r2.rows_written == 0 and r2.conflicts == 1
    got = conn.execute("SELECT title FROM announcements").fetchone()[0]
    assert got == "美的集团:公告", "首写保留"


def test_ann_identical_reread_is_not_a_conflict(conn):
    """逐位相同的重采**不是**冲突（否则每天的增量都会报一串假冲突）。"""
    ingest_announcements(conn, [_ann()], now=NOW)
    r2 = ingest_announcements(conn, [_ann()], now=NOW)
    assert r2.conflicts == 0


def test_ann_display_time_jitter_is_not_a_conflict(conn):
    """⚠️ 真源实测：同一 `art_code` 两次抓取的 `display_time` **毫秒后缀会变**
    （`19:35:36:855` → `:753`）。它只作留痕（D2）⇒ 不许拿它判冲突，
    否则每次重跑都「9 行全冲突」，`conflicts` 就不再能回答「源站改没改内容」。
    """
    ingest_announcements(conn, [_ann(display_time="2026-09-15 19:35:36:855")],
                         now=NOW)
    r2 = ingest_announcements(conn, [_ann(display_time="2026-09-15 19:35:36:753")],
                              now=NOW)
    assert r2.conflicts == 0, "毫秒抖动不是内容变更"
    # 首写保留：旧值不被新值覆盖（留痕列也不许被改写）
    got = conn.execute("SELECT display_time FROM announcements").fetchone()[0]
    assert got == "2026-09-15 19:35:36:855"


def test_ann_a_real_content_change_is_still_a_conflict(conn):
    """反向自检：真的改了内容（标题）仍须被判成冲突。"""
    ingest_announcements(conn, [_ann()], now=NOW)
    assert ingest_announcements(conn, [_ann(title="换了标题")], now=NOW).conflicts == 1
    assert ingest_announcements(conn, [_ann(column_name="分红送配")],
                                now=NOW).conflicts == 1


def test_ann_empty_batch_is_a_legal_empty(conn):
    """没有新公告是**合法空**（源站就给这么几行），不是失败。"""
    r = ingest_announcements(conn, [], now=NOW)
    assert r.ok and r.rows_written == 0


# ---------- 北向 ----------


def test_nb_writes_quarterly_row(conn):
    r = ingest_northbound_holdings(conn, [_nb()], now=NOW)
    assert r.ok and r.rows_written == 1
    row = conn.execute("SELECT * FROM northbound_holdings").fetchone()
    assert row["trade_date"] == "2026-06-30"
    assert row["hold_shares"] == 893478198
    assert row["frequency"] == "quarterly"
    assert row["resp_sha256"] == SHA


def test_nb_is_idempotent_and_first_write_wins(conn):
    ingest_northbound_holdings(conn, [_nb()], now=NOW)
    r2 = ingest_northbound_holdings(conn, [_nb(shares=1)], now=NOW)
    assert r2.rows_written == 0 and r2.conflicts == 1
    assert conn.execute("SELECT hold_shares FROM northbound_holdings"
                        ).fetchone()[0] == 893478198
    assert conn.execute("SELECT COUNT(*) FROM northbound_holdings"
                        ).fetchone()[0] == 1


def test_nb_a_row_claiming_daily_is_refused_before_any_write(conn):
    """频率写错了要**响亮地失败**，而且**一行都不落**（不是写一半再炸）。

    D3：公开源上北向日度持股已不存在（§0.3）—— 谁想写 `daily` 都必须被拦住。
    """
    with pytest.raises(ValueError) as e:
        ingest_northbound_holdings(
            conn, [_nb(), _nb("2026-03-31", frequency="daily")], now=NOW)
    assert "quarterly" in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM northbound_holdings"
                        ).fetchone()[0] == 0


def test_nb_accepts_two_quarters(conn):
    r = ingest_northbound_holdings(
        conn, [_nb("2026-06-30"), _nb("2026-03-31", shares=880000000)], now=NOW)
    assert r.rows_written == 2
    got = [tuple(x) for x in conn.execute(
        "SELECT trade_date, hold_shares FROM northbound_holdings"
        " ORDER BY trade_date")]
    assert got == [("2026-03-31", 880000000), ("2026-06-30", 893478198)]


def test_nb_empty_batch_is_a_legal_empty(conn):
    r = ingest_northbound_holdings(conn, [], now=NOW)
    assert r.ok and r.rows_written == 0
