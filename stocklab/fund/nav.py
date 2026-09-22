"""基金日净值与「等权平均」对照臂（P52 / D-36 第三条对照臂）。

## 数据源：**非官方**接口，页面上必须标注

天天基金的单只基金净值序列挂在
`https://fund.eastmoney.com/pingzhongdata/<code>.js` 的 `Data_netWorthTrend`
（一个 `[{x: 毫秒时间戳, y: 单位净值, ...}, …]` 的数组）。110011 实测 7741 个日净值点。

它是**非官方**接口、字段含义没有契约，所以：
- `source` 列如实记 `'eastmoney-js'`（换源 = 换口径，行必须能分开读）；
- 页面与报告里必须出现「近似 / 非官方」字样（`paper/config.FUND_NAV_APPROX_NOTE`）。

## 为什么基金臂**只能比净值曲线**

公募基金的**持仓不公开**（季报只披露前十大、且滞后）。所以这条臂与各模拟臂之间
不存在「同口径的成交与成本」—— 它是几条**净值曲线并列**，不是一条可交易的臂。
这一点决定了两个设计：

1. 它**不进** `paper_nav_daily`（那会让人以为它有持仓、有成交、有成本）；
   它自己一张 `fund_nav_daily`，页面只画线。
2. 它**不许被写成「指数」** —— 它是「持仓未知的基金组合」。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

#: 北京时间。解析毫秒时间戳用它，理由见 `parse_pingzhongdata`。
TZ = ZoneInfo("Asia/Shanghai")

TABLE_FUND_NAV = "fund_nav_daily"
SOURCE_EASTMONEY_JS = "eastmoney-js"

#: 等权平均的**固定清单**：先验选定（都是长期存续的主动权益基金），
#: **不按历史业绩挑** —— 按业绩挑出来的「等权平均」是一个被选择偏差污染过的基准，
#: 拿它当对手会让 AI 臂的成绩看起来更好，而那正是本系统最该避免的错。
#: 改这份清单 = 改口径，必须与净值一起读（`docs/decisions/ADR-024`）。
EQUAL_WEIGHT_POOL: tuple[tuple[str, str], ...] = (
    ("110011", "易方达优质精选混合"),
    ("000001", "华夏成长混合"),
    ("163402", "兴全趋势投资混合"),
    ("519066", "汇添富蓝筹稳健混合"),
    ("260108", "景顺长城新兴成长混合"),
)

_TREND_RE = re.compile(r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", re.DOTALL)


class FundNavError(RuntimeError):
    """基金净值的可预期错误（源站格式变了、区间里一个点都没有）。"""


@dataclass(frozen=True)
class NavPoint:
    date: str
    nav: float


def parse_pingzhongdata(text: str) -> list[NavPoint]:
    """从 `pingzhongdata/<code>.js` 文本里抽出日净值序列。

    抽不到 `Data_netWorthTrend` → **报错**，不返回空列表：空列表与「这只基金
    在区间内没有净值」长得一模一样，而后者会被读成「这只基金不存在」。
    """
    m = _TREND_RE.search(text)
    if m is None:
        raise FundNavError(
            "源文本里找不到 `Data_netWorthTrend = [...]` —— 源站格式可能变了；"
            "不返回空序列（空序列会被读成「这只基金没有净值」）")
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise FundNavError(f"`Data_netWorthTrend` 不是合法 JSON：{exc.msg}") from None
    out: list[NavPoint] = []
    for row in raw:
        try:
            x, y = row["x"], row["y"]
        except (TypeError, KeyError):
            continue
        if y is None:
            continue
        ms = float(x)
        # 毫秒时间戳 → **北京时间**的日期。为什么按 +08:00 而不是 UTC：
        # 源站的毫秒值可能是「当日 UTC 零点」，也可能是「当日北京零点」
        # （= 前一日的 16:00 UTC）—— 两种约定我都无法在离线环境里证实。
        # 而 +08:00 对**两种都给出同一个日期**：
        #   UTC 零点 + 8h → 同一天 08:00；北京零点 + 0h → 同一天 00:00。
        # 按 UTC 取则会在第二种约定下整体早一天（**静默**的错）。
        # 真源核对留给 nanobot 跑一次 `fund ingest --fetch`（本项目不联网）。
        date = datetime.fromtimestamp(ms / 1000.0, tz=TZ).date().isoformat()
        out.append(NavPoint(date=date, nav=float(y)))
    return sorted(out, key=lambda p: p.date)


def fetch_text(code: str, *, http=None) -> str:
    """真实抓取（唯一出网点）。**测试不用它** —— 判据走 `parse_pingzhongdata` + 本地夹具。"""
    from stocklab.data.http import HttpClient
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    client = http or HttpClient()
    return client.get_text(url, encoding="utf-8", source="fund",
                           cache_key=f"fund-{code}")


def ingest_points(conn: sqlite3.Connection, *, code: str, points: list[NavPoint],
                  now: str, source: str = SOURCE_EASTMONEY_JS,
                  commit: bool = True) -> int:
    """落库（只增）：已存在的 `(code, date)` 且值相同 → 跳过；**值不同 → 报错**。

    报错而不是覆盖：净值被源站重算过是**口径变更**，静默覆盖会让「当时算的
    净值曲线」无法复现（与 `valuation_daily` / `money_flow_daily` 同一条纪律）。
    """
    existing = {str(r["date"]): float(r["nav"]) for r in conn.execute(
        f"SELECT date, nav FROM {TABLE_FUND_NAV} WHERE code = ?", (code,))}
    written = 0
    for p in points:
        if p.date in existing:
            if abs(existing[p.date] - p.nav) > 1e-9:
                raise FundNavError(
                    f"{code} {p.date} 已有净值 {existing[p.date]}，源站现在给的是 "
                    f"{p.nav} —— append-only，不覆盖历史行；"
                    f"要改口径请换 source 或另立一只 code")
            continue
        conn.execute(
            f"INSERT INTO {TABLE_FUND_NAV} (code, date, nav, source, created_at)"
            " VALUES (?,?,?,?,?)", (code, p.date, p.nav, source, now))
        written += 1
    if commit:
        conn.commit()
    return written


def nav_by_date(conn: sqlite3.Connection, code: str, *,
                asof: str | None = None) -> dict[str, float]:
    """该基金 `<= asof` 的净值序列。**表还没前滚 → 空字典**（不是异常）。

    展示层（`/lab/paper`）是**只读**的、不外滚 schema：老库上缺这张表要能
    给出「不可比」，而不是 500。空字典与「这只基金没净值」在上层是同一个结论
    （`comparable=False`），而那正是真话。
    """
    if not has_table(conn):
        return {}
    sql = f"SELECT date, nav FROM {TABLE_FUND_NAV} WHERE code = ?"
    args: list = [code]
    if asof is not None:
        sql += " AND date <= ?"
        args.append(asof)
    return {str(r["date"]): float(r["nav"]) for r in conn.execute(sql, args)}


def has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE_FUND_NAV,)).fetchone()[0] > 0


def equal_weight_curve(conn: sqlite3.Connection, *, dates: list[str], start: str,
                       codes: tuple[str, ...] | None = None) -> dict:
    """等权平均的**累计收益**曲线（各基金自 `start` 的累计收益取算术平均）。

    口径写死三句，免得日后有人换一种算法把结论「救」回来：

    1. **等权 = 各基金累计收益的算术平均**（起点各投 1/n，此后不再平衡）。
       这不是「指数」——没有编制规则、没有成分调整，只有一条**持仓未知**的组合曲线；
    2. `start` 之前没有净值的基金**不参与**（`n_funds_used` 如实上报），
       它的权重也不摊给别人 —— 悄悄重分配等于悄悄换了一个基准；
    3. 某个日期上**一只基金都没有**净值 → 该点 `None`（**不可比**），
       **不是 0**。写 0 会把「没数据」画成「那天没涨没跌」。

    这一臂不发单、不记净值行到 `paper_nav_daily`：它没有持仓、没有成交、没有成本。
    """
    pool = tuple(c for c, _ in EQUAL_WEIGHT_POOL) if codes is None else tuple(codes)
    series = {c: nav_by_date(conn, c, asof=None) for c in pool}
    bases: dict[str, float] = {}
    for c, points in series.items():
        for d in sorted(points):
            if d >= start:
                bases[c] = points[d]
                break
    used = sorted(bases)
    out: list[float | None] = []
    for d in dates:
        rets = []
        for c in used:
            v = series[c].get(d)
            if v is not None:
                rets.append(v / bases[c] - 1.0)
        out.append(round(sum(rets) / len(rets), 6) if rets else None)
    missing = sorted({c for c in pool if c not in bases})
    return {
        "codes": list(pool),
        "codes_used": used,
        "codes_missing": missing,
        "n_funds_used": len(used),
        "start": start,
        "points": out,
        "n_points_missing": sum(1 for v in out if v is None),
        "latest": next((v for v in reversed(out) if v is not None), None),
        "tradable": False,
        "is_index": False,
        "comparable": bool(used) and any(v is not None for v in out),
    }


def latest_nav_date(conn: sqlite3.Connection) -> str | None:
    if not has_table(conn):
        return None
    row = conn.execute(f"SELECT MAX(date) AS d FROM {TABLE_FUND_NAV}").fetchone()
    return (str(row["d"]) if row and row["d"] else None)


def all_codes(conn: sqlite3.Connection) -> list[str]:
    if not has_table(conn):
        return []
    return sorted({str(r["code"]) for r in conn.execute(
        f"SELECT DISTINCT code FROM {TABLE_FUND_NAV}")})


__all__ = ["EQUAL_WEIGHT_POOL", "FundNavError", "NavPoint", "SOURCE_EASTMONEY_JS",
           "TABLE_FUND_NAV", "all_codes", "equal_weight_curve", "fetch_text",
           "has_table", "ingest_points", "latest_nav_date", "nav_by_date",
           "parse_pingzhongdata"]
