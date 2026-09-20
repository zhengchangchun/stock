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

import hashlib
import re
from dataclasses import replace
from datetime import date, timedelta

from stocklab.config.settings import Settings
from stocklab.data import notice_date
from stocklab.data.errors import FetchError
from stocklab.data.http import RetryPolicy
from stocklab.data.models import Bar, CorpAction, FinancialReport, MoneyFlowDaily, Quote, ValuationDaily
from stocklab.data.sources import eastmoney, sina, sse, tencent

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


#: 腾讯快照接口的请求头。实测不带 `Referer` 也会返回，但带上更稳（源站策略会变），
#: 且固定请求头让「回放」与「实时」走同一条路径（可复现性铁律②）。
QUOTE_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def fetch_quotes(client, *, codes) -> list[Quote]:
    """抓取**实时快照**（腾讯 `qt.gtimg.cn`，GBK）。`codes` 用源站形式（`sz000333`）。

    **刻意不写 raw_cache**（`cache_key=""` → 既不读也不写）：缓存命中会直接返回
    上一次的响应体，而快照的全部价值就在「此刻」。命中缓存 = 拿旧截面冒充新截面，
    正是本项目最怕的一类错（数字合法、时刻错）。日K 用缓存是对的（历史不变），
    快照用缓存是错的 —— 差别在数据本身是否随时间变化。

    返回值可能**少于**请求的代码（源站对停牌/退市/写错的代码不回行）。
    调用方必须显式报出缺了哪些（`session/quotes.py`），不许把「少了两个标的」
    当成「今天只该有两个标的」。

    **全空即报错**：一条都没解析出来说明响应格式变了或接口降级了 ——
    这时返回 `[]` 会让上层把「接口坏了」读成「今天没有行情」，故直接抛 `FetchError`。
    """
    wanted = [c for c in codes if c]
    if not wanted:
        raise ValueError("fetch_quotes 需要至少一个源站代码（如 sz000333）")
    text = client.get_text(tencent.quote_url(wanted), encoding="gbk",
                           headers=QUOTE_HEADERS, source="tencent",
                           cache_key="")          # 空键 = 绕过缓存（刻意的，见上）
    quotes = tencent.parse_quote(text)
    if not quotes:
        raise FetchError(
            f"腾讯快照接口未返回任何可解析的行（请求 {len(wanted)} 个代码："
            f"{','.join(wanted)}）—— 格式变更或接口降级，拒绝静默当成「无行情」"
        )
    return quotes


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


# ---------------------------------------------------------------------------
# P28：估值 / 资金流采集。返回 `(row, resp_sha256, cache_key)` 三元组 ——
# 每行携带**它来自哪一份原始响应**（`resp_sha256` = 该页 utf-8 正文 sha256，
# `cache_key` = raw_cache params_key），保证任一行可复现、可溯源。
# 缓存键第 3 段 = 锚点 `end` 日 → ADR-009 的盘中快照判据自动生效
# （盘中抓的快照不进正式缓存、读侧视为未命中）。
# ---------------------------------------------------------------------------

def _resp_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_valuation_daily(
    client,
    *,
    code: str,
    start: str,
    end: str,
    page_size: int = eastmoney.VALUATION_PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> list[tuple[ValuationDaily, str, str]]:
    """抓取 `[start, end]` 的估值（东财 datacenter），按日期升序返回。

    `code` 是 **6 位代码**（000333，不带市场前缀 —— SECURITY_CODE 口径）。
    翻页按 `TRADE_DATE` 降序（源站排序）；首请求读 `result.pages` 定总页数，
    逐页抓全。**fail-closed**：任一非末页行数 != page_size 即抛 `FetchError`
    （源站截断 = 半截数据，不许当完整）。`pages=0`（无数据）返回 `[]`（合法：
    ETF 通常没有估值行，由调用方显式留痕）。
    """
    out: list[tuple[ValuationDaily, str, str]] = []
    total_pages: int | None = None
    page = 1
    while page <= max_pages:
        url = eastmoney.valuation_url(code, page=page, page_size=page_size)
        cache_key = f"valuation:{code}:{end}:{page}:{page_size}"
        text = client.get_text(url, headers=eastmoney.DATACENTER_HEADERS,
                               source="eastmoney", cache_key=cache_key)
        payload = _loads(text)
        result = (payload or {}).get("result") or {}
        if total_pages is None:
            total_pages = int(result.get("pages") or 0)
        rows = eastmoney.parse_valuation(payload, code)
        if total_pages and page < total_pages and len(rows) != page_size:
            raise FetchError(
                f"{code} 估值第 {page}/{total_pages} 页仅 {len(rows)} 行"
                f"（期望 {page_size}）—— 源站截断，拒绝把半截当完整"
            )
        sha = _resp_sha256(text)
        out.extend((r, sha, cache_key) for r in rows)
        if not total_pages or page >= total_pages or not rows:
            break
        page += 1
    else:
        raise FetchError(
            f"{code} 估值翻页超过 {max_pages} 页仍未取完（end={end}）——"
            "拒绝返回不完整的历史"
        )
    out = [(r, s, k) for r, s, k in out if start <= r.date <= end]
    out.sort(key=lambda t: t[0].date)
    return out


def fetch_money_flow_daily(
    client,
    *,
    code: str,
    start: str,
    end: str,
    page_size: int = sina.PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> list[tuple[MoneyFlowDaily, str, str]]:
    """抓取 `[start, end]` 的资金流（新浪），按日期升序返回。

    `code` 用**源站形式**（`sz000333` / `sh600690`，`daima` 口径）。
    翻页按 `opendate` 降序（`asc=0`），翻到空页即止。**fail-closed**：
    非末页行数 != page_size 即抛（源站截断）；翻页超上限抛（防死循环）。
    """
    out: list[tuple[MoneyFlowDaily, str, str]] = []
    page = 1
    while page <= max_pages:
        url = sina.moneyflow_url(code, page=page, num=page_size)
        cache_key = f"moneyflow:{code}:{end}:{page}:{page_size}"
        text = client.get_text(url, source="sina", cache_key=cache_key)
        payload = _loads(text)
        rows = sina.parse_moneyflow(payload, code)
        if len(rows) > page_size:
            raise FetchError(
                f"{code} 资金流第 {page} 页返回 {len(rows)} 行（> page_size={page_size}）"
                "—— 源站口径变化，拒绝解析"
            )
        sha = _resp_sha256(text)
        out.extend((r, sha, cache_key) for r in rows)
        if len(rows) < page_size:
            break                                  # 到头了
        page += 1
    else:
        raise FetchError(
            f"{code} 资金流翻页超过 {max_pages} 页仍未取完（end={end}）——"
            "拒绝返回不完整的历史"
        )
    out = [(r, s, k) for r, s, k in out if start <= r.date <= end]
    out.sort(key=lambda t: t[0].date)
    return out


# ---------- P30：上交所休市安排（联网；只解析，落库由调用方决定） ----------

def fetch_holiday_notices(
    client,
    *,
    list_url: str = sse.LIST_URL,
    max_articles: int = 30,
) -> list:
    """抓「休市安排」栏目 → 逐篇解析 → 返回 `MarketHoliday` 列表（按日期升序）。

    **fail-closed**：任一篇公告「标题说休市、正文解析不出」→ 抛 `FetchError`，
    **整批不返回**。理由：半批数据会让调用方以为「那几个节不开市、别的都开市」，
    而真相是「有一篇没读懂」。宁可这轮不入库（旧表原样保留），也不写半截。
    """
    from stocklab.calendar.holidays import parse_holiday_notice

    text = client.get_text(list_url, source="sse", cache_key="holiday:list")
    articles = sse.parse_article_list(text)
    if not articles:
        raise FetchError(
            f"休市安排列表页里一条公告都没解析出来（{list_url}）—— "
            "拒绝静默返回空：源站换排版必须表现为失败")

    out = []
    for art in articles[:max_articles]:
        aid = art["url"].rsplit("/", 1)[-1].removesuffix(".shtml")
        body = client.get_text(art["url"], source="sse", cache_key=f"holiday:{aid}")
        try:
            published = sse.published_at_from_article(body)
            out.extend(parse_holiday_notice(
                sse.article_text(body), source_url=art["url"], published_at=published,
                doc_kind=art["doc_kind"], covered_year=art["covered_year"]))
        except Exception as exc:                      # 逐篇失败 → 整批失败（见 docstring）
            raise FetchError(
                f"休市公告解析失败：{art['title']}（{art['url']}）：{exc}") from exc
    return sorted(out, key=lambda h: (h.date, h.source_url))


def _fetch_datacenter(client, report_name: str, *, secucode: str,
                      fetched_date: str, refs: list[dict]) -> list[dict]:
    """抓一张 datacenter 表，翻全页。`result=null` → `[]`（合法空）。"""
    out: list[dict] = []
    total_pages: int | None = None
    page = 1
    while page <= MAX_PAGES:
        url = eastmoney.datacenter_url(report_name, secucode=secucode,
                                       page=page,
                                       page_size=eastmoney.FINANCIAL_PAGE_SIZE)
        cache_key = (f"financial:{secucode[:6]}:{fetched_date}:"
                     f"{report_name}:{page}")
        text = client.get_text(url, headers=eastmoney.DATACENTER_HEADERS,
                               source="eastmoney", cache_key=cache_key)
        payload = _loads(text)
        result = (payload or {}).get("result") or {}
        if total_pages is None:
            total_pages = int(result.get("pages") or 0)
        rows = eastmoney.parse_datacenter_rows(payload)
        if (total_pages and page < total_pages
                and len(rows) != eastmoney.FINANCIAL_PAGE_SIZE):
            raise FetchError(
                f"{secucode} {report_name} 第 {page}/{total_pages} 页仅 {len(rows)} 行"
                f"（期望 {eastmoney.FINANCIAL_PAGE_SIZE}）—— 源站截断，"
                "拒绝把半截当完整")
        refs.append({"endpoint": report_name, "resp_sha256": _resp_sha256(text),
                     "cache_key": cache_key, "page": page})
        out.extend(rows)
        if not total_pages or page >= total_pages or not rows:
            break
        page += 1
    else:
        raise FetchError(
            f"{secucode} {report_name} 翻页超过 {MAX_PAGES} 页仍未取完"
            " —— 拒绝返回不完整的历史")
    return out


def fetch_financial_reports(client, *, code: str, org_type: str,
                            fetched_date: str
                            ) -> tuple[list[FinancialReport], list[dict]]:
    """抓一只标的的全部历史财报，返回 `(报告列表, raw_refs)`。

    - 数值来自 DMSK 三表；公告日与归母权益来自 F10 三变体（按 `org_type` 选）
    - **`result=null` 是合法空**（ETF 如此），不是抓取失败
    - 公告日走 `notice_date.resolve` 的三级回退
    """
    secucode = f"{code}.{'SH' if code.startswith(('6', '5')) else 'SZ'}"
    refs: list[dict] = []

    acc: dict[str, dict] = {}
    for report_name in eastmoney.DMSK_REPORTS:
        for row in _fetch_datacenter(client, report_name, secucode=secucode,
                                     fetched_date=fetched_date, refs=refs):
            rd = (row.get("REPORT_DATE") or "")[:10]
            if not rd:
                continue
            slot = acc.setdefault(rd, {})
            for src_col, dst in eastmoney.DMSK_FIELD_MAP.items():
                if row.get(src_col) is not None:
                    slot[dst] = row[src_col]

    f10_notice: dict[str, str] = {}
    f10_parent: dict[str, float] = {}
    # 迭代顺序**刻意固定**为 BALANCE → INCOME → CASHFLOW：
    # 对同一 report_date，第一张表提供非空 NOTICE_DATE 即胜出（`rd not in f10_notice` 守卫），
    # 后续表的日期被静默忽略——这决定了 PIT 锚点来自哪张表。改变顺序即改变锚点来源。
    for statement in ("BALANCE", "INCOME", "CASHFLOW"):
        name = eastmoney.f10_report_name(org_type, statement)
        for row in _fetch_datacenter(client, name, secucode=secucode,
                                     fetched_date=fetched_date, refs=refs):
            rd = (row.get("REPORT_DATE") or "")[:10]
            if not rd:
                continue
            if row.get("NOTICE_DATE") and rd not in f10_notice:
                f10_notice[rd] = row["NOTICE_DATE"][:10]
            if row.get("TOTAL_PARENT_EQUITY") is not None:
                f10_parent.setdefault(rd, row["TOTAL_PARENT_EQUITY"])

    out: list[FinancialReport] = []
    for rd, slot in acc.items():
        notice, source, _suspect = notice_date.resolve(
            f10_notice.get(rd), report_date=rd)
        out.append(FinancialReport(
            code=code, report_date=rd, notice_date=notice,
            notice_date_source=source,
            report_type=notice_date.report_type_of(rd),
            parent_equity=f10_parent.get(rd), **slot))
    out.sort(key=lambda r: r.report_date)
    return out, refs
