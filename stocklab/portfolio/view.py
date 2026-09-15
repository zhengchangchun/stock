"""组合视图（P12 / Task 58）：把账本 + 行情拼成一份**稳定的 JSON**。

`build_portfolio()` 的返回结构就是 P13 页面消费的接口。
字段名与层级**改不得**（除非同步改 `docs/architecture/portfolio-json.md`
和 `tests/test_portfolio_view.py::test_json_schema_is_pinned`）——
悄悄改名的代价是页面静默渲染出空格子，而不是报错。

## 口径（详见 ADR-006）

- **成本默认含费**，同时输出不含费（字段名带 `_incl_fee` / `_excl_fee` 后缀）
- **现金** = Σ现金流 − Σ买入(价×量+费) + Σ卖出(价×量−费)
- **总资产** = 现金 + Σ**有现价**的持仓市值
- **拿到现价的标的才算市值**；拿不到 → `missing_price`，排除出合计并报警。
  **不用成本价冒充、不插值** —— 用成本价冒充会让浮亏恒显示 0，
  这是最坏的一类错误：它让人以为没事。
- **累计净投入** = Σdeposit + Σwithdraw（withdraw 记负数）；分红/税费不算本金

## 取数一律按 `date <= asof`

`asof` 是「那一天收盘后我知道什么」。09-15 的成交与快照不能出现在
09-14 的视图里 —— 那会让复现历史时的每一个数字都带上未来信息。
"""

from __future__ import annotations

import sqlite3
from datetime import date as _date
from datetime import timedelta

from stocklab.portfolio.discipline import DISCIPLINE, run_checks
from stocklab.portfolio.positions import open_positions
from stocklab.portfolio.prices import resolve_prices

#: 现价口径的「说明书」，随 JSON 一起给出去 —— 页面不必再猜数字是怎么来的。
PRICE_POLICY = (
    "同日 quote_snapshots（取 ts 最大）→ bars_daily 最近收盘（adj_mode='none'）"
    "→ missing_price（排除出市值合计并报警；不用成本价冒充、不插值）"
)
COST_POLICY = (
    "成本默认含费（avg_cost_incl_fee / cost_basis_incl_fee）；"
    "不含费口径同时输出（*_excl_fee）。差额即累计费用"
)


def _money(x: float) -> float:
    """金额一律取到分 —— 输出层取整，中间层不取（否则清仓归零的恒等式会破）。"""
    return round(float(x), 2)


def _pct(x: float) -> float:
    return round(float(x), 4)


def _week_start(d: str) -> str:
    """`d` 所在 ISO 周的周一。"""
    dt = _date.fromisoformat(d)
    return (dt - timedelta(days=dt.weekday())).isoformat()


def daily_close(conn: sqlite3.Connection, code: str, asof: str) -> tuple[float | None, str | None]:
    """最近一个 `date ≤ asof` 的**不复权日收盘** → `(close, date)`。

    这是「收盘价口径」的那条价格。**不是**现价 —— 现价可能是盘中快照，
    拿盘中价判「收盘破没破位」，盘中每一分钟都会给出不同答案。
    """
    row = conn.execute(
        "SELECT date, close FROM bars_daily"
        " WHERE code = ? AND date <= ? AND adj_mode = 'none'"
        " ORDER BY date DESC LIMIT 1", (code, asof)).fetchone()
    return (float(row["close"]), str(row["date"])) if row else (None, None)


def weekly_close(conn: sqlite3.Connection, code: str, asof: str) -> tuple[float | None, str | None]:
    """`asof` 所在周、截至 `asof` 的最后一个不复权收盘 → `(close, date)`。

    周中查询拿到的是「本周至今最后一个收盘」，不是「完整一周的收盘」——
    调用方必须把它当成**部分周**看待，`price_asof` 会如实给出是哪天的。
    """
    row = conn.execute(
        "SELECT date, close FROM bars_daily"
        " WHERE code = ? AND date <= ? AND date >= ? AND adj_mode = 'none'"
        " ORDER BY date DESC LIMIT 1",
        (code, asof, _week_start(asof))).fetchone()
    if row is None:
        return None, None
    return float(row["close"]), str(row["date"])


def _names(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["code"]: r["name"]
            for r in conn.execute("SELECT code, name FROM instruments")}


def _advisory(code: str, qty: int) -> list[dict]:
    """把「动作规则」换算成**具体股数**回显。

    纪律数字（10% / 20%）只判状态没有意义 —— 人要的是「那我该卖多少股」。
    """
    out = []
    for rule, key in (("trim_light", "trim_light_pct"),
                      ("trim_on_break", "trim_on_break_pct")):
        pct = DISCIPLINE[key]
        shares = int(qty * pct / 100)
        out.append({
            "rule": f"{rule}_{int(pct)}pct",
            "code": code,
            "pct": pct,
            "shares": shares,
            "note": f"单次{'减仓' if rule == 'trim_light' else '破位减仓'} "
                    f"{pct:.0f}% = {shares} 股（按当前 {qty} 股）",
        })
    return out


def build_portfolio(conn: sqlite3.Connection, asof: str) -> dict:
    """构建 `asof` 收盘后的组合视图。返回稳定 JSON（见模块 docstring）。"""
    trade_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM real_trades WHERE date <= ? ORDER BY date, trade_id", (asof,))]
    flow_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM cash_flows WHERE date <= ? ORDER BY date, flow_id", (asof,))]

    positions = open_positions(trade_rows)
    cash = _cash(flow_rows, trade_rows)
    net_deposits = sum(r["amount"] for r in flow_rows
                       if r["kind"] in ("deposit", "withdraw"))
    other_cash = sum(r["amount"] for r in flow_rows
                     if r["kind"] not in ("deposit", "withdraw"))
    trade_cash = cash - net_deposits - other_cash

    prices = resolve_prices(conn, sorted(positions), asof)
    names = _names(conn)

    priced_mv = 0.0
    missing_mv = 0.0
    missing_codes: list[str] = []
    pos_out: list[dict] = []
    for code in sorted(positions, key=lambda c: (-positions[c].qty, c)):
        p = positions[code]
        price = prices.get(code)
        if price is None:
            missing_codes.append(code)
            missing_mv += 0.0
            pos_out.append({
                "code": code, "name": names.get(code), "qty": p.qty,
                "avg_cost_incl_fee": _pct(p.avg_cost_incl_fee),
                "avg_cost_excl_fee": _pct(p.avg_cost_excl_fee),
                "cost_basis_incl_fee": _money(p.cost_incl_fee),
                "cost_basis_excl_fee": _money(p.cost_excl_fee),
                "price": None, "price_source": None, "price_asof": None,
                "price_detail": None, "market_value": None,
                "float_pnl_incl_fee": None, "float_pnl_excl_fee": None,
                "weight_of_total_assets": None,
                "realized_pnl_incl_fee": _money(p.realized_incl_fee),
                "realized_pnl_excl_fee": _money(p.realized_excl_fee),
                "fees_paid": _money(p.fees_paid),
                "status": "missing_price",
            })
            continue
        mv = price.price * p.qty
        priced_mv += mv
        pos_out.append({
            "code": code, "name": names.get(code), "qty": p.qty,
            "avg_cost_incl_fee": _pct(p.avg_cost_incl_fee),
            "avg_cost_excl_fee": _pct(p.avg_cost_excl_fee),
            "cost_basis_incl_fee": _money(p.cost_incl_fee),
            "cost_basis_excl_fee": _money(p.cost_excl_fee),
            "price": price.price, "price_source": price.source,
            "price_asof": price.price_asof, "price_detail": price.detail,
            "market_value": _money(mv),
            "float_pnl_incl_fee": _money(mv - p.cost_incl_fee),
            "float_pnl_excl_fee": _money(mv - p.cost_excl_fee),
            "weight_of_total_assets": None,      # 总资产算出来后再填
            "realized_pnl_incl_fee": _money(p.realized_incl_fee),
            "realized_pnl_excl_fee": _money(p.realized_excl_fee),
            "fees_paid": _money(p.fees_paid),
            "status": "ok",
        })

    total_assets = _money(cash + priced_mv)
    # 权重按**总资产**算（不是按已定价市值）—— 分母换成后者会让集中度看起来更小
    for row in pos_out:
        if row["market_value"] is None or total_assets <= 0:
            continue
        row["weight_of_total_assets"] = _pct(row["market_value"] / total_assets * 100.0)

    weights = [(r["code"], r["weight_of_total_assets"]) for r in pos_out]
    first = pos_out[0] if pos_out else None
    if first:
        dclose, ddate = daily_close(conn, first["code"], asof)
        wclose, wdate = weekly_close(conn, first["code"], asof)
    else:
        (dclose, ddate), (wclose, wdate) = (None, None), (None, None)
    checks = run_checks(
        positions=weights,
        cash_pct=_pct(cash / total_assets * 100.0) if total_assets > 0 else None,
        close=dclose,
        close_source="bars_daily" if dclose is not None else None,
        close_asof=ddate,
        weekly_close=wclose,
        weekly_source="bars_daily" if wclose is not None else None,
        weekly_asof=wdate,
        price=(first["price"] if first else None),
        total_assets=total_assets or None,
    )

    net_invested = _money(net_deposits)
    total_pnl = _money(total_assets - net_invested)

    warnings: list[str] = []
    for code in missing_codes:
        warnings.append(
            f"{code} 无可用现价（同日无快照、bars_daily 无 ≤asof 的不复权收盘）"
            f"—— 已排除出市值合计，现价字段为 null。**不许**用成本价冒充")
    for c in checks:
        if c["status"] == "FAIL":
            warnings.append(f"[FAIL] {c['detail']}")
        elif c["status"] == "WARN":
            warnings.append(f"[WARN] {c['detail']}")
        elif c["status"] == "UNDETERMINED":
            warnings.append(f"[未判定] {c['detail']}")

    return {
        "asof": asof,
        "cash": _money(cash),
        "cash_breakdown": {
            "net_deposits": _money(net_deposits),
            "trade_cash": _money(trade_cash),
            "other_cash": _money(other_cash),
        },
        "market_value_priced": _money(priced_mv),
        "market_value_missing": _money(missing_mv),
        "total_assets": total_assets,
        "net_invested": net_invested,
        "total_pnl_incl_fee": total_pnl,
        "total_return_incl_fee": (round(total_pnl / net_invested, 6)
                                  if net_invested else None),
        "positions": pos_out,
        "missing_price_codes": missing_codes,
        "discipline": checks,
        "advisory": [a for r in pos_out if r["qty"] for a in _advisory(r["code"], r["qty"])],
        "warnings": warnings,
        "ledger": {"trades": len(trade_rows), "cash_flows": len(flow_rows)},
        "price_policy": PRICE_POLICY,
        "cost_policy": COST_POLICY,
    }


def nav_series(conn: sqlite3.Connection, asof: str, *,
               n_sessions: int = 120) -> dict:
    """按日 mark-to-market 的净值序列（P15 页面曲线用）。

    **口径与 `build_portfolio` 逐字相同**（同一份 `open_positions` /
    `resolve_prices` / `_cash`）：每一天都当一次「那天收盘后我知道什么」，
    取数一律 `date <= 那一天`。这里不引入任何新的估值口径 ——
    曲线与卡片对不上时，人会先怀疑画图，然后怀疑整套数字。

    **缺价的那一天 `total_assets = None`**，不是 0、也不是拿成本价顶上：
    用成本价冒充会让浮亏恒为 0，曲线会在最该报警的那天看起来最平静。
    调用方必须把 `None` 画成**断点**并显式标注。
    """
    days = [str(r["date"]) for r in conn.execute(
        "SELECT date FROM trading_calendar WHERE is_open = 1 AND date <= ?"
        " ORDER BY date DESC LIMIT ?", (asof, n_sessions))]
    days.reverse()

    points: list[dict] = []
    missing: list[str] = []
    for day in days:
        trade_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM real_trades WHERE date <= ? ORDER BY date, trade_id",
            (day,))]
        flow_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM cash_flows WHERE date <= ? ORDER BY date, flow_id",
            (day,))]
        positions = open_positions(trade_rows)
        cash = _cash(flow_rows, trade_rows)
        if not trade_rows and not flow_rows:
            # 账户还没诞生（既无本金也无成交）。给 0 会画出一条「0 元净值」的
            # 平线，读起来像「亏光了」——曲线应当从账户起始日开始。
            points.append({"date": day, "cash": None, "market_value": None,
                           "total_assets": None, "status": "not_started"})
            continue
        prices = resolve_prices(conn, sorted(positions), day)
        if any(prices.get(c) is None for c in positions):
            missing.append(day)
            points.append({"date": day, "cash": _money(cash), "market_value": None,
                           "total_assets": None, "status": "missing_price"})
            continue
        mv = sum(prices[c].price * positions[c].qty for c in positions)
        points.append({"date": day, "cash": _money(cash), "market_value": _money(mv),
                       "total_assets": _money(cash + mv), "status": "ok"})

    return {
        "asof": asof,
        "policy": ("按交易日逐日 mark-to-market：现金 + Σ(当日持仓 × 当日现价)；"
                   "取数一律 date ≤ 该日。缺现价的那天 total_assets = null"
                   "（画成断点），不用成本价冒充"),
        "n_sessions": len(days),
        "window": {"start": days[0] if days else None,
                   "end": days[-1] if days else None},
        "points": points,
        "missing_price_dates": missing,
    }


def _cash(flow_rows, trade_rows) -> float:
    """现金 = Σ现金流（净投入 + 其他） + Σ成交净额（买入流出 / 卖出流入，均含费）。"""
    cash = sum(float(r["amount"]) for r in flow_rows)
    for t in trade_rows:
        gross = float(t["price"]) * int(t["qty"])
        fee = float(t["fee"])
        cash += -(gross + fee) if t["side"] == "buy" else gross - fee
    return cash


def has_alarm(view: dict) -> bool:
    """是否需要人来看：有标的缺现价，或有纪律条目 FAIL。

    缺现价也是 alarm —— 一个「市值少算了一只票」的总资产不该安静地显示出来。
    """
    if view["missing_price_codes"]:
        return True
    return any(c["status"] == "FAIL" for c in view["discipline"])


def render_table(view: dict) -> str:
    """人看的表。数字与 JSON **同源**（都来自 build_portfolio 的结果）。"""
    lines: list[str] = []
    lines.append(f"=== 组合视图 asof {view['asof']} ===")
    lines.append("")
    lines.append(f"{'代码':<8}{'数量':>7}{'均价(含费)':>12}{'现价':>10}"
                 f"{'来源':>10}{'现价日期':>12}{'市值':>13}{'浮盈(含费)':>13}{'占比':>9}")
    for p in view["positions"]:
        def fmt(v, w, spec=",.2f"):
            return f"{v:>{w}{spec}}" if v is not None else f"{'—':>{w}}"
        lines.append(
            f"{p['code']:<8}{p['qty']:>7}"
            f"{p['avg_cost_incl_fee']:>12.4f}"
            f"{fmt(p['price'], 10)}"
            f"{(p['price_source'] or '—'):>10}"
            f"{(p['price_asof'] or '—'):>12}"
            f"{fmt(p['market_value'], 13)}"
            f"{fmt(p['float_pnl_incl_fee'], 13)}"
            + (f"{p['weight_of_total_assets']:>8.2f}%" if p['weight_of_total_assets'] is not None
               else f"{'—':>9}"))
        if p["status"] == "missing_price":
            lines.append(f"{'':<8}⚠️  无可用现价 → 已排除出市值合计，"
                         f"现价/市值/浮盈/占比 均为 —（不用成本价冒充）")
        else:
            lines.append(f"{'':<8}不含费口径：均价 {p['avg_cost_excl_fee']:.4f}，"
                         f"成本 {p['cost_basis_excl_fee']:,.2f}，"
                         f"浮盈 {p['float_pnl_excl_fee']:,.2f}")
    lines.append("")
    lines.append(f"现金            {view['cash']:>14,.2f}   "
                 f"（本金净投入 {view['cash_breakdown']['net_deposits']:,.2f}，"
                 f"成交净额 {view['cash_breakdown']['trade_cash']:,.2f}，"
                 f"其他 {view['cash_breakdown']['other_cash']:,.2f}）")
    lines.append(f"持仓市值(已定价){view['market_value_priced']:>13,.2f}")
    if view["market_value_missing"]:
        lines.append(f"持仓市值(缺现价){view['market_value_missing']:>13,.2f}"
                     f"   ⚠️ {','.join(view['missing_price_codes'])} 未计入")
    lines.append(f"总资产          {view['total_assets']:>14,.2f}")
    lines.append(f"累计净投入      {view['net_invested']:>14,.2f}")
    ret = view["total_return_incl_fee"]
    lines.append(f"累计收益(含费)  {view['total_pnl_incl_fee']:>14,.2f}   "
                 + (f"({ret * 100:+.2f}%)" if ret is not None else "(—)"))
    lines.append("")
    lines.append("--- 纪律检查 ---")
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "UNDETERMINED": "❔"}
    for c in view["discipline"]:
        lines.append(f"{icon.get(c['status'], '?')} {c['status']:<12} {c['detail']}")
    lines.append("")
    lines.append("--- 动作建议（按纪律换算成股数） ---")
    for a in view["advisory"]:
        lines.append(f"• {a['note']}")
    lines.append("")
    lines.append(f"口径：{view['cost_policy']}")
    lines.append(f"      {view['price_policy']}")
    return "\n".join(lines)
