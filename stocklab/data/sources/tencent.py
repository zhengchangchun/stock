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

#: 日K 的**同构镜像** base（**不含**首选，见 `KLINE_URLS`）：同一 `param` 下
#: 三者的响应体逐字节相同，故换 host 不涉及任何解析或口径变化。
#:
#: 为什么需要镜像（2026-09-25 现场）：`web.ifzq.gtimg.cn` 被腾讯 WAF 拦下，
#: **所有**请求一律返回 `HTTP 501` 的 `waf.tencent.com/501page.html` 跳转脚本
#: （326 B、非 JSON）；补 `Referer`/`Origin`/UA 等完整浏览器头**仍是 501**
#: —— 不是缺头，是这个 host 本身被拦。而 `http.py` 对 5xx 会退避重试 4 次再抛
#: `FetchError`，于是每只标的白跑 ~6-8 s，批量采集大面积失败（P74 现场：800 只
#: 里 570 只无 bars，且「重试注定再失败」）。镜像 host 让「源站换了个域名」
#: 不再等于「取数链断了」。
KLINE_MIRROR_URLS: tuple[str, ...] = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
)

#: 日K 取数的**回退链**：按序尝试，首个成功者用于本页。
#: **首选在前**：WAF 拦截可能只是临时的，首选一旦恢复，URL / 请求序列 / 缓存键
#: 就自动回到原状（逐位不变，见 `kline_url`）。
KLINE_URLS: tuple[str, ...] = (KLINE_URL, *KLINE_MIRROR_URLS)

# 事件是 kline 行上多出来的第 7 个元素（dict），不是独立的顶层字段
_EVENT_INDEX = 6

# 字段索引（真实响应核对过；`_MIN_FIELDS` 保证最大索引 46 可用）
_FIELDS = {
    "name": 1, "code": 2, "price": 3, "pre_close": 4, "open": 5,
    "ts": 30, "high": 33, "low": 34, "volume": 36, "amount": 37,
    "turnover": 38, "pe_ttm": 39, "float_mv": 44, "total_mv": 45, "pb": 46,
}
_MIN_FIELDS = 47


# 单次请求的条数硬上限（Task 12 实测：>2000 返回 `{"msg":"param error"}`）
MAX_COUNT = 2000


def kline_url(code: str, count: int = 320, adj: str = "", *,
              beg: str = "", end: str = "") -> str:
    """构造日K URL。`adj=""` = 不复权（默认，铁律①），`"qfq"` = 前复权。

    `end`（YYYY-MM-DD）是**向后翻页**的锚点：接口按「end 往前数 count 根」返回，
    `beg` 被服务端忽略。这是取全量历史（可回溯至上市首日）的唯一方式 ——
    见 ADR-003。`count` 上限 `MAX_COUNT`。

    注意：qfq 请求仅限「取除权事件 / 交叉校验」用途。

    **恒用首选 base**（`KLINE_URL`）：回退链只发生在 `fetch.py` 的取数层，
    这里产出的 URL 逐字节不变（镜像 URL 由 `kline_url_on` 构造）。
    """
    return kline_url_on(KLINE_URL, code, count, adj, beg=beg, end=end)


def kline_url_on(base: str, code: str, count: int = 320, adj: str = "", *,
                 beg: str = "", end: str = "") -> str:
    """在**指定 base** 上构造日K URL（回退链专用）。

    `base` 取自 `KLINE_URLS`。把 base 拆出来是为了让回退链能复用同一套参数
    拼装（而不是在 `fetch.py` 里重写一遍格式串 —— 那样两处一旦漂移，镜像 URL
    就会与首选 URL 不同构，「换 host 不改语义」的前提当场失效）。
    """
    return f"{base}?param={code},day,{beg},{end},{count},{adj}"


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


def parse_corp_actions(payload: dict, code: str, *,
                       adj_mode: str = "none") -> list[CorpAction]:
    """解析除权除息事件（ADR-001 D-01 自建因子链的输入）。

    事件挂在**行尾第 7 个元素**（dict）上。两个实测决定（ADR-004）：

    ① 默认 `adj_mode="none"`：探针实测**不复权响应同样带事件行**，且事件内容与
       qfq 完全一致（000333 14/14、600690 36/36）。走不复权流可用 2000/页
       （000333 两次请求搞定），而 qfq 单次上限 800、需要 4~5 次翻页 —— 且
       qfq 序列本身是**减法式**复权，历史价会变**负**（000333 2013-09-18 =
       -12.649 元），根本不能用于比率类特征。
    ② 事件的存在性**只以 `cqr` 判定**，不再要求 `fh_sh`。旧实现要求 `fh_sh`
       非空，会**静默丢弃** 600690 的 9/36 条事件（送转-only 事件的 `fh_sh`
       是空串），其中包含 `10送3股` —— 复权链少了这些事件，除权日的假跌幅
       就会原样留在序列里（正是本任务要消灭的失效模式）。
    """
    out: list[CorpAction] = []
    for row in _rows(payload, code, adj_mode):
        if not isinstance(row, (list, tuple)) or len(row) <= _EVENT_INDEX:
            continue
        event = row[_EVENT_INDEX]
        if not isinstance(event, dict):
            continue
        cqr = str(event.get("cqr") or "")
        if not cqr:
            continue                            # 没有除权日 → 无法定位事件
        out.append(
            CorpAction(
                code=code,
                cqr=cqr,
                djr=str(event.get("djr") or ""),
                fh_sh=to_float(event.get("fh_sh")),   # 可为 None：见 ②
                content=str(event.get("FHcontent") or ""),
            )
        )
    return out
