"""东财**北向个股持股**适配器（P88；datacenter `RPT_MUTUAL_HOLDSTOCKNORTH_STA`）。

只管「取什么 URL + 怎么读 + 翻页」，联网经注入的 `client`，落库在 `stocklab.data.ingest`。

## ⚠️ 本模块最重要的一条：公开源**只剩季度**持股（任务书 §0.3）

2024-08 交易所取消北向实时/每日披露后，公开源上**北向日度资金流/日度持股已不存在**：

- `push2his.eastmoney.com/api/qt/kamt.kline/get`（`hk2sh/hk2sz/s2n`）第 2 列（净额）
  实测**恒 `0.00`**，只剩额度余额列；
- `RPT_MUTUAL_DEAL_HISTORY` 的 `FUND_INFLOW/NET_DEAL_AMT/QUOTA_BALANCE/BUY_AMT/SELL_AMT`
  实测**全 `null`**；
- 本报表实测：`filter=(SECUCODE="000333.SZ")` **全历史只回 1 行**，`TRADE_DATE=2026-06-30`。

⇒ 因此每行的 `frequency` **恒 `'quarterly'`**（数据库里还有 CHECK 兜底）。
**不许**为了「凑出日度序列」把额度余额 / 南向值 / `null` 当北向净额落库 —— 那是编数据。
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping
from urllib.parse import quote

from stocklab.data.errors import FetchError

#: datacenter-web 单页上限（与估值/财报端点同款 500）。
PAGE_SIZE = 500

#: 翻页上限（防死循环）。季度数据每只标的远小于 1 页，给 50 页绰绰有余。
MAX_PAGES = 50

NORTHBOUND_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
NORTHBOUND_REPORT = "RPT_MUTUAL_HOLDSTOCKNORTH_STA"

#: 频率**恒** quarterly（见模块 docstring）。落库列 `frequency` 取它。
FREQUENCY = "quarterly"

SOURCE = "eastmoney-northbound"

#: 数据中心只要 UA（实测 200；与 `eastmoney.DATACENTER_HEADERS` 同款）。
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def secucode(code: str) -> str:
    """6 位代码 → `SECUCODE`（`000333.SZ` / `600690.SH`）。

    判定与 `fetch.py::fetch_financial_reports` **逐字一致**（`6`/`5` 开头 = 沪，
    其余 = 深）—— 同一口径在两个模块里必须是同一个规则，否则同一只标的会在
    两条路径上被分到两个市场。格式不对直接抛：静默拼出一个错的 SECUCODE
    只会让源站回一个空列表，而「没有北向数据」与「代码写错了」长得一模一样。
    """
    c = (code or "").strip()
    if len(c) != 6 or not c.isdigit():
        raise ValueError(f"SECUCODE 需要 6 位数字代码，得到 {code!r}")
    return f"{c}.{'SH' if c.startswith(('6', '5')) else 'SZ'}"


def northbound_url(sc, *, page: int, page_size: int = PAGE_SIZE) -> str:
    """构造 datacenter 请求 URL。`filter` 用 `SECUCODE`（带市场后缀）。

    ⚠️ `columns=ALL` 是必须的（与财报端点同一条实测教训：显式列清单在缺该列的
    标的上会让**整个请求**返回「字段不存在」而不是少一列）。
    """
    f = quote(f'(SECUCODE="{sc}")', safe="")
    return (f"{NORTHBOUND_URL}?reportName={NORTHBOUND_REPORT}&columns=ALL&filter={f}"
            f"&pageNumber={page}&pageSize={page_size}"
            "&sortColumns=TRADE_DATE&sortTypes=-1")


def _to_float(s: object) -> float | None:
    """宽松数值解析：非数值（`None` / `"-"` / 空串）→ None（拿不到写 NULL，不填 0）。"""
    try:
        return float(s)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_northbound_rows(payload: dict, code: str) -> list[dict]:
    """响应 → 规范化行字典（**保持源站顺序**，即 `TRADE_DATE` 倒序）。

    `result=null`（该标的没有北向持股行）⇒ `[]`，那是**合法空**不是抓取失败。
    缺 `TRADE_DATE` 的行丢掉（没日期就没法做 `(code, trade_date)` 幂等键）。
    """
    result = (payload or {}).get("result") or {}
    out: list[dict] = []
    for row in result.get("data") or []:
        if not isinstance(row, Mapping):
            continue
        date = str(row.get("TRADE_DATE") or "").strip()[:10]
        if not date:
            continue
        out.append({
            "code": code,
            "trade_date": date,
            "hold_shares": _to_float(row.get("HOLD_SHARES")),
            "hold_market_cap": _to_float(row.get("HOLD_MARKET_CAP")),
            "a_shares_ratio": _to_float(row.get("A_SHARES_RATIO")),
            "hold_shares_ratio": _to_float(row.get("HOLD_SHARES_RATIO")),
            "free_shares_ratio": _to_float(row.get("FREE_SHARES_RATIO")),
            "close_price": _to_float(row.get("CLOSE_PRICE")),
            "frequency": FREQUENCY,
            "source": SOURCE,
        })
    return out


def fetch_northbound_holdings(client, *, code: str, start: str, end: str | None = None,
                              page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES
                              ) -> tuple[list[dict], list[dict]]:
    """抓 `[start, end]` 的北向**季度**持股，按 `trade_date` 升序返回 `(rows, page_refs)`。

    翻页按 `TRADE_DATE` 降序（源站排序）。**翻到 `start` 边界即停**（某页最老一行
    `< start` ⇒ 再翻只会更老）。**fail-closed**：非末页行数 != `page_size` ⇒ 抛
    （源站截断＝半截序列，比「没有数据」更危险：它会静默丢掉更老的季度）。
    每行带 `resp_sha256`（指回它来自哪一份原始响应）。
    """
    sc = secucode(code)
    out: list[dict] = []
    refs: list[dict] = []
    total_pages: int | None = None
    page = 1
    while page <= max_pages:
        cache_key = f"northbound:{code}:{page}:{page_size}"
        text = client.get_text(northbound_url(sc, page=page, page_size=page_size),
                               headers=HEADERS, source="eastmoney",
                               cache_key=cache_key)
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise FetchError(f"{code} 北向响应不是合法 JSON: {exc}") from exc
        result = (payload or {}).get("result") or {}
        if total_pages is None:
            total_pages = int(result.get("pages") or 0)
        rows = parse_northbound_rows(payload, code)
        sha = _sha256(text)
        refs.append({"page": page, "resp_sha256": sha, "cache_key": cache_key})
        out.extend({**r, "resp_sha256": sha} for r in rows)
        if not rows:
            break
        if min(r["trade_date"] for r in rows) < start:
            break                                    # 已翻过 start，再翻只会更老
        if total_pages and page >= total_pages:
            break
        if total_pages and len(rows) != page_size:
            raise FetchError(
                f"{code} 北向第 {page}/{total_pages} 页仅 {len(rows)} 行"
                f"（期望 {page_size}）—— 源站截断，拒绝把半截序列当完整")
        page += 1
    else:
        raise FetchError(
            f"{code} 北向翻页超过 {max_pages} 页仍未取完 —— 拒绝返回不完整的序列")

    out = [r for r in out if r["trade_date"] >= start
           and (end is None or r["trade_date"] <= end)]
    out.sort(key=lambda r: r["trade_date"])
    return out, refs
