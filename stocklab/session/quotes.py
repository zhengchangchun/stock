"""快照领域层（P11）：`Quote` → `quote_snapshots` 行（纯函数，不碰库、不读时钟）。

## 为什么身份键里是源站的 `ts` 而不是我们的 `fetched_at`

`ts`（`YYYYMMDDHHMMSS`）是**源站自己报的时刻**，也就是「这份截面描述的是哪一刻的市场」。
`fetched_at` 是我们伸手去拿的时刻 —— 它属于**呈现**，不属于身份（ADR-005）。
拿它进键的话，同一刻的市场被拿两次就会变成两行，而它们说的是同一件事。

顺带得到一条**免费的强性质**：非交易日去抓，源站返回的仍是上一交易日的最后一条 tick
（`ts` 不变）→ 键相同 → `identical` → 一行不增。于是「今天到底是不是交易日」
猜错也不会造出假行 —— 这条性质是 `tick.py` 敢在没有可靠日历的情况下照抓的底气。

## 字段缺失 = 显式报错，不是静默跳过

源站时间戳格式不可识别（空串 / 非数字）→ 该标的进 `errors`，**不构造行**。
宁可这一条不进库并让调用方把原因写进摘要，也不拿本地时钟编一个 `ts` 出来 ——
编出来的 `ts` 会在键里冒充「源站说这是那一刻」，而源站从没那么说过。
"""

from __future__ import annotations

from collections.abc import Sequence

from stocklab.data.models import Quote

#: 快照来源标识（与 `bars_daily.source` 同口径）。
SNAPSHOT_SOURCE = "tencent"

#: 源站时间戳的长度（`YYYYMMDDHHMMSS`）。
_TS_LEN = 14


def trade_date_from_ts(ts: str) -> str:
    """`YYYYMMDDHHMMSS` → `YYYY-MM-DD`。无法识别 → `ValueError`（不猜、不用本地时钟）。"""
    text = (ts or "").strip()
    if len(text) < 8 or not text[:8].isdigit():
        raise ValueError(f"源站时间戳不可识别：{ts!r}（需要 YYYYMMDDHHMMSS）")
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


def ts_is_complete(ts: str) -> bool:
    """源站时间戳是否是完整的 14 位（不完整 → 该截面的时刻不可信，由调用方决定处置）。"""
    text = (ts or "").strip()
    return len(text) == _TS_LEN and text.isdigit()


def _f(x):
    """数值归一：`None` 保持 `None`（**不许**变成 0 —— 见 `close.py` 的不造数纪律）。"""
    return None if x is None else float(x)


def snapshot_row(q: Quote, *, fetched_at: str,
                 source: str = SNAPSHOT_SOURCE) -> dict:
    """单个 `Quote` → 一行快照（列名与 `quote_snapshots` 对齐）。

    `adj_mode` 不在其中：快照是**当下价**，没有复权概念。它进 `bars_daily` 时
    也永远只写 `adj_mode='none'` 的那一行（`close.py` 里另有硬拒绝）。
    """
    return {
        "code": q.code,
        "trade_date": trade_date_from_ts(q.ts),
        "ts": q.ts.strip(),
        "price": float(q.price),
        "pre_close": _f(q.pre_close),
        "open": _f(q.open),
        "high": _f(q.high),
        "low": _f(q.low),
        "volume": int(q.volume),
        "amount": _f(q.amount),
        "turnover": _f(q.turnover),
        "source": source,
        "fetched_at": fetched_at,
    }


def build_rows(quotes: Sequence[Quote], *, fetched_at: str,
               source: str = SNAPSHOT_SOURCE) -> tuple[list[dict], dict[str, str]]:
    """批量构造快照行，返回 `(rows, errors)`。

    - `rows` 按 `code` 排序（**不按源站返回顺序**）：摘要与测试要可复现；
    - 同一代码重复出现 → 取**第一条**（源站不该这么做，出现即说明格式异常）；
    - 任何一条构造失败只影响它自己，其余照常返回（否则一个坏标的会让整次采集归零）。
    """
    rows: list[dict] = []
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for q in sorted(quotes, key=lambda x: x.code):
        if q.code in seen:
            continue
        seen.add(q.code)
        if not ts_is_complete(q.ts):
            # 不完整的时间戳：时刻不可信 → 该标的拒绝入行并留痕（不编本地时钟）
            errors[q.code] = f"源站时间戳不完整：{q.ts!r}"
            continue
        try:
            rows.append(snapshot_row(q, fetched_at=fetched_at, source=source))
        except (TypeError, ValueError) as exc:
            errors[q.code] = f"{type(exc).__name__}: {exc}"
    return rows, errors


def missing_codes(requested: Sequence[str], quotes: Sequence[Quote]) -> list[str]:
    """请求了但源站**没回行**的 6 位代码（顺序稳定）。

    必须显式报出来：把「源站今天没给这只票」读成「今天只该有这几只」，
    会让一次采集缺口安静地变成一条覆盖面结论。
    """
    got = {q.code for q in quotes}
    return sorted({c[2:] if len(c) == 8 else c for c in requested} - got)
