"""东方财富适配器（纯函数）。

东财在 v1 是**交叉校验源**，不是主源（ADR-001 §1：`kline/get?beg=0` 实测返回空响应，限流）。
**例外**：估值（P28 源 A）走的是 `datacenter-web.eastmoney.com`（数据中心），与本模块
行情主站的 `push2his` / `push2` 不是同一台 —— 数据中心的 `RPT_VALUEANALYSIS_DET` 实测 200
（nanobot 2026-09-16 22:3x），故估值**是**自动采集路径。

四处注意：
  ① 行情接口必须带 `Referer`，否则被拒；datacenter 只要 `User-Agent`；
  ② `klines` 行是逗号分隔字符串，顺序同样是 **[日期, 开, 收, 高, 低, ...]**；
  ③ 成交量单位「手」需 ×100；
  ④ 成交额已经是「元」，**不要**再换算（与腾讯的「万元」不同，容易搞错）。

复权口径（铁律①）：`kline_url(adjust=0)` 默认不复权；
`parse_kline` 的 `adj_mode` 只是**如实标注**数据来源的口径，不做任何复权计算。

估值（P28）：`valuation_url` / `parse_valuation`。⚠️ 该报表是**重算报表** ——
东财按**最新股本**重算整条历史 PE/PB，属「非 PIT」（P28 计划 §4）；对策在落库侧
「首写保留」，这里只做**如实解析**，不做任何 PIT 校正。
"""

from __future__ import annotations

import urllib.parse
from urllib.parse import urlencode

from stocklab.data.models import Bar, ValuationDaily
from stocklab.data.sources._common import LOT, to_float

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FFLOW_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
VALUATION_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

HEADERS = {
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (compatible; stocklab/0.1)",
}

#: 数据中心（datacenter-web）只要 UA；`Referer` 不是必须（实测 200）。
DATACENTER_HEADERS = {"User-Agent": "Mozilla/5.0"}

#: 估值单页上限（数据中心 pageSize 上限 500，取 500 减少请求数）。
VALUATION_PAGE_SIZE = 500

#: 实测 500 可一次取回 79 行（pages=1）；默认口径下是 40 页。
FINANCIAL_PAGE_SIZE = 500


def valuation_url(code: str, *, page: int, page_size: int = VALUATION_PAGE_SIZE) -> str:
    """估值 URL（`datacenter-web`，非行情主站）。`code` 是 6 位代码（000333）。

    按 `TRADE_DATE` 降序。`filter` 用 SECURITY_CODE（6 位），**不带市场前缀**。
    """
    params = {
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageNumber": page,
        "pageSize": page_size,
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
    }
    return f"{VALUATION_URL}?{urlencode(params)}"


def parse_valuation(payload: dict, code: str) -> list[ValuationDaily]:
    """把 `RPT_VALUEANALYSIS_DET` 响应解析成 `ValuationDaily` 列表。

    - 行来自 `result.data`；`result.pages` 由抓取层用来判「翻到哪一页」。
    - `TRADE_DATE` 带时分秒（`"2026-09-16 00:00:00"`）→ 裁剪为 `YYYY-MM-DD`。
    - 数值用 `to_float`：`None` / `"-"` / 空串 → None（拿不到写 NULL，不填 0）。
    """
    result = (payload or {}).get("result") or {}
    rows = result.get("data") or []
    out: list[ValuationDaily] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("TRADE_DATE") or "").strip()
        if not raw_date:
            continue
        out.append(
            ValuationDaily(
                code=code,
                date=raw_date[:10],          # 裁剪掉 " 00:00:00"
                pe_ttm=to_float(row.get("PE_TTM")),
                pb=to_float(row.get("PB_MRQ")),
                ps_ttm=to_float(row.get("PS_TTM")),
                total_mv=to_float(row.get("TOTAL_MARKET_CAP")),
                total_shares=to_float(row.get("TOTAL_SHARES")),
                close_price=to_float(row.get("CLOSE_PRICE")),
                change_rate=to_float(row.get("CHANGE_RATE")),
                source="eastmoney-datacenter",
            )
        )
    return out

# klines 行的字段序号：f51..f61
_COL = {"date": 0, "open": 1, "close": 2, "high": 3, "low": 4,
        "volume": 5, "amount": 6, "turnover": 10}
_MIN_COLS = 7


def kline_url(secid: str, *, beg: str, end: str, adjust: int = 0,
              klt: int = 101) -> str:
    """日K URL。`secid` 形如 `0.000333`（深）/`1.600690`（沪），见 `Instrument.secid`。

    `adjust`：0=不复权（默认）、1=前复权、2=后复权。默认必须是 0（铁律①）。
    """
    return (
        f"{KLINE_URL}?secid={secid}&klt={klt}&fqt={adjust}"
        f"&beg={beg}&end={end}"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    )


def parse_kline(payload: dict, code: str, *, adj_mode: str = "none") -> list[Bar]:
    data = (payload or {}).get("data") or {}
    rows = data.get("klines") or []
    out: list[Bar] = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < _MIN_COLS:
            continue
        o, c, h, low = (to_float(parts[_COL["open"]]), to_float(parts[_COL["close"]]),
                        to_float(parts[_COL["high"]]), to_float(parts[_COL["low"]]))
        v, amt = to_float(parts[_COL["volume"]]), to_float(parts[_COL["amount"]])
        if None in (o, c, h, low, v):
            continue
        turnover = (to_float(parts[_COL["turnover"]])
                    if len(parts) > _COL["turnover"] else None)
        out.append(
            Bar(code=code, date=parts[_COL["date"]], open=o, high=h, low=low, close=c,
                volume=int(v * LOT), amount=amt, turnover=turnover,
                source="eastmoney", adj_mode=adj_mode)
        )
    return out


def parse_money_flow(payload: dict) -> list[dict]:
    """资金流（D3 决策：采集但 v1 不进特征集）。单位「元」。"""
    data = (payload or {}).get("data") or {}
    out: list[dict] = []
    for row in data.get("klines") or []:
        parts = str(row).split(",")
        if len(parts) < 6:
            continue
        vals = [to_float(p) for p in parts[1:6]]
        if any(v is None for v in vals):
            continue
        out.append({
            "date": parts[0],
            "main_net": vals[0],    # 主力净流入 = 大单 + 超大单
            "small_net": vals[1],
            "mid_net": vals[2],
            "big_net": vals[3],
            "xl_net": vals[4],
        })
    return out


# ---------- 财报（本轮）----------

DMSK_REPORTS: tuple[str, ...] = (
    "RPT_DMSK_FN_BALANCE", "RPT_DMSK_FN_INCOME", "RPT_DMSK_FN_CASHFLOW",
)

#: 报表名按机构类型选择。实测：一般工商走 G、银行走 B、保险走 I ——
#: **银行/保险的资产负债表并非缺失，只是报表名不同**（这一条决定了金融股
#: 能不能算 ROE）。
_ORG_PREFIX: dict[str, str] = {"银行": "B", "保险": "I"}
_DEFAULT_ORG_PREFIX = "G"

#: DMSK 三表 → FinancialReport 字段的映射（列名不同，值口径一致）。
#: 注意：DMSK 的 `TOTAL_EQUITY` 是**权益合计**（含少数股东），不是归母。
DMSK_FIELD_MAP: dict[str, str] = {
    "TOTAL_ASSETS": "total_assets",
    "TOTAL_EQUITY": "total_equity",
    "TOTAL_LIABILITIES": "total_liabilities",
    "INVENTORY": "inventory",
    "TOTAL_OPERATE_INCOME": "total_operate_income",
    "OPERATE_COST": "operate_cost",
    "PARENT_NETPROFIT": "parent_netprofit",
    "NETCASH_OPERATE": "netcash_operate",
    "CONSTRUCT_LONG_ASSET": "construct_long_asset",
    "INDUSTRY_NAME": "industry_name",
}


def f10_report_name(org_type: str, statement: str) -> str:
    """按机构类型拼 F10 报表名。未知类型回退通用的 `G`。"""
    prefix = _ORG_PREFIX.get(org_type or "", _DEFAULT_ORG_PREFIX)
    return f"RPT_F10_FINANCE_{prefix}{statement}"


def datacenter_url(report_name: str, *, secucode: str, page: int,
                   page_size: int) -> str:
    """构造 datacenter 请求 URL。

    ⚠️ **必须 `columns=ALL`** —— 显式列清单在缺该列的标的上会让**整个请求**
    返回 `code 9501「XXX返回字段不存在」`（实测 `601318.SH` / `510300.SH` +
    `INVENTORY` 复现）。这不是「少一列」，是「一行都拿不到」。
    """
    f = urllib.parse.quote(f'(SECUCODE="{secucode}")', safe="")
    return (f"{VALUATION_URL}?reportName={report_name}&columns=ALL&filter={f}"
            f"&pageNumber={page}&pageSize={page_size}"
            "&sortColumns=REPORT_DATE&sortTypes=-1")


def parse_datacenter_rows(payload: dict) -> list[dict]:
    """取 `result.data`。`result` 为 null 或缺失 → `[]`（**合法空**，ETF 如此）。"""
    result = (payload or {}).get("result") or {}
    return list(result.get("data") or [])
