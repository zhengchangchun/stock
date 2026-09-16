"""新浪适配器（纯函数）—— 资金流（P28 源 B）。

源 B：`MoneyFlow.ssl_qsfx_zjlrqs`（逐日主力资金流，可回溯至上市首日）。
端点返回**纯 JSON 数组**（不是 JSONP），每行字段：
  opendate（YYYY-MM-DD）、trade（收盘）、changeratio、turnover、
  netamount（主力净额，元）、ratioamount、r0_net（超大单净额，元）、
  r0_ratio、r0x_ratio、cate_ra、cate_na。

单位注意：
  - `turnover` 是**百分数值 ×100**（新浪口径）：同一交易日 `bars_daily.turnover`（%）
    实测是它的 1/100（P28 计划 §2.1 交叉验证结论）。本适配器**不做换算**、原样存，
    换算/对拍是消费方的责任，且必须在文档里写清口径差异。
  - `netamount` / `r0_net` / `ratio_amount` = 元（原值，可负）。
"""

from __future__ import annotations

from stocklab.data.models import MoneyFlowDaily
from stocklab.data.sources._common import to_float

MONEYFLOW_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
    "json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
)

#: 翻页单次上限。新浪此接口 num 未严格设限，取一个稳妥值减少请求数；过小会放大
#: 请求次数、过大可能被源站静默截断 —— 与 ADR-003 的教训同源，宁可小。
PAGE_SIZE = 100


def moneyflow_url(code: str, *, page: int, num: int = PAGE_SIZE) -> str:
    """资金流 URL。`code` 用源站形式（`sz000333` / `sh600690`）。`sort=opendate&asc=0` = 按日降序。"""
    return (
        f"{MONEYFLOW_URL}?page={page}&num={num}&sort=opendate&asc=0"
        f"&daima={code}"
    )


def parse_moneyflow(payload, code: str) -> list[MoneyFlowDaily]:
    """把响应解析成 `MoneyFlowDaily` 列表（保持源站降序，由抓取层再统一排序）。

    - 响应必须是**数组**；不是数组 / 空 → 返回空（由抓取层判「到头」还是「格式坏」）。
    - 数值用 `to_float` 宽松解析：`"-"` / 空串 → None（**拿不到写 NULL，不许填 0**）。
    """
    if not isinstance(payload, list):
        return []
    out: list[MoneyFlowDaily] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        date = str(row.get("opendate") or "").strip()
        if not date:
            continue
        out.append(
            MoneyFlowDaily(
                code=code[2:] if code[:2] in ("sz", "sh") else code,
                date=date,
                close=to_float(row.get("trade")),
                change_ratio=to_float(row.get("changeratio")),
                turnover=to_float(row.get("turnover")),
                main_net=to_float(row.get("netamount")),
                xl_net=to_float(row.get("r0_net")),
                ratio_amount=to_float(row.get("ratioamount")),
                source="sina",
            )
        )
    return out
