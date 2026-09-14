"""HTTP 层：所有出网请求的唯一入口。

契约（Task 8）：
  ① 每次请求前 `assert_host_allowed`（R13 白名单，禁止 IP 直连）；
  ② 空响应 / 5xx / 超时按指数退避重试，退避有上限；
  ③ 重试耗尽抛 `RateLimited`（空响应）或 `FetchError`（其他）；
  ④ 同域请求间隔 ≥ `min_interval`（腾讯 ≤5 req/s）；
  ⑤ `cache_key` 命中缓存则直接返回，**完全不发请求**（铁律②）。

设计取舍：
  - 编码由调用方显式指定（腾讯是 GBK），不依赖 requests 的编码猜测 ——
    这样「缓存回放」与「实时抓取」解码路径完全一致，才谈得上可复现。
  - 不做随机抖动：退避序列必须可复现（R9），故不引入 `rng`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from stocklab.config.universe import assert_host_allowed
from stocklab.data.errors import FetchError, RateLimited

TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 0.8
    max_delay: float = 8.0
    min_interval: float = 0.35
    timeout: float = 10.0

    def delay_for(self, attempt: int) -> float:
        """attempt 从 0 开始。指数退避 + 上限。"""
        return min(self.base_delay * (2**attempt), self.max_delay)


class RawCacheLike(Protocol):
    """`stocklab.data.raw_cache.RawCache` 的结构契约（便于测试替身注入）。"""

    def has(self, source: str, params_key: str) -> bool: ...

    def load(self, source: str, params_key: str) -> bytes | None: ...

    def store(self, source: str, params_key: str, url: str, body: bytes, *,
              encoding: str, fetched_at: str) -> str: ...


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _decode(body: bytes, encoding: str | None) -> str:
    return body.decode(encoding or "utf-8", errors="replace")


class HttpClient:
    """出网请求入口：白名单校验 → 缓存 → 限流 → 重试 → 统一异常。"""

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        cache: RawCacheLike | None = None,
        now: Callable[[], str] = now_iso,
    ):
        self.policy = policy or RetryPolicy()
        self.session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._cache = cache
        self._now = now
        self._last_call: dict[str, float] = {}

    # ---------- 内部 ----------

    def _throttle(self, host: str) -> None:
        """同域请求最小间隔（④）。"""
        last = self._last_call.get(host)
        now = self._clock()
        if last is not None:
            wait = self.policy.min_interval - (now - last)
            if wait > 0:
                self._sleep(round(wait, 4))
                now = self._clock()
        self._last_call[host] = now

    def _cached(self, source: str, cache_key: str, encoding: str | None) -> str | None:
        if self._cache is None or not cache_key:
            return None
        body = self._cache.load(source, cache_key)
        return None if body is None else _decode(body, encoding)

    def _store_cache(self, source: str, cache_key: str, url: str, body: bytes,
                     encoding: str | None) -> None:
        if self._cache is None or not cache_key:
            return
        self._cache.store(source, cache_key, url, body,
                          encoding=encoding or "utf-8", fetched_at=self._now())

    # ---------- 公开 ----------

    def get_text(
        self,
        url: str,
        *,
        params: dict | None = None,
        encoding: str | None = None,
        headers: dict | None = None,
        source: str = "",
        cache_key: str = "",
    ) -> str:
        assert_host_allowed(url)                      # ① 白名单先于一切
        hit = self._cached(source, cache_key, encoding)
        if hit is not None:
            return hit                                # ⑤ 命中即返回，不联网

        host = urlparse(url).hostname or ""
        empty_error: Exception = RateLimited(f"空响应（疑似限流）: {url}")
        last_error: Exception | None = None

        for attempt in range(self.policy.attempts):
            self._throttle(host)                      # ④ 限流
            body: bytes | None = None
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=self.policy.timeout)
            except (requests.RequestException, OSError) as exc:
                # 内置 ConnectionError/TimeoutError 也是 OSError：传输层错误一律可重试
                last_error = FetchError(f"请求异常: {exc}")
            else:
                code = resp.status_code
                if 400 <= code < 500 and code != 429:
                    raise FetchError(f"HTTP {code}（不重试）: {url}")
                if code == 200 and resp.content:
                    body = resp.content
                else:
                    last_error = empty_error if code == 200 else FetchError(f"HTTP {code}")

            if body is not None:
                self._store_cache(source, cache_key, url, body, encoding)
                return _decode(body, encoding)

            if attempt < self.policy.attempts - 1:
                self._sleep(self.policy.delay_for(attempt))   # ②③ 退避

        raise last_error or FetchError(f"抓取失败: {url}")
