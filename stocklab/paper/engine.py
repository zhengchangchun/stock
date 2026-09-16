"""模拟盘引擎（P19）：`init` / `step` / `show` 的编排层。

## 三条臂（口径见 `paper/config.py` 与 ADR-010）

| 账户 | 状态来源 | 交易 |
|---|---|---|
| `arm-hold` | `init` 时**冻结**的快照 | 永不 |
| `arm-now` | 每步从 `real_trades`+`cash_flows` **重放**（实盘账本的镜像） | 永不 |
| `arm-discipline-{05,10,15}` | 自身 `paper_trades` | 只在触发纪律时 |

起跑日 `arm-hold` 与 `arm-now` 数值**必然相同**（描述同一份状态），
区别在之后：用户录一笔真成交，`arm-now` 跟着变、`arm-hold` 不变。

## 输出是**数据库状态的函数**

`step()` 的返回载荷只由「库里的行 + PIT 收盘价」决定，**不含决策时刻、不含随机性**。
所以同日重跑逐字节一致（验收 ①）靠的不是「记得别写」，而是**没有东西可写**。
「本次有没有真的下单」这类过程信息走 stderr，不进载荷 —— 否则重跑就不再一致。

## PIT：价格是参数，不是环境

`prices=` 是**注入点**（与 `--now` 注入时钟同一个理由：时间/价格敏感的判定，
参数化才钉得住）。默认从 `bars_daily` 取 `adj_mode='none'`、`date <= asof` 的最近收盘。
**刻意不用 `portfolio/prices.resolve_price`**：它优先取同日盘中快照，
而模拟盘记的是**收盘**净值 —— 快照可能是未完成的盘中价（ADR-009）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from stocklab.config.costs import ASSET_ETF, ASSET_STOCK, CostModel
from stocklab.paper import store
from stocklab.paper.config import (
    ARM_HOLD,
    ARM_NOW,
    DISCLAIMER,
    DISCLOSURE_ITEMS,
    DISCIPLINE_PREFIX,
    ETF_TRANCHES,
    ETF_WHITELIST,
    HOLD_CODE,
    HOLD_QTY,
    INITIAL_CAPITAL,
    LOT,
    PAPER_START_DATE,
    PER_STEP_CASH_PCT,
    RULE_CITATIONS,
)
from stocklab.paper.rules import (
    C_LOT,
    C_NO_PRICE,
    C_SINGLE,
    Decision,
    check_no_lookahead,
    etf_leg_targets,
    floor_lot,
    plan_etf_buy,
    plan_stop_loss,
    plan_trim,
)
from stocklab.portfolio.discipline import (
    check_cash_band,
    check_no_add_above,
    check_position_weight,
    check_stop_loss_close,
)
from stocklab.portfolio.prices import Price
from stocklab.store.db import transaction

INDEX_300_SYMBOL = "sh000300"
MAX_ORDERS_PER_STEP = 10          # ETF 建仓循环上限（跑满要留痕，见 `_build_etf`）

BARS_CLOSE_SQL = (
    "SELECT date, close FROM bars_daily"
    " WHERE code = ? AND date <= ? AND adj_mode = 'none'"
    " ORDER BY date DESC LIMIT 1"
)


class PaperError(RuntimeError):
    """模拟盘层可预期的错误（起跑日不符、账本对不上、取不到价）。"""


class LedgerMismatchError(PaperError):
    """实盘账本与声明口径不符 —— **报错，不静默采用账本**。"""


class MissingPriceError(PaperError):
    """持仓/候选标的取不到收盘价 → 算不出净值。

    **报错而不是跳过**：跳过会让一个「今天没记」看起来像「今天没啥事」，
    这正是 ERROR_DIARY 里「摘要说没事、正文里有事」的同型失效。
    """


# ---------- 价格与账本 ----------

def pit_close(conn: sqlite3.Connection, code: str, asof: str) -> Price | None:
    """`asof` 当日或之前最近一个**不复权收盘价**（PIT）。取不到返回 None。"""
    row = conn.execute(BARS_CLOSE_SQL, (code, asof)).fetchone()
    if row is None:
        return None
    return Price(code=code, price=float(row["close"]), source="bars",
                 price_asof=str(row["date"]), detail=str(row["date"]))


def resolve_marks(conn: sqlite3.Connection, codes, asof: str) -> dict[str, Price]:
    out: dict[str, Price] = {}
    for code in sorted(set(codes)):
        p = pit_close(conn, code, asof)
        if p is not None:
            out[code] = p
    return out


def ledger_state(conn: sqlite3.Connection, asof: str) -> dict:
    """实盘账本在 `asof` 收盘后的状态（`arm-now` 的镜像源）。

    现金公式与 `portfolio.ledger.cash_summary` **同源**：
    净入金 + Σ(卖出净收) − Σ(买入总付)。两处算迟早会漂移，
    故 `test_ledger_cash_formula_matches_ledger_module_when_unfiltered` 直接对拍。
    """
    from stocklab.portfolio.positions import open_positions
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM real_trades WHERE date <= ? ORDER BY date, trade_id", (asof,))]
    flows = [dict(r) for r in conn.execute(
        "SELECT * FROM cash_flows WHERE date <= ? ORDER BY date, flow_id", (asof,))]
    net_deposits = sum(float(r["amount"]) for r in flows)
    trade_cash = 0.0
    for t in trades:
        gross = float(t["price"]) * int(t["qty"])
        fee = float(t["fee"])
        trade_cash += -(gross + fee) if t["side"] == "buy" else gross - fee
    return {
        "cash": round(net_deposits + trade_cash, 4),
        "positions": {c: p.qty for c, p in open_positions(trades).items()},
        "net_deposits": round(net_deposits, 4),
        "cum_cost": round(sum(float(t["fee"]) for t in trades), 4),
        "trades": trades,
    }


# ---------- 账户状态 ----------

def _positions_of(account: dict) -> dict[str, int]:
    return {p["code"]: int(p["qty"])
            for p in json.loads(account["initial_positions_json"])}


def _apply(positions: dict[str, int], code: str, side: str, qty: int) -> None:
    held = positions.get(code, 0)
    new = held + qty if side == "buy" else held - qty
    if new < 0:
        raise PaperError(f"超卖：{code} 持有 {held}，卖出 {qty}")
    if new == 0:
        positions.pop(code, None)
    else:
        positions[code] = new


def _ledger_arm_state(conn: sqlite3.Connection, account: dict, asof: str) -> dict:
    """`arm-hold`（冻结）与 `arm-discipline-*`（自身成交）的状态。"""
    cash = float(account["initial_cash"])
    positions = _positions_of(account)
    cum_cost = float(json.loads(account["params_json"]).get("seed_fee", 0.0))
    if account["arm"] == "discipline":
        for t in store.load_trades(conn, account_id=account["account_id"], asof=asof):
            _apply(positions, t["code"], t["side"], int(t["qty"]))
            gross = float(t["fill_price"]) * int(t["qty"])
            cash += -(gross + float(t["fee_total"])) if t["side"] == "buy" \
                else gross - float(t["fee_total"])
            cum_cost += float(t["fee_total"])
    # net_deposits 是**本金**（20,000）。入场费 5.09 是**成本**不是入金 ——
    # 把它算进分母会让「累计收益」凭空好看一点点，而那正是最不该有的偏差。
    return {"cash": round(cash, 4), "positions": positions,
            "cum_cost": round(cum_cost, 4),
            "net_deposits": float(json.loads(account["params_json"])["initial_capital"])}


def _arm_state(conn: sqlite3.Connection, account: dict, asof: str) -> dict:
    if account["arm"] == "now":
        led = ledger_state(conn, asof)
        return {"cash": led["cash"], "positions": dict(led["positions"]),
                "cum_cost": led["cum_cost"], "net_deposits": led["net_deposits"]}
    return _ledger_arm_state(conn, account, asof)


def _mark_to_market(positions: dict[str, int],
                    marks: dict[str, Price]) -> tuple[float, list[dict]]:
    mv, out = 0.0, []
    for code in sorted(positions):
        qty = positions[code]
        p = marks.get(code)
        if p is None:
            raise MissingPriceError(
                f"持仓 {code} × {qty} 取不到 ≤ asof 的收盘价 → 净值不可判定。"
                f"先跑 `ingest bars`；**不用成本价冒充现价**（那会把浮亏恒显示成 0）")
        mv += p.price * qty
        out.append({"code": code, "qty": qty, "price": p.price,
                    "source": p.source, "price_asof": p.price_asof})
    return round(mv, 4), out


def _drawdown(history: list[float], nav: float) -> float:
    """相对**历史峰值**的回撤（正数 = 回撤幅度）。首行峰值即自身 → 0。"""
    peak = max([*history, nav]) if history else nav
    if peak <= 0:
        return 0.0
    return round((peak - nav) / peak, 6)


# ---------- init ----------

def _declared_seed(led: dict, start_date: str) -> None:
    """校验实盘账本 == 声明口径（100 股 @86.80 / 本金 20,000）。不符**报错**。"""
    pos = led["positions"]
    if set(pos) != {HOLD_CODE} or pos.get(HOLD_CODE) != HOLD_QTY:
        raise LedgerMismatchError(
            f"实盘账本在 {start_date} 的持仓是 {pos!r}，与声明口径 "
            f"{{{HOLD_CODE!r}: {HOLD_QTY}}} 不符 —— 不静默采用账本")
    if abs(led["net_deposits"] - INITIAL_CAPITAL) > 1e-6:
        raise LedgerMismatchError(
            f"实盘账本净入金 {led['net_deposits']:,.2f} ≠ 声明本金 "
            f"{INITIAL_CAPITAL:,.2f} —— 起跑口径必须先定死")
    buys = [t for t in led["trades"] if t["code"] == HOLD_CODE and t["side"] == "buy"]
    if not buys or float(buys[-1]["price"]) != 86.80:
        raise LedgerMismatchError(
            f"实盘账本最后一笔 {HOLD_CODE} 买入价 "
            f"{float(buys[-1]['price']) if buys else None} ≠ 声明成本 86.80")


def init_accounts(conn: sqlite3.Connection, *, start_date: str = PAPER_START_DATE,
                  now: str) -> dict:
    """建三条臂（纪律臂按 `ETF_TRANCHES` 展开成 3 档）。幂等：已存在则不改写。"""
    led = ledger_state(conn, start_date)
    _declared_seed(led, start_date)
    positions = _positions_of({"initial_positions_json": json.dumps(
        [{"code": c, "qty": q} for c, q in led["positions"].items()])})
    marks = resolve_marks(conn, positions, start_date)
    if set(marks) != set(positions):
        raise MissingPriceError(f"起跑日 {start_date} 缺少持仓收盘价："
                                f"{sorted(set(positions) - set(marks))}")
    seed_fee = led["cum_cost"]
    initial_nav = round(sum(p.price * positions[c] for c, p in marks.items())
                        + led["cash"], 4)
    params = {"seed_fee": seed_fee, "lot": LOT,
              "per_step_cash_pct": PER_STEP_CASH_PCT,
              "etf_whitelist": list(ETF_WHITELIST),
              "start_date": start_date, "initial_capital": INITIAL_CAPITAL}
    specs = [(ARM_HOLD, "hold", None), (ARM_NOW, "now", None)]
    specs += [(f"{DISCIPLINE_PREFIX}{int(t):02d}", "discipline", t)
              for t in ETF_TRANCHES]
    created = []
    for account_id, arm, target in specs:
        if store.account_exists(conn, account_id):
            continue
        store.insert_account(
            conn, account_id=account_id, arm=arm, etf_target_pct=target,
            start_date=start_date, initial_cash=led["cash"],
            initial_positions=[{"code": c, "qty": q}
                               for c, q in sorted(led["positions"].items())],
            initial_nav=initial_nav, params=params, now=now)
        created.append(account_id)
    return {"created": bool(created), "accounts": created, "start_date": start_date,
            "initial_nav": initial_nav, "initial_cash": led["cash"],
            "initial_positions": positions}


# ---------- 规则评估（纯重算，不写库） ----------

def evaluate(conn: sqlite3.Connection, account: dict, *, asof: str,
             cash: float, positions: dict[str, int], marks: dict[str, Price],
             total_assets: float) -> list[Decision]:
    """当前状态下各条规则怎么说（含**不动的理由**）。不写库、无副作用。"""
    stock_costs = CostModel(asset_class=ASSET_STOCK)
    out: list[Decision] = []
    held = positions.get(HOLD_CODE, 0)
    p_hold = marks.get(HOLD_CODE)
    close = p_hold.price if p_hold else None
    src = p_hold.source if p_hold else None
    pasof = p_hold.price_asof if p_hold else None
    if held > 0 or account["arm"] == "discipline":
        out.append(_stamp(plan_stop_loss(
            code=HOLD_CODE, close=close, qty=held, costs=stock_costs,
            source=src, price_asof=pasof), p_hold))
        if held > 0 and close is not None:
            out.append(_stamp(plan_trim(
                code=HOLD_CODE, qty=held, close=close, total_assets=total_assets,
                costs=stock_costs, intent_pct=None), p_hold))
    if account["arm"] == "discipline" and account["etf_target_pct"]:
        targets = etf_leg_targets(etf_target_pct=account["etf_target_pct"],
                                  total_assets=total_assets)
        if not [c for c in ETF_WHITELIST if c in marks]:
            out.append(Decision(
                action="hold", code=None, qty=0,
                rule_citation=RULE_CITATIONS["etf_first_build"],
                reason=f"白名单 ETF {list(ETF_WHITELIST)} 在 {asof} 前都取不到收盘价 "
                       f"→ 不建仓（不猜价、不用别的标的价格顶替）",
                binding_constraints=(C_NO_PRICE,)))
        for code in ETF_WHITELIST:
            p = marks.get(code)
            if p is None:
                continue
            leg_value = (positions.get(code, 0) or 0) * p.price
            out.append(_stamp(plan_etf_buy(
                code=code, price=p.price, cash=cash, total_assets=total_assets,
                leg_gap_value=max(0.0, targets[code] - leg_value),
                costs=CostModel(asset_class=ASSET_ETF)), p))
    return out


def _stamp(d: Decision, price: Price | None) -> Decision:
    if price is None:
        return d
    return replace(d, price_source=price.source, price_asof=price.price_asof)


def discipline_checks(*, cash: float, positions: dict[str, int], total: float,
                      marks: dict[str, Price]) -> list[dict]:
    """账户在 `asof` 的状态判定（复用 `portfolio.discipline` 的口径，不另写一套）。

    取不到价 → 传 `None`，判定自然落到 `UNDETERMINED`（**不是 PASS**）——
    没有价就没有权重，把它当 0% 会静默放过一条本该报警的规则。
    """
    p = marks.get(HOLD_CODE)
    weight = (positions.get(HOLD_CODE, 0) * p.price / total * 100.0
              if p is not None and total > 0 else None)
    checks = [check_position_weight(HOLD_CODE, weight)]
    checks.append(check_cash_band(cash / total * 100.0 if total > 0 else None))
    checks.append(check_stop_loss_close(HOLD_CODE, p.price if p else None,
                                        source=p.source if p else None,
                                        price_asof=p.price_asof if p else None))
    checks.append(check_no_add_above(HOLD_CODE, p.price if p else None))
    return checks


# ---------- step ----------

def _build_etf(conn: sqlite3.Connection, account: dict, *, asof: str,
               cash: float, positions: dict[str, int], marks: dict[str, Price],
               total: float, deployed_today: float,
               evaluations: list[Decision]) -> tuple[float, float, list[Decision]]:
    """按「缺口最大者先」的顺序建仓，直到本步额度/缺口用尽。

    返回 `(cash, deployed_today, 实际下单的 decisions)`。
    """
    orders: list[Decision] = []
    etf_costs = CostModel(asset_class=ASSET_ETF)
    for _ in range(MAX_ORDERS_PER_STEP):
        targets = etf_leg_targets(etf_target_pct=account["etf_target_pct"],
                                  total_assets=total)
        # 缺口最大者先；并列取 code 升序（确定性，不含任何偏好）。
        # 取不到价的腿直接排除：不猜价、不用另一条腿的价格顶替。
        available = [c for c in ETF_WHITELIST if c in marks]
        if not available:
            break
        ranked = sorted(
            available,
            key=lambda c: (-max(0.0, targets[c] - positions.get(c, 0) * marks[c].price),
                           c))
        progressed = False
        for code in ranked:
            gap = max(0.0, targets[code] - positions.get(code, 0) * marks[code].price)
            if gap <= 0:
                continue
            d = _stamp(plan_etf_buy(
                code=code, price=marks[code].price, cash=cash, total_assets=total,
                leg_gap_value=gap, costs=etf_costs,
                deployed_today=deployed_today), marks[code])
            evaluations.append(d)
            if not d.is_trade:
                continue
            _apply(positions, code, "buy", d.qty)
            cash -= d.amount
            deployed_today += d.amount
            total = round(cash + sum(positions[c] * marks[c].price
                                     for c in positions if c in marks), 4)
            orders.append(d)
            progressed = True
            break                      # 重新按新的缺口排序（下单后缺口变了）
        if not progressed:
            break
        if len(orders) >= MAX_ORDERS_PER_STEP:
            # 触到上限**必须留痕**（铁律③：不许静默截断）
            evaluations.append(Decision(
                action="hold", code=None, qty=0,
                rule_citation=RULE_CITATIONS["etf_first_build"],
                reason=f"⚠️ 本步下单数已达上限 {MAX_ORDERS_PER_STEP} → 停止建仓（留痕）",
                binding_constraints=("max_orders_per_step",)))
            break
    return cash, deployed_today, orders


def _plan_steps(conn: sqlite3.Connection, account: dict, *, asof: str,
                cash: float, positions: dict[str, int], marks: dict[str, Price],
                total: float) -> tuple[float, dict[str, int], float, list[Decision],
                                       list[Decision]]:
    """跑一遍固定优先级：止损 → 超限减仓 → 分散建仓。返回新状态 + 评估 + 下单。"""
    evaluations = evaluate(conn, account, asof=asof, cash=cash,
                           positions=positions, marks=marks, total_assets=total)
    orders: list[Decision] = []
    stock_costs = CostModel(asset_class=ASSET_STOCK)
    deployed = 0.0

    held = positions.get(HOLD_CODE, 0)
    p_hold = marks.get(HOLD_CODE)

    # a. 止损（硬，安全）
    d = _stamp(plan_stop_loss(code=HOLD_CODE, close=p_hold.price if p_hold else None,
                              qty=held, costs=stock_costs,
                              source=p_hold.source if p_hold else None,
                              price_asof=p_hold.price_asof if p_hold else None), p_hold)
    if d.is_trade:
        _apply(positions, HOLD_CODE, "sell", d.qty)
        cash += d.amount
        total = round(cash + sum(positions[c] * marks[c].price
                                 for c in positions if c in marks), 4)
        orders.append(d)

    # b. 超限减仓（硬，上限）
    held = positions.get(HOLD_CODE, 0)
    if held > 0 and p_hold is not None:
        d = _stamp(plan_trim(code=HOLD_CODE, qty=held, close=p_hold.price,
                             total_assets=total, costs=stock_costs), p_hold)
        if d.is_trade:
            _apply(positions, HOLD_CODE, "sell", d.qty)
            cash += d.amount
            total = round(cash + sum(positions[c] * marks[c].price
                                     for c in positions if c in marks), 4)
            orders.append(d)

    # c. 分散建仓
    cash, deployed, etf_orders = _build_etf(
        conn, account, asof=asof, cash=cash, positions=positions, marks=marks,
        total=total, deployed_today=deployed, evaluations=evaluations)
    orders.extend(etf_orders)
    return cash, positions, total, evaluations, orders


def step(conn: sqlite3.Connection, asof: str, *, now: str,
         prices: dict[str, Price] | None = None) -> dict:
    """按 `asof` 收盘推进一天。**幂等**：已有净值的账户整个跳过（不重复下单）。

    `prices` 只用于把「当日可用的价格」显式注入（默认从 `bars_daily` 取）。
    注入的价格同样受 PIT 守卫检查 —— 喂未来价**报错**（验收 ⑤）。
    """
    accounts = store.load_accounts(conn)
    if not accounts:
        raise PaperError("模拟盘账户不存在；先跑 `paper init`")
    start = accounts[0]["start_date"]
    if asof < start:
        raise PaperError(f"asof {asof} 早于起跑日 {start} —— 起跑日之前不记净值")
    if prices is not None:
        check_no_lookahead(asof, prices)

    # **整个 step 一个事务**：任一账户失败（取不到价、超卖、未来价…）就整体回滚。
    # 否则会出现「前 4 个账户已落净值、第 5 个没有」的半截状态 ——
    # 那种状态比直接失败危险得多：它看起来像「这天记过了」。
    with transaction(conn):
        _step_all(conn, asof, accounts=accounts, now=now, prices=prices)
    return state_payload(conn, asof)


def _step_all(conn: sqlite3.Connection, asof: str, *, accounts: list[dict],
              now: str, prices: dict[str, Price] | None) -> None:
    for account in accounts:
        if store.nav_exists(conn, account["account_id"], asof):
            continue
        state = _arm_state(conn, account, asof)
        codes = set(state["positions"]) | ({HOLD_CODE} | set(ETF_WHITELIST)
                                          if account["arm"] == "discipline" else set())
        marks = dict(prices) if prices is not None else resolve_marks(conn, codes, asof)
        check_no_lookahead(asof, marks)
        missing = sorted(c for c in state["positions"] if c not in marks)
        if missing:
            raise MissingPriceError(f"持仓缺少 {asof} 及之前的收盘价：{missing}")
        cash, positions = state["cash"], dict(state["positions"])
        mv, _ = _mark_to_market(positions, marks)
        total = round(cash + mv, 4)
        orders: list[Decision] = []
        if account["arm"] == "discipline":
            cash, positions, total, _evals, orders = _plan_steps(
                conn, account, asof=asof, cash=cash, positions=positions,
                marks=marks, total=total)
        for d in orders:
            store.insert_trade(conn, account_id=account["account_id"], date=asof,
                               decision=d, now=now, commit=False)
        mv, _marks = _mark_to_market(positions, marks)
        nav = round(cash + mv, 4)
        history = [r["nav"] for r in store.load_nav(conn, account["account_id"],
                                                    asof=asof)]
        if account["arm"] == "discipline":
            cum_cost = state["cum_cost"] + sum(d.fees.get("total", 0.0) for d in orders)
        else:
            cum_cost = state["cum_cost"]
        idx = pit_close(conn, INDEX_300_SYMBOL, asof)
        store.insert_nav(
            conn, account_id=account["account_id"], date=asof, cash=round(cash, 4),
            positions=[{"code": c, "qty": q} for c, q in sorted(positions.items())],
            market_value=mv, nav=nav, drawdown=_drawdown(history, nav),
            cum_cost=round(cum_cost, 4),
            cum_return=round(nav / state["net_deposits"] - 1.0, 6)
            if state["net_deposits"] else 0.0,
            net_deposits=round(state["net_deposits"], 4),
            index_300_level=idx.price if idx else None,
            index_300_asof=idx.price_asof if idx else None, now=now, commit=False)


# ---------- 读回 ----------

def _account_entry(conn: sqlite3.Connection, account: dict, asof: str,
                   prices: dict[str, Price] | None = None) -> dict:
    nav_row = store.latest_nav(conn, account["account_id"], asof=asof)
    if nav_row is None or nav_row["date"] != asof:
        raise PaperError(f"{account['account_id']} 在 {asof} 没有净值行；先跑 `paper step`")
    positions = {p["code"]: int(p["qty"]) for p in json.loads(nav_row["positions_json"])}
    codes = set(positions) | ({HOLD_CODE} | set(ETF_WHITELIST)
                              if account["arm"] == "discipline" else {HOLD_CODE})
    marks = dict(prices) if prices is not None else resolve_marks(conn, codes, asof)
    check_no_lookahead(asof, marks)
    total = nav_row["nav"]
    return {
        "account_id": account["account_id"], "arm": account["arm"],
        "etf_target_pct": account["etf_target_pct"], "date": nav_row["date"],
        "cash": nav_row["cash"], "positions": positions,
        "market_value": nav_row["market_value"], "nav": nav_row["nav"],
        "drawdown": nav_row["drawdown"], "cum_cost": nav_row["cum_cost"],
        "cum_return": nav_row["cum_return"], "net_deposits": nav_row["net_deposits"],
        "marks": {c: {"price": p.price, "source": p.source,
                      "price_asof": p.price_asof} for c, p in sorted(marks.items())
                  if c in positions},
        "discipline": discipline_checks(cash=nav_row["cash"], positions=positions,
                                        total=total, marks=marks),
        "evaluation": [json.loads(json.dumps(_decision_json(d)))
                       for d in evaluate(conn, account, asof=asof,
                                         cash=nav_row["cash"], positions=positions,
                                         marks=marks, total_assets=total)],
        "decisions": [_trade_json(t) for t in
                      store.trades_on(conn, account["account_id"], asof)],
        "nav_history_points": len(store.load_nav(conn, account["account_id"],
                                                 asof=asof)),
    }


def _decision_json(d: Decision) -> dict:
    return {"rule": d.rule_citation, "action": d.action, "code": d.code,
            "qty": d.qty, "reason": d.reason,
            "binding_constraints": list(d.binding_constraints),
            "planned_shares_raw": d.planned_shares_raw,
            "violation_remaining": d.violation_remaining,
            "weight_after_pct": d.weight_after_pct,
            "price_source": d.price_source, "price_asof": d.price_asof}


def _trade_json(t: dict) -> dict:
    return {"trade_id": t["trade_id"], "code": t["code"], "side": t["side"],
            "qty": t["qty"], "ref_price": t["ref_price"],
            "fill_price": t["fill_price"],
            "fees": {"commission": t["commission"], "stamp_tax": t["stamp_tax"],
                     "transfer_fee": t["transfer_fee"],
                     "slippage_cost": t["slippage_cost"], "total": t["fee_total"]},
            "amount": round(t["fill_price"] * t["qty"]
                            + (t["fee_total"] if t["side"] == "buy"
                               else -t["fee_total"]), 2),
            "asset_class": t["asset_class"], "rule_citation": t["rule_citation"],
            "reason": t["reason"],
            "binding_constraints": json.loads(t["binding_json"]),
            "price_source": t["price_source"], "price_asof": t["price_asof"]}


def accounts_state(conn: sqlite3.Connection, asof: str | None = None) -> list[dict]:
    """全部账户的**最新**净值状态（`show` 与重跑 `step` 的返回用同一形状）。"""
    out = []
    for account in store.load_accounts(conn):
        latest = store.latest_nav(conn, account["account_id"], asof=asof)
        if latest is None:
            continue
        out.append(_account_entry(conn, account, latest["date"]))
    return out


def state_payload(conn: sqlite3.Connection, asof: str) -> dict:
    """`asof` 的载荷 —— **只由库里的行 + PIT 价格决定**，故重跑逐字节一致。"""
    idx = pit_close(conn, INDEX_300_SYMBOL, asof)
    return {
        "asof": asof,
        "accounts": [_account_entry(conn, a, asof) for a in store.load_accounts(conn)
                     if store.nav_exists(conn, a["account_id"], asof)],
        "index_300": ({"level": idx.price, "price_asof": idx.price_asof}
                      if idx else None),
        "disclosure": list(DISCLOSURE_ITEMS),
    }


# ---------- 报告 ----------

ASOF_SOURCE_EXPLICIT = "explicit"
ASOF_SOURCE_TODAY = "today"
ASOF_SOURCE_LATEST_NAV = "latest_nav"

NO_NAV_TODAY_FMT = "今日净值未生成，展示 {date}"


def resolve_show_asof(conn: sqlite3.Connection, today: str, *,
                      requested: str | None = None) -> dict:
    """`paper show` 的 asof 解析（顺序即优先级）。

    1. 显式给了 `--asof` → **照用**（`explicit`）：用户问哪天就答哪天，空就是空。
    2. 没给 → 先试今天；**今天已有净值**就用今天（`today`）。
    3. 今天一行净值都没有 → 回落到 `MAX(paper_nav_daily.date)`（`latest_nav`）。
       改前这里直接按今天过滤 → `accounts: []`，看起来像「模拟盘不存在」。
    4. 库里一条净值都没有 → 保持今天（`today`），此时 accounts 为空是**真话**。
    """
    latest = store.latest_nav_date(conn)
    if requested is not None:
        return {"asof": requested, "asof_source": ASOF_SOURCE_EXPLICIT,
                "latest_nav_date": latest}
    if store.nav_date_exists(conn, today):
        return {"asof": today, "asof_source": ASOF_SOURCE_TODAY,
                "latest_nav_date": latest}
    if latest is not None and latest < today:
        return {"asof": latest, "asof_source": ASOF_SOURCE_LATEST_NAV,
                "latest_nav_date": latest}
    return {"asof": today, "asof_source": ASOF_SOURCE_TODAY,
            "latest_nav_date": latest}


def show_payload(conn: sqlite3.Connection, today: str, *,
                 requested: str | None = None) -> dict:
    """`paper show` 的对外载荷 = `state_payload` + asof 的实际来源。

    回落时在 `disclosure` 里写明「展示的不是今天」——**不许静默**（铁律③）。
    """
    res = resolve_show_asof(conn, today, requested=requested)
    payload = state_payload(conn, res["asof"])
    payload["asof_source"] = res["asof_source"]
    payload["latest_nav_date"] = res["latest_nav_date"]
    if res["asof_source"] == ASOF_SOURCE_LATEST_NAV:
        payload["disclosure"] = [*payload["disclosure"],
                                 NO_NAV_TODAY_FMT.format(date=res["asof"])]
    return payload


def build_report(conn: sqlite3.Connection, asof: str) -> dict:
    """报告数据（纯函数式：同一库 + 同一 asof → 同一结果，不含生成时刻）。"""
    accounts = store.load_accounts(conn)
    entries = [_account_entry(conn, a, asof) for a in accounts
               if store.nav_exists(conn, a["account_id"], asof)]
    idx = pit_close(conn, INDEX_300_SYMBOL, asof)
    start = accounts[0]["start_date"] if accounts else PAPER_START_DATE
    idx_start = pit_close(conn, INDEX_300_SYMBOL, start)
    idx_ret = None
    if idx and idx_start and idx_start.price:
        idx_ret = round(idx.price / idx_start.price - 1.0, 6)
    per_account = []
    for e in entries:
        hist = store.load_nav(conn, e["account_id"], asof=asof)
        navs = [h["nav"] for h in hist]
        peak, mdd = (navs[0] if navs else 0.0), 0.0
        for v in navs:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak)
        per_account.append({
            **{k: e[k] for k in ("account_id", "arm", "etf_target_pct", "date",
                                 "cash", "positions", "market_value", "nav",
                                 "cum_cost", "cum_return", "net_deposits",
                                 "drawdown", "discipline")},
            "max_drawdown": round(mdd, 6),
            "excess_vs_index_300": (round(e["cum_return"] - idx_ret, 6)
                                    if idx_ret is not None else None),
            "trades": store.load_trades(conn, account_id=e["account_id"], asof=asof),
            "etf_actual_pct": round(
                sum(e["positions"].get(c, 0) * e["marks"][c]["price"]
                    for c in ETF_WHITELIST if c in e["marks"]) / e["nav"] * 100.0, 4)
            if e["nav"] else None,
        })
    return {
        "asof": asof, "start_date": start,
        "index_300": ({"level": idx.price, "price_asof": idx.price_asof,
                       "return_since_start": idx_ret, "tradable": False,
                       "note": "指数不可直接交易，故不含成本 —— 与三臂对照时口径偏乐观"}
                      if idx else None),
        "accounts": per_account,
        "disclosure": list(DISCLOSURE_ITEMS),
        "disclaimer": DISCLAIMER,
        "sample_note": (f"起跑日 {start} → {asof} 共 "
                        f"{len(store.load_nav(conn, accounts[0]['account_id'], asof=asof)) if accounts else 0}"
                        f" 个交易日，**<120 交易日 → 样本不足，仅供观察**"),
    }


def render_report(rep: dict) -> str:
    """渲染 Markdown 报告。**不写生成时刻**（同输入 → 逐字节一致）。"""
    L: list[str] = []
    L.append(f"# 模拟盘日报 · {rep['asof']}")
    L.append("")
    L.append(f"> {rep['disclaimer']}")
    L.append("")
    L.append(f"> {rep['sample_note']}")
    L.append("")
    L.append("## 一、三条臂净值（并列，不挑「推荐」）")
    L.append("")
    L.append("| 账户 | 口径 | 净值 | 累计收益 | 最大回撤 | 累计成本 | ETF 实际占比 |")
    L.append("|---|---|---|---|---|---|---|")
    for a in rep["accounts"]:
        target = (f"ETF 目标 {a['etf_target_pct']:.0f}%"
                  if a["etf_target_pct"] is not None else
                  ("什么都不做" if a["arm"] == "hold" else "实盘账本镜像"))
        actual = ("—" if a["etf_actual_pct"] is None
                  else f"{a['etf_actual_pct']:.2f}%")
        L.append(f"| `{a['account_id']}` | {target} | {a['nav']:,.2f} | "
                 f"{a['cum_return'] * 100:+.2f}% | {a['max_drawdown'] * 100:.2f}% | "
                 f"{a['cum_cost']:,.2f} | {actual} |")
    L.append("")
    L.append("## 二、与 index_300 对照")
    L.append("")
    idx = rep["index_300"]
    if idx is None:
        L.append("- **UNDETERMINED**：库里没有 `sh000300` 的收盘价，不做比较。"
                 "（不比较 ≠ 跑赢）")
    else:
        L.append(f"- index_300 收盘 {idx['level']:,.2f}（{idx['price_asof']}），"
                 f"起跑日至今 {idx['return_since_start'] * 100:+.2f}% "
                 f"（{idx['note']}）")
        L.append("")
        L.append("| 账户 | 累计收益 | 相对 index_300 超额 |")
        L.append("|---|---|---|")
        for a in rep["accounts"]:
            ex = a["excess_vs_index_300"]
            L.append(f"| `{a['account_id']}` | {a['cum_return'] * 100:+.2f}% | "
                     f"{'—' if ex is None else f'{ex * 100:+.2f}%'} |")
    L.append("")
    L.append("## 三、持仓与纪律判定")
    L.append("")
    for a in rep["accounts"]:
        pos = "、".join(f"{c}×{q}" for c, q in sorted(a["positions"].items())) or "空仓"
        L.append(f"### `{a['account_id']}`")
        L.append("")
        L.append(f"- 持仓：{pos}；现金 {a['cash']:,.2f}；"
                 f"净入金 {a['net_deposits']:,.2f}")
        for chk in a["discipline"]:
            L.append(f"- [{chk['status']}] {chk['detail']}")
        if a["etf_target_pct"] is not None:
            L.append(f"- ETF 目标 {a['etf_target_pct']:.0f}% → 实际 "
                     f"{a['etf_actual_pct']:.2f}%（差 "
                     f"{a['etf_target_pct'] - a['etf_actual_pct']:.2f} 个百分点；"
                     f"差额若来自现金下限 45%，见下节口径说明）")
        L.append("")
    L.append("## 四、起跑日至今的调仓流水（append-only）")
    L.append("")
    L.append("| 日期 | 账户 | 动作 | 标的 | 股数 | 收盘 | 成交价 | 佣金 | 印花税 | "
             "过户费 | 滑点 | 触发条文 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in rep["accounts"]:
        for t in a["trades"]:
            L.append(f"| {t['date']} | `{t['account_id']}` | {t['side']} | {t['code']} | "
                     f"{t['qty']} | {t['ref_price']:.4f} | {t['fill_price']:.4f} | "
                     f"{t['commission']:.2f} | {t['stamp_tax']:.2f} | "
                     f"{t['transfer_fee']:.2f} | {t['slippage_cost']:.2f} | "
                     f"{t['rule_citation']} |")
    L.append("")
    L.append("## 五、口径说明（不许改）")
    L.append("")
    for item in rep["disclosure"]:
        L.append(f"- {item}")
    L.append("")
    return "\n".join(L)
