"""已公告的休市安排（P30）：解析公告 → 落 `market_holidays` → 供 `target_date` 使用。

## 为什么另起一张表，而不是往 `trading_calendar` 里加行

`trading_calendar` 的语义是「**某个日期是不是已采集到的**交易日」—— 它由**已发生**的
指数日线日期集合构建（ADR-001 B4），是**回看**。本模块管的是「交易所**已公告**的开/休市」，
是**前瞻**。把未来日期塞进 `trading_calendar`，`Calendar.load` 会把它当成「已采集交易日」，
直接污染回测轴与 `predict` 的 `session_check`（`日历 ∩ 行情轴`）—— 那是**静默**污染，
两条防线都拦不住。故新表 `market_holidays`，语义单一。

## PIT 判断（ADR-013）

休市安排是交易所**提前公告的公开日历信息**，不是价格/成交数据。在 asof 时点它已经公开可得，
因此用它决定 `target_date` **不构成前视**。风险边界（临时休市 / 公告改期 / 只有部分年份可查）
逐条写在 ADR-013，其中「只有部分年份可查」由本模块的 `covered_year` 判据精确表达。

## 覆盖判据

`HolidayTable.covers(d)` **只在**「该日期的年份有**年度通知**」时为真。单节公告
（`doc_kind='holiday'`）只覆盖那一个节，不许拿它声称整年都知道 —— 否则「中秋公告」
会让你以为元旦也知道，于是把 1 月 1 日当成交易日。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import timedelta as _timedelta

from stocklab.store.db import transaction

#: 机构标识（落 `market_holidays.source`）。
SOURCE_SSE = "sse"

#: `（六）中秋节：9月25日（星期五）至9月27日（星期日）休市，…`
_RANGE = re.compile(
    r"(\d{1,2})月(\d{1,2})日[（(]星期([一二三四五六日天])[)）]\s*至\s*"
    r"(\d{1,2})月(\d{1,2})日[（(]星期([一二三四五六日天])[)）]\s*休市")
#: 单日休市：`（一）元旦：1月1日（星期三）休市，…`（**没有**「至」）。
#: 不覆盖它 = 静默丢掉放一天假的节（2025 元旦就是这么差点被吞掉的）。
_SINGLE = re.compile(r"(\d{1,2})月(\d{1,2})日[（(]星期([一二三四五六日天])[)）]\s*休市")
#: 条目分隔符：`（一）`…`（十）`
_BULLET = re.compile(r"（[一二三四五六七八九十]+）")
#: `另外，9月20日（星期日）、10月10日（星期六）为周末休市。`
_EXTRA_BLOCK = re.compile(r"另外[，,]([^。]*?)为周末休市")
_EXTRA_DATE = re.compile(r"(\d{1,2})月(\d{1,2})日[（(]星期([一二三四五六日天])[)）]")

_WEEKDAY_CHARS = "一二三四五六日天"


class HolidayParseError(RuntimeError):
    """公告排版与适配器约定不符 —— 显式失败，**不许**静默降级成「没有休市」。"""


def weekday_of(y: int, m: int, d: int) -> int:
    """0=**周一** … 6=周日（`datetime.date.weekday()` 的约定）。

    全项目**单一真源**：`predict.service` 的工作日外推也用它。

    为什么这里用 `datetime` 而不是手写 Sakamoto 公式：手写版**踩过两次**
    （见 ERROR_DIARY 2026-09-17）——
    ① Sakamoto 的返回值是 **0=周日**，而调用方按 **0=周一** 用，于是判周末时把
       周五/周六当周末、把**周日**当交易日；② 项目里算日期用的
       `y*372 + m*31 + d` 序数方案**表示不了 31 号**（`divmod` 会吐出 `dd=0`）。
    标准库既不会有约定歧义、也会对非法日期直接抛 `ValueError`（fail-closed）。
    """
    return _date(y, m, d).weekday()


def _iso(y: int, m: int, d: int) -> str:
    """`(y, m, d)` → `YYYY-MM-DD`；非法日期抛 `HolidayParseError`（fail-closed）。

    刻意**不**沿用项目里那套 `y*372 + m*31 + d` 序数方案：它表示不了 31 号
    （`divmod` 会给出 `dd=0`），而休市区间完全可能从 1月31日 起算。
    """
    try:
        return _date(y, m, d).isoformat()
    except ValueError as exc:
        raise HolidayParseError(f"非法日期 {y}-{m}-{d}：{exc}") from exc


def _expand(y: int, sm: int, sd: int, em: int, ed: int) -> list[str]:
    """把 `[起, 止]` 展开成日期串列表（含两端），用**真实日历**逐日推进。"""
    start = _date(y, sm, sd)          # 非法日期 → ValueError，由调用方包成 ParseError
    end = _date(y, em, ed)
    n = (end - start).days
    return [(start + _timedelta(days=i)).isoformat() for i in range(n + 1)]


@dataclass(frozen=True)
class MarketHoliday:
    """一行 = 「某份公告说某个日期休市」的事实。"""

    date: str
    is_open: int                  # 公告正文只列休市日 → 本模块产出恒为 0
    source: str
    doc_kind: str                 # 'annual' | 'holiday'
    covered_year: int
    source_url: str
    published_at: str


@dataclass(frozen=True)
class HolidayTable:
    """从 `market_holidays` 读出的**只读**视图（`target_date` 的输入）。"""

    closed: frozenset[str] = frozenset()
    annual_years: frozenset[int] = frozenset()
    rows: tuple[MarketHoliday, ...] = field(default=())

    def is_closed(self, d: str) -> bool:
        return d in self.closed

    def covers(self, d: str) -> bool:
        """该日期的年份是否有**年度通知**（决定「我们知道这一天开不开市」）。"""
        return int(d[:4]) in self.annual_years

    def bounds(self) -> tuple[str, str] | None:
        if not self.closed:
            return None
        return min(self.closed), max(self.closed)


# ---------- 解析 ----------

def parse_holiday_notice(text: str, *, source_url: str, published_at: str,
                         doc_kind: str, covered_year: int) -> list[MarketHoliday]:
    """公告**纯文本** → 休市日列表（升序去重）。

    三条硬校验，任一不过即抛 `HolidayParseError`：

    1. **星期自校验**：公告把边界日的星期写进了正文，解析出的日期必须与「该日期的真实
       星期」逐字相符 —— 年份认错、正则吃错字符都会在这里变成显式失败；
    2. **区间自洽**：跨度 ≤ 10 个自然日（实测最长 9 天），且区间用**真实日历**逐日展开；
    3. **非空**：正文含「休市」却一条都解析不出 → 抛错。

    第 3 条是本模块的 fail-closed 主线：**源站换排版**必须表现为失败，而不是「今年不放假」。
    """
    if doc_kind not in ("annual", "holiday"):
        raise HolidayParseError(f"未知 doc_kind={doc_kind!r}")

    dates: set[str] = set()
    for sm, sd, sw, em, ed, ew in _RANGE.findall(text):
        y = covered_year
        try:
            days = _expand(y, int(sm), int(sd), int(em), int(ed))
        except ValueError as exc:
            raise HolidayParseError(
                f"公告里的日期非法：{sm}月{sd}日至{em}月{ed}日（{y} 年）：{exc}"
                f"（{source_url}）") from exc
        span = len(days) - 1
        if span < 0:
            raise HolidayParseError(
                f"休市区间终点早于起点：{sm}月{sd}日至{em}月{ed}日（{source_url}）")
        # 上限取 10 个自然日：实测最长是 2026 春节 2/15~2/23（**9 天**，含两个周末），
        # 再长说明正则跨段吃错了字符（例如把「春节」与下一节的日期接在一起）。
        if span > 9:
            raise HolidayParseError(
                f"休市区间跨度过大（>10 自然日）：{sm}月{sd}日至{em}月{ed}日 —— "
                f"实测最长 9 天（2026 春节），超过说明解析吃错了字符（{source_url}）")
        got_s = weekday_of(y, int(sm), int(sd))
        got_e = weekday_of(y, int(em), int(ed))
        want_s = _WEEKDAY_CHARS.index(sw)
        want_e = _WEEKDAY_CHARS.index(ew)
        if got_s != want_s or got_e != want_e:
            raise HolidayParseError(
                f"星期自校验失败：公告说 {sm}月{sd}日（星期{sw}）至 {em}月{ed}日（星期{ew}），"
                f"但按 {y} 年算是 星期{_WEEKDAY_CHARS[got_s]} 至 星期{_WEEKDAY_CHARS[got_e]}"
                f" —— 年份或正则有误，拒绝写入（{source_url}）")
        dates.update(days)

    # 单日休市（无「至」）：同一 set 去重，故与区间模式重不漏。
    for m_, d_, w_ in _SINGLE.findall(text):
        y, mi, di = covered_year, int(m_), int(d_)
        got = weekday_of(y, mi, di)
        want = _WEEKDAY_CHARS.index(w_)
        if got != want:
            raise HolidayParseError(
                f"星期自校验失败（单日）：公告说 {m_}月{d_}日（星期{w_}），"
                f"按 {y} 年算是 星期{_WEEKDAY_CHARS[got]}（{source_url}）")
        dates.add(_iso(y, mi, di))

    # 「另外，…为周末休市」：公告明说的补充休市日（多是周末，但**公告说休就休**）。
    for block in _EXTRA_BLOCK.findall(text):
        for m_, d_, w_ in _EXTRA_DATE.findall(block):
            y = covered_year
            got = weekday_of(y, int(m_), int(d_))
            want = _WEEKDAY_CHARS.index(w_)
            if got != want:
                raise HolidayParseError(
                    f"星期自校验失败（周末休市子句）：公告说 {m_}月{d_}日（星期{w_}），"
                    f"按 {y} 年算是 星期{_WEEKDAY_CHARS[got]}（{source_url}）")
            dates.add(_iso(y, int(m_), int(d_)))

    # 逐条目完整性：每个含「休市」的 `（X）…` 条目都必须至少解出一个日期。
    # 这是防「静默漏一个节」的唯一有效判据 —— 只在整体上判「非空」是不够的：
    # 2025 年度通知里「元旦：1月1日（星期三）休市」是**单日**写法，
    # 早期版本只认「A日至B日休市」，于是**悄悄漏掉元旦**而整体仍有其它日期。
    chunks = _BULLET.split(text)[1:]
    for chunk in chunks:
        if "休市" not in chunk:
            continue
        if _RANGE.search(chunk) or _SINGLE.search(chunk) or _EXTRA_DATE.search(chunk):
            continue
        raise HolidayParseError(
            f"有条目写了「休市」但解不出任何日期：{chunk.strip()[:60]!r}"
            f"（{source_url}）—— 拒绝静默漏掉一个节")

    if not dates:
        raise HolidayParseError(
            f"公告里含「休市」但一条休市日都解析不出（doc_kind={doc_kind}、"
            f"covered_year={covered_year}、{source_url}）—— 拒绝静默返回空表："
            "「源站换了排版」与「今年真的不放假」必须能区分"
        )
    return [MarketHoliday(date=d, is_open=0, source=SOURCE_SSE, doc_kind=doc_kind,
                          covered_year=covered_year, source_url=source_url,
                          published_at=published_at)
            for d in sorted(dates)]


# ---------- 落库 / 读取 ----------

def save_holidays(conn: sqlite3.Connection, rows: list[MarketHoliday], *, now: str) -> int:
    """幂等落库：`INSERT OR IGNORE`（PK = `(date, source_url)`），返回**实际新增行数**。

    不用 `INSERT OR REPLACE`：`REPLACE` 会删行重插（ERROR_DIARY 2026-09-15 那条），
    而本表的旧行必须原样留着 —— 它们是「当时公告怎么说」的证据。
    """
    before = conn.execute("SELECT COUNT(*) FROM market_holidays").fetchone()[0]
    with transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO market_holidays"
            " (date, is_open, source, doc_kind, covered_year, source_url,"
            "  published_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
            [(r.date, r.is_open, r.source, r.doc_kind, r.covered_year, r.source_url,
              r.published_at, now) for r in rows],
        )
    after = conn.execute("SELECT COUNT(*) FROM market_holidays").fetchone()[0]
    return after - before


def load_holiday_table(conn: sqlite3.Connection) -> HolidayTable:
    """读全表 → `HolidayTable`。

    **同一日期多行时取 `published_at` 最新者**（改期/临时休市 = 追加新公告，旧行保留）——
    这是本表唯一需要「取最新」的地方，也正是「新公告覆盖旧公告」这条语义的落点。

    表不存在（老库未前滚）→ 返回空表：后果就是 P30 之前的行为（`weekday_fallback`），
    **不是**新引入的谎话。
    """
    try:
        rows = conn.execute(
            "SELECT date, is_open, source, doc_kind, covered_year, source_url,"
            " published_at FROM market_holidays ORDER BY date, published_at, source_url"
        ).fetchall()
    except sqlite3.OperationalError:
        return HolidayTable()

    latest: dict[str, int] = {}
    parsed: list[MarketHoliday] = []
    annual_years: set[int] = set()
    for r in rows:
        item = MarketHoliday(date=r["date"], is_open=r["is_open"], source=r["source"],
                             doc_kind=r["doc_kind"], covered_year=r["covered_year"],
                             source_url=r["source_url"], published_at=r["published_at"])
        parsed.append(item)
        latest[item.date] = item.is_open           # 升序遍历 → 最后写入者即最新
        if item.doc_kind == "annual":
            annual_years.add(item.covered_year)

    return HolidayTable(
        closed=frozenset(d for d, is_open in latest.items() if not is_open),
        annual_years=frozenset(annual_years),
        rows=tuple(parsed),
    )
