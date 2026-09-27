"""P88：公告（东财 np-anotice）与北向季度持股（东财 datacenter）**源客户端**。

全部离线：`client` 是假 `HttpClient`，`conftest` 的 autouse 守卫挡真实联网。
真源实测（2026-09-27 只读探针）记在任务书 §0.2 与 §7，**不是** pytest 用例 ——
这里把那次读数（`total_hits=2784`、北向 `HOLD_SHARES=893478198`）钉成离线判据。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from stocklab.data.sources import eastmoney_ann, northbound

NOW = "2026-09-27T10:00:00+08:00"


class FakeClient:
    """按调用次序返回预置响应体，并记下每次请求的 URL。"""

    def __init__(self, pages: list[str]):
        self.pages = list(pages)
        self.urls: list[str] = []

    def get_text(self, url, **_kw):
        self.urls.append(url)
        i = min(len(self.urls) - 1, len(self.pages) - 1)
        return self.pages[i]


def _ann_row(art: str, date: str, *, title: str = "美的集团:公告",
             col: str | None = "调研活动") -> dict:
    cols = [{"column_code": "050003", "column_name": col}] if col else []
    return {
        "art_code": art,
        "codes": [{"inner_code": "44052442836154", "market_code": "0",
                   "short_name": "美的集团", "stock_code": "000333",
                   "ann_type": "A,SZA"}],
        "columns": cols,
        "display_time": f"{date} 19:35:36:855",
        "eiTime": f"{date} 19:35:23:000",
        "language": "0",
        "listing_state": "0",
        "notice_date": f"{date} 00:00:00",
        "source_type": "324",
        "title": title,
        "title_ch": title,
    }


def _ann_page(rows: list[dict], total_hits: int | None = None) -> str:
    return json.dumps({"data": {"total_hits": total_hits if total_hits is not None
                                else len(rows), "list": rows}})


# ══════════════════════════════════════════════════════════════════════
# 公告：URL / 解析
# ══════════════════════════════════════════════════════════════════════


def test_ann_url_is_the_probed_shape():
    """URL 逐字复刻 §0.2 的实测形状（少一个参数都可能换回一个空列表）。"""
    url = eastmoney_ann.announcement_url("000333", page=2)
    assert url.startswith(eastmoney_ann.ANN_URL + "?")
    for frag in ("sr=-1", "page_size=50", "page_index=2", "ann_type=A",
                 "client_source=web", "stock_list=000333"):
        assert frag in url, frag
    # ⚠️ 时间窗参数**刻意不出现**：`begin_time`/`end_time` 一加就 total_hits=0
    assert "begin_time" not in url and "end_time" not in url


def test_ann_parse_trims_notice_date_and_pins_fields():
    payload = json.loads(_ann_page([_ann_row("AN202609151829423536", "2026-09-15")]))
    rows = eastmoney_ann.parse_announcements(payload, "000333")
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "000333"
    assert r["art_code"] == "AN202609151829423536"
    # PIT 锚 = 公告日（裁掉 " 00:00:00"）；display_time 只作留痕、保留源站原样
    assert r["notice_date"] == "2026-09-15"
    assert r["display_time"] == "2026-09-15 19:35:36:855"
    assert r["column_name"] == "调研活动"
    assert r["ann_type"] == eastmoney_ann.ANN_TYPE
    assert r["source"] == eastmoney_ann.SOURCE


def test_ann_parse_falls_back_to_title_ch_and_tolerates_no_columns():
    row = _ann_row("AN1", "2026-09-01", col=None)
    row["title"] = ""
    row["title_ch"] = "中文标题"
    rows = eastmoney_ann.parse_announcements(
        json.loads(_ann_page([row])), "000333")
    assert rows[0]["title"] == "中文标题"
    assert rows[0]["column_name"] is None


def test_ann_parse_skips_rows_without_art_code_or_notice_date():
    bad = _ann_row("AN2", "2026-09-01")
    bad["art_code"] = ""
    nodate = _ann_row("AN3", "2026-09-01")
    nodate["notice_date"] = ""
    rows = eastmoney_ann.parse_announcements(
        json.loads(_ann_page([bad, nodate, _ann_row("AN4", "2026-09-02")])), "000333")
    assert [r["art_code"] for r in rows] == ["AN4"]


# ══════════════════════════════════════════════════════════════════════
# 公告：翻页 + cutoff 停止 + 截断标记（D5）
# ══════════════════════════════════════════════════════════════════════


def test_ann_fetch_stops_at_cutoff_and_filters_the_window():
    """第 2 页出现 `notice_date < cutoff` ⇒ 停在那一页，不报错。

    落在 cutoff 之前的行**不落**（这是增量窗口）；返回的 truncated 为 False。
    """
    p1 = _ann_page([_ann_row(f"P1-{i}", d) for i, d in
                    enumerate(["2026-09-20", "2026-09-18", "2026-09-15"])],
                   total_hits=500)
    p2 = _ann_page([_ann_row("P2-0", "2026-09-10"),   # < cutoff 2026-09-12
                    _ann_row("P2-1", "2026-08-01")], total_hits=500)
    client = FakeClient([p1, p2])
    rows, refs, truncated = eastmoney_ann.fetch_announcements(
        client, code="000333", cutoff="2026-09-12", page_limit=5)

    assert len(client.urls) == 2, "第 2 页见到 < cutoff 就该停，不该继续翻"
    assert truncated is False
    assert {r["art_code"] for r in rows} == {"P1-0", "P1-1", "P1-2"}
    assert all(r["notice_date"] >= "2026-09-12" for r in rows)
    # 每页一条 ref（溯源：这一页来自哪份原始响应）
    assert [len(ref["resp_sha256"]) for ref in refs] == [64, 64]


def test_ann_fetch_marks_truncated_at_page_limit_without_erroring():
    """`--page-limit` 是硬上限：跑满仍未见 cutoff ⇒ truncated=True，**不报错**。"""
    pages = [_ann_page([_ann_row(f"P{n}-0", "2026-09-20")], total_hits=500)
             for n in range(1, 9)]
    client = FakeClient(pages)
    rows, _refs, truncated = eastmoney_ann.fetch_announcements(
        client, code="000333", cutoff="2026-01-01", page_limit=3)
    assert len(client.urls) == 3
    assert truncated is True
    assert len(rows) == 3


def test_ann_fetch_stops_at_end_of_list_and_is_not_truncated():
    """取完了（page*size >= total_hits）⇒ 正常收尾，不是截断。"""
    client = FakeClient([_ann_page([_ann_row("A", "2026-09-20")], total_hits=1),
                         _ann_page([], total_hits=1)])
    rows, _refs, truncated = eastmoney_ann.fetch_announcements(
        client, code="000333", cutoff="2026-01-01", page_limit=5)
    assert truncated is False and len(rows) == 1


def test_ann_fetch_stops_on_empty_page():
    client = FakeClient([_ann_page([], total_hits=0)])
    rows, _refs, truncated = eastmoney_ann.fetch_announcements(
        client, code="000333", cutoff="2026-01-01", page_limit=5)
    assert rows == [] and truncated is False


def test_ann_rows_carry_the_sha_of_the_page_they_came_from():
    """`resp_sha256` 必须指回**那一页的原始正文**（可复现、可溯源）。"""
    body = _ann_page([_ann_row("A", "2026-09-20")], total_hits=1)
    client = FakeClient([body])
    rows, refs, _t = eastmoney_ann.fetch_announcements(
        client, code="000333", cutoff="2026-01-01", page_limit=5)
    assert rows[0]["resp_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert refs[0]["resp_sha256"] == rows[0]["resp_sha256"]
    assert refs[0]["page"] == 1


def test_ann_rows_keep_source_order_descending():
    """按源站顺序（notice_date 倒序）返回，不重排 —— 落库顺序即源顺序。"""
    p1 = _ann_page([_ann_row("A", "2026-09-20"), _ann_row("B", "2026-09-18")],
                   total_hits=2)
    client = FakeClient([p1])
    rows, _refs, _t = eastmoney_ann.fetch_announcements(
        client, code="000333", cutoff="2026-01-01", page_limit=5)
    assert [r["notice_date"] for r in rows] == ["2026-09-20", "2026-09-18"]


# ══════════════════════════════════════════════════════════════════════
# 北向：URL / 解析（季度口径）
# ══════════════════════════════════════════════════════════════════════

#: 真源实测原文（2026-09-27 只读探针，§0.2）：`000333.SZ` 全历史就这一行。
NB_REAL = {"result": {"pages": 1, "count": 1, "data": [{
    "SECURITY_INNER_CODE": "1000240855", "SECUCODE": "000333.SZ",
    "TRADE_DATE": "2026-06-30 00:00:00", "SECURITY_CODE": "000333",
    "SECURITY_NAME": "美的集团", "MUTUAL_TYPE": "003", "CHANGE_RATE": -2.251844182736,
    "CLOSE_PRICE": 75.53, "HOLD_SHARES": 893478198,
    "HOLD_MARKET_CAP": 67484408294.94, "A_SHARES_RATIO": 12.83,
    "HOLD_SHARES_RATIO": 12.83, "FREE_SHARES_RATIO": 13.0201,
    "TOTAL_SHARES_RATIO": 11.7355, "HOLD_MARKETCAP_CHG1": None,
}]}}


def test_nb_url_uses_secucode_filter_and_page_size_500():
    url = northbound.northbound_url("000333.SZ", page=1)
    assert northbound.NORTHBOUND_REPORT in url
    assert "pageSize=500" in url and "pageNumber=1" in url
    assert "%22000333.SZ%22" in url or 'SECUCODE="000333.SZ"' in url


def test_nb_secucode_maps_market_prefix():
    assert northbound.secucode("000333") == "000333.SZ"
    assert northbound.secucode("600690") == "600690.SH"
    assert northbound.secucode("510300") == "510300.SH"


def test_nb_parse_pins_the_probed_quarterly_row():
    """把 §0.2 的实测值逐位钉住：`trade_date=2026-06-30`、`hold_shares=893478198`。"""
    rows = northbound.parse_northbound_rows(NB_REAL, "000333")
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "000333"
    assert r["trade_date"] == "2026-06-30"
    assert r["hold_shares"] == 893478198
    assert r["hold_market_cap"] == 67484408294.94
    assert r["a_shares_ratio"] == 12.83
    assert r["hold_shares_ratio"] == 12.83
    assert r["free_shares_ratio"] == 13.0201
    assert r["close_price"] == 75.53
    # 频率**恒** quarterly：公开源上北向日度早已不存在（§0.3），不许写 daily
    assert r["frequency"] == "quarterly"
    assert r["source"] == northbound.SOURCE


def test_nb_parse_returns_empty_for_a_null_result():
    """`result=null` 是**合法空**（该标的没有北向持股行），不是抓取失败。"""
    assert northbound.parse_northbound_rows({"result": None}, "000333") == []


def test_nb_parse_skips_rows_without_trade_date_and_keeps_none_for_missing_numbers():
    payload = {"result": {"data": [
        {"TRADE_DATE": "", "HOLD_SHARES": 1},
        {"TRADE_DATE": "2026-03-31 00:00:00", "HOLD_SHARES": None,
         "HOLD_MARKET_CAP": "-"},
    ]}}
    rows = northbound.parse_northbound_rows(payload, "000333")
    assert len(rows) == 1
    assert rows[0]["hold_shares"] is None      # 拿不到写 NULL，不填 0
    assert rows[0]["hold_market_cap"] is None


def test_nb_fetch_filters_window_and_sorts_ascending():
    pages = json.dumps({"result": {"pages": 1, "data": [
        {"TRADE_DATE": "2026-06-30 00:00:00", "HOLD_SHARES": 30},
        {"TRADE_DATE": "2026-03-31 00:00:00", "HOLD_SHARES": 20},
        {"TRADE_DATE": "2024-12-31 00:00:00", "HOLD_SHARES": 10},
    ]}})
    client = FakeClient([pages])
    rows, refs = northbound.fetch_northbound_holdings(
        client, code="000333", start="2025-01-01")
    assert [r["trade_date"] for r in rows] == ["2026-03-31", "2026-06-30"]
    assert all(r["frequency"] == "quarterly" for r in rows)
    assert len(refs) == 1 and len(refs[0]["resp_sha256"]) == 64


def test_nb_fetch_pages_until_total_pages():
    p1 = json.dumps({"result": {"pages": 2, "count": 2, "data": [
        {"TRADE_DATE": "2026-06-30 00:00:00", "HOLD_SHARES": 2}]}})
    p2 = json.dumps({"result": {"pages": 2, "count": 2, "data": [
        {"TRADE_DATE": "2026-03-31 00:00:00", "HOLD_SHARES": 1}]}})
    client = FakeClient([p1, p2])
    rows, refs = northbound.fetch_northbound_holdings(
        client, code="000333", start="2000-01-01", page_size=1)
    assert len(client.urls) == 2
    assert [r["hold_shares"] for r in rows] == [1, 2]
    assert [ref["page"] for ref in refs] == [1, 2]


def test_a_null_result_is_a_legal_empty_not_a_failure():
    """源站说「这只标的没有北向持股行」⇒ 空列表，不是异常（ETF/未纳入标的如此）。"""
    client = FakeClient([json.dumps({"result": None})])
    rows, refs = northbound.fetch_northbound_holdings(
        client, code="000333", start="2025-01-01")
    assert rows == []
    # 那一页**确实取过** ⇒ 留一条溯源（空结果也要能说清「从哪份响应读出来的」）
    assert len(refs) == 1 and refs[0]["page"] == 1


@pytest.mark.parametrize("bad", ["", "00033", "0003333", "abcdef"])
def test_nb_secucode_rejects_malformed_codes(bad):
    with pytest.raises(ValueError):
        northbound.secucode(bad)
