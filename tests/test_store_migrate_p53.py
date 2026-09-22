"""P53 T5：`instruments.sector` 回填迁移（补数据，不是补列）。

判据：

1. **按最新报告期的行业名回填**，且**幂等**（第二次调用返回 `[]`，不改任何值）。
2. **留痕**：变更写进 `system_events`，含改了哪些 code —— 这一列会进 `ctx`、
   进而进插桩0 的判据，必须能回答「什么时候写进去的」。
3. **只读探测** `instruments_need_sector_backfill` 能预先算出口径缺口。
4. 已有人工值 / 没有行业名的标的**不被改动**。
"""

from __future__ import annotations

from stocklab.data.ingest import ingest_financial_reports
from stocklab.data.models import FinancialReport
from stocklab.store.db import connect
from stocklab.store.migrate import (init_db, instruments_need_sector_backfill,
                                   migrate_instruments_sector)

NOW = "2026-09-22T16:00:00+08:00"


def _rep(code, report_date, industry, notice):
    return FinancialReport(
        code=code, report_date=report_date, notice_date=notice,
        notice_date_source="f10", report_type="中报" if report_date[5:7] == "06"
        else "年报", total_assets=6e11, parent_equity=2e11,
        total_equity=2.2e11, total_liabilities=3.8e11,
        industry_name=industry)


def _seed(conn, tmp_db):
    """两只标的：000333 有两期（行业名变了），600036 有一期。"""
    from stocklab.store.migrate import ensure_schema
    ensure_schema(tmp_db)
    ingest_financial_reports(conn, [
        _rep("000333", "2025-12-31", "白色家电", "2026-03-31"),
        _rep("000333", "2026-06-30", "白色家电Ⅱ", "2026-08-29"),
        _rep("600036", "2026-06-30", "银行Ⅱ", "2026-08-29"),
    ], [{"endpoint": "x", "resp_sha256": "s", "cache_key": "k", "page": 1}],
        now=NOW)
    conn.executemany(
        "INSERT INTO instruments (code, name, market, board, type, sector,"
        " added_at) VALUES (?,?,?,?,?,?,?)",
        [("000333", "美的集团", "sz", "main", "stock", None, NOW),
         ("600036", "招商银行", "sh", "main", "stock", None, NOW),
         ("510300", "沪深300ETF", "sh", "main", "etf", None, NOW)])


def test_backfill_uses_latest_report_period(tmp_db):
    conn = connect(tmp_db)
    _seed(conn, tmp_db)
    changed = migrate_instruments_sector(conn, now=NOW)

    got = dict(conn.execute("SELECT code, sector FROM instruments"))
    # 000333 取**最新**报告期（2026-06-30）的行业名，不是 2025 年报那个
    assert got["000333"] == "白色家电Ⅱ"
    assert got["600036"] == "银行Ⅱ"
    assert got["510300"] is None, "没有财报的 ETF 不许被填一个假行业"
    assert sorted(changed) == ["000333: None -> '白色家电Ⅱ'",
                               "600036: None -> '银行Ⅱ'"]
    conn.close()


def test_backfill_is_idempotent(tmp_db):
    conn = connect(tmp_db)
    _seed(conn, tmp_db)
    migrate_instruments_sector(conn, now=NOW)
    assert migrate_instruments_sector(conn, now=NOW) == []
    conn.close()


def test_backfill_records_audit_event(tmp_db):
    conn = connect(tmp_db)
    _seed(conn, tmp_db)
    migrate_instruments_sector(conn, now=NOW)
    rows = conn.execute(
        "SELECT module, level, message, context_json FROM system_events"
        " WHERE module = 'migrate'").fetchall()
    assert len(rows) == 1, "每次回填必须留痕（sector 会进 ctx、进分数）"
    assert "000333" in rows[0]["context_json"]
    assert "600036" in rows[0]["context_json"]
    conn.close()


def test_backfill_does_not_touch_existing_manual_value(tmp_db):
    """已有人工填的 sector → 不动（人工判断优先于自动回填）。"""
    conn = connect(tmp_db)
    _seed(conn, tmp_db)
    conn.execute("UPDATE instruments SET sector = ? WHERE code = ?",
                 ("家电（手工）", "000333"))
    conn.commit()
    changed = migrate_instruments_sector(conn, now=NOW)
    got = conn.execute(
        "SELECT sector FROM instruments WHERE code='000333'").fetchone()[0]
    assert got == "家电（手工）"
    assert changed == ["600036: None -> '银行Ⅱ'"]
    conn.close()


def test_readonly_probe_finds_the_gap(tmp_db):
    conn = connect(tmp_db)
    _seed(conn, tmp_db)
    assert instruments_need_sector_backfill(conn) == ["000333", "600036"]
    migrate_instruments_sector(conn, now=NOW)
    assert instruments_need_sector_backfill(conn) == []
    conn.close()
