"""分页抓取（Task 16）：离线回放**真实** fixture。

fixture 来源：2026-09-15 Task 12 探针的真实网络响应（raw_cache → record_fixture），
**不是合成样本**（每份 meta 里有 url / fetched_at / sha256）。
三页拼起来 = sz000333 上市首日至今的全部 3095 根日K。

为什么必须有这一层：ADR-003 实测到的「qfq 请求 801~2000 根会被服务端**静默
降级**回 640 根，且不报错」。若抓取层不设上限，缺的 260 天会被当成完整历史
一路算进因子链 —— 这种错误没有任何下游症状，只能在这里拦。
"""

import json

import pytest

from stocklab.config.paths import FIXTURE_DIR
from stocklab.data.errors import FetchError
from stocklab.data.fetch import MAX_PAGES, fetch_daily_bars, policy_from_settings
from stocklab.data.http import RetryPolicy
from stocklab.data.raw_cache import load_fixture

# 三页真实响应：end 锚点 → fixture 名
PAGES = {
    "2026-09-16": "tencent_fqkline_day_sz000333_p2000",
    "2018-04-22": "tencent_fqkline_day_sz000333_p2000b",
    "2013-09-17": "tencent_fqkline_day_sz000333_p2000c",
}


class ReplayClient:
    """按 URL 回放真实响应；记录请求过的 URL 供断言。"""

    def __init__(self, pages=PAGES, override=None):
        self.pages = pages
        self.override = override or {}
        self.urls: list[str] = []

    def get_text(self, url, **_kw):
        self.urls.append(url)
        anchor = url.rsplit(",", 3)[-3]                 # ...,{end},{count},{adj}
        if anchor in self.override:
            return self.override[anchor]
        body, _meta = load_fixture(FIXTURE_DIR, self.pages[anchor])
        return body.decode("utf-8")


def test_fetches_full_history_via_pagination():
    client = ReplayClient()
    bars = fetch_daily_bars(client, code="sz000333", start="2013-01-01",
                            end="2026-09-16")
    assert len(bars) == 3095                            # 2000 + 1095 + 0
    assert bars[0].date == "2013-09-18"                 # 上市首日
    assert bars[-1].date == "2026-09-14"
    assert len(client.urls) == 3                        # 翻到空页为止
    assert all(b.adj_mode == "none" for b in bars)      # 铁律①：不复权


def test_bars_are_sorted_and_deduplicated():
    bars = fetch_daily_bars(ReplayClient(), code="sz000333",
                            start="2013-01-01", end="2026-09-16")
    dates = [b.date for b in bars]
    assert dates == sorted(dates)
    assert len(set(dates)) == len(dates)


def test_fetch_respects_start_bound():
    """只要 2019 年以后的数据：翻页仍从 end 走，但结果被 start 截断。"""
    bars = fetch_daily_bars(ReplayClient(), code="sz000333", start="2019-01-01",
                            end="2026-09-16")
    assert bars[0].date >= "2019-01-01"
    assert bars[-1].date == "2026-09-14"


def test_fetch_returns_bars_for_the_right_code():
    bars = fetch_daily_bars(ReplayClient(), code="sz000333", start="2020-01-01",
                            end="2026-09-16")
    assert {b.code for b in bars} == {"000333"}


def test_excessive_count_is_rejected_before_requesting():
    """count > 2000 服务端报 param error → 不浪费一次请求，直接拒绝。"""
    client = ReplayClient()
    with pytest.raises(ValueError, match="2000"):
        fetch_daily_bars(client, code="sz000333", start="2020-01-01",
                         end="2026-09-16", page=2500)
    assert client.urls == []


def test_qfq_count_above_800_is_rejected():
    """ADR-003：qfq > 800 会被**静默降级**，必须在这里拦住。"""
    client = ReplayClient()
    with pytest.raises(ValueError, match="qfq"):
        fetch_daily_bars(client, code="sz000333", start="2020-01-01",
                         end="2026-09-16", adj="qfq", page=900)
    assert client.urls == []


def test_pagination_stops_when_first_date_repeats():
    """服务端不再往前翻时（首日重复）必须停，否则死循环。"""
    body, _meta = load_fixture(FIXTURE_DIR, "tencent_fqkline_day_sz000333_p2000")
    same = body.decode("utf-8")
    client = ReplayClient(override={"2026-09-16": same, "2018-04-22": same})
    bars = fetch_daily_bars(client, code="sz000333", start="1990-01-01",
                            end="2026-09-16", max_pages=5)
    assert len(client.urls) == 2                        # 第 2 页首日重复即停
    assert len({b.date for b in bars}) == len(bars)     # 未产生重复行


def test_max_pages_guard_raises_instead_of_looping_forever():
    client = ReplayClient()
    with pytest.raises(FetchError, match="上限"):
        fetch_daily_bars(client, code="sz000333", start="1990-01-01",
                         end="2026-09-16", max_pages=1)
    assert MAX_PAGES >= 10                              # 默认上限足够取全历史


def test_client_errors_propagate():
    class Boom(ReplayClient):
        def get_text(self, url, **kw):
            raise FetchError("限流了")

    with pytest.raises(FetchError, match="限流了"):
        fetch_daily_bars(Boom(), code="sz000333", start="2020-01-01",
                         end="2026-09-16")


def test_policy_from_settings_wires_every_field():
    """上半轮遗留问题：RetryPolicy 与 config/settings.py 必须接线，不留悬空。"""
    from stocklab.config.settings import Settings

    s = Settings(http_timeout=3.5, http_min_interval=1.25, retry_attempts=7,
                 retry_base_delay=0.25, retry_max_delay=4.5)
    p = policy_from_settings(s)
    assert isinstance(p, RetryPolicy)
    assert p.timeout == 3.5
    assert p.min_interval == 1.25
    assert p.attempts == 7
    assert p.base_delay == 0.25
    assert p.max_delay == 4.5


def test_policy_from_settings_defaults_match_settings():
    from stocklab.config.settings import Settings

    p = policy_from_settings(Settings())
    assert p.attempts == 4 and p.min_interval == 0.35 and p.timeout == 10.0


def test_fixture_bodies_are_real_responses_not_synthetic():
    """守住「合成数据不得伪装成真实响应」：fixture meta 必须带 url/fetched_at/sha256。"""
    for name in PAGES.values():
        body, meta = load_fixture(FIXTURE_DIR, name)
        assert meta["url"].startswith("https://web.ifzq.gtimg.cn/")
        assert meta.get("fetched_at")
        assert meta.get("sha256")
        payload = json.loads(body.decode("utf-8"))
        assert payload["msg"] == ""                     # 真实成功响应
