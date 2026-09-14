"""交易日历的离线验收测试（ADR-001 B4 / 约束②）。

日历来源 = 沪深300 指数日线日期集合，落 trading_calendar 表。
fixture 由 `scripts/record_calendar_fixture.py` 录制一次，测试**只读本地文件、不联网**。
"""

import hashlib
import json
import socket
from datetime import date
from pathlib import Path

import pytest

from stocklab.calendar.trading_calendar import Calendar
from stocklab.config.paths import FIXTURE_DIR
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

CODE = "sh000300"
META_PATH = FIXTURE_DIR / f"index_bars_{CODE}.calendar.json"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """任何联网尝试都直接失败 —— 证明本模块确实离线可跑。"""

    def _boom(*args, **kwargs):
        raise AssertionError("日历测试必须离线：检测到网络调用")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


@pytest.fixture(scope="module")
def meta() -> dict:
    return json.loads(META_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cal(meta) -> Calendar:
    return Calendar.from_dates(meta["dates"])


# ---------- fixture 自身的可信度（防止 fixture 被手改后测试假通过） ----------

def test_fixture_comes_from_recorded_raw_response(meta):
    raw = (FIXTURE_DIR / meta["raw_file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == meta["raw_sha256"]


def test_fixture_dates_match_raw_response(meta):
    payload = json.loads((FIXTURE_DIR / meta["raw_file"]).read_text(encoding="utf-8"))
    rows = payload["data"][CODE]["day"]
    assert sorted({r[0] for r in rows}) == meta["dates"]
    assert meta["n_bars"] == len(meta["dates"]) == 900


def test_fixture_dates_are_sorted_and_unique(meta):
    dates = meta["dates"]
    assert dates == sorted(dates)
    assert len(set(dates)) == len(dates)


# ---------- 验收：2026-09-14 是交易日 ----------

def test_2026_09_14_is_trading_day(cal):
    assert date(2026, 9, 14).weekday() == 0     # 前提：周一
    assert cal.is_open("2026-09-14")


def test_prev_and_next_around_2026_09_14(cal):
    assert cal.prev_trading_day("2026-09-14") == "2026-09-11"   # 上一个交易日是周五
    assert cal.is_open("2026-09-11")


# ---------- 验收：2026-09-12（周六）不是交易日 ----------

def test_2026_09_12_saturday_is_not_trading_day(cal):
    assert date(2026, 9, 12).weekday() == 5     # 前提：周六
    assert not cal.is_open("2026-09-12")


def test_weekend_is_skipped(cal):
    """周五的下一个交易日是下周一，周末两天都不在日历里。"""
    assert cal.next_trading_day("2026-09-11") == "2026-09-14"
    assert not cal.is_open("2026-09-13")        # 周日


# ---------- 验收：2026-10-01（国庆）不是交易日 ----------

def test_2026_10_01_is_not_trading_day(cal):
    """国庆节。注意当日历覆盖到该日时它也不是交易日 —— 用同规则的历史证据支撑：
    2023-10-02/03（同为国庆假期内的周一/周二）确实不在日历中。"""
    assert not cal.is_open("2026-10-01")
    assert not cal.is_open("2023-10-02")
    assert not cal.is_open("2023-10-03")


def test_holiday_weekdays_are_excluded_not_weekend_logic(cal):
    """日历不是「周一至周五即开市」：真实工作日假期必须缺席。"""
    holiday_weekdays = {
        "2026-01-01": 3,   # 元旦，周四
        "2026-05-01": 4,   # 劳动节，周五
        "2026-06-19": 4,   # 端午节，周五
        "2023-05-01": 0,   # 劳动节，周一
    }
    for d, expected_weekday in holiday_weekdays.items():
        assert date.fromisoformat(d).weekday() == expected_weekday, f"{d} 前提有误"
        assert not cal.is_open(d), f"{d} 是工作日假期，不应出现在日历中"


def test_every_recorded_session_is_a_weekday(cal):
    """反向性质：日历里的日期必须都是工作日。"""
    bad = [d for d in cal.all_dates if date.fromisoformat(d).weekday() >= 5]
    assert bad == []


def test_calendar_covers_requested_years(cal):
    assert cal.all_dates[0] == "2022-12-28"
    assert cal.all_dates[-1] == "2026-09-14"
    assert len(cal.sessions("2026-01-01", "2026-09-14")) > 150


# ---------- 落库往返 ----------

def test_fixture_calendar_roundtrip_through_db(tmp_db, cal):
    init_db(tmp_db)
    with connect(tmp_db) as conn:
        cal.save(conn, source="index_bars", now="2026-09-14T23:30:00+08:00")
        loaded = Calendar.load(conn)
    assert loaded.all_dates == cal.all_dates
    assert loaded.is_open("2026-09-14")
    assert not loaded.is_open("2026-09-12")
