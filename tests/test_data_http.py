"""Task 8：HTTP 层（白名单 + 退避重试 + 限流 + GBK + 缓存命中）。

离线可跑：网络由注入的 FakeSession 替代，全测试套另有 autouse 的 socket 屏蔽兜底。
"""

import pytest
import requests

from stocklab.data.errors import FetchError, HostNotAllowed, RateLimited
from stocklab.data.http import HttpClient, RetryPolicy

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
QUOTE_URL = "https://qt.gtimg.cn/q=sz000333"


class FakeResponse:
    def __init__(self, text="", status_code=200, content=None):
        self.text = text
        self.status_code = status_code
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = {}


class FakeSession:
    """记录调用次数并依次返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if not self.responses:
            return FakeResponse(text="")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeCache:
    """RawCache 的替身：记录读写，用于断言「命中不联网 / 落地原始字节」。"""

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.stored = []

    def has(self, source, params_key):
        return (source, params_key) in self.data

    def load(self, source, params_key):
        return self.data.get((source, params_key))

    def store(self, source, params_key, url, body, *, encoding, fetched_at):
        self.stored.append(
            {
                "source": source,
                "params_key": params_key,
                "url": url,
                "body": body,
                "encoding": encoding,
                "fetched_at": fetched_at,
            }
        )
        self.data[(source, params_key)] = body
        return "hash"


@pytest.fixture
def no_sleep():
    slept = []
    return slept, (lambda s: slept.append(s))


# ---------- 白名单 ----------

def test_host_whitelist_enforced():
    c = HttpClient(RetryPolicy(attempts=1), session=FakeSession([]))
    with pytest.raises(HostNotAllowed):
        c.get_text("https://evil.example.com/x")


def test_ip_literal_rejected():
    c = HttpClient(RetryPolicy(attempts=1), session=FakeSession([]))
    with pytest.raises(HostNotAllowed):
        c.get_text("http://127.0.0.1/x")


def test_whitelist_checked_before_any_request():
    """白名单失败必须发生在发请求之前（不留半截网络调用）。"""
    s = FakeSession([FakeResponse(text="ok")])
    c = HttpClient(RetryPolicy(attempts=1), session=s)
    with pytest.raises(HostNotAllowed):
        c.get_text("https://evil.example.com/x")
    assert s.calls == []


# ---------- 正常路径 ----------

def test_successful_fetch_returns_text(no_sleep):
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text="ok")])
    c = HttpClient(RetryPolicy(attempts=3), session=s, sleep=sleep)
    assert c.get_text(KLINE_URL) == "ok"
    assert len(s.calls) == 1
    assert slept == []


# ---------- 退避重试 ----------

def test_retries_on_empty_response_then_succeeds(no_sleep):
    """东财限流的真实表现就是返回空响应。"""
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text=""), FakeResponse(text="data")])
    c = HttpClient(RetryPolicy(attempts=3, base_delay=1.0, min_interval=0.0), session=s, sleep=sleep)
    assert c.get_text(KLINE_URL) == "data"
    assert len(s.calls) == 2
    assert slept == [1.0]      # 第一次退避


def test_backoff_is_exponential(no_sleep):
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text="")] * 3 + [FakeResponse(text="data")])
    c = HttpClient(RetryPolicy(attempts=5, base_delay=1.0, max_delay=100, min_interval=0.0), session=s,
                   sleep=sleep)
    c.get_text(KLINE_URL)
    assert slept == [1.0, 2.0, 4.0]


def test_backoff_capped(no_sleep):
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text="")] * 5)
    c = HttpClient(RetryPolicy(attempts=5, base_delay=1.0, max_delay=2.5, min_interval=0.0), session=s,
                   sleep=sleep)
    with pytest.raises(RateLimited):
        c.get_text(KLINE_URL)
    assert slept == [1.0, 2.0, 2.5, 2.5]


def test_exhausted_retries_on_empty_raises_rate_limited(no_sleep):
    _, sleep = no_sleep
    c = HttpClient(RetryPolicy(attempts=2, base_delay=0.1, min_interval=0.0), session=FakeSession(
        [FakeResponse(text=""), FakeResponse(text="")]), sleep=sleep)
    with pytest.raises(RateLimited):
        c.get_text(KLINE_URL)


def test_builtin_connection_error_retried_then_fetch_error(no_sleep):
    """传输层可能是内置 OSError（socket 层），不能只抓 requests 的异常。"""
    _, sleep = no_sleep
    s = FakeSession([ConnectionError("boom"), ConnectionError("boom")])
    c = HttpClient(RetryPolicy(attempts=2, base_delay=0.1, min_interval=0.0), session=s, sleep=sleep)
    with pytest.raises(FetchError):
        c.get_text(KLINE_URL)
    assert len(s.calls) == 2


def test_requests_exception_retried(no_sleep):
    _, sleep = no_sleep
    s = FakeSession([requests.Timeout("slow"), FakeResponse(text="ok")])
    c = HttpClient(RetryPolicy(attempts=3, base_delay=0.1, min_interval=0.0), session=s, sleep=sleep)
    assert c.get_text(KLINE_URL) == "ok"


def test_http_500_retried(no_sleep):
    _, sleep = no_sleep
    s = FakeSession([FakeResponse(status_code=500), FakeResponse(text="ok")])
    c = HttpClient(RetryPolicy(attempts=3, base_delay=0.1, min_interval=0.0), session=s, sleep=sleep)
    assert c.get_text(KLINE_URL) == "ok"


def test_404_is_not_retried(no_sleep):
    """4xx 是请求本身错了，重试没意义，直接失败。"""
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(status_code=404)])
    c = HttpClient(RetryPolicy(attempts=3, base_delay=0.1, min_interval=0.0), session=s, sleep=sleep)
    with pytest.raises(FetchError):
        c.get_text(KLINE_URL)
    assert len(s.calls) == 1
    assert slept == []


# ---------- 限流间隔 ----------

def test_min_interval_between_requests(no_sleep):
    slept, sleep = no_sleep
    s = FakeSession([FakeResponse(text="a"), FakeResponse(text="b")])
    clock = iter([100.0, 100.1, 200.0, 200.4])   # 第二次间隔 0.1s < 0.35s
    c = HttpClient(RetryPolicy(attempts=1, min_interval=0.35), session=s, sleep=sleep,
                   clock=lambda: next(clock))
    c.get_text(KLINE_URL)
    c.get_text(KLINE_URL)
    assert any(abs(x - 0.25) < 0.01 for x in slept)


# ---------- 编码 ----------

def test_gbk_decoding():
    """腾讯返回 GBK；按 UTF-8 硬解码会乱码（第 5 节坑 2）。"""
    gbk_body = "v_sz000333=\"美的集团~10.00\";".encode("gbk")
    s = FakeSession([FakeResponse(content=gbk_body)])
    c = HttpClient(RetryPolicy(attempts=1), session=s, sleep=lambda _: None)
    text = c.get_text(QUOTE_URL, encoding="gbk")
    assert "美的集团" in text


# ---------- 缓存命中（接口契约⑤） ----------

def test_cache_hit_does_not_touch_network():
    """命中缓存必须完全不发请求（复现模式铁律②）。"""
    body = "cached-美".encode("gbk")
    cache = FakeCache({("tencent", "sz000333:1"): body})
    s = FakeSession([])          # 空响应池：一旦发请求就会走重试并报错
    c = HttpClient(RetryPolicy(attempts=1), session=s, sleep=lambda _: None,
                   cache=cache, clock=lambda: 0.0)
    text = c.get_text(QUOTE_URL, encoding="gbk", source="tencent",
                      cache_key="sz000333:1")
    assert "cached-美" in text
    assert s.calls == []
    assert cache.stored == []


def test_cache_miss_fetches_then_stores_raw_bytes():
    """未命中 → 发请求 → 落地**原始字节**（不是解码后的文本）。"""
    gbk_body = "v_sz000333=\"美的集团\";".encode("gbk")
    cache = FakeCache()
    s = FakeSession([FakeResponse(content=gbk_body)])
    c = HttpClient(RetryPolicy(attempts=1), session=s, sleep=lambda _: None,
                   cache=cache, clock=lambda: 0.0,
                   now=lambda: "2026-09-14T19:00:00+08:00")
    assert "美的集团" in c.get_text(QUOTE_URL, encoding="gbk", source="tencent",
                                    cache_key="sz000333:1")
    assert len(s.calls) == 1
    assert cache.stored[0]["body"] == gbk_body       # 原始字节，非 decode 结果
    assert cache.stored[0]["encoding"] == "gbk"
    assert cache.stored[0]["fetched_at"] == "2026-09-14T19:00:00+08:00"
    assert cache.stored[0]["url"] == QUOTE_URL


def test_cache_key_absent_means_no_cache_io():
    cache = FakeCache()
    s = FakeSession([FakeResponse(text="ok")])
    c = HttpClient(RetryPolicy(attempts=1), session=s, sleep=lambda _: None,
                   cache=cache, clock=lambda: 0.0)
    assert c.get_text(KLINE_URL, source="tencent") == "ok"
    assert cache.stored == []


# ---------- 重试策略本身 ----------

def test_delay_for_is_exponential_and_capped():
    p = RetryPolicy(base_delay=0.8, max_delay=8.0)
    assert [p.delay_for(i) for i in range(6)] == [0.8, 1.6, 3.2, 6.4, 8.0, 8.0]


def test_default_policy_limits_tencent_rate():
    """腾讯 ≤5 req/s → 最小间隔不得低于 0.2s。"""
    assert RetryPolicy().min_interval >= 0.2
