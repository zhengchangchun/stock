"""通路 A 主干（纯模拟）：A1 选股 → 模拟买入 → A2 调仓退出 → 更新账户 → A3 预测。

## 顺序写在主干里，插桩只能填内容（需求 02 §主流程）

| 步 | 谁 | 做什么 |
|---|---|---|
| 1 | 主干 | 只取 `<= asof` 的 PIT 快照（持仓 / 收盘价 / 候选池 / 特征） |
| 2 | `m2_a1` | 池内二次选股 → `{picks, cash_pct}`（**只产出，不落库**） |
| 3 | 主干 | 按权重翻译成订单并成交（费用/整手/滑点全部由 `paper/rules.py` 提供） |
| 4 | `m2_a2` | 对**成交后**的持仓给卖出指令（止盈止损 / 调仓退出） |
| 5 | 主干 | 结算 A2 的卖出 → 现金 / 持仓 / 净值 / 交易日志 |
| 6 | `m2_a3` | 逐持仓标的做收益预测 → `m2_forecasts`（append-only） |

## 为什么 A2 看到的是**成交后**的持仓

这是需求 02 主流程的原文顺序（A1 → 模拟买入 → A2），不是随手排的：
A2 的职责里有「**调仓退出**」——它要能看到「今天刚建起来的那条腿」是否就该退。
反过来（两个钩子都看开盘前快照）会让「今天买了、今天卖」这件事在两条独立判断之间
凭空发生一次，也说不清是谁决定的。

## 一笔成交都不自己算

订单由 `agent_decide.execute_decision` 生成（与 P52 的 AI 操盘手**同一条**执行路径，
P52 又复用 `paper/rules.py::plan_target_weight`），落库由
`paper/store.py::insert_trade` 写（成交**只有一个写入口** —— ERROR_DIARY #61 的
反面教材就是「新路径自己也能落库」导致同一天同一笔被插两次、整个 step 回滚）。

A2 的「清仓」表达为目标权重 0，而不是另写一条「整清」路径：目标权重是这套引擎
唯一的订单语言，多一种表达就多一处口径。

## 幂等

幂等单元 = `(账户, asof)`，判据是 `m2_channel_runs` 里那一行 `status='ran'`
（**不是** `paper_nav_daily` 的存在性 —— 那个格子在通路 A 之外也可能被 `paper step`
占掉，见 `m2/config.py` 的 `executor` 说明）。命中就整个跳过，**一个字节都不写**。
"""

from __future__ import annotations

import json
import sqlite3

from stocklab.m2 import config
from stocklab.m2 import context as ctx_mod
from stocklab.m2 import plugin_hooks
from stocklab.m2 import store as m2_store
from stocklab.paper import agent_decide, agent_pool, engine
from stocklab.paper import store as paper_store
from stocklab.paper.config import ARM_HOLD, ARM_KIND_AGENT
from stocklab.paper.engine import INDEX_300_SYMBOL
from stocklab.paper.rules import check_no_lookahead
from stocklab.plugin.contract import PluginContractError
from stocklab.plugin.lifecycle import NoActivePlugin
from stocklab.store.db import transaction

#: 通路把「脚本自己的错」折成一次拒绝运行。**只收这一组**：
#: `sqlite3` 的错误、`KeyError`、断言失败这些一律向上抛 ——
#: 把未知异常也记成「脚本越界」，等于把 bug 藏进一张看起来正常的台账里。
REJECTED = (config.ChannelReject, PluginContractError, NoActivePlugin,
            agent_decide.DecisionPayloadError, engine.PaperError)


def load_account(conn: sqlite3.Connection, account_id: str) -> dict:
    """取通路 A 的账户行；不存在 / 不是通路 A 的账户 → 拒绝。

    第二道判据（`executor`）不是装饰：少了它，`--strategy-version now` 就能把
    实盘镜像账户当成模拟臂来下单。
    """
    account = next((a for a in paper_store.load_accounts(conn)
                    if a["account_id"] == account_id), None)
    if account is None:
        raise config.ChannelReject(
            "account", f"账户 {account_id} 不存在 —— 先跑 "
                       f"`m2 account init --strategy-version <v>`（每个策略版本 = "
                       f"一个账户行 + 独立 NAV，D-26）")
    executor = json.loads(account["params_json"]).get(config.EXECUTOR_KEY)
    if executor != config.EXECUTOR_CHANNEL_A:
        raise config.ChannelReject(
            "account", f"账户 {account_id} 的 params.{config.EXECUTOR_KEY} = "
                       f"{executor!r}，不是 {config.EXECUTOR_CHANNEL_A!r} —— "
                       f"拒绝在别人的账户上下单（本通路只碰自己那条臂）")
    return account


def create_account(conn: sqlite3.Connection, *, strategy_version: str,
                   now: str) -> dict:
    """建一个策略版本的账户行（幂等：已存在则不改写）。

    起跑口径**从既有 `arm-hold` 账户派生**，不另起一套初始化路径（任务书 §1 的
    ⚠️：两套初始化路径必然走样）。`arm` 取 `agent` —— D-35 拍板通路 A 就用
    `arm-agent` 家族；`paper step` 因为 `executor` 字段会让出它的日终（见 config）。
    """
    account_id = config.account_id_for(strategy_version)
    if paper_store.account_exists(conn, account_id):
        return {"account_id": account_id, "strategy_version": strategy_version,
                "created": False}
    base = next((a for a in paper_store.load_accounts(conn)
                 if a["account_id"] == ARM_HOLD), None)
    if base is None:
        raise config.ChannelReject(
            "account", "起跑账户 `arm-hold` 不存在 —— 先跑 `paper init`"
                       "（通路 A 的起跑口径从它派生，不另起一套）")
    params = {
        **json.loads(base["params_json"]),
        config.EXECUTOR_KEY: config.EXECUTOR_CHANNEL_A,
        "strategy_version": strategy_version,
        "decision_cadence": config.DECISION_CADENCE,
        "plugin_hooks": list(config.CHANNEL_PLUGINS[config.CHANNEL_A]),
        "wired_from": "P47 / D-35：通路 A = arm-agent 家族账户，A1/A2/A3 插桩＝策略实现",
    }
    paper_store.insert_account(
        conn, account_id=account_id, arm=ARM_KIND_AGENT, etf_target_pct=None,
        start_date=base["start_date"], initial_cash=base["initial_cash"],
        initial_positions=json.loads(base["initial_positions_json"]),
        initial_nav=base["initial_nav"], params=params, now=now)
    return {"account_id": account_id, "strategy_version": strategy_version,
            "created": True, "start_date": base["start_date"],
            "initial_nav": base["initial_nav"], "initial_cash": base["initial_cash"],
            "decision_cadence": config.DECISION_CADENCE}


def _cum_cost_before(conn: sqlite3.Connection, account: dict, asof: str) -> float:
    """`asof` **之前**的累计成本（净值行的 `cum_cost`）。

    为什么不去问引擎要：`arm_state_for` 是「写决策时用的状态」，它刻意不带这条
    累计量。而净值行的 `cum_cost` 是同一口径的既有落点（每行都是「到那天为止」），
    读它比另算一遍稳 —— 首日没有净值行时才回落到账户行里冻结的 `seed_fee`。
    """
    row = paper_store.latest_nav(conn, account["account_id"], asof=asof)
    if row is not None:
        return float(row["cum_cost"])
    return float(json.loads(account["params_json"]).get("seed_fee", 0.0))


def _weights_items(picks: list[dict], *, positions: dict, marks: dict,
                   total_assets: float, rationale: str) -> list[dict]:
    """A1 的 `{code, weight_pct}` → 执行层的目标市值条目。

    `side` 由「目标市值 vs 现市值」**推出**（`agent_decide.side_for`），
    与 P52 的写入口同一条规则 —— 于是「A1 说要减到 5%」不会被误读成买入。
    """
    items: list[dict] = []
    for pick in picks:
        code = str(pick["code"])
        weight = float(pick["weight_pct"])
        if weight <= 0.0:
            continue                      # 0 权重 = 不投它，不是一条订单
        if code not in marks:
            raise config.ChannelReject(
                "picks", f"A1 选了 {code}，但取不到它的 PIT 收盘价 —— 目标权重无法落地"
                         "（不许用别的标的价格顶替，也不许静默丢掉这笔）")
        price = float(marks[code].price)
        qty = int(positions.get(code, 0))
        current_value = round(price * qty, 4)
        target_value = round(total_assets * weight / 100.0, 4)
        side = agent_decide.side_for(target_value=target_value,
                                     current_value=current_value) or "buy"
        items.append({
            "code": code, "side": side, "target_weight_pct": weight,
            "reason": str(pick["reason"]), "target_value": target_value,
            "price": price, "price_asof": str(marks[code].price_asof),
            "price_source": str(marks[code].source), "current_qty": qty,
            "current_value": current_value,
        })
    if not items:
        raise config.ChannelReject(
            "picks", f"A1 的选股全是 0 权重或空清单 → 今天没有任何订单可下。"
                     f"{rationale}。**空仓要由 cash_pct=100 表达**，不是由一份空的"
                     f" picks 表达 —— 后者与「A1 没跑起来」在读数上无法区分")
    return items


def _sell_items(orders: list[dict], *, positions: dict, marks: dict) -> list[dict]:
    """A2 的 `{code, side:'sell'}` → 目标市值 0（清仓）。

    A2 只能卖**已经持有**的标的（`plugin_hooks.sell_orders` 的允许集合就是持仓）。
    幂等地再挡一次：万一将来允许集合被放宽，这里也不会产生一笔「卖空」。
    """
    items: list[dict] = []
    for order in orders:
        code = str(order["code"])
        qty = int(positions.get(code, 0))
        if qty <= 0:
            raise config.ChannelReject(
                "orders", f"A2 要求卖出 {code}，但账户里没有它 —— A 股无做空，"
                          f"拒绝而不是静默跳过（跳过会让报告显示『已调仓』而实际没动）")
        if code not in marks:
            raise config.ChannelReject(
                "orders", f"A2 要求卖出 {code}，但取不到它的 PIT 收盘价 → 无法成交")
        price = float(marks[code].price)
        items.append({
            "code": code, "side": agent_decide.SIDE_SELL, "target_weight_pct": 0.0,
            "reason": str(order["reason"]), "target_value": 0.0, "price": price,
            "price_asof": str(marks[code].price_asof),
            "price_source": str(marks[code].source), "current_qty": qty,
            "current_value": round(price * qty, 4),
        })
    return items


def _execute(conn: sqlite3.Connection, *, account: dict, asof: str, now: str,
             prices: dict | None) -> dict:
    """整天的写入在**一个事务**里（与 `engine.step` 同一个理由：

    一次失败不得留下「A1 的单已落、A2 的没落」这种半截状态 —— 它看起来
    像「这天跑过了」，而实际上只跑了一半。
    """
    account_id = account["account_id"]
    with transaction(conn):
        state = engine.arm_state_for(conn, account_id, asof)
        if state is None:
            raise config.ChannelReject("account", f"取不到 {account_id} 的状态")
        pool = agent_pool.pool_snapshot(conn, asof)
        pool_codes = set(pool["codes"])
        codes = set(state["positions"]) | pool_codes
        marks = dict(prices) if prices is not None else engine.resolve_marks(
            conn, codes, asof)
        check_no_lookahead(asof, marks)

        missing_marks = sorted(c for c in state["positions"] if c not in marks)
        if missing_marks:
            raise config.ChannelSkip(
                config.SKIP_NO_BARS,
                f"{asof} 取不到持仓 {missing_marks} 的收盘价 → 净值不可判定。"
                f"**跳过这一天并留痕**（不用成本价冒充现价）")
        missing_bars = ctx_mod.require_daily_bars(conn, state["positions"], asof=asof)
        if missing_bars:
            raise config.ChannelSkip(
                config.SKIP_NO_BARS,
                f"{asof} 缺当日（不复权）K 线：{missing_bars} → 这一天没有收盘，"
                f"跳过并留痕（拿前值顶上去会让净值曲线在缺口处凭空延续）")

        total = float(state["total_assets"])
        cost_prices = ctx_mod.cost_prices_of(account)
        ctx1 = ctx_mod.channel_ctx(
            conn, channel=config.CHANNEL_A, account_id=account_id, asof=asof,
            cash=state["cash"], positions=state["positions"], marks=marks,
            total_assets=total, cost_prices=cost_prices)

        # ---- A1：选股（只产出）+ 模拟买入 ----
        a1, fp1 = plugin_hooks.pick(conn, ctx1, pool_codes=pool_codes)
        positions = dict(state["positions"])
        items1 = _weights_items(a1["picks"], positions=positions, marks=marks,
                                total_assets=total,
                                rationale=f"A1 插桩 {fp1['version']}")
        cash, positions, orders1, _evals1 = agent_decide.execute_decision(
            conn, arm=account_id, asof=asof,
            decision={"asof": asof, "decisions": items1,
                      "cash_pct": float(a1["cash_pct"]),
                      "rationale": f"通路A·A1({fp1['version']})",
                      "total_assets": total},
            cash=float(state["cash"]), positions=positions, marks=marks,
            total_assets=total)
        for d in orders1:
            paper_store.insert_trade(conn, account_id=account_id, date=asof,
                                     decision=d, now=now, commit=False)

        # ---- A2：卖出条件（看的是**成交后**的持仓，见模块 docstring）----
        market_value, _ = engine.mark_to_market(positions, marks)
        total2 = round(cash + market_value, 4)
        ctx2 = ctx_mod.channel_ctx(
            conn, channel=config.CHANNEL_A, account_id=account_id, asof=asof,
            cash=cash, positions=positions, marks=marks, total_assets=total2,
            cost_prices=cost_prices)
        a2, fp2 = plugin_hooks.sell_orders(conn, ctx2, held_codes=set(positions))
        items2 = _sell_items(a2["orders"], positions=positions, marks=marks)
        cash, positions, orders2, _evals2 = agent_decide.execute_decision(
            conn, arm=account_id, asof=asof,
            decision={"asof": asof, "decisions": items2, "cash_pct": 0.0,
                      "rationale": f"通路A·A2({fp2['version']})",
                      "total_assets": total2},
            cash=cash, positions=positions, marks=marks, total_assets=total2)
        for d in orders2:
            paper_store.insert_trade(conn, account_id=account_id, date=asof,
                                     decision=d, now=now, commit=False)

        # ---- 更新账户：现金 / 持仓 / 净值（口径全部来自既有引擎）----
        orders = [*orders1, *orders2]
        market_value, _ = engine.mark_to_market(positions, marks)
        nav = round(cash + market_value, 4)
        history = [r["nav"] for r in
                   paper_store.load_nav(conn, account_id, asof=asof)]
        idx = engine.pit_close(conn, INDEX_300_SYMBOL, asof)
        deposits = float(state["net_deposits"])
        cum_cost_before = _cum_cost_before(conn, account, asof)
        paper_store.insert_nav(
            conn, account_id=account_id, date=asof, cash=round(cash, 4),
            positions=[{"code": c, "qty": q} for c, q in sorted(positions.items())],
            market_value=market_value, nav=nav,
            drawdown=engine.drawdown(history, nav),
            cum_cost=round(cum_cost_before
                           + sum(d.fees.get("total", 0.0) for d in orders), 4),
            cum_return=round(nav / deposits - 1.0, 6) if deposits else 0.0,
            net_deposits=round(deposits, 4),
            index_300_level=(idx.price if idx else None),
            index_300_asof=(idx.price_asof if idx else None),
            now=now, commit=False)

        # ---- A3：逐持仓标的的收益预测（append-only）----
        ctx3 = ctx_mod.channel_ctx(
            conn, channel=config.CHANNEL_A, account_id=account_id, asof=asof,
            cash=cash, positions=positions, marks=marks, total_assets=nav,
            cost_prices=cost_prices)
        n_forecasts = 0
        fp3: dict | None = None
        for code in sorted(positions):
            focus = {"code": code, "qty": int(positions[code])}
            fctx = {**ctx3, "focus": focus}
            payload, fp3 = plugin_hooks.call_forecast(conn, config.PLUGIN_A3, fctx)
            m2_store.insert_forecast(
                conn, plugin_id=config.PLUGIN_A3, channel=config.CHANNEL_A,
                account_id=account_id, asof=asof, code=code, payload=payload,
                script_id=fp3["script_id"], script_version=fp3["version"],
                input_sha256=ctx_mod.ctx_sha256(fctx), now=now, commit=False)
            n_forecasts += 1
        if fp3 is None:                    # 空仓：没有可预测的持仓
            fp3 = plugin_hooks.script_fingerprint(conn, config.PLUGIN_A3)

        m2_store.insert_run(
            conn, channel=config.CHANNEL_A, account_id=account_id, asof=asof,
            status=config.STATUS_RAN,
            reason=(f"通路A：A1({fp1['version']}) 选股 {len(a1['picks'])} 只"
                    f"（现金 {float(a1['cash_pct']):g}%）→ 成交 {len(orders1)} 笔；"
                    f"A2({fp2['version']}) 卖出指令 {len(a2['orders'])} 条"
                    f"→ 成交 {len(orders2)} 笔；A3 预测 {n_forecasts} 条；"
                    f"净值 {nav:,.2f}"),
            plugins={config.PLUGIN_A1: fp1, config.PLUGIN_A2: fp2,
                     config.PLUGIN_A3: fp3},
            n_orders=len(orders), now=now, commit=False,
            detail={"nav": nav, "cash": round(cash, 4),
                    "positions": {c: int(q) for c, q in sorted(positions.items())},
                    "cash_pct": float(a1["cash_pct"]),
                    "pool_codes": sorted(pool_codes),
                    "candidates_excluded": ctx1["candidates_excluded"],
                    "ctx_sha256": ctx_mod.ctx_sha256(ctx1),
                    "orders": [{"code": d.code, "side": d.action, "qty": d.qty,
                                "fill_price": d.fill_price,
                                "fee_total": d.fees.get("total", 0.0),
                                "rule_citation": d.rule_citation}
                               for d in orders]})
    return {"status": config.STATUS_RAN, "nav": nav, "cash": round(cash, 4),
            "positions": {c: int(q) for c, q in sorted(positions.items())},
            "n_orders": len(orders), "n_forecasts": n_forecasts,
            "plugin_versions": {"m2_a1": fp1["version"], "m2_a2": fp2["version"],
                                "m2_a3": fp3["version"]}}


def _record(conn: sqlite3.Connection, *, account_id: str, asof: str, status: str,
            code: str, reason: str, now: str) -> int:
    return m2_store.insert_run(
        conn, channel=config.CHANNEL_A, account_id=account_id, asof=asof,
        status=status, reason=f"[{code}] {reason}", plugins={}, n_orders=0, now=now)


def run(conn: sqlite3.Connection, *, asof: str, strategy_version: str,
        now: str, prices: dict | None = None) -> dict:
    """跑通路 A 的一天。返回 `{"status": ran|skipped|rejected|already, ...}`。

    **不抛异常**（除调用方的编程错误）：一天跑不成是**业务结论**，
    落进 `m2_channel_runs` 并如实返回，而不是让整个收盘链因为一个标的缺 K 线崩掉。
    """
    account_id = config.account_id_for(strategy_version)
    account = load_account(conn, account_id)
    done = m2_store.ran_run(conn, config.CHANNEL_A, account_id, asof)
    if done is not None:
        return {"status": config.STATUS_ALREADY, "account_id": account_id,
                "asof": asof, "run_id": int(done["run_id"]),
                "note": ("同 (通路, 账户, 日) 已经跑过 → **一个字节都不写**"
                         "（幂等重放；连台账行都不追加）")}
    if paper_store.nav_exists(conn, account_id, asof):
        reason = (f"{account_id} 在 {asof} 已有净值行，但台账里没有通路 A 的 `ran` "
                  f"记录 —— 这一格是**别人**写的（`paper step` 认领了它，"
                  f"或有人直连库写了行）。拒绝而不是接着写："
                  f"静默覆盖会让「谁算的这一天」永远说不清")
        run_id = _record(conn, account_id=account_id, asof=asof,
                         status=config.STATUS_REJECTED, code="nav_conflict",
                         reason=reason, now=now)
        return {"status": config.STATUS_REJECTED, "account_id": account_id,
                "asof": asof, "run_id": run_id, "reject_code": "nav_conflict",
                "reason": reason}
    try:
        out = _execute(conn, account=account, asof=asof, now=now, prices=prices)
    except config.ChannelSkip as exc:
        run_id = _record(conn, account_id=account_id, asof=asof,
                         status=config.STATUS_SKIPPED, code=exc.code,
                         reason=exc.reason, now=now)
        return {"status": config.STATUS_SKIPPED, "account_id": account_id,
                "asof": asof, "run_id": run_id, "skip_code": exc.code,
                "reason": exc.reason}
    except REJECTED as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        code = getattr(exc, "code", None) or type(exc).__name__
        run_id = _record(conn, account_id=account_id, asof=asof,
                         status=config.STATUS_REJECTED, code=str(code),
                         reason=str(reason), now=now)
        return {"status": config.STATUS_REJECTED, "account_id": account_id,
                "asof": asof, "run_id": run_id, "reject_code": str(code),
                "reason": str(reason)}
    out.update({"account_id": account_id, "asof": asof})
    return out
