"""P65 / Q1-A：执行层的**资金闸门** —— 成交后现金不得为负、总敞口不得 > 100%。

| 判据 | 被摘掉后会红的实现 |
|---|---|
| 载荷合规（`Σw + cash == 100`）但叠加存量持仓后透支 ⇒ 具名 `CashShortfall` | `_settle` 直接 `cash += gross − fee`，允许为负 |
| 反例（**单变量**）：同一份决策，只把「存量持仓」这一维去掉 ⇒ 不再拒 | 拒绝其实来自别的闸门（越界 / 超 100） |
| 通路 A：整轮拒绝 ＋ **零写入**，只多一条 `rejected` 台账行 | 先落成交再校验 |
| `paper step` 那条路上转成**具名** `PaperError`，该臂当日没有净值行 | 让 `CashShortfall` 以 traceback 冒出去（读数上等同崩溃） |
| 网格：A1 的 picks 走完整执行路径后 `cash ≥ 0` 且总敞口 ≤ 100% | 闸门只在真库那一个形态上碰巧成立 |

真库依据（P64 §7.3 / ERROR_DIARY #73）：`m2_a1` v1.0.2 跑出
`cash = −4,490.01 / market_value = 24,024.00`（总敞口 122.8%）—— 目标敞口按
`total_assets × 18% × 5` 算，而 42% 的钱锁在**不在 picks 里**的存量持仓上。

数字一律**从被测模块取**（`agent_decide.CASH_TOL` / `_P65_*` 夹具常量），
不手抄一份到测试里。
"""

from __future__ import annotations

import json

import pytest

from stocklab.m2 import channel_a, config as m2_config
from stocklab.m2 import store as m2_store
from stocklab.paper import agent_decide, agent_spec
from stocklab.paper import engine as paper_engine
from stocklab.paper import store as paper_store
from stocklab.paper.config import ARM_KIND_AGENT
from stocklab.portfolio.prices import Price
from stocklab.store.db import connect
from tests.test_m2_channels import (ACCOUNT, DAY2, NOW, POOL, START, VERSION,
                                    _dump, _init_account,
                                    _init_account_without_the_seed_holding,
                                    _install, _nav_row, build_db,
                                    conn, db)  # noqa: F401

#: 真库 2026-09-23 的账户读数（P64 §7.3）：现金 ¥11,320 ＋ `000333 ×100 @ ¥82.47`。
P65_CASH = 11320.0
P65_HOLD_CLOSE = 87.60          # 夹具 `BARS` 里 000333 在 DAY2 的收盘价
P65_HOLD_QTY = 100

#: A1 的**真形态透支源**：一份**完全合规**的载荷（Σw + cash == 100），
#: 但按 `total_assets` 定敞口 —— 这正是 v1.0.2 的那条公式。
A1_LEVERAGE = """
def run(ctx):
    held = {h["code"] for h in ctx["holdings"]}
    codes = sorted({i["code"] for p in ("short", "mid", "long")
                    for i in ctx["candidates"].get(p, [])} - held)
    picks = codes[:3]
    if not picks:
        return {"picks": [], "cash_pct": 100.0, "schema_version": "t1"}
    w = round(60.0 / len(picks), 2)
    return {"picks": [{"code": c, "weight_pct": w,
                       "reason": "夹具：60% 敞口，不看存量持仓"}
                      for c in picks],
            "cash_pct": round(100.0 - w * len(picks), 2), "schema_version": "t1"}
"""


def _mark(code: str, price: float) -> Price:
    return Price(code=code, price=price, source="bars_daily", price_asof=DAY2,
                 detail="夹具")


def _buy(code: str, *, price: float, weight: float, total: float) -> dict:
    return {"code": code, "side": agent_decide.SIDE_BUY,
            "target_weight_pct": weight, "reason": "夹具：按 total_assets 定敞口",
            "target_value": round(total * weight / 100.0, 4), "price": price,
            "price_asof": DAY2, "price_source": "bars_daily",
            "current_qty": 0, "current_value": 0.0}


#: 夹具 `BARS` 在 DAY2 的收盘价（`000333` 那笔存量持仓按它计值）。
_DAY2_CLOSES = {"000333": 87.60, "510300": 4.550, "510880": 3.390}


def _marks() -> dict:
    return {code: _mark(code, price) for code, price in _DAY2_CLOSES.items()}


# ══════════════════════════════════════════════════════════════════════
# 决策级：闸门在说什么、不在说什么
# ══════════════════════════════════════════════════════════════════════


def _overdrawing_decision(*, marks: dict, positions: dict, cash: float) -> dict:
    """一份**载荷层完全合规**、但叠加存量持仓后透支的决策（单变量夹具）。"""
    total = round(cash + sum(float(marks[c].price) * int(q) for c, q in positions.items()), 4)
    items = [_buy("510300", price=4.550, weight=30.0, total=total),
             _buy("510880", price=3.390, weight=30.0, total=total)]
    return {"asof": DAY2, "decisions": items, "cash_pct": 40.0,
            "rationale": "夹具：60% 敞口", "total_assets": total}


def test_p65_the_gate_fires_on_a_payload_that_is_itself_compliant(conn):
    """载荷合规 ≠ 账上拿得出钱：叠加存量持仓后透支 ⇒ `CashShortfall`。"""
    marks = _marks()
    positions = {"000333": P65_HOLD_QTY}
    cash = P65_CASH
    decision = _overdrawing_decision(marks=marks, positions=positions, cash=cash)
    # 判据自证：**载荷本身**是合规的（Σw + cash_pct == 100）——
    # 所以下面那次拒绝不可能来自载荷层那道闸门。
    assert round(sum(d["target_weight_pct"] for d in decision["decisions"])
                 + decision["cash_pct"], 6) == 100.0

    with pytest.raises(agent_decide.CashShortfall) as exc:
        agent_decide.execute_decision(
            conn, arm=ACCOUNT, asof=DAY2, decision=decision, cash=cash,
            positions=dict(positions), marks=marks,
            total_assets=float(decision["total_assets"]))

    assert exc.value.code == "cash", "读数上必须能与载荷错分开"
    reason = exc.value.reason
    for needle in (ACCOUNT, DAY2, "可用现金", "越界", "code=cash"):
        assert needle in reason, f"消息里少了 {needle!r}：{reason}"


def test_p65_dropping_the_locked_holding_only_turns_the_same_payload_green(conn):
    """**反例（单变量）**：同一份决策，只把「存量持仓」这一维去掉 ⇒ 不再拒。

    唯一变的是 `positions` / `cash`：那 43.6% 从「锁在 `000333` 里」变成
    「本来就是现金」。少了这条，「拒绝」有可能来自别的闸门而本判据空转。
    """
    marks = _marks()
    positions = {"000333": P65_HOLD_QTY}
    cash = P65_CASH
    decision = _overdrawing_decision(marks=marks, positions=positions, cash=cash)

    free_positions: dict[str, int] = {}
    free_cash = float(decision["total_assets"])
    cash_after, positions_after, orders, _evals = agent_decide.execute_decision(
        conn, arm=ACCOUNT, asof=DAY2, decision=decision, cash=free_cash,
        positions=dict(free_positions), marks=marks,
        total_assets=float(decision["total_assets"]))

    assert orders, "同一个目标敞口在「全是现金」的账户上应当买得成"
    assert cash_after >= -agent_decide.CASH_TOL, cash_after
    market_value, _ = paper_engine.mark_to_market(positions_after, marks)
    assert market_value <= float(decision["total_assets"]) + 1e-6


# ══════════════════════════════════════════════════════════════════════
# 通路 A：整轮拒绝 ＋ 零写入
# ══════════════════════════════════════════════════════════════════════


def test_p65_channel_a_rejects_the_round_by_name_and_writes_nothing(conn):
    """A1 交一份会透支的载荷 ⇒ `rejected` / `reject_code='cash'` / **零写入**。"""
    _init_account(conn)
    _install(conn, "m2_a1", A1_LEVERAGE, version="p65")
    before = _dump(conn)
    decisions_before = conn.execute(
        "SELECT COUNT(*) FROM paper_agent_decisions").fetchone()[0]

    out = channel_a.run(conn, asof=DAY2, strategy_version=VERSION, now=NOW)

    assert out["status"] == m2_config.STATUS_REJECTED, out
    assert out["reject_code"] == "cash", out
    after = _dump(conn)
    assert paper_store.trades_on(conn, ACCOUNT, DAY2) == [], "拒绝路径落了成交"
    assert _nav_row(conn, ACCOUNT, DAY2) is None, "拒绝路径落了净值行"
    for table in ("paper_trades", "paper_nav_daily"):
        assert after[table] == before[table], f"{table} 被写入了"
    assert conn.execute(
        "SELECT COUNT(*) FROM paper_agent_decisions").fetchone()[0] \
        == decisions_before, "拒绝路径写下了决策行"
    runs = m2_store.list_runs(conn, channel=m2_config.CHANNEL_A,
                              account_id=ACCOUNT, asof=DAY2)
    assert [r["status"] for r in runs] == [m2_config.STATUS_REJECTED]
    assert "code=cash" in runs[0]["reason"], runs[0]["reason"]


def test_p65_dropping_the_locked_holding_turns_the_same_round_green(conn):
    """**反例**：同一份 A1、同一日，只把账户的存量持仓换成现金 ⇒ 跑到 `ran`。"""
    aid = _init_account_without_the_seed_holding(conn)
    _install(conn, "m2_a1", A1_LEVERAGE, version="p65")
    out = channel_a.run(conn, asof=DAY2, strategy_version="nohold", now=NOW)
    assert out["status"] == m2_config.STATUS_RAN, out
    assert paper_store.trades_on(conn, aid, DAY2), "反例该真的下单"
    assert out["cash"] >= 0.0, out


# ══════════════════════════════════════════════════════════════════════
# `paper step` 那条路：必须转成**具名** `PaperError`，不许漏成 traceback
# ══════════════════════════════════════════════════════════════════════

#: 走 `paper step` 的 AI 臂账户（`executor = agent_decision`）。
P65_ARM = "arm-agent-p65"
P65_MODEL = "claude-code"
P65_PROMPT = "b" * 64


@pytest.fixture
def agent_db(tmp_path):
    """一份**装了 AI 臂账户与一条会透支的决策**的库（真库只读，这里全在 tmp）。"""
    db = build_db(tmp_path / "gate.db")
    c = connect(db)
    try:
        base = next(a for a in paper_store.load_accounts(c)
                    if a["account_id"] == "arm-hold")
        paper_store.insert_account(
            c, account_id=P65_ARM, arm=ARM_KIND_AGENT, etf_target_pct=None,
            start_date=base["start_date"], initial_cash=base["initial_cash"],
            initial_positions=json.loads(base["initial_positions_json"]),
            initial_nav=base["initial_nav"],
            params={**json.loads(base["params_json"]),
                    paper_engine.EXECUTOR_KEY: paper_engine.EXECUTOR_AGENT_DECISION,
                    "strategy_version": "p65"},
            now=NOW)
        marks = _marks()
        payload = _overdrawing_decision(
            marks=marks, positions={"000333": P65_HOLD_QTY}, cash=P65_CASH)
        agent_decide.record_portfolio_decision(
            c, arm=P65_ARM, asof=DAY2, payload=payload, pool={},
            agent_kind=agent_spec.AGENT_KIND_LLM, model_id=P65_MODEL, prompt_sha256=P65_PROMPT,
            seed=1, context_sha256="f" * 64, now=NOW)
    finally:
        c.close()
    return db


def test_p65_paper_step_raises_a_named_paper_error_and_writes_no_nav(agent_db):
    """`paper step` 那条路：`PaperError`（具名、带金额）、该臂当日**没有净值行**。"""
    c = connect(agent_db)
    try:
        before = _dump(c)
        with pytest.raises(paper_engine.PaperError) as exc:
            paper_engine.agent_run(c, DAY2, now=NOW)
        reason = str(exc.value)
        assert P65_ARM in reason and DAY2 in reason and "越界" in reason, reason
        assert _nav_row(c, P65_ARM, DAY2) is None, "透支那天该臂不许有净值行"
        assert paper_store.trades_on(c, P65_ARM, DAY2) == []
        assert _dump(c)["paper_trades"] == before["paper_trades"], \
            "该臂的成交必须一起回滚（整日一个事务）"
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T4 网格：A1 的 picks 走完整执行路径后，现金与敞口都不越界
# ══════════════════════════════════════════════════════════════════════


def _p65_closes() -> tuple[float, ...]:
    return (31.26, 20.0, 10.0, 5.0, 3.0)


def test_p65_the_grid_never_overdraws_after_the_full_execution_path():
    """4 档总资产 × 4 档存量占比 × 若干个权重解 ⇒ 逐点 `cash ≥ 0`、敞口 ≤ 100%。

    这里把 A1 的 picks **真的**过一遍 `_weights_items → plan_orders → _settle
    → _gate_cash`（全是纯函数，不需要库），而不是只断言权重和 —— 「权重和
    对」推不出「账上拿得出钱」，那正是本站的病根。
    """
    from stocklab.m2.builtin import BUILTIN_PLUGINS, a1_pick

    a1 = BUILTIN_PLUGINS["m2_a1"]
    closes = _p65_closes()
    ranked = tuple(("C%d" % i, close) for i, close in enumerate(closes))
    checked = 0

    for total in (5_000.0, 19_567.0, 100_000.0, 1_000_000.0):
        for held_pct in (0.0, 10.0, 42.147, 80.0):
            hold_value = round(total * held_pct / 100.0, 4)
            positions = {} if held_pct == 0.0 else {"HOLD": 100}
            cash = round(total - hold_value, 4)
            holdings = [] if held_pct == 0.0 else [
                {"code": "HOLD", "qty": 100, "weight_pct": held_pct,
                 "market_value": hold_value, "close": round(hold_value / 100.0, 4)}]
            marks = {code: _mark(code, close) for code, close in ranked}
            marks["HOLD"] = _mark("HOLD", round(hold_value / 100.0, 4) or 1.0)
            ctx = {"candidates": {"short": [
                       {"code": code, "adj_score": 9.0 - i, "close": close,
                        "pool_reason": "夹具"} for i, (code, close) in enumerate(ranked)]},
                   "candidates_excluded": {}, "holdings": holdings, "cash": cash,
                   "total_assets": total}
            out = a1_pick_run(a1, ctx)
            label = f"total={total} held={held_pct}%"

            if not out["picks"]:
                continue                      # 空仓：没有成交可结算
            items = channel_a._weights_items(
                out["picks"], positions=dict(positions), marks=marks,
                total_assets=total, rationale="P65 网格")
            decision = {"asof": DAY2, "decisions": items,
                        "cash_pct": float(out["cash_pct"]), "rationale": "网格",
                        "total_assets": total}
            orders, _evals = agent_decide.plan_orders(
                decision=decision, cash=cash, positions=dict(positions),
                marks=marks, total_assets=total,
                asset_classes={c: "stock" for c in marks})
            cash_after, positions_after = agent_decide._settle(
                cash, dict(positions), orders)
            agent_decide._gate_cash(
                arm="grid", asof=DAY2, cash_before=cash, cash_after=cash_after,
                positions=positions_after, marks=marks, total_assets=total)
            assert cash_after >= -agent_decide.CASH_TOL, label
            market_value, _ = paper_engine.mark_to_market(positions_after, marks)
            assert market_value <= total * (1.0 + agent_decide.EXPOSURE_TOL), label
            checked += 1

    assert checked >= 8, f"网格只跑到了 {checked} 个有成交的点，判据会空转"


def a1_pick_run(source: str, ctx: dict) -> dict:
    """用真执行器跑一份 A1 源文本（走静态预检那条路，不绕过去）。"""
    from stocklab.plugin import contract, runtime
    return contract.validate_return(
        "m2_a1", runtime.load_script(source, plugin_id="m2_a1")(ctx))
