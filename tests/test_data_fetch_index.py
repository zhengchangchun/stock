"""指数日线抓取（index_300 基准）：离线回放**真实** fixture。

fixture：`tests/fixtures/index_bars_sh000300.raw.json`（2026-09-14 录制的沪深300
真实响应，900 根；SHA256 与 `.calendar.json` 里的登记值核对后才允许使用）。

指数行情是**基准对照**的数据源（R5）：没有它就无法回答「策略跑不跑得赢躺平」。
口径：指数无复权概念 → 一律 `adj_mode="none"`（与铁律①天然一致）。
"""

import hashlib
import json

import pytest

from stocklab.config.paths import FIXTURE_DIR
from stocklab.data.fetch import fetch_index_daily
from stocklab.data.sources import tencent

CODE = "sh000300"
RAW = FIXTURE_DIR / f"index_bars_{CODE}.raw.json"
META = FIXTURE_DIR / f"index_bars_{CODE}.calendar.json"


def fixture_payload() -> dict:
    """读真实 fixture，并**校验 SHA256**（防止手改 fixture 让测试假通过）。"""
    raw = RAW.read_bytes()
    meta = json.loads(META.read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == meta["raw_sha256"]
    return json.loads(raw.decode("utf-8"))


class ReplayClient:
    """按调用次序回放响应；记录 URL 供断言。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.urls: list[str] = []

    def get_text(self, url, **_kw):
        self.urls.append(url)
        return self.pages.pop(0) if self.pages else json.dumps(
            {"code": 0, "msg": "", "data": {CODE: {"day": []}}})


def test_real_index_fixture_parses_as_index_bars():
    payload = fixture_payload()
    bars = tencent.parse_kline(payload, "000300", adj_mode="none")
    meta = json.loads(META.read_text(encoding="utf-8"))
    assert len(bars) == meta["n_bars"] == 900
    assert bars[0].date == meta["first_date"]
    assert bars[-1].date == meta["last_date"]
    assert bars[0].date < bars[-1].date
    assert all(b.close > 0 for b in bars)
    assert all(b.adj_mode == "none" for b in bars)
    assert all(b.source == "tencent" for b in bars)


def test_fetch_index_daily_uses_symbol_as_code():
    """指数用**源站符号** `sh000300` 落库：000300 同时是基金代码，6 位会撞键。"""
    payload = json.dumps(fixture_payload())
    client = ReplayClient([payload])
    bars = fetch_index_daily(client, symbol=CODE, start="2022-12-01", end="2026-09-14")
    assert len(bars) == 900
    assert {b.code for b in bars} == {CODE}
    assert all(b.adj_mode == "none" for b in bars)


def test_fetch_index_daily_pages_backwards():
    """第二页为空 → 停止；不因「只有一页」就以为抓到了全历史。"""
    payload = json.dumps(fixture_payload())
    client = ReplayClient([payload])
    bars = fetch_index_daily(client, symbol=CODE, start="1990-01-01", end="2026-09-14")
    assert len(client.urls) == 2                  # 第二页空响应 → 收敛
    assert bars[0].date == "2022-12-28"


def test_fetch_index_daily_filters_window():
    client = ReplayClient([json.dumps(fixture_payload())])
    bars = fetch_index_daily(client, symbol=CODE, start="2026-09-01", end="2026-09-14")
    assert bars
    assert all("2026-09-01" <= b.date <= "2026-09-14" for b in bars)


def test_index_symbol_must_carry_market_prefix():
    with pytest.raises(ValueError, match="指数符号"):
        fetch_index_daily(ReplayClient([]), symbol="000300", start="2020-01-01",
                          end="2020-12-31")
    with pytest.raises(ValueError, match="指数符号"):
        fetch_index_daily(ReplayClient([]), symbol="sh00030", start="2020-01-01",
                          end="2020-12-31")


def test_index_request_is_unadjusted_and_within_limits():
    """指数请求不能带 qfq（会触发 800 根静默降级），也不得超过单次上限。"""
    client = ReplayClient([json.dumps(fixture_payload())])
    fetch_index_daily(client, symbol=CODE, start="2022-12-01", end="2026-09-14")
    url = client.urls[0]
    assert url.endswith(",2000,")                    # 不复权口径（adj 为空）


def test_index_fixture_meta_records_provenance():
    """fixture 必须带完整溯源（url / params / fetched_at / sha256）——
    否则「这份数据从哪来、是不是真响应」无从复核（ERROR_DIARY 2026-09-15）。"""
    meta = json.loads(META.read_text(encoding="utf-8"))
    assert meta["params"]["param"].startswith(f"{CODE},day")
    assert meta["url"].startswith("https://web.ifzq.gtimg.cn/")
    assert meta["fetched_at"] and meta["raw_sha256"]
    assert meta["name"] == "沪深300"
