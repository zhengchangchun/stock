"""Task 9：历史扩充脚本的分页与 fail-closed（离线，注入假 client）。

Falsifiability note for each test:
- test_extend_one_stops_at_empty_page:
    红灯触发：去掉 fetch_daily_bars 的 `if not bars: break`，
    或把 `extend_one` 改成只翻一页 → written 会 ≠ 4，或 calls ≠ 3。
- test_extend_one_cache_key_is_dated_and_not_qfq:
    红灯触发：把 fetch_daily_bars 里 adj 换成 "qfq" → cache_key 含 "qfq"。
    红灯触发（新）：从缓存键构造中删掉 anchor 字段 → 键不含任何日期串，
    断言 `any(NOW[:10] in k ...)` 失败（缓存碰撞回归被捕获）。
- test_extend_one_unknown_code_raises:
    红灯触发：去掉 `next(i for i in SEED_UNIVERSE if i.code == code)` 的
    StopIteration 传播（改成返回 None）→ 不再抛 StopIteration。
- test_extend_one_propagates_fetch_error:
    红灯触发：在 extend_one 内把 fetch_daily_bars 调用包进 bare `except:` →
    FetchError 被吞，pytest.raises 断言失败。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from extend_history import extend_one          # noqa: E402

from stocklab.data.errors import FetchError    # noqa: E402
from stocklab.store.db import connect          # noqa: E402
from stocklab.store.migrate import init_db     # noqa: E402

NOW = "2026-09-18T19:00:00+08:00"


class _FakeClient:
    """按 `end` 锚点回放：每次返回更早的一页，直到空。"""

    def __init__(self, pages):
        self.pages = pages          # list[dict]，从新到旧；每项是一页的 payload
        self.calls = []

    def get_text(self, url, *, headers=None, source, cache_key):
        import json
        self.calls.append(cache_key)
        i = len(self.calls) - 1
        payload = self.pages[i] if i < len(self.pages) else _page([])
        return json.dumps(payload)


def _page(rows):
    """不复权口径：key 是 "day"（不是 "qfqday"）。
    parse_kline 用 adj_mode="none" → _rows 取 key "day"。
    """
    return {"data": {"sz000333": {"day": rows}}}


def _row(date: str, close: float = 10.0):
    """腾讯 day 行：[date, open, close, high, low, volume(手)]。
    对应 parse_kline 的索引：0=date, 1=open, 2=close, 3=high, 4=low, 5=volume。
    """
    return [date, str(close), str(close), str(close), str(close), "1000"]


def test_extend_one_stops_at_empty_page(tmp_db):
    """翻到空页即停，返回最早期与写入行数。

    Mutation that turns this RED:
    - 删除 fetch_daily_bars 中的 `if not bars: break` → 不停，calls 会超过 3。
    - 或 extend_one 只翻一页 → written=2，calls=1，断言全失。
    """
    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('000333','美的','sz','main','stock',?)",
              (NOW,))
    c.commit()

    client = _FakeClient([_page([_row("2010-01-04"), _row("2010-01-05")]),
                          _page([_row("2009-12-31"), _row("2009-12-30")]),
                          _page([])])
    earliest, written = extend_one(c, client, "000333", now=NOW)
    assert earliest == "2009-12-30"
    assert written == 4
    assert len(client.calls) == 3, "空页应终止翻页"


def test_extend_one_cache_key_is_dated_and_not_qfq(tmp_db):
    """缓存键要带日期锚点；且绝不能用 qfq（ADR-003：801-2000 会静默降级）。

    fetch_daily_bars 的键形如 `kline:{tencent_code}:{anchor}:{page}:{adj}`。
    第一次调用时 anchor = end_date = NOW[:10]（即 2026-09-18）。

    Mutation that turns this RED:
    - 把 fetch_daily_bars 的 adj 参数改成 "qfq" → cache_key 含 "qfq"，第二断言失败。
    - 从 cache_key 构造中删掉 anchor 字段 → 键不含日期串，第一断言失败
      （缓存碰撞回归：不同 end 的历史请求会共享同一个缓存条目）。
    """
    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('000333','美的','sz','main','stock',?)",
              (NOW,))
    c.commit()
    client = _FakeClient([_page([])])
    extend_one(c, client, "000333", now=NOW)
    # anchor-dated: the end-date anchor must appear in every cache key
    assert any(NOW[:10] in k for k in client.calls), (
        f"缓存键未含日期锚点 {NOW[:10]!r}，缓存碰撞风险（当前键：{client.calls}）"
    )
    # no qfq: ADR-003 铁律
    assert all("qfq" not in k for k in client.calls)


def test_extend_one_unknown_code_raises(tmp_db):
    """SEED_UNIVERSE 里没有的 code 立即抛 StopIteration。

    Mutation that turns this RED:
    - 把 StopIteration 捕获返回 None → 不再抛，pytest.raises 断言失败。
    """
    init_db(tmp_db)
    c = connect(tmp_db)
    with pytest.raises(StopIteration):
        extend_one(c, _FakeClient([]), "999999", now=NOW)


def test_extend_one_propagates_fetch_error(tmp_db, monkeypatch):
    """翻页超出 max_pages 时 FetchError 必须透传出 extend_one，不许静默截断。

    策略：把 extend_history 模块引用的 fetch_daily_bars 替换为固定 max_pages=1
    的版本——一页之后 for-else 触发 FetchError；断言 extend_one 不吞这个异常。

    Falsifiability:
    - 在 extend_one 内给 fetch_daily_bars 调用加 bare `except: pass` →
      FetchError 被吞，pytest.raises 断言失败（测试变红）。
    """
    import functools
    import extend_history as _ext_mod
    from stocklab.data.fetch import fetch_daily_bars as _real_fdb

    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('000333','美的','sz','main','stock',?)",
              (NOW,))
    c.commit()

    # 永远返回有内容的页，确保翻页不因「空页」停止
    always_full = _FakeClient([_page([_row("2026-09-18")])] * 100)

    # 把 max_pages 压到 1 以强制触发 FetchError，不必构造 50 页数据
    monkeypatch.setattr(
        _ext_mod, "fetch_daily_bars",
        functools.partial(_real_fdb, max_pages=1),
    )

    with pytest.raises(FetchError):
        extend_one(c, always_full, "000333", now=NOW)
