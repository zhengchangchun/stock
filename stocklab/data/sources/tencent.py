"""腾讯行情适配器（纯函数：raw → 领域对象）。

字段索引由真实响应核对（2026-09-14 实测 88 个字段，见
`docs/tasks/2026-09-14-p2-上半-数据层-task8-11.md` §1.1）；测试回放真实 fixture。

三处坑：
  ① 快照返回体是 **GBK**；
  ② 日K 每行是 **[日期, 开, 收, 最高, 最低, 量]**，不是 OHLC；
  ③ 接口的「手 / 万元 / 亿元」必须换算成「股 / 元」（铁律④）。

复权口径（铁律①）：`kline_url()` 默认请求**不复权**序列；
qfq 序列只用于取除权事件与交叉校验，**禁止**作为 `bars_raw` 落库。
"""

from __future__ import annotations

from stocklab.data.models import Bar, CorpAction, Quote
from stocklab.data.sources._common import LOT, WAN, YI, to_float

QUOTE_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
# 事件是 kline 行上多出来的第 7 个元素（dict），不是独立的顶层字段
_EVENT_INDEX = 6

# 字段索引（真实响应核对过；`_MIN_FIELDS` 保证最大索引 46 可用）
_FIELDS = {
    "name": 1, "code": 2, "price": 3, "pre_close": 4, "open": 5,
    "ts": 30, "high": 33, "low": 34, "volume": 36, "amount": 37,
    "turnover": 38, "pe_ttm": 39, "float_mv": 44, "total_mv": 45, "pb": 46,
}
_MIN_FIELDS = 47


def kline_url(code: str, count: int = 320, adj: str = "") -> str:
    """构造日K URL。`adj=""` = 不复权（默认，铁律①），`"qfq"` = 前复权。

    注意：qfq 请求仅限「取除权事件 / 交叉校验」用途。
    """
    return f"{KLINE_URL}?param={code},day,,,{count},{adj}"


def quote_url(codes: list[str]) -> str:
    return f"{QUOTE_URL}{','.join(codes)}"


def parse_quote(text: str) -> list[Quote]:
    """解析腾讯快照。坏行跳过（调用方负责留痕）。"""
    out: list[Quote] = []
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        lhs, _, rhs = line.partition("=")
        parts = rhs.strip().strip('"').split("~")
        if len(parts) < _MIN_FIELDS:
            continue
        code = parts[_FIELDS["code"]]
        market_code = lhs[2:]                       # "v_sz000333" -> "sz000333"
        if len(market_code) != 8 or market_code[2:] != code:
            continue                                # 前缀与代码不一致：行可能错位
        price = to_float(parts[_FIELDS["price"]])
        pre_close = to_float(parts[_FIELDS["pre_close"]])
        if not code or price is None or pre_close is None:
            continue
        vol_lots = to_float(parts[_FIELDS["volume"]]) or 0.0
        amount_wan = to_float(parts[_FIELDS["amount"]]) or 0.0
        out.append(
            Quote(
                code=code,
                name=parts[_FIELDS["name"]],
                price=price,
                pre_close=pre_close,
                open=to_float(parts[_FIELDS["open"]]) or 0.0,
                high=to_float(parts[_FIELDS["high"]]) or 0.0,
                low=to_float(parts[_FIELDS["low"]]) or 0.0,
                volume=int(vol_lots * LOT),
                amount=amount_wan * WAN,
                turnover=to_float(parts[_FIELDS["turnover"]]),
                pe_ttm=to_float(parts[_FIELDS["pe_ttm"]]),
                float_mv=(to_float(parts[_FIELDS["float_mv"]]) or 0.0) * YI,
                total_mv=(to_float(parts[_FIELDS["total_mv"]]) or 0.0) * YI,
                pb=to_float(parts[_FIELDS["pb"]]),
                ts=parts[_FIELDS["ts"]],
            )
        )
    return out


def _node(payload: dict, code: str) -> dict:
    data = (payload or {}).get("data") or {}
    return data.get(f"sz{code}") or data.get(f"sh{code}") or {}


def _rows(payload: dict, code: str, adj_mode: str) -> list:
    """按复权口径取**对应**的节点，不做跨口径回退。

    曾经写成「取不到 qfqday 就退回 day」，后果是把复权价标成 `adj_mode="none"`
    静默下发 —— 直接违反铁律①，且下游无从发现。口径不匹配就该取不到数据，
    让调用方看到「0 根」而不是看到「一堆贴上错误标签的价格」。
    """
    key = "qfqday" if adj_mode == "qfq" else "day"
    rows = _node(payload, code).get(key)
    return rows if isinstance(rows, list) else []


def parse_kline(payload: dict, code: str, *, adj_mode: str = "none") -> list[Bar]:
    """解析日K。行格式 [日期, 开, 收, 最高, 最低, 成交量(手)]。

    默认 `adj_mode="none"`：抓取层默认只产出不复权数据（铁律①）。
    """
    out: list[Bar] = []
    for row in _rows(payload, code, adj_mode):
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        o, c, h, low = (to_float(row[1]), to_float(row[2]),
                        to_float(row[3]), to_float(row[4]))
        v = to_float(row[5])
        if None in (o, c, h, low, v):
            continue
        out.append(
            Bar(code=code, date=str(row[0]), open=o, high=h, low=low, close=c,
                volume=int(v * LOT), amount=None, turnover=None,
                source="tencent", adj_mode=adj_mode)
        )
    return out


def parse_corp_actions(payload: dict, code: str) -> list[CorpAction]:
    """解析除权除息事件（ADR-001 D-01 自建因子链的输入）。

    事件挂在**行尾第 7 个元素**（dict）上；缺 `cqr`/`fh_sh` 的行跳过（由调用方留痕）。
    """
    out: list[CorpAction] = []
    for row in _rows(payload, code, "qfq"):
        if not isinstance(row, (list, tuple)) or len(row) <= _EVENT_INDEX:
            continue
        event = row[_EVENT_INDEX]
        if not isinstance(event, dict):
            continue
        cqr = str(event.get("cqr") or "")
        fh_sh = to_float(event.get("fh_sh"))
        if not cqr or fh_sh is None:
            continue
        out.append(
            CorpAction(
                code=code,
                cqr=cqr,
                djr=str(event.get("djr") or ""),
                fh_sh=fh_sh,
                content=str(event.get("FHcontent") or ""),
            )
        )
    return out
