"""模块2 通路的 **PIT 上下文**（喂给 `m2_a1/a2/a3/b1` 的那份东西）。

## 判据只有两条，但两条都不许松

1. **只用 `<= asof` 的行**：K 线、财报（`notice_date <= asof`）、候选池快照
   （`snpashot.asof <= asof`）、收盘价（`pit_close`）、账户状态（`arm_state_for`）。
   PIT 守卫复用 `paper/rules.py::check_no_lookahead`，与执行内核**同一条**
   （不是另写一遍 —— 两套守卫迟早有一套先松）。
2. **可复算**：`ctx_sha256` 是 canonical JSON 的 sha256，不含生成时刻、
   不含自增 id。把 `asof` 之后的行塞进库，指纹**必须不变**（T5 的判据）。

## 逐标的的上下文与模块1 **同形**

`candidates` / `holdings` 的每一项都带一份 `build_ctx` 的产物
（`code/name/sector/bars/features/asset_type/board`）—— 直接调
`candidate/score.py::build_ctx`，**不另写一份因子拼装**。
理由：财务因子的口径（`_value_at` 取哪一行、`na_reasons` 怎么落）在这里复制一份，
就等于给「模块1 与模块2 看到的同一个标的不是同一件事」留了门。

顶层的 `candidates` / `holdings` / `cash` 三个键是 P45 在 `PROBE_CTX` 里
**已经占位**的形状（P45 原话：「P46 定稿 ctx 时必须回来核对」）。本模块就是
那次核对：键名沿用、项的形状在这里定死。

## 候选池的「哪一份快照」不在这里决定

`agent_pool.pool_snapshot` 决定「每池取 `asof <= 决策日` 的最近一份」
（ERROR_DIARY #62 的坑：那个循环必须分开表达「取最近一份」与「取一份里的全部」）。
本模块**复用它的选择结果**，只按选中的 `snapshot_id` 去读成员明细 ——
再写一遍那个循环就等于把 #62 复制一份。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from stocklab.candidate import score as candidate_score
from stocklab.candidate.seeds import SEED_UNIVERSE
from stocklab.config.universe import Instrument
from stocklab.data.models import Bar
from stocklab.m2.config import ChannelReject
from stocklab.paper import agent_pool
from stocklab.paper.agent_context import GUARDRAILS, NON_GOALS
from stocklab.paper.config import DISCLOSURE_ITEMS
from stocklab.paper.engine import INDEX_300_SYMBOL, pit_close, resolve_marks
from stocklab.paper.rules import check_no_lookahead

#: `code` → `Instrument`。模块1 只对种子宇宙打分，所以候选池成员必定在其中 ——
#: 找不到就是口径对不上（**拒绝，不猜** board / asset_type：
#: 猜错的 board 会算错涨跌停，猜错的 asset_type 会算错税费）。
INSTRUMENTS: dict[str, Instrument] = {i.code: i for i in SEED_UNIVERSE}

#: 落进 ctx 的标的明细键（`build_ctx` 的产物正好是这些）。
_DETAIL_KEYS: tuple[str, ...] = ("code", "name", "asset_type", "board", "sector",
                                 "bars", "features")

#: 指纹**覆盖**的键。加字段必须显式加到这里 —— 「上下文变了但指纹没变」
#: 是这里最危险的一种失效：它会让 T5 的「PIT 输入不变」悄悄失效却一直显示为真。
HASHED_KEYS: tuple[str, ...] = ("channel", "account_id", "asof", "cash",
                                "total_assets", "holdings", "candidates",
                                "marks", "index_300", "guardrails",
                                "candidate_pool", "focus")


def bars_upto(conn: sqlite3.Connection, code: str, *, asof: str) -> list[Bar]:
    """该标的 `<= asof` 的全部 K 线（**不复权与复权行都在**，与模块1 同款）。

    `build_ctx` 自己按 `date <= asof` 再筛一次；这里多给一天都不给 ——
    SQL 层就挡住了，免得将来有人以为「反正 build_ctx 会筛」。
    """
    rows = conn.execute(
        "SELECT code, date, open, high, low, close, volume, amount, turnover,"
        " source, adj_mode FROM bars_daily WHERE code = ? AND date <= ?"
        " ORDER BY date", (code, asof)).fetchall()
    return [Bar(**dict(r)) for r in rows]


def daily_bar_codes(conn: sqlite3.Connection, codes, *,
                    asof: str) -> dict[str, bool]:
    """每个标的在 `asof` **当日**有没有不复权 K 线（T7 的判据）。

    为什么是「当日」而不是「`<= asof` 的最近一根」：`pit_close` 的口径是后者
    （PIT 允许用最近一根，并把实际日期记进 `price_asof`），那对**读价格**是对的；
    但**推进一天**是另一件事 —— 没有当日的行就意味着这一天没有收过盘
    （非交易日、数据没采到）。拿前值顶上去，会让净值曲线在缺口处凭空延续，
    而报告里看不出任何异常。
    """
    wanted = sorted(set(codes))
    if not wanted:
        return {}
    found = {str(r["code"]) for r in conn.execute(
        "SELECT DISTINCT code FROM bars_daily WHERE adj_mode = 'none' AND date = ?"
        " AND code IN (" + ",".join("?" * len(wanted)) + ")", (asof, *wanted))}
    return {code: code in found for code in wanted}


def require_daily_bars(conn: sqlite3.Connection, codes, *, asof: str) -> list[str]:
    """**必需**当日 K 线的那些标的里缺了谁（空 = 齐全）。

    「必需」由调用方给：通路 A 的必需集合 = **当前持仓**（缺一根就算不出净值）；
    候选池里取不到价的标的**不是**必需 —— 它只是今天不可投，不该让一整天停摆。
    """
    have = daily_bar_codes(conn, codes, asof=asof)
    return sorted(code for code, ok in have.items() if not ok)


def instrument_for(code: str) -> Instrument:
    inst = INSTRUMENTS.get(code)
    if inst is None:
        raise ChannelReject(
            "code", f"{code} 不在种子宇宙（SEED_UNIVERSE）里 —— 阶段是『21 只种子』，"
                    f"池外标的所属板块与标的口径都无从得知。拒绝，不猜 board/asset_type"
                    f"（猜错的 board 会算错涨跌停，猜错的 asset_type 会算错税费）")
    return inst


# ---------- 候选池 ----------


def member_rows(conn: sqlite3.Connection, asof: str,
                pool: Mapping[str, object] | None = None) -> dict[str, list[dict]]:
    """`{池: [成员明细]}` —— 快照的选择权在 `agent_pool`（见模块 docstring）。

    只取 `agent_pool` 选中的那些 `snapshot_id` 的行，**不再自己挑快照**。
    `pool` 已给就用它（同一次运行里不许有两份「当日池」）。
    """
    snap = agent_pool.pool_snapshot(conn, asof) if pool is None else pool
    out: dict[str, list[dict]] = {}
    for name, info in (snap.get("pools") or {}).items():
        rows = conn.execute(
            "SELECT code, pool, raw_score, adj_score, reason, status, entered_at"
            " FROM candidate_members WHERE snapshot_id = ? AND pool = ?"
            " ORDER BY adj_score DESC, code", (info["snapshot_id"], name)).fetchall()
        out[name] = [dict(r) for r in rows]
    return out


def cost_prices_of(account: dict) -> dict[str, float | None]:
    """账户的建仓成本：只认账户行里冻结的 `initial_positions`。

    成交表里没有成本列（那是刻意的），所以后续买入的标的成本**未知** ——
    写 `None` 而不是拿现价顶（「不知道」≠「没赚没赔」）。
    """
    return {str(p["code"]): (None if p.get("cost_price") is None
                             else float(p["cost_price"]))
            for p in json.loads(account["initial_positions_json"])}


# ---------- 逐标的明细 ----------


def per_code_ctx(conn: sqlite3.Connection, code: str, *, asof: str,
                 pool: str | None) -> dict:
    """一个标的的 PIT 明细（直接调模块1 的 `build_ctx`，因子口径不复制）。"""
    inst = instrument_for(code)
    return candidate_score.build_ctx(
        conn, inst, pool, bars_upto(conn, code, asof=asof), asof=asof)


def _detail(conn: sqlite3.Connection, code: str, *, asof: str,
            pool: str | None, extra: dict) -> dict:
    ctx = per_code_ctx(conn, code, asof=asof, pool=pool)
    ctx["pool"] = pool
    out = {**{k: ctx[k] for k in _DETAIL_KEYS}, **extra}
    out["asof"] = asof
    return out


def candidates_ctx(conn: sqlite3.Connection, *, asof: str,
                   marks: Mapping[str, object],
                   pool: Mapping[str, object]) -> tuple[dict, dict]:
    """`(candidates, excluded)` —— 三池成员的 PIT 明细 + 被剔除的池内标的。

    **取不到 `asof` 及之前收盘价的池内标的会被剔除并如实列出**（进 `excluded`），
    而不是塞一个 `None` 价格进去：插桩拿到 `None` 会自己编一个判断，
    而「这一步到底看没看见它」就再也查不出来了。剔除是静默的反面 —— 有名单。
    """
    out: dict[str, list[dict]] = {}
    excluded: dict[str, list[str]] = {}
    for name, rows in member_rows(conn, asof, pool).items():
        kept, dropped = [], []
        for row in rows:
            code = str(row["code"])
            if code not in marks:
                dropped.append(code)
                continue
            kept.append(_detail(conn, code, asof=asof, pool=name, extra={
                "raw_score": float(row["raw_score"]),
                "adj_score": float(row["adj_score"]),
                "pool_reason": str(row["reason"]),
                "status": str(row["status"]),
                "entered_at": str(row["entered_at"]),
                "close": float(marks[code].price),
                "price_asof": str(marks[code].price_asof),
                "price_source": str(marks[code].source),
            }))
        if kept:
            out[name] = kept
        if dropped:
            excluded[name] = dropped
    return out, excluded


def holdings_ctx(conn: sqlite3.Connection, *, asof: str,
                 positions: Mapping[str, int],
                 marks: Mapping[str, object],
                 cost_prices: Mapping[str, float | None],
                 total_assets: float,
                 pool_of: Mapping[str, str | None]) -> list[dict]:
    """当前持仓的 PIT 明细（含成本、收盘价、权重）。缺成本的写 `None`，不填 0。"""
    out: list[dict] = []
    for code in sorted(positions):
        qty = int(positions[code])
        mark = marks.get(code)
        value = None if mark is None else round(float(mark.price) * qty, 4)
        cost = cost_prices.get(code)
        out.append(_detail(conn, code, asof=asof,
                           pool=pool_of.get(code), extra={
            "qty": qty,
            "cost_price": None if cost is None else float(cost),
            "close": None if mark is None else float(mark.price),
            "price_asof": None if mark is None else str(mark.price_asof),
            "price_source": None if mark is None else str(mark.source),
            "market_value": value,
            "weight_pct": (None if value is None or total_assets <= 0
                           else round(value / total_assets * 100.0, 4)),
        }))
    return out


# ---------- 整份上下文 ----------


def channel_ctx(conn: sqlite3.Connection, *, channel: str, account_id: str,
                asof: str, cash: float, positions: Mapping[str, int],
                marks: Mapping[str, object], total_assets: float,
                cost_prices: Mapping[str, float | None],
                focus: dict | None = None) -> dict:
    """喂给四支插桩的整份上下文。

    `focus` 只有 A3/B1（预测类）用：它们**逐持仓标的各调一次**，而契约的返回里
    没有 `code`（形状是「一条预测」），所以「这一次在预测谁」只能由输入侧说清。
    A1/A2 是组合级的（返回里自带 `code`），`focus` 为 `None`。

    `check_no_lookahead(asof, marks)` 在这里再跑一次（执行内核里也跑）——
    同一个判据跑两遍不冗余：这一遍挡的是「喂给插桩的价格」，
    那一遍挡的是「用来成交的价格」，两者不是同一个入口。
    """
    check_no_lookahead(asof, marks)
    pool = agent_pool.pool_snapshot(conn, asof)
    candidates, excluded = candidates_ctx(conn, asof=asof, marks=marks, pool=pool)
    pool_of = {str(r["code"]): name
               for name, rows in candidates.items() for r in rows}
    holdings = holdings_ctx(conn, asof=asof, positions=positions, marks=marks,
                            cost_prices=cost_prices, total_assets=total_assets,
                            pool_of=pool_of)
    idx = pit_close(conn, INDEX_300_SYMBOL, asof)
    if idx is not None:
        check_no_lookahead(asof, {INDEX_300_SYMBOL: idx})
    mark_map = {c: {"price": float(p.price), "price_asof": str(p.price_asof),
                    "source": str(p.source)} for c, p in sorted(marks.items())}
    return {
        "channel": channel,
        "account_id": account_id,
        "asof": asof,
        "cash": round(float(cash), 4),
        "total_assets": round(float(total_assets), 4),
        "holdings": holdings,
        "candidates": candidates,
        "candidates_excluded": excluded,
        "candidate_pool": pool,
        "focus": focus,
        "marks": mark_map,
        "index_300": (None if idx is None else
                      {"level": float(idx.price), "price_asof": str(idx.price_asof),
                       "tradable": False,
                       "note": "指数不可直接交易 —— 与各臂对照时口径偏乐观"}),
        "guardrails": list(GUARDRAILS),
        "disclosure": list(DISCLOSURE_ITEMS),
        "non_goals": list(NON_GOALS),
    }


def ctx_sha256(ctx: Mapping[str, object]) -> str:
    """上下文指纹（canonical JSON，不含时间戳）。同输入 ⇒ 逐字节同指纹。"""
    missing = [k for k in HASHED_KEYS if k not in ctx]
    if missing:
        raise KeyError(f"上下文缺字段 {missing} —— 指纹会漏掉它们，故直接报错")
    payload = {k: ctx[k] for k in HASHED_KEYS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def marks_for(conn: sqlite3.Connection, codes, asof: str) -> dict:
    return resolve_marks(conn, codes, asof)
