"""分页抓取：把「能取到全量历史」这件事封装成一个可离线验证的函数。

依据 ADR-003：
  - 腾讯 `fqkline` 单次请求上限 **2000 根**（不复权）；>2000 服务端报 `param error`。
  - **qfq 上限 800 根**；801~2000 之间服务端**不报错**，而是静默降级回 640 根。
  - `beg` 被服务端忽略；只有 `end` 能定位窗口 → 以 `end` 为锚点**向后翻页**，
    直到返回空页，即可回溯至上市首日。

因此本模块有两道**硬守卫**（宁可报错，也不接受静默降级）：
  ① `page > MAX_COUNT` → `ValueError`（不浪费一次注定失败的请求）；
  ② `adj == "qfq" and page > MAX_QFQ_COUNT` → `ValueError`（拦住静默降级）。

本模块只做「取数 + 解析 + 翻页」，不落库；落库由 `stocklab.data.ingest` 负责。
"""

from __future__ import annotations

from datetime import date, timedelta

from stocklab.config.settings import Settings
from stocklab.data.errors import FetchError
from stocklab.data.http import RetryPolicy
from stocklab.data.models import Bar
from stocklab.data.sources import tencent

#: 不复权单次上限（ADR-003 实测）
MAX_COUNT = tencent.MAX_COUNT
#: qfq 单次上限；超过会被服务端静默降级（ADR-003 实测）
MAX_QFQ_COUNT = 800
#: 翻页次数上限。26 年历史 ≈ 5 页；给到 50 页足够，且能防死循环。
MAX_PAGES = 50


def policy_from_settings(settings: Settings) -> RetryPolicy:
    """把 `config/settings.py` 接到 `RetryPolicy`（上半轮遗留的悬空项）。

    单一真源：HTTP 行为参数只在 `Settings` 里定义，`RetryPolicy` 不再自带
    另一套默认值 —— 否则「改配置不生效」这类问题会非常难查。
    """
    return RetryPolicy(
        attempts=settings.retry_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
        min_interval=settings.http_min_interval,
        timeout=settings.http_timeout,
    )


def _validate(page: int, adj: str) -> None:
    if page > MAX_COUNT:
        raise ValueError(
            f"count={page} 超过单次上限 {MAX_COUNT}（服务端返回 param error，见 ADR-003）"
        )
    if adj == "qfq" and page > MAX_QFQ_COUNT:
        raise ValueError(
            f"qfq 口径 count={page} 超过 {MAX_QFQ_COUNT}：服务端会**静默降级**"
            "返回更少的数据且不报错（ADR-003），因此本层直接拒绝"
        )


def fetch_daily_bars(
    client,
    *,
    code: str,
    start: str,
    end: str,
    adj: str = "",
    page: int = MAX_COUNT,
    max_pages: int = MAX_PAGES,
) -> list[Bar]:
    """抓取 `[start, end]` 的日K，按日期升序返回。

    `code` 用腾讯形式（`sz000333`）；返回的 `Bar.code` 是 6 位代码。
    `adj=""`（默认）为**不复权** —— 落库只能是这个口径（铁律①）。
    翻页锚点从 `end` 开始，逐页向前推到返回空页或超出 `max_pages`。
    """
    _validate(page, adj)
    anchor = end
    seen_first: set[str] = set()
    collected: dict[str, Bar] = {}

    for _ in range(max_pages):
        url = tencent.kline_url(code, page, adj, end=anchor)
        text = client.get_text(url, source="tencent",
                               cache_key=f"kline:{code}:{anchor}:{page}:{adj}")
        bars = tencent.parse_kline(_loads(text), code[2:], adj_mode=adj or "none")
        if not bars:
            break                                    # 到头了（上市首日之前）
        first = bars[0].date
        if first in seen_first:
            break                                    # 服务端不再往前翻 → 停止
        seen_first.add(first)
        for b in bars:
            collected.setdefault(b.date, b)          # 页间重叠时保留先取到的
        if first <= start:
            break                                    # 已经覆盖到窗口起点
        anchor = (date.fromisoformat(first) - timedelta(days=1)).isoformat()
    else:
        raise FetchError(
            f"{code} 翻页达到上限 {max_pages} 页仍未取完（start={start}）——"
            "拒绝返回不完整的历史"
        )

    out = [b for d, b in sorted(collected.items()) if start <= d <= end]
    return out


def _loads(text: str) -> dict:
    import json

    try:
        return json.loads(text)
    except ValueError as exc:
        raise FetchError(f"响应不是合法 JSON: {exc}") from exc
