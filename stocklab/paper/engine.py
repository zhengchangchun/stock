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
import re
import sqlite3
from dataclasses import replace
from statistics import median
from typing import Mapping, Sequence

from stocklab.calendar.trading_calendar import Calendar
from stocklab.config.costs import ASSET_ETF, ASSET_STOCK, CostModel
from stocklab.paper import agent_decide, agent_spec, store
from stocklab.paper.config import (
    AGENT_ARM_PREFIX,
    AGENT_CAPITAL_TOPUP_AMOUNT,
    AGENT_CAPITAL_TOPUP_DATE,
    AGENT_CAPITAL_TOPUP_NOTE,
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
    ARM_VERSION_RE,
    DISCLAIMER,
    DISCLOSURE_ITEMS,
    DISCIPLINE_PREFIX,
    ETF_TRANCHES,
    ETF_WHITELIST,
    EXECUTOR_AGENT_DECISION,
    EXECUTOR_KEY,
    HOLD_CODE,
    HOLD_QTY,
    INITIAL_CAPITAL,
    KNOWN_EXECUTORS,
    LIVE_KEY,
    LOT,
    NOT_COMPARABLE,
    PAPER_START_DATE,
    PER_STEP_CASH_PCT,
    PREREGISTERED_KEY,
    PREREGISTRATION_KEYS,
    RULE_CITATIONS,
    agent_capital_topup_idem,
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
    one_lot_cost,
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


class UnknownExecutorError(PaperError):
    """`params.executor` 是一个**没有对应执行者**的值（P56 §1.7）。

    认领关系 fail-closed：既不是通路 A 的 `m2_channel_a`，也不是 AI 操盘手的
    `agent_decision` ⇒ 点名报错。静默跳过 = 那条策略悄悄不下单，
    而症状只会在净值表上出现。
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


# ---------- 资本事件（P80 / D2–D4） ----------

def capital_events_sum(conn: sqlite3.Connection, account_id: str,
                       asof: str | None = None) -> float:
    """该账户 `date <= asof` 的**带符号**资本事件合计（deposit 正 / withdraw 负）。

    符号**只在这里解释一次**：`paper_capital_events.amount` 恒正、方向由 `kind` 表达，
    于是「加还是减」这个问题在代码里只有一个答案（两处各推一次，迟早有一处反了，
    而且反了的症状是净值少/多 3 万，看起来像「算错了」而不是「符号错了」）。
    """
    rows = store.load_capital_events(conn, account_id, asof=asof)
    return round(sum(float(r["amount"]) if r["kind"] == "deposit"
                     else -float(r["amount"]) for r in rows), 4)


def net_deposits_at(conn: sqlite3.Connection, account: dict, asof: str) -> float:
    """`net_deposits` 的**唯一**实现：起跑本金 ＋ Σ(带符号事件 ≤ asof)。

    起跑本金取 `params_json.initial_capital`（不是 `INITIAL_CAPITAL` 常量）：
    老账户的账户行里冻结着它当时的口径，常量只保证**新账户**从同一个数起跑。
    `arm-now`（实盘镜像）不走这里 —— 它的净入金来自 `cash_flows`（`ledger_state`）。
    """
    base = float(json.loads(account["params_json"])["initial_capital"])
    return round(base + capital_events_sum(conn, str(account["account_id"]), asof), 4)


def agent_capital_events_for(account_id: str) -> list[tuple[str, str, float, str, str]]:
    """AI 家族账户要自动挂上的资本事件：`[(date, kind, amount, note, idem)]`。

    **一处定义**（P80 / D3）：三条建账路径（`init_accounts` 的 agent 分支 /
    `enroll_agent_arm` / 通路 A 的 `m2.channel_a.create_account`）都调它，
    不许各写一遍 —— 各写一遍的下场是某天只改了其中一处，于是同一天的
    `paper init` 与 `enroll` 造出两个不同的本金口径。

    非 AI 家族（账户 id 不以 `arm-agent` 开头）⇒ `[]`：静态臂 / `arm-now`
    的本金口径**一字不动**（D1：它们永远是 20,000）。
    """
    aid = str(account_id)
    if not aid.startswith(ARM_AGENT):
        return []
    return [(AGENT_CAPITAL_TOPUP_DATE, "deposit", float(AGENT_CAPITAL_TOPUP_AMOUNT),
             AGENT_CAPITAL_TOPUP_NOTE, agent_capital_topup_idem(aid))]


def attach_agent_capital_events(conn: sqlite3.Connection, account_id: str, *,
                                now: str) -> list[str]:
    """把 `agent_capital_events_for` 的事件**幂等**写进库；返回本次真写入的 `idem`。

    三条建账路径调本函数（而不是各自展开那个循环）：事件的定义在
    `agent_capital_events_for`，写入动作在这里，于是「有哪些事件」与「怎么写进去」
    各只有一个地方。重跑（账户已存在 + 事件已挂）⇒ `[]`、库一行不动。
    """
    written = []
    for date, kind, amount, note, idem in agent_capital_events_for(account_id):
        if store.insert_capital_event(conn, account_id=account_id, date=date,
                                      kind=kind, amount=amount, note=note,
                                      idem=idem, now=now):
            written.append(idem)
    return written


def max_lots_affordable(cash: float, one_lot_cost_value: float) -> int:
    """`floor(现金 / 一手含费成本)` —— 「这笔钱最多能买几手」。

    **唯一**实现：`paper account` 的 `buying_power` 与 AI 上下文里的
    `tradability.affordable_lots` 都调它（T3 的判据就是这两处同源同值）。

    `+1e-9` 是浮点写法容差，不是「允许差一点」的额度：`cash / cost` 恰好是整数时
    （如现金 3,000 / 一手 1,000）二进制浮点可能给出 2.9999999996，
    截断后会少一整手 —— 那是**量级错**，不是舍入误差。
    """
    cost = float(one_lot_cost_value)
    if cost <= 0:
        return 0
    return int(float(cash) / cost + 1e-9)


# ---------- 臂的分派 ----------

def missing_account_error(arm: str) -> PaperError:
    """「账户不存在」的**唯一**措辞（P81 / D2：新读出口沿用 `paper account` 的口径）。

    账户名拼错与「还没建」都点到这里，**不静默返回空** —— 空列表与不存在的账户
    在 JSON 上长得一样，而它们不是一回事。
    """
    return PaperError(
        f"账户 {arm} 不存在；先 `paper init`（或 `paper agent enroll`）—— "
        f"账户名拼错与「还没建」都点到这里，不静默返回空")


def min_weight_pct(one_lot_cost_value: float, total_assets: float) -> float | None:
    """`一手含费成本 / 总资产 × 100`（4 位小数）—— 这条占比公式的**唯一**实现。

    AI 输入侧的 `tradability.min_weight_pct`（P79/P80）与可下手域读数
    （`trading_domain.min_tradable_weight_pct`，P81/D4）都是它。
    `total_assets ≤ 0` ⇒ `None`：算不出占比时**不猜一个数**。
    """
    total = float(total_assets)
    if total <= 0:
        return None
    return round(float(one_lot_cost_value) / total * 100.0, 4)


def tradable_verdict(*, affordable_lots: int, one_lot_cost_value: float,
                     total_assets: float, cash: float) -> tuple[bool, str | None]:
    """「这只标的**今天**下不下得了手」—— 判据的**唯一**实现（P81 / D1 ＋ D4）。

    ## 为什么必须只有一处

    同一个结论要在两个面上出现：AI 的输入侧（`agent_context._tradability_block`
    的 `tradable`）与账户读数的购买力（`account_view.buying_power.by_code`）。
    两处各写一遍「买不买得起」，迟早会有一处口径漂移 —— 那就是 D-37 那类
    「同一页两个数」的事故形状（ERROR_DIARY #70）。

    ## 判据只有一条

    `tradable = (affordable_lots >= 1)`，而 `affordable_lots` 由
    `max_lots_affordable`（唯一的整手数公式）算出。**不另写**「买不买得起」的算法。

    ## 这是「现金口径的今天」，不是「这标的不许交易」

    `tradable=False` 只说明**现在**下不了手，不预测「卖掉别的标的是否能凑够」，
    也**不许**被任何写入口当成硬拒绝（P79 / D2 的条款：写入口不因可成交性拒载荷）。
    理由文本把两个数都点名，正是为了让读者能自己判断该不该先卖出别的标的。

    成因分两种（两种**分开**说，因为处置不同：一种是无解，一种是先卖再买）：
    - 一手成本 > 总资产 ⇒ 「已超过总资产」（这个账户再怎么样也买不起）；
    - 否则（总资产够、现金不够）⇒ 「现金不足…（先卖出其他标的可释放现金）」。
    """
    if int(affordable_lots) >= 1:
        return True, None
    cost = float(one_lot_cost_value)
    total = float(total_assets)
    if cost > total:
        return False, f"一手 ¥{cost:.2f} 已超过总资产 ¥{total:.2f}"
    return False, (f"现金 ¥{float(cash):.2f} 不足一手 ¥{cost:.2f}"
                   f"（先卖出其他标的可释放现金）")


def trading_domain(*, by_code: Mapping[str, Mapping[str, object]],
                   n_pool_codes: int, total_assets: float) -> dict:
    """**可下手域**的读数面（P81 / D4）：池内一手成本分布 ＋ 可下手只数。

    ## 为什么它是一块独立的读数

    「小面额也能下手」这件事此前没有任何可核对的数：池内一手成本分布、可下手
    只数、最小可成交权重全要手工算。`min_tradable_weight_pct` 是「用最少的钱
    铺开最多只」的那把尺子 —— 低于它的目标权重买不到任何东西。

    ## 口径（**不重算**任何判据）

    数据源是 `account_view.buying_power.by_code` 的成品行：
    `tradable` / `one_lot_cost` 都是那边已经算好的，本函数只做汇总
    （计数、最小值、中位数），不重新判一次「买不买得起」。

    - `one_lot_cost_p50` / `one_lot_cost_min`：**池内可定价**（`by_code` 全部行）
      的一手成本中位数与最小值 —— 「池内成本分布」描述的是这一池，不是可下手子集；
    - `cheapest_code`：**可下手子集**里一手最便宜的那只（与 D1 的
      `tradability.cheapest` 同一个含义：真能下手的里面最便宜的）；
    - `untradable`：每只买不起的 code ＋ 一手成本 ＋ 理由原文（页面/报告据此
      **点名**，而不是只报一个数）；
    - 两边都为空 ⇒ 相关键一律 `None`（**不猜数**）。
    """
    rows = {str(c): r for c, r in by_code.items()}
    costs = sorted(float(r["one_lot_cost"]) for r in rows.values())
    tradable = sorted(c for c, r in rows.items() if r.get("tradable"))
    untradable = [{"code": c, "one_lot_cost": float(rows[c]["one_lot_cost"]),
                   "reason": rows[c].get("untradable_reason")}
                  for c in sorted(c for c, r in rows.items() if not r.get("tradable"))]
    cheapest = None
    if tradable:
        code = min(tradable, key=lambda c: (float(rows[c]["one_lot_cost"]), c))
        cheapest = {"code": code, "one_lot_cost": float(rows[code]["one_lot_cost"])}
    mins = [p for c in tradable
            if (p := min_weight_pct(float(rows[c]["one_lot_cost"]), total_assets))
            is not None]
    return {
        "n_pool_codes": int(n_pool_codes),
        "n_priced": len(rows),
        "n_tradable": len(tradable),
        "n_untradable": len(rows) - len(tradable),
        "min_tradable_weight_pct": (min(mins) if mins else None),
        "one_lot_cost_p50": (median(costs) if costs else None),
        "one_lot_cost_min": (costs[0] if costs else None),
        "cheapest_code": (None if cheapest is None else cheapest["code"]),
        "cheapest": cheapest,
        "untradable": untradable,
    }


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

    ## 资本事件（P80 / D4）

    `date <= asof` 的 `deposit` 加现金、`withdraw` 减现金；`net_deposits` 同步
    变成「起跑本金 ＋ Σ(带符号金额 ≤ asof)」。两条一起动是**必须**的：只有现金动
    会让「净入金」少 3 万（于是 `cum_return` 凭空多出 150%），只有净入金动会让
    账户多出 3 万却买不起东西 —— 两种都是假账。

    **`cum_cost` 不因入金变化**：入金不是成本（与 P19 对入场费的处置同一条理由 ——
    把钱转进账户不产生佣金）。这一条是刻意的，`test_p80_cum_cost_ignores_topup` 钉住。

    `arm-now`（`ledger_state`，读实盘账本）与 `arm-hold`（冻结快照）**不受影响**：
    前者根本不走这条路径，后者没有事件（`attach_agent_capital_events` 只挂 AI 家族）。
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
    # 入金/出金：只动现金与净入金，不动成本（理由见 docstring）。
    cash += capital_events_sum(conn, str(account["account_id"]), asof)
    # net_deposits 是**本金**（起跑 20,000 ＋ 入金/出金）。入场费 5.09 是**成本**
    # 不是入金 —— 把它算进分母会让「累计收益」凭空好看一点点，而那正是最不该有的偏差。
    return {"cash": round(cash, 4), "positions": positions,
            "cum_cost": round(cum_cost, 4),
            "net_deposits": net_deposits_at(conn, account, asof)}


def mark_to_market(positions: dict[str, int],
                   marks: dict[str, Price]) -> tuple[float, list[dict]]:
    """持仓市值。**这是净值口径的唯一实现** —— 模块2 的通路（`m2/`）也调它。

    公开而不是下划线私有：通路 A/B 已经要用同一口径算净值（D-25/D-30 的
    「一切复用 paper 引擎」），而「从别的包调私有函数」这种耦合没有测试能拦住 ——
    与其让下一条通路偷偷抄一份，不如把入口正名。
    取不到价 → `MissingPriceError`（**不用成本价冒充现价**：那会把浮亏恒显示成 0）。
    """
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


def drawdown(history: list[float], nav: float) -> float:
    """相对**历史峰值**的回撤（正数 = 回撤幅度）。首行峰值即自身 → 0。

    与 `mark_to_market` 同理公开：回撤是净值的一部分口径，不许有第二份实现。
    """
    peak = max([*history, nav]) if history else nav
    if peak <= 0:
        return 0.0
    return round((peak - nav) / peak, 6)


def executor_kind(account: dict) -> str | None:
    """这条账户声明的执行者（`params.executor`）——**未知值点名报错**（P56 §1.7）。

    「谁落这一天的净值」是一个**显式字段**，不靠账户名前缀猜：前缀猜法会让
    `arm-agent-v1` 与 `arm-agent` 这种只差后缀的命名各走一条路，
    而改名字的那天没人会记得同步这条判据。

    fail-closed 的理由：出现一个**没有对应执行者**的值时，让出它等于那条策略
    悄悄不下单（`paper step` 跳过、又没人接），而症状只会在净值表上出现 ——
    排查方向会跑到订单生成上去。所以这里直接报错，并**点名**账户与那个值。
    """
    value = json.loads(account["params_json"]).get(EXECUTOR_KEY) or None
    if value is not None and value not in KNOWN_EXECUTORS:
        raise UnknownExecutorError(
            f"账户 {account['account_id']} 的 params.{EXECUTOR_KEY} = {value!r} "
            f"没有对应的执行者（已知：{list(KNOWN_EXECUTORS)}）—— "
            f"`paper step` 让出它、又没人认领它，这条账户就永远不会记净值。"
            f"改回已知值，或先实现那个执行者")
    return value


def external_executor(account: dict) -> str | None:
    """这条账户的日终净值由**外部通路**认领吗？（非空 = 是，返回执行者名）

    P47 / D-35：模块2 通路 A 的账户（`arm-agent-<策略版本>`）由 A1/A2/A3 插桩驱动，
    它**不进** `paper step` 的遍历 —— 否则 `paper step` 会先给它写一条「没有成交的
    净值」，而 `step` 的幂等判据正是净值行的存在性，于是那条策略**永远不下单**。

    P56 / D-50 把同一个字段用到 AI 操盘手家族上：值 = `agent_decision` ⇒ 日终由
    `paper agent run --asof` 落（**让出逻辑本身不变**，只是写这个键的地方多了一处）。
    """
    return executor_kind(account)


# ---------- P69：在飞 / 停飞（T2） ----------

def live_of(params: Mapping) -> bool:
    """`params.live` → 这条臂还在飞吗。**缺省 `True`**（老账户不加键也照跑）。

    非布尔值**点名报错**（fail-closed）：写成字符串 `"false"` 是真值 ⇒ 「停飞」会被
    读成「在飞」，而症状正好是这条字段要消掉的那个假警的背面 —— **漏报**。
    与 `executor_kind` 同一个理由：一个「看得出来是坏的」值不该被静默按默认值处理。
    """
    value = params.get(LIVE_KEY)
    if value is None:
        return True
    if not isinstance(value, bool):
        raise PaperError(
            f"params.{LIVE_KEY} 必须是布尔（现在 {value!r}，类型 "
            f"{type(value).__name__}）—— 写错会让「停飞」被读成「在飞」，"
            f"这条臂就会继续每天报一条**假警**；反过来漏报更糟。"
            f"要停飞写 `false`（JSON 布尔），在飞写 `true` 或干脆不写")
    return value


def is_live(account: Mapping) -> bool:
    """账户行（`paper_accounts` 的一行）→ 还在飞吗。见 `live_of`。"""
    return live_of(json.loads(account["params_json"]) or {})


# ---------- P56：预注册（D-48） ----------

def preregistration(account: dict) -> dict | None:
    """账户行里那版预注册的 `(model_id, prompt_sha256)`；没有 → `None`。

    **没有预注册不是「随便什么模型都行」**：调用方（写入口）据此拒写。见
    `require_preregistration`。
    """
    raw = json.loads(account["params_json"]).get(PREREGISTERED_KEY)
    if not isinstance(raw, Mapping):
        return None
    got = {k: str(raw.get(k) or "") for k in PREREGISTRATION_KEYS}
    return got if all(got.values()) else None


def require_preregistration(account: dict, *, model_id: str,
                            prompt_sha256: str) -> dict:
    """这条账户的预注册与本次调用**逐字段相同**吗？不同就拒（D-48）。

    三个拒绝理由都点名「字段 / 值 / 为什么」，且**不 warn、不静默改写**：
    - 没有预注册：这个账户没声明它是哪一版 ⇒ 先 `paper agent enroll` 开新版本账户；
    - 对不上：**换模型 / 换提示词 = 开新版本账户**，旧账户保留不删。
    """
    want = {k: str(v or "") for k, v in
            zip(PREREGISTRATION_KEYS, (model_id, prompt_sha256))}
    got = preregistration(account)
    if got is None:
        raise agent_decide.DecisionPayloadError(
            PREREGISTERED_KEY, None,
            f"账户 {account['account_id']} 没有预注册（`{PREREGISTERED_KEY}`）—— "
            f"它没声明自己是哪一版模型/提示词，本入口一律拒写（不 warn、不静默采用）。"
            f"要接模型请开新版本账户：`paper agent enroll --arm-name "
            f"{AGENT_ARM_PREFIX}<版本> --model-id <m> --prompt-sha256 <s>`")
    diff = [k for k in PREREGISTRATION_KEYS if got[k] != want[k]]
    if diff:
        raise agent_decide.DecisionPayloadError(
            diff[0], want[diff[0]],
            f"与账户 {account['account_id']} 的预注册不符"
            f"（预注册 {got[diff[0]]!r}，本次 {want[diff[0]]!r}）—— "
            f"**换模型 / 换提示词 = 开新版本账户**，旧账户保留不删。"
            f"这条纪律挡的是「换到好看为止」")
    return got


# ---------- P56：交易日与「这一格该有决策吗」 ----------

def is_trading_day(conn: sqlite3.Connection, d: str) -> dict:
    """`d` 是不是交易日 —— 真源是**交易日历**（`Calendar`，由指数日线生成）。

    日历**没覆盖** `d` 时返回 `is_trading_day: None` + 原因，**不猜**（沿用
    `patrol.session_day` 的「判不了就明说」）：把它读成 False 会把「不知道」
    显示成「休市」，进而把缺决策的异常吞掉。
    """
    try:
        cal = Calendar.load(conn)
    except ValueError as exc:
        return {"is_trading_day": None, "why": "calendar_not_covered",
                "error": str(exc)}
    dates = cal.all_dates
    if not dates or not (dates[0] <= d <= dates[-1]):
        return {"is_trading_day": None, "why": "calendar_not_covered",
                "error": f"日历区间 [{dates[0] if dates else None}, "
                         f"{dates[-1] if dates else None}] 不含 {d}"}
    return {"is_trading_day": cal.is_open(d),
            "why": "trading_calendar" if cal.is_open(d) else "trading_calendar_closed"}


def decision_expectation(conn: sqlite3.Connection, *, account: dict,
                         asof: str) -> dict:
    """「`(这条臂, 这一天)` 本该有决策吗」——三字段，`show` 与 `run` **共用**。

    - `is_trading_day`：交易日历说了算（`None` = 判不了）；
    - `live`：这条臂**还在飞吗**（P69 / T2；`params.live`，缺省 `true`）；
    - `account_in_flight`：这条账户在 `asof` 时**已经在飞**（不早于起跑日）——
      还没起跑的账户不该有决策，那不是缺。**停飞臂恒为 `False`**：它不该有决策；
    - `expected`：两者都成立 ⇒ 缺决策就是**异常**（不是「这天不用决策」）。
      停飞臂的 `expected` 恒 `False` ⇒ 页面/回执都**不把它读成缺决策**。
    """
    day = is_trading_day(conn, asof)
    live = is_live(account)
    in_flight = live and asof >= str(account["start_date"])
    return {"asof": asof, **day, "live": live, "account_in_flight": in_flight,
            "expected": bool(day["is_trading_day"]) and in_flight}



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


def _initial_state(conn: sqlite3.Connection, start_date: str) -> dict:
    """起跑日的现金 / 持仓 / 净值（`init_accounts` 与 `enroll_agent_arm` **共用**）。

    AI 操盘手的版本账户（D-48）必须与其余各臂**同起点同本金**，否则「对照」这件事
    在第一个数上就不成立。两条路各算一遍迟早分叉，所以只有这一份实现。
    """
    led = ledger_state(conn, start_date)
    _declared_seed(led, start_date)
    positions = _position_snapshot({"initial_positions_json": json.dumps(
        [{"code": c, "qty": q} for c, q in led["positions"].items()])})
    marks = resolve_marks(conn, positions, start_date)
    if set(marks) != set(positions):
        raise MissingPriceError(f"起跑日 {start_date} 缺少持仓收盘价："
                                f"{sorted(set(positions) - set(marks))}")
    return {"cash": led["cash"], "positions": positions,
            "seed_fee": led["cum_cost"],
            "initial_nav": round(sum(p.price * positions[c] for c, p in marks.items())
                                 + led["cash"], 4)}


def _agent_params(params: dict) -> dict:
    """AI 操盘手家族的账户参数：**认领声明** `executor='agent_decision'`（D-50）。

    写在这里（而不是只靠 P56 的迁移补）是为了让**新库直接是对的** ——
    迁移只负责把老库前滚，它不该是「这个键从哪来」的唯一答案。
    """
    return {**params, EXECUTOR_KEY: EXECUTOR_AGENT_DECISION,
            "decision_source": agent_decide.TABLE_DECISIONS,
            "decision_kind": "portfolio"}


def init_accounts(conn: sqlite3.Connection, *, start_date: str = PAPER_START_DATE,
                  now: str) -> dict:
    """建臂（纪律臂按 `ETF_TRANCHES` 展开 3 档 + 智能体臂与它的随机对照）。

    幂等：已存在则不改写。

    `arm-agent` / `arm-agent-random` 的 `etf_target_pct` 列写 `None` —— 它们的 ETF
    目标来自**台账里的当前 spec**，不来自账户行。把当时的 spec 抄进这一列，
    等于在库里存下第二个真相，而它不会随 spec 变。
    """
    state = _initial_state(conn, start_date)
    params = {"seed_fee": state["seed_fee"], "lot": LOT,
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
               {**_agent_params(params),
                "wired_from": "P52：每交易日一条操盘决策（写入口 paper agent decide）"}),
              (ARM_AGENT_RANDOM, ARM_KIND_AGENT_RANDOM, None,
               {**_agent_params(params),
                "counter_arm": ARM_AGENT,
                "wired_from": "P52：随机抽标的与权重，同护栏同成本（归因必需）"})]
    created = []
    for account_id, arm, target, acct_params in specs:
        if store.account_exists(conn, account_id):
            continue
        store.insert_account(
            conn, account_id=account_id, arm=arm, etf_target_pct=target,
            start_date=start_date, initial_cash=state["cash"],
            initial_positions=[{"code": c, "qty": q}
                               for c, q in sorted(state["positions"].items())],
            initial_nav=state["initial_nav"], params=acct_params, now=now)
        created.append(account_id)
    # P80 / D3 路径①：AI 家族账户自动挂标准入金事件（幂等）。
    # **不在 `if not exists` 分支里**：老库重跑 `paper init` 也要能把缺的事件补上
    # （三条路径共用同一个 helper，谁先跑到谁把它挂上）。
    for account_id, *_ in specs:
        attach_agent_capital_events(conn, account_id, now=now)
    return {"created": bool(created), "accounts": created, "start_date": start_date,
            "initial_nav": state["initial_nav"], "initial_cash": state["cash"],
            "initial_positions": state["positions"]}


# ---------- P56：预注册账户（D-48） ----------

def validate_arm_name(arm_name: str) -> str:
    """`--arm-name` 必须是 `arm-agent-<版本>` 且版本形状合法 —— 否则**拒绝**。

    拒绝清单（T2）：`arm-agent`（无版本）、`arm-agent-random`（**对照臂的名字，
    不能拿它注册模型**：那条臂的产出者标识由 `RANDOM_MODEL_ID` 固定）、
    以及大写 / 空格 / 超长 / 空版本。**不 sanitize** —— 悄悄替换字符会让
    「报告里的版本」与「台账里的版本」变成两个东西。
    """
    name = str(arm_name or "")
    reserved = (ARM_AGENT, ARM_AGENT_RANDOM)
    if name in reserved:
        raise agent_decide.DecisionPayloadError(
            "arm_name", name,
            f"{name!r} 是**内置账户名**，不能拿来注册新版本：`{ARM_AGENT}` 是 P52 的"
            f"默认臂、`{ARM_AGENT_RANDOM}` 是随机对照臂（产出者标识固定）。"
            f"开新版本请用 `{AGENT_ARM_PREFIX}<版本>`，如 `{AGENT_ARM_PREFIX}ds-v1`")
    if not name.startswith(AGENT_ARM_PREFIX):
        raise agent_decide.DecisionPayloadError(
            "arm_name", name,
            f"必须以 {AGENT_ARM_PREFIX!r} 开头（D-48：`{AGENT_ARM_PREFIX}<版本>`）")
    version = name[len(AGENT_ARM_PREFIX):]
    if not ARM_VERSION_RE.match(version):
        raise agent_decide.DecisionPayloadError(
            "arm_name", name,
            f"版本号 {version!r} 形状不合法（小写字母数字开头，只允许 `. _ -`，"
            f"总长 ≤ 32，不许空）—— 它会进账户 id 与台账，必须可读、可比较")
    return name


def enroll_agent_arm(conn: sqlite3.Connection, *, arm_name: str, model_id: str,
                     prompt_sha256: str, now: str,
                     start_date: str = PAPER_START_DATE) -> dict:
    """建一个**预注册过的** AI 操盘手版本账户（D-48）：`(model_id, prompt_sha256)`
    写进 `params_json`，此后 `paper agent decide` 的这两个参数**不匹配即拒**。

    幂等：账户已存在且预注册**逐字段相同** ⇒ `created: false`、行一字不改；
    预注册不同 ⇒ **拒**（换模型 / 换提示词 = 开新版本账户，旧账户保留不删）。

    起跑状态（现金 / 持仓 / 净值）与其余各臂**同一份实现**（`_initial_state`）——
    换版本不该换起跑口径，否则对照从第一个数起就不成立。
    """
    name = validate_arm_name(arm_name)
    for field, value in (("model_id", model_id), ("prompt_sha256", prompt_sha256)):
        if not str(value or "").strip():
            raise agent_decide.DecisionPayloadError(
                field, value, "不许留空（换模型/换提示词 = 换口径，必须留痕）")
    if not re.fullmatch(r"[0-9a-f]{64}", str(prompt_sha256)):
        raise agent_decide.DecisionPayloadError(
            "prompt_sha256", prompt_sha256,
            "必须是 64 位小写十六进制（用 `paper agent sha256 --file <提示词文件>` 算，"
            "不要手算 —— 各算各的迟早对不上）")

    want = {"model_id": str(model_id), "prompt_sha256": str(prompt_sha256)}
    existing = next((a for a in store.load_accounts(conn)
                     if a["account_id"] == name), None)
    if existing is not None:
        got = preregistration(existing)
        if got is None:
            raise UnknownExecutorError(
                f"账户 {name} 已存在但没有预注册（`{PREREGISTERED_KEY}`）—— "
                f"enroll **只建新版本**，不就地改写旧账户的预注册；"
                f"要换口径请开新版本名")
        if got != want:
            raise agent_decide.DecisionPayloadError(
                PREREGISTERED_KEY, got["model_id"],
                f"账户 {name} 已预注册 {got['model_id']!r}，本次是 "
                f"{want['model_id']!r} —— 换模型 = 开新版本账户（旧账户保留）")
        # P80 / D3 路径②：老版本账户也把入金事件补上（幂等；账户行仍一字未改）。
        attach_agent_capital_events(conn, name, now=now)
        return {"created": False, "account_id": name, **got,
                "start_date": str(existing["start_date"]), "initial_nav": None,
                "note": "账户已存在且预注册一致：**一行都没改**（幂等）"}

    state = _initial_state(conn, start_date)
    params = {"seed_fee": state["seed_fee"], "lot": LOT,
              "per_step_cash_pct": PER_STEP_CASH_PCT,
              "etf_whitelist": list(ETF_WHITELIST),
              "start_date": start_date, "initial_capital": INITIAL_CAPITAL}
    store.insert_account(
        conn, account_id=name, arm=ARM_KIND_AGENT, etf_target_pct=None,
        start_date=start_date, initial_cash=state["cash"],
        initial_positions=[{"code": c, "qty": q}
                           for c, q in sorted(state["positions"].items())],
        initial_nav=state["initial_nav"],
        params={**_agent_params(params), PREREGISTERED_KEY: want,
                "wired_from": "P56 / D-48：AI 操盘手版本账户（预注册模型与提示词指纹）"},
        now=now)
    # P80 / D3 路径②：新版本账户同样自动挂标准入金事件（幂等）。
    attach_agent_capital_events(conn, name, now=now)
    return {"created": True, "account_id": name, **want,
            "start_date": start_date, "initial_nav": state["initial_nav"],
            "note": ("新版本账户：模型/提示词指纹已预注册，decision 时逐字段比对；"
                     "**没有存任何凭据**（项目内零 API key）")}


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


def step_account(conn: sqlite3.Connection, account_id: str, asof: str, *,
                 now: str, prices: dict[str, Price] | None = None) -> dict:
    """只推进**一个**账户的一天（模块2 通路 B 的镜像用，P47）。

    与 `step` 共用 `_step_all` 的**同一段实现** —— D-25 明令镜像「不新建第二套
    镜像代码」：通路 B 的净值必须与 `arm-now` 逐字段相同，所以它只能是同一个函数，
    不能是「照着它写的那一份」。

    只允许推进 `arm-now`（实盘账本镜像）与「自身成交驱动」的臂：拿这个入口去推
    `arm-hold` 或 `arm-discipline-*` 等于绕过 `step` 的整批语义，
    而调用方多半是想「只跑这一条」—— 那属于另一条口径，应当显式写出来。

    幂等仍由 `_step_all` 的 `nav_exists` 判据承担；返回值里的 `wrote_nav` 让调用方
    能分清「这次真写了」与「早就有了」（两者都不能靠猜）。
    """
    account = next((a for a in store.load_accounts(conn)
                    if a["account_id"] == account_id), None)
    if account is None:
        raise PaperError(f"账户 {account_id} 不存在；先跑 `paper init`")
    if account["arm"] != ARM_KIND_NOW and not replays_own_trades(account):
        raise PaperError(
            f"账户 {account_id} 的臂是 {account['arm']!r} —— `step_account` 只推进"
            f"实盘账本镜像与自身成交驱动的臂（{ARM_KIND_NOW} / "
            f"{list(ARM_KINDS_SELF_DRIVEN)}）")
    if asof < account["start_date"]:
        raise PaperError(f"asof {asof} 早于 {account_id} 的起跑日 "
                         f"{account['start_date']} —— 起跑日之前不记净值")
    if prices is not None:
        check_no_lookahead(asof, prices)
    existed = store.nav_exists(conn, account_id, asof)
    with transaction(conn):
        _step_all(conn, asof, accounts=[account], now=now, prices=prices)
    return {"account_id": account_id, "asof": asof,
            "wrote_nav": (not existed) and store.nav_exists(conn, account_id, asof),
            "nav": (store.latest_nav(conn, account_id, asof=asof) or {}).get("nav")}


def _step_all(conn: sqlite3.Connection, asof: str, *, accounts: list[dict],
              now: str, prices: dict[str, Price] | None,
              claim_handover: bool = True) -> None:
    for account in accounts:
        # 外部通路（模块2 通路 A）认领的账户：它的日终由那条通路自己落，
        # 本引擎**让出**这一格。理由见 `external_executor`——不让出会让那条
        # 策略永远不下单，而症状出现在净值表上，排查方向会跑到订单生成上去。
        # 跳过是**显式**的（账户行里写着 `executor`），所以 `paper show` 里
        # 仍然能看到这条账户（它有净值行），不是「悄悄消失」。
        #
        # `claim_handover=False` 只有**被指定的那个执行者**会传（`paper agent run`
        # 就是 `agent_decision` 的执行者）：让出是对 `paper step` 说的，
        # 不是对这个执行者说的。让出逻辑本身一字未改。
        if claim_handover and external_executor(account):
            continue
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
        mv, _ = mark_to_market(positions, marks)
        total = round(cash + mv, 4)
        orders: list[Decision] = []
        evals: list[Decision] = []
        if decision is not None and decision_codes:
            # 目标市值按**执行时**的总资产重算：写载荷时与执行时看的是同一批
            # `<= asof` 的行，所以两者同口径；重算是为了不把「报告里的权重」
            # 变成写载荷那一刻的快照（那会让 ¥ 与 % 对不上）。
            payload = agent_decide.rebase_payload(payload, total_assets=total,
                                                  marks=marks, positions=positions)
            try:
                cash, positions, orders, evals = agent_decide.execute_decision(
                    conn, arm=account["account_id"], asof=asof, decision=payload,
                    cash=cash, positions=positions, marks=marks,
                    total_assets=total)
            except agent_decide.CashShortfall as exc:
                # 资金闸门（Q1-A）在 `paper step` 这条路上也必须是**具名**的：
                # 让它以 traceback 冒出去，读数上等同于崩溃（不可诊断）。转成
                # `PaperError` ⇒ 整日事务回滚、这一天该臂**没有净值行**
                # （可见地缺，不偷偷透支）。
                raise PaperError(
                    f"账户 {account['account_id']} 在 {asof} 的操盘决策要透支，"
                    f"整日不执行：{exc.reason} —— 该臂这一天**没有净值行**"
                    f"（缺得可见，不偷偷透支）") from exc
        elif has_rules(account):
            cash, positions, total, _evals, orders = _plan_steps(
                conn, account, asof=asof, cash=cash, positions=positions,
                marks=marks, total=total, params=params)
        for d in orders:
            store.insert_trade(conn, account_id=account["account_id"], date=asof,
                               decision=d, now=now, commit=False)
        # P79 / D3：AI 臂的**未成交腿**落库（这里此前是 `_evals`，一个下划线丢掉）。
        # 只有**决策驱动**的那条路落 —— 静态臂 / `arm-agent-v1`（m2 通路）的
        # `_evals` 仍不落库：它们不是「AI 说了没做到」，落进来会把两种读数混成一种。
        for e in evals:
            store.insert_agent_eval(conn, arm=account["account_id"], asof=asof,
                                    decision=e, now=now, commit=False)
        mv, _marks = mark_to_market(positions, marks)
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
            market_value=mv, nav=nav, drawdown=drawdown(history, nav),
            cum_cost=round(cum_cost, 4),
            cum_return=round(nav / state["net_deposits"] - 1.0, 6)
            if state["net_deposits"] else 0.0,
            net_deposits=round(state["net_deposits"], 4),
            index_300_level=idx.price if idx else None,
            index_300_asof=idx.price_asof if idx else None, now=now, commit=False)


# ---------- 读回 ----------

def _account_entry(conn: sqlite3.Connection, account: dict, asof: str,
                   prices: dict[str, Price] | None = None) -> dict:
    # 认领关系 fail-closed（P56 §1.7）：未知 `params.executor` 在这里就点名报错。
    # 放在账目条目的**入口**而不是渲染时，是为了让 `paper show` / 报告 / 页面
    # 三条读出的路都撞上同一道闸，而不是各写一份判定。
    executor_kind(account)
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
        # P69 / T2：这条臂还在飞吗。净值行读得出来 ⇒ 这条账户有历史；
        # `live` 说的是「**还认不认领日终**」，两件事都要能同时看见。
        "live": is_live(account),
        "cash": nav_row["cash"], "positions": positions,
        "market_value": nav_row["market_value"], "nav": nav_row["nav"],
        "drawdown": nav_row["drawdown"], "cum_cost": nav_row["cum_cost"],
        "cum_return": nav_row["cum_return"], "net_deposits": nav_row["net_deposits"],
        # P80 / D8：考核目标是「扣完成本之后的净收益（元）」，而
        # `cum_return` 的分母（净入金）会被入金改大 ⇒ 只看收益率会把「加钱了」
        # 读成「赚少了」。两个数并列摆出来，**口径只有这一处**（净值行两列相减）。
        "profit_cny": round(float(nav_row["nav"]) - float(nav_row["net_deposits"]), 4),
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


# ---------- P56：AI 操盘手的日终（D-50） ----------

def agent_claim_accounts(conn: sqlite3.Connection) -> list[dict]:
    """`executor == 'agent_decision'` 的**在飞**账户（`paper agent run` 的认领范围）。

    遍历全部账户并逐个走 `executor_kind` ⇒ **未知 `executor` 在这里就报错**（fail-closed），
    而不是被悄悄地漏掉。范围按**字段**判定，不按账户名前缀猜。

    P69 / T2 起再排除 `params.live == false` 的账户：停飞臂的日终**不由本命令落**
    （它已经没有产决策的通路了），所以不该被认领、更不该被记成「缺决策」。
    历史台账与净值行**一行不动** —— 停飞是「不再认领」，不是「删掉」。
    """
    return [a for a in store.load_accounts(conn)
            if executor_kind(a) == EXECUTOR_AGENT_DECISION and is_live(a)]


def agent_run(conn: sqlite3.Connection, asof: str, *, now: str,
              arms: Sequence[str] | None = None) -> dict:
    """AI 操盘手家族的**日终**：先要决策在台账里，再按当日收盘价成交并写净值。

    ## 为什么这条命令必须存在（D-50，写进注释与 runbook）

    `paper step` 的幂等判据是「**当日净值行存在**」。收盘链 15:30 先跑完 ⇒ 之后
    写进台账的决策**永远不会被执行**（症状出现在净值表上，排查方向会跑到订单
    生成上去 —— P47 `external_executor` 里那条坑的同型）。所以 `agent_decision`
    账户在 `paper step` 里**让出**，日终改由本命令落 —— **顺序是硬的：先 decide，
    后 run**。

    ## 三种结果（都不静默）

    - **有决策**：按决策调仓、写成交与净值。取值与 `paper step` **同一段实现**
      （`_step_all`，只把让出关掉），所以「AI 臂的净值」不是第二套口径。
    - **无决策**：写一条**平盘净值行**（持仓现金照旧、成交 0 笔），并在回执里带
      `anomaly.missing_decision` —— 交易日 ⇒ 退出码 1，非交易日 ⇒ 0。
      **不补造默认决策**：那会把「没决定」显示成「决定按默认纪律办」。
    - **重跑**：当日净值行已存在 ⇒ `already`，**一行都不写**、不会有第二笔成交。

    ## `arms`（P69 / T1）：认领范围可指定

    不传 = 认领全部（**逐字段保持现状**）。传了 = **只认领点名的臂**：其余账户不写
    平盘净值行、不进 `anomaly.missing_decision`、不进 `n_claimed`，也不会因为
    「当日净值行已存在」而回一条 `already`（它们压根不在遍历里）。

    为什么需要它：内置的 `arm-agent`（P52 占位臂）没有预注册 ⇒ **按定义不可能有决策**，
    却每天被认领 ⇒ 交易日恒报一条 `missing_decision` ⇒ 日更**退出码恒 1**，页面上还多
    一条死平线。驱动侧显式点名在飞的两条臂，这条假警就没有了（P69 §T1）。

    **fail-closed**：点名的账户不在认领范围（名字不存在 / 已停飞 / 是别的执行者如通路 A）
    ⇒ **点名报错**（`PaperError` ⇒ CLI 退出码 2），**零写入**。静默跳过 = 驱动以为那天
    落了日终，而净值表上什么都没有。
    """
    claim = agent_claim_accounts(conn)
    if arms:
        # 可重复传参 ⇒ 去重保序（`--arm A --arm A` 不该算两条）
        wanted = list(dict.fromkeys(str(a) for a in arms))
        known = {str(a["account_id"]) for a in claim}
        unknown = [a for a in wanted if a not in known]
        if unknown:
            raise PaperError(
                f"--arm 点名的账户不在本命令的认领范围：{unknown}。"
                f"`{EXECUTOR_KEY}={EXECUTOR_AGENT_DECISION}` 的账户只有 "
                f"{sorted(known)} —— 名字不存在、已停飞（params.{LIVE_KEY}=false）、"
                f"或是别的执行者（如通路 A）的账户都不由本命令落日终。"
                f"**不静默跳过**：跳过会让驱动以为那天落了日终")
        keep = set(wanted)
        claim = [a for a in claim if str(a["account_id"]) in keep]
    if not claim:
        raise PaperError(
            f"没有 `{EXECUTOR_KEY}={EXECUTOR_AGENT_DECISION}` 的账户 —— "
            f"本命令是那个执行者，没有认领对象就无事可做。"
            f"先 `paper init` / `paper agent enroll`")
    entries: list[dict] = []
    missing: list[dict] = []
    for account in claim:
        aid = str(account["account_id"])
        expectation = decision_expectation(conn, account=account, asof=asof)
        decision = agent_decide.portfolio_decision_on(conn, aid, asof)
        present = decision is not None
        existed = store.nav_exists(conn, aid, asof)
        if not existed:
            # 与 `step_account` 同款：一天的落盘是一个事务，失败不留半截状态
            with transaction(conn):
                _step_all(conn, asof, accounts=[account], now=now, prices=None,
                          claim_handover=False)
        trades = [t for t in store.trades_on(conn, aid, asof)]
        latest = store.latest_nav(conn, aid, asof=asof) or {}
        entry = {
            "account_id": aid, "status": "already" if existed else "ran",
            "wrote_nav": (not existed) and store.nav_exists(conn, aid, asof),
            "date": latest.get("date"), "nav": latest.get("nav"),
            "n_trades": len(trades), "decision_present": present,
            "decision_id": (None if not present else int(decision["decision_id"])),
            "model_id": (None if not present else str(decision["model_id"])),
            "missing_decision": None,
        }
        if not present:
            note = (f"{aid} 在 {asof} **没有决策行** ⇒ 本日不下单，"
                    f"净值行是**平盘**（照当日收盘价重估、成交 0 笔）。"
                    f"**不补造默认决策**：那会把「没决定」显示成「决定按默认"
                    f"纪律办」")
            entry["missing_decision"] = {**expectation, "note": note}
            if expectation["expected"]:
                missing.append({"account_id": aid, **expectation, "note": note})
        entries.append(entry)
    return {
        "asof": asof, "accounts": entries,
        "n_claimed": len(claim),
        "anomaly": {"missing_decision": missing},
        "note": ("AI 操盘手的日终：先要决策在台账里，再按当日收盘价成交并写净值。"
                 "顺序是硬的 —— `paper step` 先跑会写掉当日净值，之后的决策"
                 "永远不会被执行（D-50）"),
    }


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
    mv, _ = mark_to_market(state["positions"], marks)
    return {
        "account_id": arm, "arm": account["arm"],
        "cash": state["cash"], "positions": dict(state["positions"]),
        "marks": marks, "market_value": mv,
        "total_assets": round(state["cash"] + mv, 4),
        "net_deposits": state["net_deposits"],
        "has_nav_on_asof": store.nav_exists(conn, arm, asof),
    }


# ---------- P80：实时账户与购买力（D5 / D6 的**唯一**投影） ----------

def latest_pit_close_date(conn: sqlite3.Connection) -> str | None:
    """库里有 PIT 收盘价的**最近**日期。一条收盘价都没有 → `None`。

    为什么不用 `paper_nav_daily` 的最新日期当默认 asof：那个要等收盘链跑完
    （`paper step` / `paper agent run` 才写净值），而 D5 要的是「**随时可查**」
    —— 盘中、收盘链之前也要能问「我现在有多少钱、每只最多能买几手」。
    `bars_daily` 只收交易日行，所以「有收盘价的最近日期」就是最近交易日。
    """
    row = conn.execute("SELECT MAX(date) AS d FROM bars_daily"
                       " WHERE adj_mode = 'none'").fetchone()
    return None if row is None or row["d"] is None else str(row["d"])


def sellable_positions(conn: sqlite3.Connection, account: dict, asof: str) -> dict:
    """可卖数量（**T+1**）：当日买入的部分当日不可卖。

    判据只有一条恒等式：`可卖 = 当前持仓 − 当日买入`（当日卖出已经反映在当前持仓里，
    再减一次就少算了）。**不重放第二遍成交**：重放出来的持仓与 `_ledger_arm_state`
    是同一份，`- 当日买入` 是同一个量上的一次减法，不会造出第二个真相。
    """
    current = _arm_state(conn, account, asof)["positions"]
    out = {code: int(qty) for code, qty in current.items()}
    for t in store.trades_on(conn, str(account["account_id"]), asof):
        if t["side"] != "buy":
            continue
        code = str(t["code"])
        if code in out:
            out[code] = max(0, out[code] - int(t["qty"]))
    return out


def account_view(conn: sqlite3.Connection, arm: str, asof: str | None = None) -> dict:
    """一条臂在 `asof` 的**实时账户 ＋ 购买力**（P80 / D5）。

    ## 为什么它必须是「一处的投影」

    CLI 的 `paper account` 与 AI 的输入侧（`tradability` / D6）回答的是**同一个问题**
    （「我手上有多少现金、每只最多能买几手」）。两条路各拼一套的话，页面上会出现
    两个「能买几手」—— 而这正是 D-37（同一页两个「总收益」差一个常数）那类事故。
    所以购买力只有这一个函数：`max_lots_affordable(rules.one_lot_cost)`。

    ## 不落净值行也能读

    账户状态由 `_arm_state` **重放**算出（`<= asof` 的成交 ＋ `<= asof` 的资本事件），
    不要求 `paper_nav_daily` 有这一天的行 —— 这正是「随时可查」与「等收盘链」的区别。
    `account.date` 是**问的那一天**（`asof`），不是净值行的日期。

    ## 缺什么就报什么，不猜数

    - 账户不存在 / 库里没有任何收盘价 ⇒ `PaperError`（点名）；
    - 持仓缺 `<= asof` 的收盘价 ⇒ `MissingPriceError`（不用成本价冒充现价）；
    - 池内标的缺 `instruments.type` ⇒ 进 `errors`，**不进** `by_code`
      （口径未知时不得退化成股票费率）。
    """
    account = next((a for a in store.load_accounts(conn)
                    if a["account_id"] == arm), None)
    if account is None:
        raise missing_account_error(arm)
    executor_kind(account)      # 认领关系 fail-closed（与 `_account_entry` 同一道闸）

    resolved = asof if asof is not None else latest_pit_close_date(conn)
    if resolved is None:
        raise PaperError("库里一条收盘价都没有（`bars_daily` 为空）—— "
                         "「最多能买几手」无从算起；先 `ingest bars`")
    state = _arm_state(conn, account, resolved)
    positions = dict(state["positions"])
    marks = resolve_marks(conn, set(positions), resolved)
    missing = sorted(set(positions) - set(marks))
    if missing:
        raise MissingPriceError(
            f"账户 {arm} 的持仓 {missing} 取不到 ≤ {resolved} 的收盘价 → "
            f"账户状态不可判定。先跑 `ingest bars`；**不用成本价冒充现价**")
    mv, marked = mark_to_market(positions, marks)
    cash = round(float(state["cash"]), 4)
    total = round(cash + mv, 4)
    nav = round(cash + mv, 4)
    net_deposits = round(float(state["net_deposits"]), 4)

    errors: list[dict] = []
    sellable = sellable_positions(conn, account, resolved)
    detail = []
    for row in marked:
        code = str(row["code"])
        try:
            asset_class = agent_decide.asset_class_for(conn, code)
        except agent_decide.DecisionPayloadError as exc:
            asset_class, cost = None, None
            errors.append({"code": code, "where": "positions_detail",
                           "error": str(exc), "type": type(exc).__name__})
        else:
            cost = one_lot_cost(price=float(row["price"]), asset_class=asset_class)
        detail.append({
            "code": code, "qty": int(row["qty"]), "price": float(row["price"]),
            "price_asof": str(row["price_asof"]), "source": str(row["source"]),
            "market_value": round(float(row["price"]) * int(row["qty"]), 4),
            "one_lot_cost": cost, "asset_class": asset_class,
            "sellable_qty": int(sellable.get(code, 0)),
        })

    pool = agent_decide.pool_for(conn, resolved)
    pool_codes = sorted({str(c) for c in (pool.get("codes") or [])})
    pool_marks = resolve_marks(conn, set(pool_codes), resolved)
    by_code: dict[str, dict] = {}
    for code in pool_codes:
        p = pool_marks.get(code)
        if p is None:
            # 池内无 PIT 收盘价 ⇒ 算不出一手成本。**不进 `by_code`**
            # （与 `tradability` 的 `marks ∩ pool` 同一个集合），也不猜一个数。
            continue
        try:
            asset_class = agent_decide.asset_class_for(conn, code)
        except agent_decide.DecisionPayloadError as exc:
            errors.append({"code": code, "where": "buying_power",
                           "error": str(exc), "type": type(exc).__name__})
            continue
        cost = one_lot_cost(price=float(p.price), asset_class=asset_class)
        lots = max_lots_affordable(cash, cost)
        # P81 / D4：「今天下不下得了手」的结论。判据是 `tradable_verdict` 一处实现
        # —— AI 输入侧的 `tradability.by_code[c].tradable` 调的是**同一个函数**，
        # 两处各写一份就会造出同一页两个数（ERROR_DIARY #70）。
        tradable, reason = tradable_verdict(affordable_lots=lots,
                                            one_lot_cost_value=cost,
                                            total_assets=total, cash=cash)
        by_code[code] = {
            "one_lot_cost": cost, "asset_class": asset_class,
            "price": float(p.price), "price_asof": str(p.price_asof),
            "max_lots_affordable": lots,
            "tradable": tradable, "untradable_reason": reason,
        }

    domain = trading_domain(by_code=by_code, n_pool_codes=len(pool_codes),
                            total_assets=total)

    fills = store.trades_on(conn, arm, resolved)
    return {
        "arm": arm,
        "asof": resolved,
        "asof_source": "explicit" if asof is not None else "latest_pit_close",
        "account_exists": True,
        "account": {
            # 与 CLI 的降级分支同形（那里 `available=False` + 点名原因）：
            # 「算得出」与「算不出」必须长得不一样。
            "available": True,
            "date": resolved, "cash": cash, "market_value": mv,
            "total_assets": total, "net_deposits": net_deposits,
            "cum_return": (round(nav / net_deposits - 1.0, 6)
                           if net_deposits else 0.0),
            "cum_cost": round(float(state["cum_cost"]), 4),
            "drawdown": drawdown([float(r["nav"]) for r in
                                  store.load_nav(conn, arm, asof=resolved)], nav),
            "nav": nav,
            # **考核目标就是这一个数**（净收益 = 净值 − 累计净入金）。入金会让
            # 分子同步变大，所以它**不因入金跳变** —— 跳变的只是现金，
            # 而现金不是收益。
            "profit_cny": round(nav - net_deposits, 4),
        },
        "positions_detail": detail,
        "buying_power": {
            "cash": cash,
            "lot": LOT,
            "pool_asof": pool.get("asof"),
            "by_code": by_code,
            "n_pool_codes": len(pool_codes),
            "n_priced": len(by_code),
            # P81 / D4：可下手域的读数面（计数 / 中位数 / 最小可成交权重）。
            # 它就是本函数算出来的那一份 —— `build_report` 与页面直接取它，
            # **不另算**（另算就是第二个真相）。
            "tradable_domain": domain,
            "fills_on_asof": {
                "n": len(fills),
                "amount": round(sum(float(t["fill_price"]) * int(t["qty"])
                                    for t in fills), 4),
                "fee": round(sum(float(t["fee_total"]) for t in fills), 4),
            },
        },
        "errors": errors,
    }


def decision_context_for(conn: sqlite3.Connection, *, arm: str,
                         asof: str) -> dict:
    """喂给 AI 操盘手的 **PIT 上下文**（`paper agent context` 与两个写入口共用）。

    「生成器看过了什么」只能有一个答案：`paper agent context` 打印它、
    `paper agent decide` 用它重算指纹做比对。两条路各拼一套的话，
    `context_sha256` 比对就变成「自己跟自己比」——永远为真，也就永远没有意义。
    """
    # 懒 import：`agent_context` 模块级 import 本模块（取 `INDEX_300_SYMBOL` /
    # `pit_close`），反向 import 会成环 —— 与 `comparison_block` 同款。
    from stocklab.paper import agent_context

    state = arm_state_for(conn, arm, asof)
    if state is None:
        raise PaperError(f"账户 {arm} 不存在；先 `paper init`（或 `paper agent enroll`）")
    pool = agent_decide.pool_for(conn, asof)
    marks = {**resolve_marks(conn, set(state["positions"]) | set(pool["codes"]), asof),
             **state["marks"]}
    # `net_deposits` 与 `sellable_qty`（P80 / D6）：前者是「净入金」这条口径的唯一
    # 来源（`_ledger_arm_state`，含资本事件），后者是同一份持仓上的一次减法。
    # 购买力（`affordable_lots`）**不在这里传**：它由 `agent_context` 用
    # `engine.max_lots_affordable` ＋ `rules.one_lot_cost` 与 `account_view` 同源算出。
    account = next(a for a in store.load_accounts(conn) if a["account_id"] == arm)
    return agent_context.build_decision_context(
        conn, arm=arm, asof=asof, pool=pool, cash=state["cash"],
        positions=state["positions"], marks=marks,
        total_assets=state["total_assets"],
        net_deposits=float(state["net_deposits"]),
        sellable_qty=sellable_positions(conn, account, asof))


def state_payload(conn: sqlite3.Connection, asof: str) -> dict:
    """`asof` 的载荷 —— **只由库里的行 + PIT 价格决定**，故重跑逐字节一致。"""
    accounts = store.load_accounts(conn)
    # 认领关系 fail-closed：**先**把每一条账户的 `executor` 验一遍，再按「当日有没有
    # 净值行」过滤。放在过滤之后的话，一个坏掉的账户只要那天没有净值行就会被
    # 悄悄跳过 —— 而「跳过」正是这条闸门要挡的东西（静默 ⇒ 只在净值表上看得出症状）。
    for a in accounts:
        executor_kind(a)
    idx = pit_close(conn, INDEX_300_SYMBOL, asof)
    return {
        "asof": asof,
        "accounts": [_account_entry(conn, a, asof) for a in accounts
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


#: 未成交腿理由在**对账表**里截断到多少字符（原文仍是 `paper agent evals` 的出口）。
RECON_REASON_MAX = 120

#: 台账里一条有决策的 AI 臂都没有时的**唯一**措辞 —— 空列表不是「全都落地了」。
NO_RECON_FMT = ("`paper_agent_decisions` 里没有任何一条 AI 臂的决策 ⇒ 没有「意图」"
                "可比，本块**不是**「全都落地了」")


def _truncate_reason(text: str, limit: int = RECON_REASON_MAX) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def reconciliation_block(conn: sqlite3.Connection, asof: str) -> dict:
    """「AI 意图」vs「账户落地」的逐臂对账（P81 / D3）。

    ## 为什么必须有它

    P79 把「为什么没动」落进了 `paper_agent_evals`（`plan_orders` 早就算得出的
    理由），但**一个消费者都没有**：`paper show` / `/lab/paper` / 日报全都不读它。
    于是「AI 意图 67% 仓位、账户 0% 现金」这类事故（P79 §0.1 的真实故障）
    在页面上依然看不见。可观测性是本站的全部目的。

    ## 覆盖面：**有决策的臂**，不写死 `arm-agent`

    `agent_block` 的其余字段讲的是 `arm-agent`；而真库在跑的其实是
    `arm-agent-ds-v1` / `-v2`（`arm-agent` 一条决策都没有，见 P81 §0.2）。
    写死一条臂 ⇒ 页面长期展示一条空臂。故本块是**列表**：覆盖 `paper_accounts`
    里 `arm` 以 `agent` 开头、且在 `paper_agent_decisions` 里有行的每一条臂，
    各取自己最新的决策日（且 `asof <=` 本块的 asof —— 历史视图不显示未来）。

    ## 三个数各自如实，**不用 `min()` 抹平**

    - `n_legs_planned`：最新一条 `portfolio` 决策的载荷条数；
    - `n_legs_filled`：该臂**那个决策日**的 `paper_trades` 行数；
    - `n_legs_unfilled` 与 `n_evals`：同一日的 `paper_agent_evals` 行数。
      后两个**故意重复**（一个是「腿数」、一个是「台账行数」）：两者不等的差额
      本身就是信号，抹平它等于把信号藏起来。

    该臂只有 spec 复审行、没有操盘决策 ⇒ `n_legs_planned` 是 `None`（**不猜 0**：
    「没有载荷可比」与「载荷是空的」不是一回事）。
    """
    arms: list[dict] = []
    for account in store.load_accounts(conn):
        if not str(account["arm"]).startswith("agent"):
            continue
        arm = str(account["account_id"])
        rows = [d for d in agent_decide.load_decisions(conn, arm)
                if str(d["asof"]) <= asof]
        if not rows:
            continue
        portfolio = agent_decide.portfolio_decisions_only(rows)
        decision = portfolio[-1] if portfolio else None
        d_asof = str(decision["asof"] if decision is not None else rows[-1]["asof"])
        planned = (None if decision is None else
                   len((decision.get("payload") or {}).get("decisions") or []))
        trades = store.trades_on(conn, arm, d_asof)
        evals = store.load_agent_evals(conn, arm=arm, asof=d_asof)
        arms.append({
            "arm": arm,
            "asof": d_asof,
            "n_legs_planned": planned,
            "n_legs_filled": len(trades),
            "n_legs_unfilled": len(evals),
            # 与 `n_legs_unfilled` 同值（故意重复，见 docstring）：一个是台账行数、
            # 一个是腿数；不等时**如实报**两个数。
            "n_evals": len(evals),
            "unfilled": [{"code": str(e["code"]), "action": str(e["action"]),
                          "reason": _truncate_reason(str(e["reason"]))}
                         for e in evals],
        })
    return {"arms": arms, "note": (None if arms else NO_RECON_FMT)}


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
    # P81 / D3：逐臂对账（覆盖**有决策的**臂，不写死 `arm-agent`）。
    recon = reconciliation_block(conn, asof)
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
        # P81 / D3：「AI 说的」vs「账户做的」逐臂对账。**只增键** —— 既有读者
        # （`paper show` / 页面 / `--json` 的下游）拿到的是多出来的两个键，不是变了形的键。
        "reconciliation": recon["arms"],
        "reconciliation_note": recon["note"],
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


def tradable_domain_block(conn: sqlite3.Connection, arm: str, asof: str) -> dict:
    """一条 AI 臂在 `asof` 的可下手域读数（P81 / D4）。

    数据源是 `account_view`（账户 ＋ 购买力的**唯一**投影）—— 这里只把
    `buying_power.tradable_domain` 原样取出来，**不另算**一遍：「同一个数只许有一个
    来源」是这一页的既有纪律（ADR-023 / D-37）。

    取不到（缺价 / 无池 / 账户口径坏掉）⇒ `available: False` ＋ 点名原因，**不编数**
    （与页面既有降级分支同形：取不到就写取不到，不填 0）。「无池」与「池内一只都取不到
    价」都要走这条路：那时 `n_tradable=0` 是**不可判定**，不是「一只都买不起」。

    ⚠️ 降级分支**必须真的降级**：这一段挂在 `build_report` / 页面上，一个账户的口径
    读不出来（例如老账户的 `params_json` 里没有 `initial_capital`）**不许把整份报告
    打崩** —— 报告里少一行是真的，500 是坏的。故这里连 `KeyError` / `ValueError`
    一起接住并**点名类型**（`type`），而不是让它冒到渲染层。
    """
    try:
        view = account_view(conn, arm, asof)
    except (PaperError, agent_decide.DecisionPayloadError, KeyError,
            ValueError) as exc:
        return {"available": False, "arm": arm, "asof": asof,
                "reason": str(exc), "type": type(exc).__name__}
    domain = dict(view["buying_power"]["tradable_domain"])
    if domain["n_pool_codes"] == 0:
        return {"available": False, "arm": arm, "asof": view["asof"],
                "reason": f"{view['asof']} 没有候选池（可投集合为空）—— "
                          f"可下手域不可判定，不填 0"}
    if domain["n_priced"] == 0:
        return {"available": False, "arm": arm, "asof": view["asof"],
                "reason": f"池内 {domain['n_pool_codes']} 只标的都取不到 "
                          f"≤ {view['asof']} 的收盘价 —— 不可判定，不填 0"}
    return {"available": True, "arm": arm, "asof": view["asof"], **domain}


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
        entry = {
            **{k: e[k] for k in ("account_id", "arm", "etf_target_pct", "date",
                                 "live",
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
        }
        if e["arm"] in (ARM_KIND_AGENT, ARM_KIND_AGENT_RANDOM):
            # P81 / D4：**每条 AI 臂**补一个可下手域块（非 AI 臂不加键：它们的
            # 「一手买不买得起」不是这套口径要回答的问题）。
            entry["tradable_domain"] = tradable_domain_block(conn, e["account_id"],
                                                             asof)
        per_account.append(entry)
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


def render_tradable_domain(domain: Mapping | None) -> list[str]:
    """可下手域那一行的 Markdown（P81 / D4；报告与页面用**同一批措辞**）。

    数字全部来自 `tradable_domain`（`account_view.buying_power` 的成品），
    本函数不重算；形状不全就写「取不到」——**不填 0**。
    买不起的标的**点名**（code ＋ 理由原文），因为「买不起」有两种成因、
    处置不同（一种是这个账户无解，另一种是先卖出别的标的）。
    """
    if not domain:
        return ["- 可下手域：**取不到**（这条臂当日没有账户投影）—— 不填 0"]
    if not domain.get("available"):
        return [f"- 可下手域：**取不到**（{domain.get('reason')}）—— 不编数"]
    pct, p50 = domain["min_tradable_weight_pct"], domain["one_lot_cost_p50"]
    lines = [f"- 池内可下手 {domain['n_tradable']}/{domain['n_pool_codes']} 只；"
             f"最小可成交权重 {'—' if pct is None else f'{pct:.2f}%'}；"
             f"一手成本中位数 {'—' if p50 is None else f'¥{p50:,.2f}'}"]
    for row in domain.get("untradable") or []:
        lines.append(f"  - 买不起：{row['code']} —— {row['reason']}")
    return lines


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
        if "tradable_domain" in a:
            # P81 / D4：AI 臂才有的「可下手域」读数（小面额能不能下手，得有个数）。
            L.extend(render_tradable_domain(a["tradable_domain"]))
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
