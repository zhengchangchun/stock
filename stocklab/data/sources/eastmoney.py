"""东方财富适配器（纯函数）。

东财在 v1 是**交叉校验源**，不是主源（ADR-001 §1：`kline/get?beg=0` 实测返回空响应，限流）。

四处注意：
  ① 必须带 `Referer`，否则被拒；
  ② `klines` 行是逗号分隔字符串，顺序同样是 **[日期, 开, 收, 高, 低, ...]**；
  ③ 成交量单位「手」需 ×100；
  ④ 成交额已经是「元」，**不要**再换算（与腾讯的「万元」不同，容易搞错）。

复权口径（铁律①）：`kline_url(adjust=0)` 默认不复权；
`parse_kline` 的 `adj_mode` 只是**如实标注**数据来源的口径，不做任何复权计算。
"""

from __future__ import annotations

from stocklab.data.models import Bar
from stocklab.data.sources._common import LOT, to_float

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FFLOW_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"

HEADERS = {
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (compatible; stocklab/0.1)",
}

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
