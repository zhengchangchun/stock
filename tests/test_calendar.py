import pytest

from stocklab.calendar.trading_calendar import Calendar
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

# 2026-09-11(五) 14(一) 15(二)；12/13 是周末
DATES = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15"]


@pytest.fixture
def cal():
    return Calendar.from_dates(DATES)


def test_is_open(cal):
    assert cal.is_open("2026-09-11")
    assert not cal.is_open("2026-09-12")   # 周六
    assert not cal.is_open("2026-09-13")   # 周日


def test_next_trading_day_skips_weekend(cal):
    """R11 的经典场景：周五的次日是下周一。"""
    assert cal.next_trading_day("2026-09-11") == "2026-09-14"


def test_next_trading_day_from_saturday(cal):
    assert cal.next_trading_day("2026-09-12") == "2026-09-14"


def test_prev_trading_day(cal):
    assert cal.prev_trading_day("2026-09-14") == "2026-09-11"


def test_next_beyond_range_raises(cal):
    with pytest.raises(IndexError):
        cal.next_trading_day("2026-09-15")


def test_prev_beyond_range_raises(cal):
    with pytest.raises(IndexError):
        cal.prev_trading_day("2026-09-10")


def test_sessions_inclusive(cal):
    assert cal.sessions("2026-09-10", "2026-09-14") == [
        "2026-09-10", "2026-09-11", "2026-09-14",
    ]


def test_sessions_excludes_non_sessions(cal):
    """区间内非交易日（周末）不得出现。"""
    assert cal.sessions("2026-09-11", "2026-09-15") == [
        "2026-09-11", "2026-09-14", "2026-09-15",
    ]


def test_from_dates_sorts_and_dedupes():
    cal = Calendar.from_dates(["2026-09-14", "2026-09-10", "2026-09-14"])
    assert cal.all_dates == ("2026-09-10", "2026-09-14")


def test_from_dates_drops_empty_strings():
    cal = Calendar.from_dates(["2026-09-10", "", None])
    assert cal.all_dates == ("2026-09-10",)


def test_roundtrip_through_db(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        cal = Calendar.from_dates(DATES)
        n = cal.save(conn, source="index_bars", now="2026-09-14T20:00:00+08:00")
        assert n == 4
        loaded = Calendar.load(conn)
    assert loaded.all_dates == cal.all_dates


def test_save_is_idempotent(tmp_db):
    """前滚迁移的精神：重复落库不报错、不重复。"""
    init_db(tmp_db)
    cal = Calendar.from_dates(DATES)
    with connect(tmp_db) as conn:
        cal.save(conn, source="index_bars", now="2026-09-14T20:00:00+08:00")
        cal.save(conn, source="index_bars", now="2026-09-14T21:00:00+08:00")
        n = conn.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0]
    assert n == 4


def test_load_empty_db_raises(tmp_db):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        with pytest.raises(ValueError, match="empty"):
            Calendar.load(conn)
