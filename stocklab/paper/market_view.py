"""「今天的市场是什么样」——喂给 AI 操盘手的 `market` 块（P84 / K1）。

## 只描述，不预测（K5）

这一块回答的是「**已经发生了什么**」：指数已经实现的收益、当日涨跌家数、
估值中位数、当日资金净流入。**没有一个前向字段** —— 没有预测价 / 目标价 / 概率 /
信号分 / 评级 / 买卖建议 / 均线方向 / 情绪分（`agent_context.NON_GOALS` 第 1 条
把这条禁令写死在上下文里，本模块的用例也扫一遍）。

「涨跌家数」不是「接下来会涨」：它是**当日横截面**的计数。把它读成方向信号，
读错的人不是代码 —— 所以 `notes` 里那句话必须留着。

## 零新采集、零网络（K4）

只查库内已有表：`bars_daily` / `valuation_daily` / `money_flow_daily` /
`trading_calendar`。不发起任何请求、不写任何表、不碰 `ops/`。

## 每类统计一条 SQL（K8）

800 只标的逐只查价 = 800 条 SQL，真库上必然超时。所以：

- 指数：一条窗口函数查询，一次拿回 2 个指数各 60 个收盘价；
- 横截面：一条查询拿回「最近交易日 ＋ 其前一交易日」两天的全部行，涨跌家数在
  **Python 里数**（804 行，纯加法）；
- 估值 / 资金流：各两条 / 一条按 `date` 过滤的聚合查询。

## 认不出的数一律 `None`

表空、该日无行、分母为 0 ⇒ `None`（**不猜数**，也不拿邻近日期顶替）。
测试夹具库就是空表 —— 两块都必须能正常返回而不是抛异常。
"""

from __future__ import annotations

import sqlite3

from stocklab.paper.engine import INDEX_300_SYMBOL, Price
from stocklab.paper.rules import check_no_lookahead

#: 上下文里报告的指数。库内两个都有（`trading_calendar` 4789 行 ⇒ 覆盖充分）。
INDEX_SYMBOLS: tuple[str, ...] = (INDEX_300_SYMBOL, "sh000905")

#: 指数收益的观察窗口（交易日个数）。
RET_WINDOWS: tuple[int, ...] = (5, 20, 60)

#: 估值中位数的对照窗口：`trading_calendar` 里 `<= asof` 的最后 N 个交易日。
VALUATION_LOOKBACK_DAYS = 250

#: `money_flow_daily.main_net` 的单位是元；报告用亿元。
YI = 1e8

#: 固定说明（K5 的「这是状态描述、不是信号」写在这里，1–3 条）。
_NOTES: tuple[str, ...] = (
    "本块只描述**已经发生**的行情统计（指数已实现收益、当日涨跌家数、估值中位数、"
    "当日资金净流入）——它们是**状态描述**，不是方向信号；块内没有任何前向字段"
    "（预测价/目标价/概率/信号分/评级/买卖建议）。",
    "口径：全部取 `bars_daily`（`adj_mode='none'`）里 `date <= asof` 的行；估值与"
    "资金流各取自己表里 `<= asof` 的最近一个交易日，并在各自 `asof` 里写明是哪天；"
    "`breadth` 的**家数不含指数**（`sh000300` / `sh000905` 也在 `bars_daily` 里，"
    "但它们不是一个「家」）。",
    "取不到一律 `None`（表为空 / 该日无行 / 分母为 0），不猜数、不拿邻近日期顶替。",
)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _round4(x: float | None) -> float | None:
    return None if x is None else round(float(x), 4)


def _median(values: list[float]) -> float | None:
    """中位数（偶数个取中间两个的均值）。空 ⇒ `None`。"""
    if not values:
        return None
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def _quantile(values: list[float], q: float) -> float | None:
    """线性插值分位数（与 numpy 默认同款）。空 ⇒ `None`，单个值 ⇒ 它自己。"""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def _index_block(conn: sqlite3.Connection, *, asof: str) -> dict:
    """每条指数：最新点位 ＋ 5/20/60 交易日已实现收益。取不到就省略该条。"""
    if not _has_table(conn, "bars_daily"):
        return {}
    need = max(RET_WINDOWS)
    rows = conn.execute(
        "SELECT code, date, close FROM ("
        " SELECT code, date, close,"
        "        ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn"
        "  FROM bars_daily WHERE adj_mode = 'none' AND code IN"
        f" ({','.join('?' * len(INDEX_SYMBOLS))}) AND date <= ?"
        ") WHERE rn <= ? ORDER BY code, date",
        (*INDEX_SYMBOLS, asof, need)).fetchall()
    series: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        series.setdefault(str(r["code"]), []).append(
            (str(r["date"]), float(r["close"])))

    out: dict[str, dict] = {}
    for code in INDEX_SYMBOLS:                    # 报告顺序 = 声明顺序，不靠 dict
        points = series.get(code) or []
        if not points:
            continue                              # 取不到 ⇒ 整条省略
        price_asof, level = points[-1]
        # PIT 判据**复用** `rules.check_no_lookahead`（不另写一遍，§0.3.2）。
        check_no_lookahead(asof, {code: Price(
            code=code, price=level, source="bars", price_asof=price_asof,
            detail=price_asof)})
        entry: dict = {"level": round(level, 4), "price_asof": price_asof}
        for window in RET_WINDOWS:
            first = points[-window][1] if len(points) >= window else None
            entry[f"ret_{window}_pct"] = (
                None if first in (None, 0.0)
                else round((level / first - 1) * 100, 4))
        out[code] = entry
    return out


def _trading_dates(conn: sqlite3.Connection, *, asof: str,
                   limit: int | None = None) -> list[str]:
    """`trading_calendar` 里 `is_open=1 AND date <= asof` 的交易日（倒序取前 limit）。"""
    if not _has_table(conn, "trading_calendar"):
        return []
    sql = ("SELECT date FROM trading_calendar WHERE is_open = 1 AND date <= ?"
           " ORDER BY date DESC")
    args: list = [asof]
    if limit is not None:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [str(r["date"]) for r in conn.execute(sql, args)]


#: `breadth` 的候选窗口（自然日）。A 股最长休市（春节 / 国庆）也不会超过 ~12 天，
#: 45 天足够；窗口内一行都没有时**再放宽一次**（见 `_breadth_block`）——
#: 不为此写第二条实现。
_BREADTH_WINDOW_DAYS = 45
_BREADTH_WIDE_DAYS = 400


def _breadth_block(conn: sqlite3.Connection, *, asof: str) -> dict:
    """当日横截面：涨跌家数 ＋ 中位涨跌幅。**一条 SQL**（K8），数在 Python 里。

    为什么用「日期窗口」而不是 `MAX(date)` 子查询：`bars_daily` 的主键是
    `(code, date)`，**没有 date 打头的索引** —— `SELECT MAX(date)` 与
    `SELECT DISTINCT date ORDER BY date DESC LIMIT 2` 都得全表/全索引扫，
    实测各 ~0.3–0.7 s，两块合计会把 K8 的 1.5 s 预算吃光。日期窗口只扫一遍，
    `d` 与「它的前一个交易日」都是从**取回的行**里现算的 ——
    所以口径仍然是「`bars_daily` 中 `<= asof` 的最近一个交易日」，没有换口径。

    指数（`INDEX_SYMBOLS`）**不计入家数** —— 它们也在 `bars_daily` 里，
    但「涨跌家数」数的是标的，不是点位。
    """
    empty = {"asof": None, "n_total": 0, "n_up": 0, "n_down": 0, "n_flat": 0,
             "up_ratio": None, "median_change_pct": None}
    if not _has_table(conn, "bars_daily"):
        return empty
    skip = ",".join("?" * len(INDEX_SYMBOLS))
    rows: list = []
    for window in (_BREADTH_WINDOW_DAYS, _BREADTH_WIDE_DAYS):
        rows = conn.execute(
            "SELECT date, code, close, pre_close FROM bars_daily"
            f" WHERE adj_mode = 'none' AND code NOT IN ({skip})"
            " AND date <= ? AND date >= date(?, ?)",
            (*INDEX_SYMBOLS, asof, asof, f"-{window} day")).fetchall()
        if rows:
            break
    if not rows:
        return empty
    # 行里现算「最近一个交易日」与「它的前一个交易日」（升序 ⇒ 取尾部两个）。
    dates = sorted({str(r["date"]) for r in rows})
    cur_date = dates[-1]
    prev_date = dates[-2] if len(dates) > 1 else None
    prev_close: dict[str, float] = {}
    current: list[tuple[str, float | None, float | None]] = []
    for r in rows:
        date, code = str(r["date"]), str(r["code"])
        close = None if r["close"] is None else float(r["close"])
        pre_close = None if r["pre_close"] is None else float(r["pre_close"])
        if date == prev_date:
            prev_close[code] = close
        elif date == cur_date:
            current.append((code, close, pre_close))
    changes: list[float] = []
    n_up = n_down = n_flat = 0
    for code, close, pre_close in current:
        # 基准优先用行自带的 `pre_close`；真库里这一列**整列为 NULL**，
        # 于是回落到该标的前一交易日的收盘价（`pre_close` 的定义就是这个数）。
        base = pre_close if pre_close is not None else prev_close.get(code)
        if close is None or base in (None, 0.0):
            continue                              # 算不出 ⇒ 只计 n_total，不计涨跌
        change = close / base - 1.0
        changes.append(change)
        if change > 0:
            n_up += 1
        elif change < 0:
            n_down += 1
        else:
            n_flat += 1
    denom = n_up + n_down + n_flat
    return {"asof": cur_date,
            "n_total": len(current),
            "n_up": n_up, "n_down": n_down, "n_flat": n_flat,
            "up_ratio": None if not denom else round(n_up / denom, 4),
            "median_change_pct": (None if not changes
                                  else round(_median(changes) * 100, 4))}


def _valuation_on(conn: sqlite3.Connection, date: str) -> dict:
    """某个交易日的估值横截面（中位数 / 分位）。该日无行 ⇒ 全 `None`。"""
    rows = conn.execute(
        "SELECT pe_ttm, pb FROM valuation_daily WHERE date = ?", (date,)).fetchall()
    pes = [float(r["pe_ttm"]) for r in rows if r["pe_ttm"] is not None]
    pbs = [float(r["pb"]) for r in rows if r["pb"] is not None]
    return {"n_total": len(rows),
            "pe_ttm_median": _round4(_median(pes)),
            "pe_ttm_p25": _round4(_quantile(pes, 0.25)),
            "pe_ttm_p75": _round4(_quantile(pes, 0.75)),
            "pb_median": _round4(_median(pbs))}


def _valuation_block(conn: sqlite3.Connection, *, asof: str) -> dict:
    """估值横截面 ＋ 与 250 交易日前的中位数对照。"""
    if not _has_table(conn, "valuation_daily"):
        return {"asof": None, "n_total": 0, "pe_ttm_median": None,
                "pe_ttm_p25": None, "pe_ttm_p75": None, "pb_median": None,
                "pe_ttm_median_250d_ago": None,
                "pe_ttm_median_change_pct": None}
    row = conn.execute(
        "SELECT MAX(date) AS d FROM valuation_daily WHERE date <= ?",
        (asof,)).fetchone()
    date = row["d"] if row else None
    cur = ({"n_total": 0, "pe_ttm_median": None, "pe_ttm_p25": None,
            "pe_ttm_p75": None, "pb_median": None}
           if date is None else _valuation_on(conn, str(date)))
    # 250 交易日前：`trading_calendar` 里 `<= asof` 的最后 250 个交易日里**最早那个**。
    past = _trading_dates(conn, asof=asof, limit=VALUATION_LOOKBACK_DAYS)
    past_date = past[-1] if past else None
    past_median = None
    if past_date is not None:
        past_median = _valuation_on(conn, past_date)["pe_ttm_median"]
    change = None
    if cur["pe_ttm_median"] is not None and past_median not in (None, 0.0):
        change = round((cur["pe_ttm_median"] - past_median) / past_median * 100, 4)
    return {"asof": None if date is None else str(date),
            "n_total": cur["n_total"],
            "pe_ttm_median": cur["pe_ttm_median"],
            "pe_ttm_p25": cur["pe_ttm_p25"],
            "pe_ttm_p75": cur["pe_ttm_p75"],
            "pb_median": cur["pb_median"],
            "pe_ttm_median_250d_ago": _round4(past_median),
            "pe_ttm_median_change_pct": change}


def _money_flow_block(conn: sqlite3.Connection, *, asof: str) -> dict:
    """当日资金净流入：总净额（亿元）与净流入只数占比。"""
    empty = {"asof": None, "n_total": 0, "main_net_sum_yi": None,
             "net_inflow_ratio": None}
    if not _has_table(conn, "money_flow_daily"):
        return empty
    row = conn.execute(
        "SELECT MAX(date) AS d FROM money_flow_daily WHERE date <= ?",
        (asof,)).fetchone()
    date = row["d"] if row else None
    if date is None:
        return empty
    agg = conn.execute(
        "SELECT COUNT(*) AS n_total,"
        " SUM(CASE WHEN main_net IS NOT NULL THEN 1 ELSE 0 END) AS n_main,"
        " SUM(main_net) AS main_sum,"
        " SUM(CASE WHEN main_net > 0 THEN 1 ELSE 0 END) AS n_inflow"
        " FROM money_flow_daily WHERE date = ?", (str(date),)).fetchone()
    n_main = int(agg["n_main"] or 0)
    main_sum = agg["main_sum"]
    return {"asof": str(date),
            "n_total": int(agg["n_total"] or 0),
            "main_net_sum_yi": (None if main_sum is None
                                else round(float(main_sum) / YI, 2)),
            "net_inflow_ratio": (None if not n_main
                                 else round(int(agg["n_inflow"] or 0) / n_main, 4))}


def market_block(conn: sqlite3.Connection, *, asof: str) -> dict:
    """`market` 块（字段照 P84 任务书 §0.6，逐字）。

    `asof` = **决策日**（不是数据日）；各子块自己的 `asof` 才是数据日。
    表为空 / 缺表一律返回 `n_total: 0` ＋ 各字段 `None`，**不抛**。
    """
    return {"asof": asof,
            "index": _index_block(conn, asof=asof),
            "breadth": _breadth_block(conn, asof=asof),
            "valuation": _valuation_block(conn, asof=asof),
            "money_flow": _money_flow_block(conn, asof=asof),
            "notes": list(_NOTES)}


__all__: list[str] = ["market_block", "INDEX_SYMBOLS", "RET_WINDOWS",
                      "VALUATION_LOOKBACK_DAYS"]
