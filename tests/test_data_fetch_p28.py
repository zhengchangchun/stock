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
