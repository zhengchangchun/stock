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

import re
from dataclasses import replace
from datetime import date, timedelta

from stocklab.config.settings import Settings
from stocklab.data.errors import FetchError
from stocklab.data.http import RetryPolicy
from stocklab.data.models import Bar, CorpAction
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


#: 指数符号：源站形式（沪深300 = `sh000300`），**不是** 6 位数字。
#: `000300` 在 A 股同时是基金代码，用 6 位会把指数与基金混进同一个键空间。
_INDEX_SYMBOL = re.compile(r"^(sh|sz|bj)\d{6}$")


def fetch_index_daily(client, *, symbol: str, start: str, end: str,
                      page: int = MAX_COUNT, max_pages: int = MAX_PAGES) -> list[Bar]:
    """抓取**指数**日线（Task 23 的 index_300 基准，复用腾讯 `fqkline` 不复权口径）。

    - 指数无复权概念（无分红送转）→ 一律 `adj_mode="none"`（铁律①，正好一致）；
    - `Bar.code` 用源站符号（`sh000300`），不用 6 位数字 —— 见 `_INDEX_SYMBOL`；
    - 翻页/上限/静默降级守卫全部复用 `fetch_daily_bars`（同一份实测结论）。
    - 指数的 `volume` 是源站口径 ×100（手→股），**只作停牌判据**，不用于成交额。

    基准缺失时的正确姿势是显式 `UNDETERMINED`（见 `backtest.benchmark`），
    而不是拿个股行情凑一个「指数」。
    """
    if not _INDEX_SYMBOL.match(symbol or ""):
        raise ValueError(
            f"指数符号必须形如 sh000300（市场前缀 + 6 位数字），得到 {symbol!r}"
        )
    bars = fetch_daily_bars(client, code=symbol, start=start, end=end, adj="",
                            page=page, max_pages=max_pages)
    return [replace(b, code=symbol) for b in bars]


def _loads(text: str) -> dict:
    import json

    try:
        return json.loads(text)
    except ValueError as exc:
        raise FetchError(f"响应不是合法 JSON: {exc}") from exc


#: 事件回补的默认起点：早于所有 A 股上市日，即「取全历史事件」。
#: **不要**把它收窄到与日K 相同的窗口 —— 见 `fetch_corp_actions` 的说明。
EARLIEST = "1990-01-01"


def fetch_corp_actions(
    client,
    *,
    code: str,
    end: str,
    start: str = EARLIEST,
    page: int = MAX_COUNT,
    max_pages: int = MAX_PAGES,
) -> list[CorpAction]:
    """抓取 `[start, end]` 的除权除息事件，按 `cqr` 升序返回。

    与日K 共用 `end` 锚点翻页（ADR-003），但**走不复权口径**（ADR-004 实测：
    不复权响应同样带事件行）—— 于是单次上限是 2000 根而不是 qfq 的 800 根，
    000333 只需 2 次请求（qfq 要 4 次）。

    **`start` 默认取全历史，且不应随意收窄**：`corp_actions` 缺一条事件与
    「该事件不存在」在库里长得一模一样，因子链会把缺席的事件当成没有，
    于是**该除权日之后的整段复权价都会错**，而且没有任何报错。
    所以事件表的正确不变式是「覆盖该标的上市至今的全部事件」，
    由本函数的默认值保证。
    """
    _validate(page, "")                    # 不复权口径：只受 MAX_COUNT 约束
    anchor = end
    seen_first: set[str] = set()
    collected: dict[str, CorpAction] = {}

    for _ in range(max_pages):
        url = tencent.kline_url(code, page, "", end=anchor)
        text = client.get_text(url, source="tencent",
                               cache_key=f"actions:{code}:{anchor}:{page}")
        payload = _loads(text)
        events = tencent.parse_corp_actions(payload, code[2:], adj_mode="none")
        rows = tencent.parse_kline(payload, code[2:], adj_mode="none")
        if not rows:
            break                          # 到头了（上市首日之前）
        first = rows[0].date
        if first in seen_first:
            break                          # 服务端不再往前翻 → 停止
        seen_first.add(first)
        for ev in events:
            collected.setdefault(ev.cqr, ev)
        if first <= start:
            break
        anchor = (date.fromisoformat(first) - timedelta(days=1)).isoformat()
    else:
        raise FetchError(
            f"{code} 事件翻页达到上限 {max_pages} 页仍未取完（start={start}）——"
            "拒绝返回不完整的事件表"
        )

    if not seen_first:
        # 一根 K 线都没拿到 → 抓取失败（而不是「这个标的没有事件」）
        raise FetchError(f"{code} 事件采集未取到任何日K 行，无法判断事件覆盖范围")
    # 注意：**取到 K 线但零事件是合法结果**（从未分红的标的），
    # ADR-001 要求这类标的所有日期 `adj_factor = 1` 显式入库，而不是报错。
    # 两者必须分开处理：把「零事件」也当失败会让干净标的永远进不了库，
    # 把「零 K 线」当零事件则会把抓取失败静默成「没有除权」。
    return sorted((e for d, e in collected.items() if start <= d <= end),
                  key=lambda e: e.cqr)
