"""P11：快照领域层（`Quote` → `quote_snapshots` 行）+ `fetch_quotes`。

本文件的重点是**身份键**与**不造数**：

  - 身份键里是**源站自己的 `ts`**，不是我们的 `fetched_at` —— 于是「同一刻的市场」
    被拿两次仍然是一行（幂等），而「重抓时写个新时间戳」不会凭空多出一行；
  - 字段缺失（时间戳不可识别 / `amount` 为 None）**显式报错或保持 None**，
    绝不用本地时钟或 0 顶上。
"""

from __future__ import annotations

import pytest

from stocklab.config.paths import FIXTURE_DIR
from stocklab.data.errors import FetchError
from stocklab.data.fetch import fetch_quotes
from stocklab.data.models import Quote
from stocklab.data.raw_cache import load_fixture
from stocklab.session.quotes import (build_rows, missing_codes, snapshot_row,
                                     trade_date_from_ts, ts_is_complete)

QUOTE_FIXTURE = "tencent_quote_sz000333_sh600690"
FETCHED = "2026-09-15T11:47:03+08:00"


@pytest.fixture(scope="module")
def quote_text() -> str:
    body, _ = load_fixture(FIXTURE_DIR, QUOTE_FIXTURE)
    return body.decode("gbk")


def _q(code="000333", ts="20260915114321", **over) -> Quote:
    base = dict(code=code, name="美的集团", price=87.56, pre_close=86.80, open=86.70,
                high=88.10, low=86.44, volume=10_558_300, amount=925_191_406.0,
                turnover=0.15, pe_ttm=15.05, float_mv=None, total_mv=None, pb=None,
                ts=ts)
    base.update(over)
    return Quote(**base)


class _FakeClient:
    """记录 `get_text` 的入参：本文件要钉住「快照请求**绕过** raw_cache」。"""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def get_text(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return self.text


# ---------- 交易日 / 时间戳 ----------

def test_trade_date_from_ts():
    assert trade_date_from_ts("20260915114321") == "2026-09-15"


@pytest.mark.parametrize("bad", ["", "  ", "2026-09-15", "2026091X", None])
def test_trade_date_from_ts_refuses_to_guess(bad):
    """不可识别的时间戳 → `ValueError`。**不许**退化成「用本地时钟编一个」。"""
    with pytest.raises(ValueError):
        trade_date_from_ts(bad)


def test_ts_is_complete():
    assert ts_is_complete("20260915114321")
    assert not ts_is_complete("2026091511")     # 截断：时刻不可信
    assert not ts_is_complete("")


# ---------- 行构造 ----------

def test_snapshot_row_fields():
    row = snapshot_row(_q(), fetched_at=FETCHED)
    assert row["code"] == "000333"
    assert row["trade_date"] == "2026-09-15"
    assert row["ts"] == "20260915114321"
    assert row["amount"] == 925_191_406.0
    assert row["turnover"] == 0.15
    assert row["volume"] == 10_558_300
    assert row["fetched_at"] == FETCHED
    assert "adj_mode" not in row                 # 快照是当下价，没有复权口径


def test_none_stays_none_and_is_never_coerced_to_zero():
    """`amount=None` 必须保持 `None` —— 0 与「源站没给」在下游必须分得开。"""
    row = snapshot_row(_q(amount=None, turnover=None), fetched_at=FETCHED)
    assert row["amount"] is None and row["turnover"] is None


def test_identity_key_excludes_fetched_at():
    """同一份截面、两次不同的抓取时刻 → 除 `fetched_at` 外逐字段相同（ADR-005）。"""
    a = snapshot_row(_q(), fetched_at="2026-09-15T11:47:03+08:00")
    b = snapshot_row(_q(), fetched_at="2026-09-15T13:47:11+08:00")
    assert {k: v for k, v in a.items() if k != "fetched_at"} == \
           {k: v for k, v in b.items() if k != "fetched_at"}


def test_build_rows_sorted_and_deduped():
    rows, errors = build_rows([_q(code="600690"), _q(code="000333"),
                               _q(code="000333")], fetched_at=FETCHED)
    assert [r["code"] for r in rows] == ["000333", "600690"]   # 稳定顺序
    assert errors == {}


def test_build_rows_reports_bad_ts_without_killing_the_rest():
    """一个坏标的只影响它自己：其余照常入行，坏的那个进 `errors`。"""
    rows, errors = build_rows([_q(code="000333"), _q(code="600690", ts="")],
                              fetched_at=FETCHED)
    assert [r["code"] for r in rows] == ["000333"]
    assert "600690" in errors and "时间戳" in errors["600690"]


def test_missing_codes_reports_what_the_source_did_not_return():
    quotes = [_q(code="000333")]
    assert missing_codes(["sz000333", "sh600690"], quotes) == ["600690"]
    assert missing_codes(["sz000333"], quotes) == []


# ---------- fetch_quotes ----------

def test_fetch_quotes_parses_real_fixture(quote_text):
    client = _FakeClient(quote_text)
    quotes = fetch_quotes(client, codes=["sz000333", "sh600690"])
    assert {q.code for q in quotes} == {"000333", "600690"}
    assert all(q.ts for q in quotes)
    assert len(client.calls) == 1                 # 一次请求拿全部代码


def test_fetch_quotes_bypasses_the_raw_cache(quote_text):
    """`cache_key=""` 是本命令最要紧的一行：命中缓存 = 拿旧截面冒充新截面。"""
    client = _FakeClient(quote_text)
    fetch_quotes(client, codes=["sz000333"])
    call = client.calls[0]
    assert call["cache_key"] == ""
    assert call["encoding"] == "gbk"
    assert "qt.gtimg.cn" in call["url"]


def test_fetch_quotes_empty_response_is_a_hard_error():
    """全空 ≠「今天没有行情」：格式变了/接口降级了，必须报出来。"""
    with pytest.raises(FetchError):
        fetch_quotes(_FakeClient(""), codes=["sz000333"])


def test_fetch_quotes_requires_codes():
    with pytest.raises(ValueError):
        fetch_quotes(_FakeClient(""), codes=[])
