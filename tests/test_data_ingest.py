"""采集编排（Task 15）：离线可测。

关键设计：**所有联网动作经 `fetch` 注入**。生产传真实抓取函数，
测试与复现模式传缓存读取函数 —— 两条路径走**同一段代码**，
所以「测试通过」对生产路径有真实意义（否则测试只是在测测试替身）。

纪律（对应铁律③与度量纪律）：
  - 失败的标的必须留痕，且**计入分母**（C4）—— 静默跳过会让成功率虚高。
  - 硬错误（severity=error）的数据**不得落库**：脏数据比没数据更危险。
"""

import pytest

from stocklab.calendar.trading_calendar import Calendar
from stocklab.config.universe import Instrument
from stocklab.data.ingest import ingest_daily_bars
from stocklab.data.models import Bar
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T19:00:00+08:00"
UNIVERSE = (Instrument("000333", "美的集团", "sz", "main"),
            Instrument("600690", "海尔智家", "sh", "main"))
CAL = Calendar.from_dates(["2026-09-10", "2026-09-11", "2026-09-14"])


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    repo.upsert_instruments(c, UNIVERSE, now=NOW)
    yield c
    c.close()


def good_bars(code, n=3):
    dates = ["2026-09-10", "2026-09-11", "2026-09-14"][:n]
    return [Bar(code=code, date=d, open=10.0, high=10.8, low=9.9, close=10.5,
                volume=1000, amount=10_500.0, turnover=1.0, source="test")
            for d in dates]


def bad_bar(code, date="2026-09-10"):
    """OHLC 不一致：high 低于 open/close → 硬错误。"""
    return Bar(code=code, date=date, open=10.0, high=9.0, low=9.5, close=10.5,
               volume=1000, amount=10_500.0, turnover=None, source="test")


def ingest(conn, universe=UNIVERSE, fetch=None, **kw):
    kw.setdefault("start", "2026-09-10")
    kw.setdefault("end", "2026-09-14")
    return ingest_daily_bars(conn, None, None, universe, now=NOW, fetch=fetch, **kw)


def test_ingest_writes_bars(conn):
    report = ingest(conn, fetch=lambda code: good_bars(code))
    assert report.ok_count == 2
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 6


def test_ingest_records_failure_without_aborting_others(conn):
    """C4：失败的标的必须留痕，且计入分母，不能静默跳过。"""
    def fetch(code):
        if code == "600690":
            raise RuntimeError("boom")
        return good_bars(code)

    report = ingest(conn, fetch=fetch)
    assert report.ok_count == 1
    assert report.failed_codes == ["600690"]
    events = conn.execute(
        "SELECT COUNT(*) FROM system_events WHERE level='error'").fetchone()[0]
    assert events >= 1


def test_ingest_records_quality_issues(conn):
    def fetch(code):
        bars = good_bars(code)
        bars[0] = bad_bar(code)
        return bars

    report = ingest(conn, universe=UNIVERSE[:1], fetch=fetch)
    assert report.total_issues >= 1
    n = conn.execute("SELECT COUNT(*) FROM data_quality").fetchone()[0]
    assert n >= 1


def test_ingest_does_not_write_bars_when_quality_fails(conn):
    """硬错误（OHLC 不一致）的数据不得落库 —— 脏数据比没数据更危险。"""
    report = ingest(conn, universe=UNIVERSE[:1], fetch=lambda code: [bad_bar(code)])
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0
    assert report.ok_count == 0
    assert report.failed_codes == ["000333"]


def test_ingest_records_job_run(conn):
    ingest(conn, fetch=lambda code: good_bars(code))
    row = conn.execute("SELECT status, detail, started_at, finished_at FROM job_runs"
                       " ORDER BY run_id DESC").fetchone()
    assert row["status"] == "ok"
    # `.started_at` 仍是调用方注入的那个时刻（起点不动）。
    assert row["started_at"] == NOW
    # `.finished_at` **不再**等于它（P67 T3）：收尾时另取一个真实墙钟时刻，否则
    # 「这步跑了多久」在 `job_runs` 里恒为 0 —— 旧断言 `finished_at == NOW` 钉住的
    # 正是那个假读数（真库实测 `ingest_moneyflow` 269.647s 而两字段逐字相同）。
    assert row["finished_at"] != NOW
    assert row["finished_at"] > NOW


def test_ingest_marks_job_failed_when_all_fail(conn):
    def fetch(code):
        raise RuntimeError("boom")

    ingest(conn, fetch=fetch)
    row = conn.execute("SELECT status FROM job_runs ORDER BY run_id DESC").fetchone()
    assert row["status"] == "failed"


def test_ingest_result_reports_error_message(conn):
    def fetch(code):
        raise RuntimeError("限流了")

    report = ingest(conn, universe=UNIVERSE[:1], fetch=fetch)
    assert "限流了" in report.results[0].error


# ---------- 边界与错误路径（计划外补充） ----------

def test_ingest_filters_bars_outside_window(conn):
    """窗口外的数据不写库：避免把越界数据混进当期。"""
    def fetch(code):
        return good_bars(code) + [Bar(
            code=code, date="2020-01-02", open=1.0, high=1.0, low=1.0, close=1.0,
            volume=1, amount=1.0, turnover=None, source="test")]

    ingest(conn, fetch=fetch)
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 6


def test_ingest_empty_result_is_not_a_failure(conn):
    """新上市标的当日无数据：算成功（0 根），不算失败。"""
    report = ingest(conn, fetch=lambda code: [])
    assert report.ok_count == 2
    assert report.failed_codes == []
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0


def test_ingest_passes_calendar_to_checks(conn):
    """缺一个交易日 → calendar_gap（warn，不阻断写入）。"""
    def fetch(code):
        return [b for b in good_bars(code) if b.date != "2026-09-11"]

    report = ingest(conn, universe=UNIVERSE[:1], fetch=fetch, calendar=CAL)
    assert report.total_issues == 1
    assert report.ok_count == 1                        # warn 不阻断
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 2


def test_ingest_requires_fetch_to_be_injected(conn):
    """不注入 fetch 时**不得**自行联网：返回 0 根而不是偷偷抓取。"""
    report = ingest(conn)
    assert report.ok_count == 2
    assert conn.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0


def test_ingest_result_is_frozen(conn):
    report = ingest(conn, universe=UNIVERSE[:1],
                    fetch=lambda code: good_bars(code))
    assert report.results[0].bars_written == 3
    assert isinstance(report.results[0].issues, tuple)
    with pytest.raises(Exception):
        report.results[0].ok = False        # type: ignore[misc]


def test_ingest_quality_issue_deduped_across_two_runs(conn):
    """同一问题重复出现只累加 occurrences，不刷屏。"""
    def fetch(code):
        return [bad_bar(code)]

    ingest(conn, universe=UNIVERSE[:1], fetch=fetch)
    ingest(conn, universe=UNIVERSE[:1], fetch=fetch)
    rows = conn.execute("SELECT occurrences FROM data_quality").fetchall()
    assert len(rows) == 1
    assert rows[0]["occurrences"] == 2
