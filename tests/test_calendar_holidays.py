"""P30：已公告休市安排（解析 / 落库 / target_date 接线）。

四条主线，每条都对应一个**实测踩到的坑**：

1. **星期自校验**：公告把边界日星期写进正文 → 解析结果必须与真实星期逐字相符。
   这条判据在开发期**当场抓到**「Sakamoto 返回 0=周日却被当成 0=周一用」的旧 bug。
2. **单日休市不许漏**（2025 元旦：`1月1日（星期三）休市`，无「至」）——
   漏它 = 把放假日当交易日，且**整体仍有其它日期**，只看「非空」判不出来。
3. **覆盖判据**：只有**年度通知**才代表整年都知道；单节公告不行。
4. **fail-closed**：解析不出 → 抛错，绝不静默返回空表。

fixture 是**真实响应**（`scripts/record_p30_fixtures.py` 录制，SHA256 在 meta 里校验）。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from stocklab.calendar import holidays as H
from stocklab.calendar.trading_calendar import Calendar
from stocklab.config.paths import FIXTURE_DIR
from stocklab.config.universe import Instrument
from stocklab.data.fetch import fetch_holiday_notices
from stocklab.data.models import Bar
from stocklab.data.raw_cache import load_fixture
from stocklab.data.sources import sse
from stocklab.predict import service as S
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-17T19:00:00+08:00"
CODE = "000333"

#: fixture 名 → (doc_kind, covered_year)
FIXTURES = {
    "sse_holiday_2026_annual": ("annual", 2026),
    "sse_holiday_2026_dragonboat": ("holiday", 2026),
    "sse_holiday_2025_annual": ("annual", 2025),
}


def fixture_text(name: str) -> tuple[str, dict]:
    """读真实 fixture（`load_fixture` 内部校验 SHA256，防手改 fixture 假通过）。"""
    raw, meta = load_fixture(FIXTURE_DIR, name)
    return raw.decode("utf-8"), meta


def parsed(name: str) -> list[H.MarketHoliday]:
    text, meta = fixture_text(name)
    kind, year = FIXTURES[name]
    return H.parse_holiday_notice(sse.article_text(text), source_url=meta["url"],
                                  published_at=meta["published_at"],
                                  doc_kind=kind, covered_year=year)


def weekdays(rows) -> list[str]:
    return [d for d in sorted({r.date for r in rows})
            if date.fromisoformat(d).weekday() < 5]


# ---------- 1. 星期函数（旧 bug 的回归钉子） ----------

def test_weekday_of_agrees_with_stdlib_on_known_dates():
    """2026-09-17 是**星期四**（会话日期，独立于本函数可核对）。

    旧实现（Sakamoto 原始返回值当成 0=周一）会给出 4=周五 —— 差一档。
    """
    assert H.weekday_of(2026, 9, 17) == 3          # 周四
    assert H.weekday_of(2026, 1, 1) == 3           # 周四（公告原文亦写「星期四」）
    assert H.weekday_of(2026, 9, 20) == 6          # 周日
    assert H.weekday_of(2026, 9, 19) == 5          # 周六
    for y, m, d in [(2024, 2, 29), (2025, 10, 1), (2026, 10, 7)]:
        assert H.weekday_of(y, m, d) == date(y, m, d).weekday()


def test_next_weekday_never_returns_a_weekend():
    """回归：周五 asof 曾被外推成**周日**（Sakamoto 约定搞反的直接后果）。"""
    for asof, want in [("2026-09-18", "2026-09-21"),   # 周五 → 下周一（曾错给 09-20 周日）
                       ("2026-09-19", "2026-09-21"),   # 周六 → 下周一
                       ("2026-09-20", "2026-09-21"),   # 周日 → 周一
                       ("2026-09-24", "2026-09-25")]:  # 周四 → 周五
        got = S._next_weekday(asof)
        assert got == want, f"{asof} → {got}（期望 {want}）"
        assert date.fromisoformat(got).weekday() < 5


# ---------- 2. 解析真实公告 ----------

def test_2026_annual_notice_gives_exact_weekday_closure_set():
    """2026 全年公告 → 工作日休市日**精确**集合（周末不列，另有 weekend 子句）。"""
    assert weekdays(parsed("sse_holiday_2026_annual")) == [
        "2026-01-01", "2026-01-02",                                  # 元旦
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
        "2026-02-20", "2026-02-23",                                  # 春节
        "2026-04-06",                                                # 清明
        "2026-05-01", "2026-05-04", "2026-05-05",                    # 劳动
        "2026-06-19",                                                # 端午
        "2026-09-25",                                                # 中秋
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
        "2026-10-07",                                                # 国庆
    ]


def test_single_day_holiday_is_not_dropped():
    """2025 元旦是**单日**写法（无「至」）—— 早期版本静默漏掉它。

    该公告里其它节都在，所以「整体非空」判不出来；只有逐条目完整性判据能抓。
    """
    got = weekdays(parsed("sse_holiday_2025_annual"))
    assert "2025-01-01" in got                    # 元旦（单日）
    assert "2025-01-28" in got                    # 春节（区间）
    assert len(got) == 18


def test_holiday_only_notice_covers_only_its_own_dates():
    """单节公告只列那一个节（端午 6/19–6/21），doc_kind 必须是 holiday。"""
    rows = parsed("sse_holiday_2026_dragonboat")
    assert [r.date for r in rows] == ["2026-06-19", "2026-06-20", "2026-06-21"]
    assert {r.doc_kind for r in rows} == {"holiday"}


def test_published_at_comes_from_the_article_not_the_fetch_time():
    _, meta = fixture_text("sse_holiday_2026_annual")
    assert meta["published_at"] == "2025-12-22"   # 上证公告〔2025〕45号


# ---------- 3. fail-closed ----------

def test_wrong_year_is_rejected_by_the_weekday_selfcheck():
    """把 2026 的公告按 2027 年解析 → 星期对不上 → 必须抛错（不许写入错日期）。"""
    text, meta = fixture_text("sse_holiday_2026_annual")
    with pytest.raises(H.HolidayParseError, match="星期自校验失败"):
        H.parse_holiday_notice(sse.article_text(text), source_url=meta["url"],
                               published_at=meta["published_at"],
                               doc_kind="annual", covered_year=2027)


def test_notice_without_any_parsable_date_fails_closed():
    """正文说「休市」却解不出日期 → 抛错（源站换排版 ≠ 今年不放假）。"""
    with pytest.raises(H.HolidayParseError):
        H.parse_holiday_notice("一、休市安排：另行通知。", source_url="u",
                               published_at="2026-01-01", doc_kind="annual",
                               covered_year=2026)


def test_bullet_with_holiday_but_no_date_fails_closed():
    """逐条目完整性：某一条写了「休市」但没有日期 → 抛错（防静默漏一个节）。"""
    text = ("一、休市安排：\n（一）元旦：1月1日（星期四）至1月3日（星期六）休市。\n"
            "（二）春节：另行通知休市。\n")
    with pytest.raises(H.HolidayParseError, match="解不出任何日期"):
        H.parse_holiday_notice(text, source_url="u", published_at="2026-01-01",
                               doc_kind="annual", covered_year=2026)


def test_illegal_date_fails_closed():
    with pytest.raises(H.HolidayParseError):
        H.parse_holiday_notice("元旦：2月30日（星期一）至3月2日（星期三）休市。",
                               source_url="u", published_at="2026-01-01",
                               doc_kind="annual", covered_year=2026)


# ---------- 4. 落库 / 覆盖判据 ----------

def _db(tmp_path, name="h.db"):
    init_db(tmp_path / name)
    return connect(tmp_path / name)


def test_save_is_idempotent_and_append_only(tmp_path):
    rows = parsed("sse_holiday_2026_annual")
    conn = _db(tmp_path)
    try:
        first = H.save_holidays(conn, rows, now=NOW)
        assert first == len(rows) > 0
        again = H.save_holidays(conn, rows, now=NOW)          # 重复执行
        assert again == 0                                     # 幂等：一行不增
        n = conn.execute("SELECT COUNT(*) FROM market_holidays").fetchone()[0]
        assert n == len(rows)
        # append-only：改/删都必须被触发器顶回来
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE market_holidays SET is_open=1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM market_holidays")
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM market_holidays").fetchone()[0] == n
    finally:
        conn.close()


def test_holiday_table_coverage_requires_an_annual_notice(tmp_path):
    """覆盖判据：只有年度通知才说「整年都知道」。"""
    conn = _db(tmp_path)
    try:
        H.save_holidays(conn, parsed("sse_holiday_2026_dragonboat"), now=NOW)
        t = H.load_holiday_table(conn)
        assert t.is_closed("2026-06-19")
        assert not t.covers("2026-06-22")      # 只有单节公告 → 不许说知道
        H.save_holidays(conn, parsed("sse_holiday_2026_annual"), now=NOW)
        t = H.load_holiday_table(conn)
        assert t.covers("2026-06-22") and not t.covers("2027-01-04")
        assert t.bounds() == ("2026-01-01", "2026-10-10")
    finally:
        conn.close()


# ---------- 5. resolve_target_date 接线 ----------

def _cal(*dates):
    return Calendar.from_dates(dates)


def test_resolve_target_date_prefers_calendar_then_holiday_table():
    hol = H.HolidayTable(closed=frozenset({"2026-09-25", "2026-10-01"}),
                         annual_years=frozenset({2026}))
    # 日历里还有 > asof 的日期 → 仍走日历（最强）
    assert S.resolve_target_date(_cal("2026-09-24", "2026-09-28"),
                                 "2026-09-24", holidays=hol) == ("2026-09-28",
                                                                  "trading_calendar")
    # 日历耗尽 + 命中已公告休市 → 跳过，且 source 点明用了公告
    assert S.resolve_target_date(_cal("2026-09-24"), "2026-09-24",
                                 holidays=hol) == ("2026-09-28", "holiday_table")
    assert S.resolve_target_date(_cal("2026-09-30"), "2026-09-30",
                                 holidays=hol) == ("2026-10-02", "holiday_table")


def test_resolve_target_date_falls_back_when_the_table_does_not_cover():
    """覆盖不到（未公告的年份）→ 行为与 P30 之前**逐字一致**。"""
    hol = H.HolidayTable(closed=frozenset({"2026-10-01"}), annual_years=frozenset({2026}))
    assert S.resolve_target_date(_cal("2027-01-04"), "2027-01-04",
                                 holidays=hol) == ("2027-01-05", "weekday_fallback")
    # 连 holidays 都不传（旧调用点）→ 必须与原语义完全一致
    assert S.resolve_target_date(_cal("2026-09-24"),
                                 "2026-09-24") == ("2026-09-25", "weekday_fallback")


# ---------- 6. 端到端：predict 载荷 ----------

def _bar(d: str, i: int) -> Bar:
    """**确定性但非常数**的一根 K 线。

    ⚠️ 不能播常数价（早期版本 close 恒为 10.0）：60 日对数收益标准差 = 0 →
    `compute_forecast` 抛 `DegenerateInput`，`predictions` 又变空，
    断言再次「看不出问题地」失败。**非退化**是这条端到端链路能被测到的前提。
    """
    close = round(10.0 + 0.3 * math.sin(i / 3.0) + 0.02 * (i % 5), 4)
    return Bar(code=CODE, date=d, open=close, high=round(close + 0.1, 4),
               low=round(close - 0.1, 4), close=close, volume=1000,
               amount=10000.0, turnover=1.0, source="test", adj_mode="none")


def _seed(path, dates):
    """最小库：一个标的 + 一段行情 + 把所有日期登记为交易日。"""
    init_db(path)
    conn = connect(path)
    repo.upsert_instruments(conn, [Instrument(CODE, CODE, "sz", "main")], now=NOW)
    repo.insert_bars(conn, [_bar(d, i) for i, d in enumerate(dates)], now=NOW)
    Calendar.from_dates(dates).save(conn, source="test", now=NOW)
    return conn


#: `compute_forecast` 的 WINDOW=60 → 少于 61 根 K 线只会得到
#: `skipped[DegenerateInput]` 与**空 `predictions`**。
MIN_BARS = 61
ASOF = date(2026, 9, 24)


def _history(end: date, n: int = MIN_BARS) -> list[str]:
    """`end` 往前数 n 个**真实开市日**（跳过周末与已公告休市日）。

    ⚠️ 这里给足 `MIN_BARS` 是本文件唯一**真实踩到的坑**：早期版本只播 3 天行情，
    于是 `build_predictions` 返回的 `predictions` **恒为空** ——
    `rep["predictions"][0]` 直接 IndexError，而隔壁那条不读 `predictions` 的
    兄弟测试则**空转通过**（它测的 `target_date` 是顶层字段，不需要任何预测行）。
    「断言绿了」不等于「断言被测到了」。
    """
    closed = {r.date for r in parsed("sse_holiday_2026_annual")}
    out: list[str] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5 and d.isoformat() not in closed:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def test_predict_payload_skips_announced_holidays_end_to_end(tmp_path):
    """asof = 长假前最后一个交易日 → target_date 跳过休市日，source 如实反映来源。"""
    conn = _seed(tmp_path / "a.db", _history(ASOF))
    try:
        H.save_holidays(conn, parsed("sse_holiday_2026_annual"), now=NOW)
        rep = S.build_predictions(conn, ASOF.isoformat(), [CODE])
        # 2026-09-25（中秋）与 09-26/27（周末）休市 → 下一个交易日是 09-28
        assert rep["target_date"] == "2026-09-28"
        assert rep["target_date_source"] == "holiday_table"
        assert "休市" in rep["notes"]["target_date"]
        assert rep["predictions"][0]["target_date"] == "2026-09-28"
        assert rep["evidence"][CODE]["target_date_source"] == "holiday_table"
    finally:
        conn.close()


def test_predict_payload_is_unchanged_when_there_are_no_holidays(tmp_path):
    """休市表为空（老库）→ 与 P30 之前完全一致：weekday_fallback + 原文案。"""
    conn = _seed(tmp_path / "b.db", _history(ASOF))
    try:
        rep = S.build_predictions(conn, ASOF.isoformat(), [CODE])
        assert rep["target_date"] == "2026-09-25"
        assert rep["target_date_source"] == "weekday_fallback"
        assert "不是交易日历" in rep["notes"]["target_date"]
        # 非空转判据：上面三条断言的 `target_date` 是**顶层**字段，不需要任何预测行。
        # 不钉这一条，本测试在「一根 K 线都不播」时也会绿。
        assert rep["predictions"] and rep["predictions"][0]["target_date"] == "2026-09-25"
    finally:
        conn.close()


# ---------- 7. 抓取（离线回放真实响应） ----------

class ReplayClient:
    """按 URL 回放 fixture；未登记的 URL → 显式失败（不许静默返回空）。"""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.urls: list[str] = []

    def get_text(self, url, **_kw):
        self.urls.append(url)
        if url not in self.pages:
            raise AssertionError(f"未录制的 URL: {url}")
        return self.pages[url]


def _replay_client() -> ReplayClient:
    pages = {}
    for name in FIXTURES:
        text, meta = fixture_text(name)
        pages[meta["url"]] = text
    list_text, list_meta = fixture_text("sse_holiday_list")
    pages[list_meta["url"]] = list_text
    return ReplayClient(pages)


def test_fetch_holiday_notices_parses_every_article_offline():
    rows = fetch_holiday_notices(_replay_client())
    dates = {r.date for r in rows}
    assert {"2026-01-01", "2026-09-25", "2026-10-01", "2025-01-01"} <= dates
    assert {r.doc_kind for r in rows} == {"annual", "holiday"}
    assert {r.source for r in rows} == {"sse"}


def test_fetch_fails_closed_when_an_article_cannot_be_parsed():
    """任一篇解析不出 → 整批失败（半批数据会让调用方以为「别的节都开市」）。"""
    from stocklab.data.errors import FetchError

    pages = dict(_replay_client().pages)
    broken = [u for u in pages if "c_20260611" in u][0]
    pages[broken] = "<html><body>一、休市安排：另行通知。</body></html>"
    with pytest.raises(FetchError, match="休市公告解析失败"):
        fetch_holiday_notices(ReplayClient(pages))


def test_fetch_fails_closed_when_the_list_page_has_no_articles():
    from stocklab.data.errors import FetchError

    _, meta = fixture_text("sse_holiday_list")
    with pytest.raises(FetchError, match="一条公告都没解析出来"):
        fetch_holiday_notices(ReplayClient({meta["url"]: "<html></html>"}))


def test_article_list_keeps_only_holiday_notices():
    text, _ = fixture_text("sse_holiday_list")
    arts = sse.parse_article_list(text)
    assert len(arts) == 15
    assert all("休市安排" in a["title"] for a in arts)
    assert sum(a["doc_kind"] == "annual" for a in arts) == 2      # 2025 / 2026 年度通知
