import bisect
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from stocklab.store.db import transaction


@dataclass(frozen=True)
class Calendar:
    """交易日历（R11）。用指数日线的日期集合构建，不手写节假日表。

    只回答「某个日期是不是已采集到的交易日」：
    区间之外的日期一律返回 False（未知 ≠ 休市），因此回测/预测取
    ``next_trading_day`` 时会显式 IndexError，而不是静默给出错误日期。
    """

    _dates: tuple[str, ...]

    @classmethod
    def from_dates(cls, dates: Iterable[str | None]) -> "Calendar":
        return cls(tuple(sorted({d for d in dates if d})))

    @property
    def all_dates(self) -> tuple[str, ...]:
        return self._dates

    def is_open(self, d: str) -> bool:
        i = bisect.bisect_left(self._dates, d)
        return i < len(self._dates) and self._dates[i] == d

    def next_trading_day(self, d: str) -> str:
        i = bisect.bisect_right(self._dates, d)
        if i >= len(self._dates):
            raise IndexError(f"日历已到末尾，{d} 之后没有交易日")
        return self._dates[i]

    def prev_trading_day(self, d: str) -> str:
        i = bisect.bisect_left(self._dates, d)
        if i == 0:
            raise IndexError(f"日历已到开头，{d} 之前没有交易日")
        return self._dates[i - 1]

    def sessions(self, start: str, end: str) -> list[str]:
        lo = bisect.bisect_left(self._dates, start)
        hi = bisect.bisect_right(self._dates, end)
        return list(self._dates[lo:hi])

    def save(self, conn: sqlite3.Connection, *, source: str, now: str) -> int:
        """幂等落库（前滚精神：重复执行不报错、不重复）。返回写入的候选行数。"""
        rows = [(d, 1, source, now) for d in self._dates]
        with transaction(conn):
            conn.executemany(
                "INSERT OR IGNORE INTO trading_calendar (date, is_open, source, created_at)"
                " VALUES (?,?,?,?)",
                rows,
            )
        return len(rows)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "Calendar":
        rows = conn.execute(
            "SELECT date FROM trading_calendar WHERE is_open = 1 ORDER BY date"
        ).fetchall()
        if not rows:
            raise ValueError("trading_calendar is empty; run ingest first")
        return cls(tuple(r["date"] for r in rows))
