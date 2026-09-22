"""模拟盘引擎（P19）：`init` / `step` / `show` 的编排层。

## 臂分三类（口径见 `paper/config.py`、ADR-010 与 ADR-024）

| 账户 | 状态来源 | 交易 | 决策从哪来 |
|---|---|---|---|
| `arm-hold` | `init` 时**冻结**的快照 | 永不 | —— |
| `arm-now` | 每步从 `real_trades`+`cash_flows` **重放**（实盘账本的镜像） | 永不 | —— |
| `arm-discipline-{05,10,15}` | 自身 `paper_trades` | 只在触发纪律时 | 写死常量 |
| `arm-agent` | 自身 `paper_trades` | 只在当日有决策时 | 台账里当日那一条**操盘决策**（P52/D-34） |
| `arm-agent-random` | 自身 `paper_trades` | 同上（随机抽） | 台账里的随机对照决策 |

起跑日 `arm-hold` 与 `arm-now` 数值**必然相同**（描述同一份状态），
区别在之后：用户录一笔真成交，`arm-now` 跟着变、`arm-hold` 不变。

## 「写死的条文」与「台账里的决策」共用一条**成交**路径，但决策来源不同

`arm-discipline-*` 走 `_plan_steps`（条文 → 订单），`arm-agent*` 走
`agent_decide.execute_decision`（台账里的目标权重 → 订单）。两条路**共用**
`paper/rules.py` 的成本与整手口径：费用只由 `_fee_parts` 算一次，
所以「同一笔 `(side, price, qty, 口径)`」在两条路上的成交字段逐字段相同
（`tests/test_paper_agent_decide.py` 钉住这一条）。

P52 之前 `arm-agent` 走的是 `_plan_steps` + spec 参数（P37 阶段 1）；D-34 把这条臂
重构成「每交易日一条决策的操盘手」之后，条文参数化那条路只服务静态臂。
`paper spec set` 与它的台账**保留**（历史不删），但不再决定这条臂下不下单。

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
from stocklab.paper import agent_decide, agent_spec, store
from stocklab.paper.config import (
    ARM_AGENT,
    ARM_AGENT_RANDOM,
    ARM_HOLD,
    ARM_KIND_AGENT,
    ARM_KIND_AGENT_RANDOM,
    ARM_KIND_DISCIPLINE,
    ARM_KIND_HOLD,
    ARM_KIND_NOW,
    ARM_KINDS_SELF_DRIVEN,
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
    NOT_COMPARABLE,
    PAPER_START_DATE,
    PER_STEP_CASH_PCT,
    RULE_CITATIONS,
)
from stocklab.paper.rules import (
    C_LOT,
    C_NO_PRICE,
    C_SINGLE,
    Decision,
    RuleParams,
    STATIC_PARAMS,
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

#: `paper show` / 页面的 `agent.history` 保留最近几版（台账本身不截断）。
HISTORY_KEEP = 5

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


# ---------- 臂的分派 ----------

def has_rules(account: dict) -> bool:
    """这条臂跑不跑**写死的纪律条文**（`_plan_steps` 那条路径）。

    P52 起 `arm-agent*` 不在这个集合里：它们的决策来自**决策台账**
    （D-34：AI 当操盘手，方向 ＋ 仓位 ＋ 池内选标的），纪律条文不再给它们下单。
    注意这与「会不会下单」不是同一个问题 —— 它们照旧下单，只是走
    `_execute_agent_decision`。判断「会不会下单」用 `trades_by_decision`。
    """
    return account["arm"] == ARM_KIND_DISCIPLINE


def trades_by_decision(account: dict) -> bool:
    """这条臂的成交来自**决策台账里的目标权重**（P52 的 AI 操盘手与它的随机对照）。"""
    return account["arm"] in (ARM_KIND_AGENT, ARM_KIND_AGENT_RANDOM)


def replays_own_trades(account: dict) -> bool:
    """现金/持仓从**自己的成交**推出来（与 `arm-hold` 的冻结快照、`arm-now` 的
    实盘账本重放相对）。"""
    return account["arm"] in ARM_KINDS_SELF_DRIVEN


def _position_snapshot(account: dict) -> dict[str, int]:
    return {p["code"]: int(p["qty"])
            for p in json.loads(account["initial_positions_json"])}


def params_for_account(conn: sqlite3.Connection, account: dict, asof: str) -> RuleParams:
    """这条臂本次决策的参数组。

    - `arm-discipline-*` ⇒ `STATIC_PARAMS` 换一个 `etf_target_pct`（账户列里的档位）；
    - `arm-agent*` ⇒ `STATIC_PARAMS`（P52 起它们不下纪律条文的单 —— 决策走
      `paper_agent_decisions` 里的目标权重，见 `agent_decide.plan_orders`）；
    - `arm-hold` / `arm-now` ⇒ 同上（它们不下单；参数只用于 `evaluate` 里的
      「不动的理由」，`etf_target_pct=None` ⇒ 不建仓）。
    """
    target = account["etf_target_pct"]
    return replace(STATIC_PARAMS,
                   etf_target_pct=None if target is None else float(target))


def _arm_state(conn: sqlite3.Connection, account: dict, asof: str) -> dict:
    if account["arm"] == ARM_KIND_NOW:
        led = ledger_state(conn, asof)
        return {"cash": led["cash"], "positions": dict(led["positions"]),
                "cum_cost": led["cum_cost"], "net_deposits": led["net_deposits"]}
    return _ledger_arm_state(conn, account, asof)


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
    """`arm-hold`（冻结）与「自身成交」那些臂（纪律 / 智能体 / 随机对照）的状态。

    `replays_own_trades(account)` 决定要不要重放自己的成交 —— `arm-agent*` 与
    `arm-discipline-*` 共用同一条重放路径（都不读 `real_trades`）。
    """
    cash = float(account["initial_cash"])
    positions = _position_snapshot(account)
    cum_cost = float(json.loads(account["params_json"]).get("seed_fee", 0.0))
    if replays_own_trades(account):
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
    """建臂（纪律臂按 `ETF_TRANCHES` 展开 3 档 + 智能体臂与它的随机对照）。

    幂等：已存在则不改写。

    `arm-agent` / `arm-agent-random` 的 `etf_target_pct` 列写 `None` —— 它们的 ETF
    目标来自**台账里的当前 spec**，不来自账户行。把当时的 spec 抄进这一列，
    等于在库里存下第二个真相，而它不会随 spec 变。
    """
    led = ledger_state(conn, start_date)
    _declared_seed(led, start_date)
    positions = _position_snapshot({"initial_positions_json": json.dumps(
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
    specs = [(ARM_HOLD, ARM_KIND_HOLD, None, params),
             (ARM_NOW, ARM_KIND_NOW, None, params)]
    specs += [(f"{DISCIPLINE_PREFIX}{int(t):02d}", ARM_KIND_DISCIPLINE, t, params)
              for t in ETF_TRANCHES]
    # 智能体臂：P52 起它**不跑纪律条文** —— 决策来自 `paper_agent_decisions` 里
    # 当日那一条（D-34：方向 ＋ 仓位 ＋ 池内选标的）。台账为空 ⇒ 那天不下单，
    # 于是它在起跑日与 `arm-hold` 同值（同一起点、还没决定任何事），
    # 差别从第一条决策开始。**不给它编一个默认条文**：那会把「还没决定」
    # 显示成「决定按默认纪律办」，两者在读数上是两回事。
    specs += [(ARM_AGENT, ARM_KIND_AGENT, None,
               {**params, "decision_source": agent_decide.TABLE_DECISIONS,
                "decision_kind": "portfolio",
                "wired_from": "P52：每交易日一条操盘决策（写入口 paper agent decide）"}),
              (ARM_AGENT_RANDOM, ARM_KIND_AGENT_RANDOM, None,
               {**params, "decision_source": agent_decide.TABLE_DECISIONS,
                "decision_kind": "portfolio",
                "counter_arm": ARM_AGENT,
                "wired_from": "P52：随机抽标的与权重，同护栏同成本（归因必需）"})]
    created = []
    for account_id, arm, target, acct_params in specs:
        if store.account_exists(conn, account_id):
            continue
        store.insert_account(
            conn, account_id=account_id, arm=arm, etf_target_pct=target,
            start_date=start_date, initial_cash=led["cash"],
            initial_positions=[{"code": c, "qty": q}
                               for c, q in sorted(led["positions"].items())],
            initial_nav=initial_nav, params=acct_params, now=now)
        created.append(account_id)
    return {"created": bool(created), "accounts": created, "start_date": start_date,
            "initial_nav": initial_nav, "initial_cash": led["cash"],
            "initial_positions": positions}


# ---------- 规则评估（纯重算，不写库） ----------

def evaluate(conn: sqlite3.Connection, account: dict, *, asof: str,
             cash: float, positions: dict[str, int], marks: dict[str, Price],
             total_assets: float,
             params: RuleParams | None = None) -> list[Decision]:
    """当前状态下各条规则怎么说（含**不动的理由**）。不写库、无副作用。

    `params` 不给就用写死条文（静态臂）；给了就按它跑 —— 这就是「智能体臂与静态臂
    共用同一个内核」在代码上的全部含义。
    """
    p = params or STATIC_PARAMS
    stock_costs = CostModel(asset_class=ASSET_STOCK)
    out: list[Decision] = []
    held = positions.get(HOLD_CODE, 0)
    p_hold = marks.get(HOLD_CODE)
    close = p_hold.price if p_hold else None
    src = p_hold.source if p_hold else None
    pasof = p_hold.price_asof if p_hold else None
    if held > 0 or has_rules(account):
        out.append(_finalize(plan_stop_loss(
            code=HOLD_CODE, close=close, qty=held, costs=stock_costs,
            source=src, price_asof=pasof, params=p), p_hold, p))
        if held > 0 and close is not None:
            out.append(_finalize(plan_trim(
                code=HOLD_CODE, qty=held, close=close, total_assets=total_assets,
                costs=stock_costs, intent_pct=None, params=p), p_hold, p))
    if p.etf_target_pct:
        targets = etf_leg_targets(etf_target_pct=p.etf_target_pct,
                                  total_assets=total_assets, whitelist=p.whitelist)
        if not [c for c in p.whitelist if c in marks]:
            out.append(Decision(
                action="hold", code=None, qty=0,
                rule_citation=p.cite("etf_first_build"),
                reason=f"白名单 ETF {list(p.whitelist)} 在 {asof} 前都取不到收盘价 "
                       f"→ 不建仓（不猜价、不用别的标的价格顶替）",
                binding_constraints=(C_NO_PRICE,)))
        for code in p.whitelist:
            mark = marks.get(code)
            if mark is None:
                continue
            leg_value = (positions.get(code, 0) or 0) * mark.price
            out.append(_finalize(plan_etf_buy(
                code=code, price=mark.price, cash=cash, total_assets=total_assets,
                leg_gap_value=max(0.0, targets[code] - leg_value),
                costs=CostModel(asset_class=ASSET_ETF), params=p), mark, p))
    return out


def _finalize(d: Decision, price: Price | None,
              params: RuleParams | None = None) -> Decision:
    """盖章：标上价格出处 + （智能体臂）spec 溯源标签。

    标价格出处：规则层不认识数据源，而「这个价是哪来的」是事后审计的第一问。
    标 spec 溯源：`paper_trades` 是 append-only 的，所以「这笔单照哪一版 spec 下的」
    必须在成交行自己说得清，否则读单笔成交的人还得自己 JOIN 台账。
    静态臂的 `spec_tag` 为空 → reason 一个字不改（逐字节对拍的前提）。
    """
    p = params or STATIC_PARAMS
    if price is not None:
        d = replace(d, price_source=price.source, price_asof=price.price_asof)
    if p.spec_tag:
        d = replace(d, reason=f"{d.reason}（{p.spec_tag}）")
    return d


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
               evaluations: list[Decision],
               params: RuleParams | None = None) -> tuple[float, float, list[Decision]]:
    """按「缺口最大者先」的顺序建仓，直到本步额度/缺口用尽。

    返回 `(cash, deployed_today, 实际下单的 decisions)`。
    """
    p = params or STATIC_PARAMS
    orders: list[Decision] = []
    etf_costs = CostModel(asset_class=ASSET_ETF)
    for _ in range(MAX_ORDERS_PER_STEP):
        targets = etf_leg_targets(etf_target_pct=p.etf_target_pct or 0.0,
                                  total_assets=total, whitelist=p.whitelist)
        # 缺口最大者先；并列取 code 升序（确定性，不含任何偏好）。
        # 取不到价的腿直接排除：不猜价、不用另一条腿的价格顶替。
        available = [c for c in p.whitelist if c in marks]
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
            d = _finalize(plan_etf_buy(
                code=code, price=marks[code].price, cash=cash, total_assets=total,
                leg_gap_value=gap, costs=etf_costs,
                deployed_today=deployed_today, params=p), marks[code], p)
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
                rule_citation=p.cite("etf_first_build"),
                reason=f"⚠️ 本步下单数已达上限 {MAX_ORDERS_PER_STEP} → 停止建仓（留痕）",
                binding_constraints=("max_orders_per_step",)))
            break
    return cash, deployed_today, orders


def _plan_steps(conn: sqlite3.Connection, account: dict, *, asof: str,
                cash: float, positions: dict[str, int], marks: dict[str, Price],
                total: float,
                params: RuleParams | None = None,
                ) -> tuple[float, dict[str, int], float, list[Decision],
                           list[Decision]]:
    """跑一遍固定优先级：止损 → 超限减仓 → 分散建仓。返回新状态 + 评估 + 下单。

    静态臂与智能体臂都走这里（区别只在 `params`）。
    """
    p = params or STATIC_PARAMS
    evaluations = evaluate(conn, account, asof=asof, cash=cash,
                          positions=positions, marks=marks, total_assets=total,
                          params=p)
    orders: list[Decision] = []
    stock_costs = CostModel(asset_class=ASSET_STOCK)
    deployed = 0.0

    def _recompute() -> float:
        return round(cash + sum(positions[c] * marks[c].price
                                for c in positions if c in marks), 4)

    held = positions.get(HOLD_CODE, 0)
    p_hold = marks.get(HOLD_CODE)

    # a. 止损（硬，安全）
    d = _finalize(plan_stop_loss(
        code=HOLD_CODE, close=p_hold.price if p_hold else None,
        qty=held, costs=stock_costs,
        source=p_hold.source if p_hold else None,
        price_asof=p_hold.price_asof if p_hold else None, params=p), p_hold, p)
    if d.is_trade:
        _apply(positions, HOLD_CODE, "sell", d.qty)
        cash += d.amount
        total = _recompute()
        orders.append(d)

    # b. 超限减仓（硬，上限）
    held = positions.get(HOLD_CODE, 0)
    if held > 0 and p_hold is not None:
        d = _finalize(plan_trim(code=HOLD_CODE, qty=held, close=p_hold.price,
                               total_assets=total, costs=stock_costs, params=p),
                      p_hold, p)
        if d.is_trade:
            _apply(positions, HOLD_CODE, "sell", d.qty)
            cash += d.amount
            total = _recompute()
            orders.append(d)

    # c. 分散建仓
    cash, deployed, etf_orders = _build_etf(
        conn, account, asof=asof, cash=cash, positions=positions, marks=marks,
        total=total, deployed_today=deployed, evaluations=evaluations, params=p)
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
        params = params_for_account(conn, account, asof)
        # AI 操盘手（与它的随机对照）只认**当日那一条**决策（`== asof`，不是「最近一版」）：
        # 「每交易日一条决策」是口径的一部分，用「最近一版」会把昨天那条悄悄执行两次。
        decision = (agent_decide.portfolio_decision_on(
            conn, account["account_id"], asof)
            if trades_by_decision(account) else None)
        payload = (decision or {}).get("payload") or {}
        decision_codes = {str(d["code"]) for d in payload.get("decisions", [])}
        codes = set(state["positions"])
        if has_rules(account):
            codes |= {HOLD_CODE} | set(params.whitelist)
        codes |= decision_codes
        marks = dict(prices) if prices is not None else resolve_marks(conn, codes, asof)
        check_no_lookahead(asof, marks)
        missing = sorted(c for c in (set(state["positions"]) | decision_codes)
                         if c not in marks)
        if missing:
            raise MissingPriceError(f"持仓/决策标的缺少 {asof} 及之前的收盘价：{missing}")
        cash, positions = state["cash"], dict(state["positions"])
        mv, _ = _mark_to_market(positions, marks)
        total = round(cash + mv, 4)
        orders: list[Decision] = []
        if decision is not None and decision_codes:
            # 目标市值按**执行时**的总资产重算：写载荷时与执行时看的是同一批
            # `<= asof` 的行，所以两者同口径；重算是为了不把「报告里的权重」
            # 变成写载荷那一刻的快照（那会让 ¥ 与 % 对不上）。
            payload = agent_decide.rebase_payload(payload, total_assets=total,
                                                  marks=marks, positions=positions)
            cash, positions, orders, _evals = agent_decide.execute_decision(
                conn, arm=account["account_id"], asof=asof, decision=payload,
                cash=cash, positions=positions, marks=marks,
                total_assets=total)
        elif has_rules(account):
            cash, positions, total, _evals, orders = _plan_steps(
                conn, account, asof=asof, cash=cash, positions=positions,
                marks=marks, total=total, params=params)
        for d in orders:
            store.insert_trade(conn, account_id=account["account_id"], date=asof,
                               decision=d, now=now, commit=False)
        mv, _marks = _mark_to_market(positions, marks)
        nav = round(cash + mv, 4)
        history = [r["nav"] for r in store.load_nav(conn, account["account_id"],
                                                    asof=asof)]
        if has_rules(account) or decision is not None:
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
    params = params_for_account(conn, account, asof)
    codes = set(positions) | ({HOLD_CODE} if has_rules(account) else set())
    if params.etf_target_pct:
        codes |= set(params.whitelist)
    decision = (agent_decide.portfolio_decision_on(
        conn, account["account_id"], asof)
        if trades_by_decision(account) else None)
    payload = (decision or {}).get("payload") or {}
    codes |= {str(d["code"]) for d in payload.get("decisions", [])}
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
        # AI 操盘手不跑纪律条文 ⇒ 它的 `evaluation` 为空是**真话**，
        # 不是「没有理由」：理由在 `agent_decision` 里（台账那一条）。
        "evaluation": ([] if trades_by_decision(account) else
                       [json.loads(json.dumps(_decision_json(d)))
                        for d in evaluate(conn, account, asof=asof,
                                          cash=nav_row["cash"], positions=positions,
                                          marks=marks, total_assets=total,
                                          params=params)]),
        "agent_decision": (_decision_entry(decision, payload, positions, marks, asof)
                           if trades_by_decision(account) else None),
        "decisions": [_trade_json(t) for t in
                      store.trades_on(conn, account["account_id"], asof)],
        "nav_history_points": len(store.load_nav(conn, account["account_id"],
                                                 asof=asof)),
    }


def _decision_entry(decision: dict | None, payload: dict, positions: dict[str, int],
                    marks: dict[str, Price], asof: str) -> dict:
    """账户条目里的「当日决策」块（台账里那一条的**只读**投影）。"""
    if decision is None:
        return {"asof": asof, "present": False,
                "note": (f"{asof} 没有决策行 ⇒ 本日**不下单**。"
                         f"非交易日不出决策；交易日缺决策也是「没决定」，不补造")}
    total = float(payload.get("total_assets") or 0.0)
    return {
        "asof": asof, "present": True,
        "decision_id": int(decision["decision_id"]),
        "decision_kind": str(decision.get("decision_kind") or ""),
        "agent_kind": str(decision["agent_kind"]),
        "model_id": str(decision["model_id"]),
        "seed": int(decision["seed"]),
        "context_sha256": str(decision["context_sha256"]),
        "payload_sha256": agent_decide.payload_sha256(payload),
        "cash_pct": float(payload.get("cash_pct") or 0.0),
        "rationale": str(payload.get("rationale") or ""),
        "weights": agent_decide.weight_table(payload, positions, marks),
        "pool": dict(decision.get("pool") or {}),
        "total_assets": total or None,
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


def comparison_block(conn: sqlite3.Connection, asof: str) -> dict:
    """对照臂同轴表（懒 import：`comparison` 模块级 import 本模块，反向 import 会成环）。"""
    from stocklab.paper import comparison
    return comparison.build(conn, asof)


def arm_state_for(conn: sqlite3.Connection, arm: str, asof: str) -> dict | None:
    """一条臂在 `asof` 收盘后的状态（现金 / 持仓 / 市值 / 总资产 / 收盘价）。

    写决策时要用它算出「目标市值」与「现市值」的方向（`side` 的一致性判据）。
    只看 `<= asof` 的行 —— 与执行期同一个口径，因此写载荷与执行不会各算一套。
    账户不存在 → `None`（**不抛错**：调用方要能自己决定说什么）。
    """
    account = next((a for a in store.load_accounts(conn)
                    if a["account_id"] == arm), None)
    if account is None:
        return None
    state = _arm_state(conn, account, asof)
    marks = resolve_marks(conn, set(state["positions"]), asof)
    mv, _ = _mark_to_market(state["positions"], marks)
    return {
        "account_id": arm, "arm": account["arm"],
        "cash": state["cash"], "positions": dict(state["positions"]),
        "marks": marks, "market_value": mv,
        "total_assets": round(state["cash"] + mv, 4),
        "net_deposits": state["net_deposits"],
        "has_nav_on_asof": store.nav_exists(conn, arm, asof),
    }


def state_payload(conn: sqlite3.Connection, asof: str) -> dict:
    """`asof` 的载荷 —— **只由库里的行 + PIT 价格决定**，故重跑逐字节一致。"""
    idx = pit_close(conn, INDEX_300_SYMBOL, asof)
    return {
        "asof": asof,
        "accounts": [_account_entry(conn, a, asof) for a in store.load_accounts(conn)
                     if store.nav_exists(conn, a["account_id"], asof)],
        "index_300": ({"level": idx.price, "price_asof": idx.price_asof}
                      if idx else None),
        "agent": agent_block(conn, asof),
        "comparison": comparison_block(conn, asof),
        "disclosure": list(DISCLOSURE_ITEMS),
    }


# ---------- 智能体臂的报告块 ----------

NO_RANDOM_FMT = ("`{arm}` 还没有任何一条决策 ⇒ 差分**不存在**，不是 0。"
                 "拿一条没出过决策的臂当基准，算出来的差是噪音。缺它的时候"
                 "「AI 选对了」与「多试了几次」分不开（D-19）。")


def arm_target_label(arm_kind: str, etf_target_pct: float | None) -> str:
    """账户 → 「它的规矩是什么」的人话（报告与页面共用同一套措辞）。"""
    if arm_kind == ARM_KIND_HOLD:
        return "什么都不做"
    if arm_kind == ARM_KIND_NOW:
        return "实盘账本镜像"
    if arm_kind == ARM_KIND_AGENT:
        return "AI 操盘手（每交易日一条决策，台账在 paper_agent_decisions）"
    if arm_kind == ARM_KIND_AGENT_RANDOM:
        return "AI 操盘手·随机对照（同护栏同成本，标的与权重随机抽）"
    if arm_kind == ARM_KIND_DISCIPLINE and etf_target_pct is not None:
        return f"ETF 目标 {etf_target_pct:.0f}%"
    return f"{arm_kind}（口径未登记）"


def _cum_return_at(conn: sqlite3.Connection, account_id: str,
                   asof: str) -> float | None:
    row = conn.execute(
        "SELECT cum_return FROM paper_nav_daily WHERE account_id = ? AND date <= ?"
        " ORDER BY date DESC LIMIT 1", (account_id, asof)).fetchone()
    return None if row is None else float(row["cum_return"])


def agent_block(conn: sqlite3.Connection, asof: str) -> dict:
    """`arm-agent` 的报告块（`paper show` 与 `/lab/paper` **同源**，不重算）。

    `delta_vs_random` 在随机臂还没有决策时恒为 `null`，但**字段必须出现**：省略字段
    会让「还没有对照」和「两条臂一样」在 JSON 上长得一模一样。

    P52 起这一段同时要读**两段历史**：P37 的 spec 复审（`decision_kind='spec'`）与
    P52 的操盘决策（`'portfolio'`）。混在一起报 `n_reviews` 会让读者以为
    「改了 N 次纪律数字」，而那已经不发生 —— 故两个计数分开给。
    """
    spec = agent_spec.current_spec(conn, ARM_AGENT, asof)
    ledger = agent_spec.ledger_summary(conn, ARM_AGENT, asof)
    random_ledger = agent_spec.ledger_summary(conn, ARM_AGENT_RANDOM, asof)
    rows = agent_decide.load_decisions(conn, ARM_AGENT)
    portfolio = agent_decide.portfolio_decisions_only(rows)
    history = []
    for d in rows[-HISTORY_KEEP:]:
        payload = d.get("payload") or {}
        history.append({
            "decision_id": int(d["decision_id"]), "asof": str(d["asof"]),
            "agent_kind": str(d["agent_kind"]),
            "decision_kind": str(d.get("decision_kind") or ""),
            "model_id": str(d["model_id"]),
            "n_trials": int(d["n_trials"]), "n_rejected": len(d["rejected"]),
            "spec_sha256": agent_spec.spec_sha256(d["spec_after"]),
            "spec_after": d["spec_after"], "rationale": str(d["rationale"]),
            "context_sha256": str(d["context_sha256"]),
            "payload_sha256": (agent_decide.payload_sha256(payload)
                               if payload else None),
            "cash_pct": (None if not payload else float(payload.get("cash_pct") or 0.0)),
            "weights": [
                {"code": str(x["code"]), "side": str(x["side"]),
                 "target_weight_pct": float(x["target_weight_pct"]),
                 "reason": str(x["reason"])}
                for x in (payload.get("decisions") or [])],
        })
    delta, available = None, random_ledger["n_reviews"] > 0
    if available:
        mine = _cum_return_at(conn, ARM_AGENT, asof)
        theirs = _cum_return_at(conn, ARM_AGENT_RANDOM, asof)
        if mine is not None and theirs is not None:
            delta = round(mine - theirs, 6)
        else:
            available = False
    return {
        "arm": ARM_AGENT,
        "asof": asof,
        "spec": spec,
        "spec_sha256": agent_spec.spec_sha256(spec),
        "stop_loss_line": agent_spec.stop_loss_line(spec),
        "change_space": {name: f.range_text()
                         for name, f in agent_spec.SPEC_SCHEMA.items()},
        # P52：操盘决策的计数（这才是决定下不下单的那个数）。
        "n_decisions": len(portfolio),
        "n_spec_versions": len(rows) - len(portfolio),
        "last_decision_asof": (str(portfolio[-1]["asof"]) if portfolio else None),
        "last_decision": (None if not portfolio else {
            "asof": str(portfolio[-1]["asof"]),
            "model_id": str(portfolio[-1]["model_id"]),
            "seed": int(portfolio[-1]["seed"]),
            "payload_sha256": agent_decide.payload_sha256(
                portfolio[-1].get("payload") or {}),
            "cash_pct": float((portfolio[-1].get("payload") or {}).get("cash_pct") or 0.0),
        }),
        "n_reviews": ledger["n_reviews"],
        "n_trials_total": ledger["n_trials_total"],
        "n_rejected": ledger["n_rejected"],
        "max_trials_per_review": ledger["max_trials_per_review"],
        "rebalance_cadence": ledger["cadence"],
        "first_asof": ledger["first_asof"],
        "last_asof": ledger["last_asof"],
        "history": history,
        "delta_vs_random": delta,
        "delta_vs_random_available": available,
        "delta_vs_random_note": (
            None if available else NO_RANDOM_FMT.format(arm=ARM_AGENT_RANDOM)),
        "counter_arm": {"arm": ARM_AGENT_RANDOM,
                        "n_reviews": random_ledger["n_reviews"],
                        "wired": available},
        "evidence_note": ("n_trials_total 与 n_rejected 必须与净值同时读："
                          "试了很多版选最好那版，读数是**上界**不是期望"),
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
        "comparison": comparison_block(conn, asof),
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
    L.append("## 一、各臂净值（并列，不挑「推荐」）")
    L.append("")
    L.append("| 账户 | 口径 | 净值 | 累计收益 | 最大回撤 | 累计成本 | ETF 实际占比 |")
    L.append("|---|---|---|---|---|---|---|")
    for a in rep["accounts"]:
        target = arm_target_label(a["arm"], a["etf_target_pct"])
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
    L.append("## 三、对照臂同轴（5 条 + 随机臂；缺数据的写「不可比」，不填 0）")
    L.append("")
    cmp = rep.get("comparison") or {}
    if not cmp:
        L.append("- 对照块取不到（旧库）—— 不编数。")
    else:
        L.append(f"- 样本：{cmp['n_sessions']} 个交易日，门槛 "
                 f"{cmp['sample_gate']['threshold']} 日 → "
                 f"**{cmp['sample_gate']['status']}**"
                 f"{'' if cmp['sample_gate']['note'] is None else '（' + cmp['sample_gate']['note'] + '）'}")
        L.append("")
        L.append("| 臂 | 口径 | 累计收益 | 可交易 | 成本 | 备注 |")
        L.append("|---|---|---|---|---|---|")
        for a in cmp["arms"]:
            ret = (f"{a['latest'] * 100:+.2f}%" if a["latest"] is not None
                   else f"**{NOT_COMPARABLE}**")
            L.append(f"| {a['label']} | {a['kind']} | {ret} | "
                     f"{'是' if a['tradable'] else '否'} | "
                     f"{'有' if a['has_cost'] else '无'} | "
                     f"{a['note'] or ''} |")
        L.append("")
        d = cmp["delta_vs_random"]
        if cmp["delta_vs_random_available"] and d is not None:
            L.append(f"- Δ(AI 操盘手 − 随机对照) = {d * 100:+.2f}%"
                     f"（n_decision={cmp['n_decisions_agent']} / "
                     f"n_random={cmp['n_decisions_random']}）—— **这是归因用的差分**，"
                     f"没有它，AI 的领先分不清是选对了还是碰巧。")
        else:
            L.append(f"- Δ(AI 操盘手 − 随机对照) = **{NOT_COMPARABLE}**，"
                     f"不是 0：{cmp['delta_vs_random_note']}")
        L.append(f"- {cmp['not_comparable_note']}")
    L.append("")
    L.append("## 四、持仓与纪律判定")
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
    L.append("## 五、起跑日至今的调仓流水（append-only）")
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
    L.append("## 六、口径说明（不许改）")
    L.append("")
    for item in rep["disclosure"]:
        L.append(f"- {item}")
    L.append("")
    return "\n".join(L)
