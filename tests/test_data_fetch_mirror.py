"""P74a：日K 取数的**镜像 host 回退链**（F1–F3）。

现场：首选 host `web.ifzq.gtimg.cn` 被腾讯 WAF 拦下，**所有**请求返回
`HTTP 501` 的跳转脚本（非 JSON）；同载荷镜像 host 返回逐字节相同的合法 JSON。
本文件钉住四件事：

  ① 首选可用时路径**逐位不变**（含 URL 与缓存键）—— 否则「换 host」会顺手把
     194 MB 的既有 raw_cache 全部作废（F3）；
  ② 首选抛 `FetchError` ⇒ 换下一个 base，且**实际发过哪些 URL** 可断言；
  ③ 链上全失败 ⇒ 抛错并**点名试过哪些 host**（下次一眼定位，而不是又一轮猜）；
  ④ 「源坏了」与「我请求错了」必须分开：4xx / 白名单违规**不换** host。

离线：假 client / 假 session，不联网。真源上的取数实证在任务书 §7 T6-5（≤12 请求）。
"""

from urllib.parse import urlparse

import pytest

from stocklab.config.paths import FIXTURE_DIR
from stocklab.data.errors import FetchError, HostNotAllowed
from stocklab.data.fetch import fetch_corp_actions, fetch_daily_bars, fetch_index_daily
from stocklab.data.http import HttpClient, RetryPolicy
from stocklab.data.raw_cache import load_fixture
from stocklab.data.sources import tencent

#: 首选 + 两个镜像（顺序即 `KLINE_URLS`，F1）
HOSTS = ("web.ifzq.gtimg.cn", "ifzq.gtimg.cn", "proxy.finance.qq.com")

STOCK = "sz000333"
END = "2026-09-16"                      # = fixture 的 end 锚点
START = "2020-01-01"                    # fixture 首页本身更早 ⇒ 一次请求就到窗口起点


def _kline_body() -> str:
    """真实录制的 2000 根不复权日K（含 10 条除权事件）。"""
    body, _meta = load_fixture(FIXTURE_DIR, "tencent_fqkline_day_sz000333_p2000")
    return body.decode("utf-8")


def _only(host: str, exc: Exception):
    """只在 `host` 上抛 `exc`，其余 host 正常返回。"""
    return lambda h: exc if h == host else None


def _hosts(client) -> list[str]:
    return [h for h, _url, _key in client.calls]


class HostFakeClient:
    """按 host 决定行为的最小假 client；记录每次 `(host, url, cache_key)`。"""

    def __init__(self, *, body: str = "", raise_for=None):
        self.body = body
        self.raise_for = raise_for or (lambda _host: None)
        self.calls: list[tuple[str, str, str]] = []

    def get_text(self, url, *, cache_key="", source="", **_kw):
        host = urlparse(url).hostname or ""
        self.calls.append((host, url, cache_key))
        exc = self.raise_for(host)
        if exc is not None:
            raise exc
        return self.body


# ---------- ① 首选可用 ⇒ 路径逐位不变 ----------

def test_preferred_host_path_is_unchanged_when_it_works():
    client = HostFakeClient(body=_kline_body())
    bars = fetch_daily_bars(client, code=STOCK, start=START, end=END)

    assert bars and bars[-1].date == "2026-09-14"
    assert _hosts(client) == [HOSTS[0]]                 # 一发命中，镜像零请求
    # URL 与缓存键必须与「加回退链之前」逐字节相同（F3：既有 raw_cache 照旧命中）
    assert client.calls[0][1] == tencent.kline_url(STOCK, 2000, "", end=END)
    assert client.calls[0][1] == (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        "?param=sz000333,day,,2026-09-16,2000,")
    assert client.calls[0][2] == "kline:sz000333:2026-09-16:2000:"


# ---------- ② 首选 501 ⇒ 镜像成功 ----------

def test_waf_501_on_preferred_falls_back_to_the_first_mirror():
    client = HostFakeClient(body=_kline_body(),
                            raise_for=_only(HOSTS[0], FetchError("HTTP 501")))
    bars = fetch_daily_bars(client, code=STOCK, start=START, end=END)

    assert bars and bars[-1].date == "2026-09-14"
    assert _hosts(client) == [HOSTS[0], HOSTS[1]]       # 换到第一个镜像就成功
    assert client.calls[1][1] == (
        "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
        "?param=sz000333,day,,2026-09-16,2000,")
    assert all(b.source == "tencent" for b in bars)      # 解析层零改动


def test_fallback_does_not_replay_pages_on_other_hosts():
    """换 host **不是**换页重放：某页在某 base 上成功后就定了，不再在镜像上重取。"""
    client = HostFakeClient(body=_kline_body(),
                            raise_for=_only(HOSTS[0], FetchError("HTTP 501")))
    fetch_daily_bars(client, code=STOCK, start=START, end=END)
    # 首页在镜像上取到且已覆盖窗口起点 ⇒ 停；镜像 host 只被请求了一次
    assert _hosts(client).count(HOSTS[1]) == 1


# ---------- ③ 链上全失败 ⇒ 点名三个 host ----------

def test_all_three_bases_failing_names_every_host():
    client = HostFakeClient(raise_for=lambda _h: FetchError("HTTP 501"))
    with pytest.raises(FetchError) as ei:
        fetch_daily_bars(client, code=STOCK, start=START, end=END)

    msg = str(ei.value)
    assert "web.ifzq.gtimg.cn → ifzq.gtimg.cn → proxy.finance.qq.com" in msg
    assert f"共 {len(HOSTS)} 个 host 全部失败" in msg
    assert "最后一个异常：HTTP 501" in msg          # 保留最后一个异常，不吞掉原因
    assert _hosts(client) == list(HOSTS)            # 三个都真试过，且只各试一次


def test_non_json_body_also_triggers_the_fallback():
    """拦页若以 200 返回 HTML（非 JSON），同样算「这个 base 坏了」。"""
    client = HostFakeClient(body="<html>waf</html>")
    with pytest.raises(FetchError) as ei:
        fetch_daily_bars(client, code=STOCK, start=START, end=END)
    assert "不是合法 JSON" in str(ei.value)
    assert _hosts(client) == list(HOSTS)


# ---------- ④ 「源坏了」vs「我请求错了」 ----------

def test_4xx_does_not_switch_host():
    """F2：4xx（非 429）**不换** —— 它说的是「我请求错了」，不是「源坏了」。"""
    client = HostFakeClient(raise_for=_only(
        HOSTS[0],
        FetchError("HTTP 404（不重试）: https://web.ifzq.gtimg.cn/...")))
    with pytest.raises(FetchError, match="404"):
        fetch_daily_bars(client, code=STOCK, start=START, end=END)
    assert _hosts(client) == [HOSTS[0]]             # 镜像一次都没碰


def test_whitelist_violation_is_not_masked_by_the_mirror_hosts():
    """R13：白名单是自己配的闸门，不许拿镜像 host 把它掩盖成「取到数了」。"""
    client = HostFakeClient(raise_for=_only(HOSTS[0], HostNotAllowed("域名不在白名单")))
    with pytest.raises(HostNotAllowed):
        fetch_daily_bars(client, code=STOCK, start=START, end=END)
    assert _hosts(client) == [HOSTS[0]]


# ---------- 缓存键（真 HttpClient + 假 RawCache / 假 session） ----------

class _Resp:
    def __init__(self, status_code: int, content: bytes = b""):
        self.status_code = status_code
        self.content = content
        self.headers: dict = {}


class _HostSession:
    """按 host 发 200 或 501；记录 URL。"""

    def __init__(self, body: bytes, *, alive: set[str], status: int = 501):
        self.body = body
        self.alive = alive
        self.status = status
        self.urls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.urls.append(url)
        if (urlparse(url).hostname or "") in self.alive:
            return _Resp(200, self.body)
        return _Resp(self.status, b"<html>waf</html>")


class _RecordingCache:
    """`RawCacheLike` 替身：只记录 `store` 调用。"""

    def __init__(self):
        self.stored: list[tuple[str, str, str]] = []

    def has(self, source, params_key):
        return False

    def load(self, source, params_key):
        return None

    def store(self, source, params_key, url, body, *, encoding, fetched_at):
        self.stored.append((source, params_key, url))
        return "sha256"


def _wire(*, alive: set[str]):
    cache = _RecordingCache()
    session = _HostSession(_kline_body().encode("utf-8"), alive=alive)
    client = HttpClient(
        RetryPolicy(attempts=1, base_delay=0.0, max_delay=0.0, min_interval=0.0,
                    timeout=1.0),
        session=session, cache=cache, sleep=lambda _s: None)
    return client, cache, session


def test_cache_key_is_unchanged_for_preferred_and_host_scoped_for_mirror():
    """F3：首选键**一字不改**；镜像键多一段 `@{host}` —— 两键不同且互不污染。"""
    ok, ok_cache, _ = _wire(alive={HOSTS[0]})
    fetch_daily_bars(ok, code=STOCK, start=START, end=END)
    preferred_keys = [k for _s, k, _u in ok_cache.stored]

    fb, fb_cache, fb_session = _wire(alive={HOSTS[1]})
    fetch_daily_bars(fb, code=STOCK, start=START, end=END)
    mirror_keys = [k for _s, k, _u in fb_cache.stored]

    assert preferred_keys == ["kline:sz000333:2026-09-16:2000:"]
    assert mirror_keys == ["kline@ifzq.gtimg.cn:sz000333:2026-09-16:2000:"]
    assert preferred_keys[0] != mirror_keys[0]
    assert HOSTS[1] in mirror_keys[0]
    # 键不同 ⇒ 落的是**两份**原始响应，各自带自己的 URL（可复现、可溯源）
    assert [u for _s, _k, u in fb_cache.stored][0].startswith(
        "https://ifzq.gtimg.cn/")
    assert fb_session.urls[0].startswith("https://web.ifzq.gtimg.cn/")  # 先试首选


# ---------- 另两个 kline 调用点：指数 / 除权事件 ----------

def test_index_bars_use_the_same_fallback_chain():
    """`index_300` 基准（R5）走的是同一条链 —— 它委托给 `fetch_daily_bars`。"""
    raw = (FIXTURE_DIR / "index_bars_sh000300.raw.json").read_text(encoding="utf-8")
    client = HostFakeClient(body=raw,
                            raise_for=_only(HOSTS[0], FetchError("HTTP 501")))
    bars = fetch_index_daily(client, symbol="sh000300", start="2025-01-01", end="2026-09-14")

    assert bars and {b.code for b in bars} == {"sh000300"}
    assert _hosts(client)[:2] == [HOSTS[0], HOSTS[1]]


def test_corp_actions_fall_back_and_keep_their_own_cache_key_shape():
    """除权事件那一处同样加回退，且键形状仍是 `actions:…`（不是 `kline:…`）。"""
    client = HostFakeClient(body=_kline_body(),
                            raise_for=_only(HOSTS[0], FetchError("HTTP 501")))
    actions = fetch_corp_actions(client, code=STOCK, end=END)

    assert len(actions) == 10                       # 真 fixture 里的 10 条事件
    assert client.calls[0][2] == "actions:sz000333:2026-09-16:2000"
    assert client.calls[1][2] == "actions@ifzq.gtimg.cn:sz000333:2026-09-16:2000"
