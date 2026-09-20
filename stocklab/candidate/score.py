"""步骤4/7：调用插桩 0（行业排雷）与 1/2/3（三池打分）。

## `ctx` 必须可 JSON 序列化

这是执行器方案的硬约束，也是将来的保险：把执行器从「进程内 exec」
换成「子进程」（设计文档方案 B）时，`ctx` 要能过管道。**现在就要求
它可序列化**，等于提前把那条路留着 —— 而 Bar 是 dataclass，直接喂
会带上类型信息，所以这里显式转成普通 dict。

## 池 → 插桩的映射写死

短期池→插桩1、中期→插桩2、长期→插桩3。这是文档 01 主流程第 7 步的
原话，属于主干，不做成配置。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from stocklab.candidate import indicators
from stocklab.config.universe import Instrument
from stocklab.data.models import Bar, FinancialReport
from stocklab.plugin import lifecycle

#: 池 → 打分插桩编号。
POOL_PLUGIN: dict[str, str] = {"short": "1", "mid": "2", "long": "3"}

#: 行业特殊排雷插桩编号。
INDUSTRY_SCREEN_PLUGIN = "0"


@dataclass(frozen=True)
class ScoreOutcome:
    code: str
    pool: str
    raw_score: float
    pass_flag: bool
    reason: str
    risk_list: tuple[str, ...]


def _bar_to_dict(b: Bar) -> dict:
    return {"date": b.date, "open": b.open, "high": b.high, "low": b.low,
            "close": b.close, "volume": b.volume, "amount": b.amount,
            "turnover": b.turnover}


_FIN_COLS = ("code", "report_date", "notice_date", "notice_date_source",
             "report_type", "total_assets", "parent_equity", "total_equity",
             "total_liabilities", "inventory", "total_operate_income",
             "operate_cost", "parent_netprofit", "netcash_operate",
             "construct_long_asset", "industry_name", "source")


def load_financials(conn, code: str, *, asof: str) -> list:
    """按 PIT 读该标的**已公告**的财报期（`notice_date <= asof`），按报告期升序。"""
    rows = conn.execute(
        "SELECT * FROM financial_reports WHERE code = ? AND notice_date <= ?"
        " ORDER BY report_date", (code, asof)).fetchall()
    return [FinancialReport(**{k: r[k] for k in _FIN_COLS}) for r in rows]


def build_ctx(conn, inst: Instrument, pool: str, bars: list[Bar], *,
              asof: str, cross_section: dict | None = None) -> dict:
    """构造喂给插桩的上下文。**PIT**：K 线只放 `date <= asof`，
    财报只放 `notice_date <= asof`。

    `features` 的键**永远齐全**（不可算给 `None` + `na_reasons`）——
    缺键会让插桩 KeyError，被沙盒探针判成脚本 bug。
    """
    usable = sorted((b for b in bars if b.date <= asof), key=lambda b: b.date)
    feats = indicators.factors(load_financials(conn, inst.code, asof=asof))
    if cross_section and inst.code in cross_section:
        feats.update(cross_section[inst.code])
    for key in ("roe", "gross_margin", "gm_yoy_pp", "inv_days", "fcf_margin"):
        feats.setdefault(f"{key}_pct", None)
        feats.setdefault(f"{key}_n", 0)
    feats.setdefault("period_mixed", False)
    feats.setdefault("dupont", None)
    feats["asof"] = asof

    sector = conn.execute("SELECT sector FROM instruments WHERE code = ?",
                          (inst.code,)).fetchone()
    return {
        "code": inst.code, "name": inst.name, "asof": asof, "pool": pool,
        "asset_type": inst.asset_type, "board": inst.board,
        "sector": (sector["sector"] if sector else None),
        "bars": [_bar_to_dict(b) for b in usable],
        "features": feats,
    }


def industry_screen(conn: sqlite3.Connection, inst: Instrument,
                    ctx: dict) -> dict:
    """步骤4：调用插桩0 行业特殊排雷。没有 active 版本 → `NoActivePlugin`。"""
    return lifecycle.call_active(conn, INDUSTRY_SCREEN_PLUGIN, ctx)


def score_pool(conn: sqlite3.Connection, inst: Instrument, pool: str,
               ctx: dict) -> ScoreOutcome:
    """步骤7：按池调用对应的打分插桩。"""
    if pool not in POOL_PLUGIN:
        raise ValueError(f"未知池 {pool!r}；已知：{sorted(POOL_PLUGIN)}")
    plugin_id = POOL_PLUGIN[pool]
    result = lifecycle.call_active(conn, plugin_id, ctx)
    return ScoreOutcome(
        code=inst.code, pool=pool, raw_score=float(result["score"]),
        pass_flag=bool(result["pass_flag"]), reason=str(result["reason"]),
        risk_list=tuple(result["risk_list"]))
