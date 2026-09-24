"""P28 估值 / 资金流抓取层：离线回放**真实** fixture（无网络）。

fixture 来源：2026-09-16 `scripts/record_p28_fixtures.py` 的真实网络响应
（raw_cache → record_fixture），meta 里有 url / fetched_at / sha256。

覆盖点（P28 计划 §3）：
  - 资金流按 `page` 翻页，翻到空数组即止；
  - 估值按 `result.pages` 翻页，末页允许短页；
  - fail-closed：非末页行数不符 → 抛 `FetchError`（不许把半截当完整）；
  - ETF 无数据（`result:null` / 9201）→ 返回 `[]`（合法，由调用方显式留痕）；
  - 每行返回 `(row, resp_sha256, cache_key)` 三元组，可溯源。
"""

import hashlib
import json
import re

import pytest

from stocklab.config.paths import FIXTURE_DIR
from stocklab.data.errors import FetchError
from stocklab.data.fetch import fetch_money_flow_daily, fetch_valuation_daily
from stocklab.data.raw_cache import load_fixture

_PAGE_RE = re.compile(r"(?:pageNumber|page)=(\d+)")


class Replay:
    """按 URL 里的页码回放响应；记录请求过的 URL 供断言。"""

    def __init__(self, responses: dict[int, str]):
        self.responses = responses
        self.urls: list[str] = []

    def get_text(self, url, **_kw):
        self.urls.append(url)
        m = _PAGE_RE.search(url)
        page = int(m.group(1)) if m else 1
        return self.responses[page]


def _fixture(name: str) -> str:
    body, _meta = load_fixture(FIXTURE_DIR, name)
    return body.decode("utf-8")


def _val_payload(pages: int, dates: list[str]) -> str:
    return json.dumps({"result": {"pages": pages, "data": [
        {"TRADE_DATE": f"{d} 00:00:00", "PE_TTM": "10.0", "PB_MRQ": "2.0"}
        for d in dates
    ]}})


# ---------- 资金流（新浪） ----------

def test_moneyflow_paginates_and_terminates_on_empty():
    client = Replay({1: _fixture("sina_moneyflow_000333_p1_n3"),
                     2: _fixture("sina_moneyflow_000333_pastend_n3")})
    rows = fetch_money_flow_daily(client, code="sz000333", start="2026-09-01",
                                  end="2026-09-16", page_size=3)
    assert len(rows) == 3
    assert len(client.urls) == 2                        # 第 2 页空 → 停
    assert all(m.code == "000333" for m, _, _ in rows)  # 前缀已剥
    assert [m.date for m, _, _ in rows] == sorted(m.date for m, _, _ in rows)
    assert all(m.source == "sina" for m, _, _ in rows)


def test_moneyflow_rows_carry_sha_and_cache_key():
    client = Replay({1: _fixture("sina_moneyflow_000333_p1_n3"),
                     2: _fixture("sina_moneyflow_000333_pastend_n3")})
    rows = fetch_money_flow_daily(client, code="sz000333", start="2026-09-01",
                                  end="2026-09-16", page_size=3)
    _, sha, key = rows[0]
    text = _fixture("sina_moneyflow_000333_p1_n3")
    assert sha == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert key == "moneyflow:sz000333:2026-09-16:1:3"   # 第 3 段 = end 锚点（ADR-009）
    assert "page=1" in client.urls[0] and "num=3" in client.urls[0]


def test_moneyflow_fail_closed_on_oversize_page():
    # 源站口径变化：page_size=3 却回了 4 行 → 拒绝解析
    payload = json.dumps([
        {"opendate": f"2026-09-{d:02d}", "trade": "1", "changeratio": "0",
         "turnover": "1", "netamount": "1", "ratioamount": "0", "r0_net": "0"}
        for d in (16, 15, 14, 13)
    ])
    client = Replay({1: payload})
    with pytest.raises(FetchError, match="口径变化"):
        fetch_money_flow_daily(client, code="sz000333", start="2026-09-01",
                               end="2026-09-16", page_size=3)


# ---------- 资金流翻页：`start` 越界即停（P67 T1） ----------
#
# 旧口径**先翻到底、再按 `[start,end]` 过滤** ⇒ 「增量窗口」在请求数上完全失效：
# 真库实测每只标的 4019 行 / 42 页，21 只 = 882 请求 / 269s，换 0 行当日新数据
# （P67 §1③）。页按 `opendate` **降序**（`asc=0`），所以某页最老一行 `<= start`
# 时再往后翻只会更老 —— 立即停。
#
# 下面这组用例的**基准是旧口径本身**（`_ref_fetch_moneyflow`）：新旧两版在同一个
# 假源上返回的**三元组逐条相同**（含 `resp_sha256` / `cache_key`），只有请求数不同。

def _mf_dates_desc(n: int, end: str = "2026-09-23") -> list[str]:
    """`end` 往前 n 个交易日（跳过周末）的**降序**日期表 —— 与源站 `asc=0` 同形状。"""
    from datetime import date, timedelta

    day = date.fromisoformat(end)
    out: list[str] = []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return out


def _mf_page_body(dates: list[str]) -> str:
    return json.dumps([
        {"opendate": d, "trade": "10.0", "changeratio": "0", "turnover": "1",
         "netamount": "1", "ratioamount": "0", "r0_net": "0"}
        for d in dates
    ])


class PagedSina:
    """整段历史按页给（降序、每页 `page_size` 条、翻过末页给空数组），记下请求过的页。"""

    def __init__(self, dates_desc: list[str], page_size: int):
        self.page_size = page_size
        self.pages = {i // page_size + 1: dates_desc[i:i + page_size]
                      for i in range(0, len(dates_desc), page_size)}
        self.urls: list[str] = []

    def get_text(self, url, **_kw):
        self.urls.append(url)
        m = _PAGE_RE.search(url)
        page = int(m.group(1)) if m else 1
        return _mf_page_body(self.pages.get(page, []))

    @property
    def pages_requested(self) -> int:
        return len(self.urls)


def _ref_fetch_moneyflow(client, *, code: str, start: str, end: str, page_size: int):
    """**旧口径**（翻到底再过滤）—— 对拍基准，与实现改动无关的独立复写。"""
    from stocklab.data.fetch import MAX_PAGES
    from stocklab.data.sources import sina

    out = []
    page = 1
    while page <= MAX_PAGES:
        text = client.get_text(sina.moneyflow_url(code, page=page, num=page_size),
                               source="sina", cache_key="ref")
        rows = sina.parse_moneyflow(json.loads(text), code)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        out.extend((r, sha, f"moneyflow:{code}:{end}:{page}:{page_size}") for r in rows)
        if len(rows) < page_size:
            break
        page += 1
    out = [t for t in out if start <= t[0].date <= end]
    out.sort(key=lambda t: t[0].date)
    return out


@pytest.mark.parametrize("page_size", [3, 5, 10])
def test_moneyflow_stop_at_boundary_returns_the_same_rows_as_bruteforce(page_size):
    """主张：**输出逐条不变** —— 只省请求，不改结果（同 `(code,start,end)`）。

    窗口跨度 = 一页 + 1 行 ⇒ 第 1 页最老一行仍然 **新于** `start`（不能停），
    第 2 页才翻到边界。既钉「逐条相同」，也钉「该多翻的没少翻」。
    """
    dates = _mf_dates_desc(120)                  # 半年历史，24 页（page_size=5）
    start, end = dates[page_size], dates[0]
    new = PagedSina(dates, page_size)
    old = PagedSina(dates, page_size)

    got = fetch_money_flow_daily(new, code="sz000333", start=start, end=end,
                                 page_size=page_size)
    want = _ref_fetch_moneyflow(old, code="sz000333", start=start, end=end,
                                page_size=page_size)

    assert got == want                                  # 逐条相同（含 sha / cache_key）
    assert [m.date for m, _, _ in got] == sorted(m.date for m, _, _ in got)
    assert [m.date for m, _, _ in got] == sorted(dates[:page_size + 1])
    assert new.pages_requested == 2 < old.pages_requested


def test_moneyflow_stops_when_page_oldest_equals_start():
    """边界：`min(该页日期) == start` **可以停**（该页已含 `start` 那天的行）。"""
    dates = _mf_dates_desc(30)
    client = PagedSina(dates, 10)
    rows = fetch_money_flow_daily(client, code="sz000333", start=dates[29],
                                  end=dates[0], page_size=10)
    assert client.pages_requested == 3                  # 第 3 页最老一行就是 start
    assert [m.date for m, _, _ in rows] == sorted(dates)


def test_moneyflow_keeps_paging_while_page_is_still_inside_the_window():
    """反向：第 1 页最老一行 **新于** `start` ⇒ 窗口没到头，必须继续翻（不能提前停）。

    窗口 `[dates[12], dates[0]]` 跨到第 2 页中部：第 1 页最老是 `dates[9]`（> start），
    第 2 页最老是 `dates[19]`（< start）⇒ 恰好在第 2 页收口。
    """
    dates = _mf_dates_desc(30)
    client = PagedSina(dates, 10)
    rows = fetch_money_flow_daily(client, code="sz000333", start=dates[12],
                                  end=dates[0], page_size=10)
    assert client.pages_requested == 2
    assert [m.date for m, _, _ in rows] == sorted(dates[:13])


def test_moneyflow_start_older_than_all_rows_still_pages_to_the_end():
    """`start` 比整段历史都老 ⇒ 边界永不触发，照旧翻到短页为止。"""
    dates = _mf_dates_desc(23)
    new, old = PagedSina(dates, 5), PagedSina(dates, 5)
    got = fetch_money_flow_daily(new, code="sz000333", start="2000-01-01",
                                 end=dates[0], page_size=5)
    want = _ref_fetch_moneyflow(old, code="sz000333", start="2000-01-01",
                                end=dates[0], page_size=5)
    assert got == want
    assert new.pages_requested == old.pages_requested == 5   # 4 满页 + 1 短页


def test_moneyflow_start_newer_than_newest_page_stops_after_one_page():
    """窗口整个落在最新一页之内 ⇒ 1 个请求（旧口径要翻完整段历史）。"""
    dates = _mf_dates_desc(4019)                        # 与真库同量级（4019 行）
    new, old = PagedSina(dates, 100), PagedSina(dates, 100)
    got = fetch_money_flow_daily(new, code="sz000333", start=dates[0],
                                 end=dates[0], page_size=100)
    want = _ref_fetch_moneyflow(old, code="sz000333", start=dates[0], end=dates[0],
                                page_size=100)
    assert got == want
    assert [m.date for m, _, _ in got] == [dates[0]]
    assert new.pages_requested == 1
    assert old.pages_requested == 41                    # 4019 行 / 100 = 41 页


def test_moneyflow_days30_window_costs_at_most_one_page_per_code():
    """§4.2 判据：`--days 30`（≈21 个交易日）下 **21 只标的总请求数 ≤ 21**（旧 ≈882）。

    真库实测（§1③）：每只标的 4019 行 = 41 页满 + 1 短页 ⇒ 旧口径 42 请求/只。
    """
    dates = _mf_dates_desc(4019)                        # 与真库同量级（4019 行）
    start, end = dates[21], dates[0]                    # --days 30 ≈ 21 个交易日
    per_code: list[int] = []
    for code in ("sz000333", "sh600690"):
        client = PagedSina(dates, 100)
        rows = fetch_money_flow_daily(client, code=code, start=start, end=end,
                                      page_size=100)
        per_code.append(client.pages_requested)
        assert [m.date for m, _, _ in rows] == sorted(dates[:22])
        assert all(m.code == code[2:] for m, _, _ in rows)
    assert per_code == [1, 1]                           # 每只 1 个请求 ⇒ 21 只 = 21
    assert sum(per_code) / len(per_code) * 21 <= 21


# ---------- 估值（东财 datacenter） ----------

def test_valuation_no_data_returns_empty():
    """ETF 无估值（result:null / 9201）→ []，不抛错（由调用方留痕）。"""
    client = Replay({1: _fixture("eastmoney_valuation_510300_p1_s3")})
    rows = fetch_valuation_daily(client, code="510300", start="2026-09-01",
                                 end="2026-09-16", page_size=3)
    assert rows == []


def test_valuation_paginates_via_result_pages():
    client = Replay({
        1: _val_payload(2, ["2026-09-16", "2026-09-15", "2026-09-14"]),
        2: _val_payload(2, ["2026-09-13"]),             # 末页允许短页
    })
    rows = fetch_valuation_daily(client, code="000333", start="2026-09-01",
                                 end="2026-09-16", page_size=3)
    assert [r.date for r, _, _ in rows] == \
        ["2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16"]  # 升序
    assert len(client.urls) == 2


def test_valuation_fail_closed_on_truncated_nonfinal_page():
    client = Replay({
        1: _val_payload(2, ["2026-09-16", "2026-09-15"]),   # 非末页仅 2 行
        2: _val_payload(2, ["2026-09-13"]),
    })
    with pytest.raises(FetchError, match="截断"):
        fetch_valuation_daily(client, code="000333", start="2026-09-01",
                              end="2026-09-16", page_size=3)


def test_valuation_fetch_carries_sha_and_cache_key():
    text = _val_payload(1, ["2026-09-16", "2026-09-15"])
    client = Replay({1: text})
    rows = fetch_valuation_daily(client, code="000333", start="2026-09-01",
                                 end="2026-09-16", page_size=3)
    _, sha, key = rows[0]
    assert sha == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert key == "valuation:000333:2026-09-16:1:3"      # 第 3 段 = end 锚点（ADR-009）
