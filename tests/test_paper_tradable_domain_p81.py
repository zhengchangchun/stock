"""P81 / D4：可下手域读数（`buying_power` 两键 ＋ `tradable_domain` ＋ 报告一行）。

「小面额也能下手」这件事此前没有任何可核对的数：池内一手成本分布、可下手只数、
最小可成交权重全要手工算。本站把它们摆出来 —— 而且**只摆一份**：
判据是 `engine.tradable_verdict`（AI 输入侧与账户读数调同一个函数），
数据是 `account_view.buying_power`（报告与页面取同一份投影）。

判据（逐条对应任务书 T4）：

| # | 判据 | 被摘掉后会红的实现 |
|---|---|---|
| ① | `buying_power.by_code[c].tradable` 与 `tradability.by_code[c].tradable` **同一输入同值** | 两处各判一次（D-37「同一页两个数」） |
| ② | 报告出现「池内可下手 X/Y 只」「最小可成交权重 z%」，且数字 == 数据块 | 渲染层自己再算一遍 |
| ③ | 缺数据（无池 / 无价 / 账户坏）⇒ 写「取不到」，**不填 0** | 把不可判定渲染成 0 |

数字一律**从被测模块取**（`account_view` / `decision_context_for`），不手抄。
"""

from __future__ import annotations

import pytest

from stocklab.paper import agent_decide, engine
from stocklab.paper import store as paper_store
from stocklab.paper.config import (ARM_AGENT, EXECUTOR_CHANNEL_A,
                                   PAPER_START_DATE)
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE
EARLIER = "2026-09-14"

CAL = ("2026-09-11", EARLIER, START)
BARS = {
    "000333": {EARLIER: 86.80, START: 87.23},
    "510300": {START: 4.523},
    "510880": {START: 3.382},
    "600900": {START: 28.08},
    "sh000300": {START: 4450.04},
}
POOL = tuple(c for c in BARS if c != "sh000300")   # 指数不是可投标的（不进候选池）


def _seed(c, *, with_pool: bool):
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES ('000333','000333','sz','main','stock',?)", (NOW,))
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sh','main',?,?)",
                  [(code, code, "etf" if code.startswith("51") else "stock", NOW)
                   for code in POOL if code not in ("000333", "sh000300")])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, series in BARS.items()
         for d, v in series.items()])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES (?, 'deposit', 20000.0, '本金', ?)", (EARLIER, NOW))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES (?, '000333','buy',86.80,100,5.09, '首笔', ?)",
              (EARLIER, NOW))
    c.commit()
    if with_pool:
        c.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
                  " created_at) VALUES (?,'light','{}',?)", (START, NOW))
        sid = c.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
        for code in POOL:
            c.execute("INSERT INTO candidate_members (snapshot_id, code, pool,"
                      " raw_score, adj_score, reason, risk_json, status, entered_at)"
                      " VALUES (?,?, 'short', 1,1,'夹具','{}','观察中',?)",
                      (sid, code, NOW))
        c.commit()


@pytest.fixture
def db(tmp_path):
    """有候选池的库：`arm-agent` 有一条净值行（`build_report` 只收有净值行的账户）。"""
    path = tmp_path / "p81-domain.db"
    init_db(path)
    c = connect(path)
    _seed(c, with_pool=True)
    engine.init_accounts(c, start_date=START, now=NOW)
    state = engine.arm_state_for(c, ARM_AGENT, START)
    paper_store.insert_nav(
        c, account_id=ARM_AGENT, date=START, cash=state["cash"],
        positions=[{"code": code, "qty": qty}
                   for code, qty in sorted(state["positions"].items())],
        market_value=state["total_assets"] - state["cash"],
        nav=state["total_assets"], drawdown=0.0, cum_cost=5.09, cum_return=0.0,
        net_deposits=state["net_deposits"], index_300_level=None,
        index_300_asof=None, now=NOW)
    c.close()
    return path


@pytest.fixture
def db_no_pool(tmp_path):
    """**没有**候选池的库：③ 用它验「取不到」那一支（不填 0）。"""
    path = tmp_path / "p81-domain-nopool.db"
    init_db(path)
    c = connect(path)
    _seed(c, with_pool=False)
    engine.init_accounts(c, start_date=START, now=NOW)
    state = engine.arm_state_for(c, ARM_AGENT, START)
    paper_store.insert_nav(
        c, account_id=ARM_AGENT, date=START, cash=state["cash"],
        positions=[{"code": code, "qty": qty}
                   for code, qty in sorted(state["positions"].items())],
        market_value=state["total_assets"] - state["cash"],
        nav=state["total_assets"], drawdown=0.0, cum_cost=5.09, cum_return=0.0,
        net_deposits=state["net_deposits"], index_300_level=None,
        index_300_asof=None, now=NOW)
    c.close()
    return path


# ---------- ① 两处同源 ----------

def test_t4_buying_power_and_tradability_agree_code_by_code(db):
    """①：同一个输入下，两处的 `tradable` **逐只同值**（这是 D-37 类事故的直接防线）。

    `buying_power`（账户读数/页面）与 `tradability`（AI 输入侧）是两个面，
    但判据只有 `engine.tradable_verdict` 一处 —— 各写一份的实现会在这里红。
    """
    c = connect(db)
    try:
        view = engine.account_view(c, ARM_AGENT, START)
        trad = engine.decision_context_for(c, arm=ARM_AGENT, asof=START)["tradability"]
        assert set(view["buying_power"]["by_code"]) == set(trad["by_code"]), \
            "两处的键集必须一样（池 ∩ 有价）"
        for code, row in view["buying_power"]["by_code"].items():
            assert row["tradable"] == trad["by_code"][code]["tradable"], code
            assert row["untradable_reason"] == \
                trad["by_code"][code]["untradable_reason"], code
            assert row["one_lot_cost"] == trad["by_code"][code]["one_lot_cost"]
            assert row["max_lots_affordable"] == \
                trad["by_code"][code]["affordable_lots"]
        # 两个顶层的「可下手子集」读数同源（计数与最小权重）。
        domain = view["buying_power"]["tradable_domain"]
        assert domain["n_tradable"] == trad["n_tradable"]
        assert domain["n_untradable"] == trad["n_untradable"]
        assert domain["min_tradable_weight_pct"] == trad["min_tradable_weight_pct"]
        # 夹具里 600519 不在池内、`600900` 一手 ¥2,816 是买得起的那端。
        assert view["buying_power"]["by_code"]["600900"]["tradable"] is True
    finally:
        c.close()


# ---------- ② 报告那一行 ----------

def test_t4_the_report_line_matches_the_data_block(db):
    """②：报告里出现「池内可下手 X/Y 只」「最小可成交权重 z%」，数字 == 数据块。"""
    c = connect(db)
    try:
        rep = engine.build_report(c, START)
        entry = next(a for a in rep["accounts"] if a["account_id"] == ARM_AGENT)
        domain = entry["tradable_domain"]
        assert domain["available"] is True
        # 报告里的数字来自 `account_view` 的同一份投影（不是渲染层重算的）。
        view = engine.account_view(c, ARM_AGENT, START)
        assert domain["n_tradable"] == view["buying_power"]["tradable_domain"][
            "n_tradable"]
        assert domain["min_tradable_weight_pct"] == view["buying_power"][
            "tradable_domain"]["min_tradable_weight_pct"]
        text = engine.render_report(rep)
        assert f"池内可下手 {domain['n_tradable']}/{domain['n_pool_codes']} 只" in text
        assert f"最小可成交权重 {domain['min_tradable_weight_pct']:.2f}%" in text
        assert f"一手成本中位数 ¥{domain['one_lot_cost_p50']:,.2f}" in text
        # 买不起的那只**点名**（若夹具里有买不起的）。
        for row in domain["untradable"]:
            assert f"买不起：{row['code']}" in text
    finally:
        c.close()


# ---------- ③ 取不到 = 取不到 ----------

def test_t4_missing_data_renders_as_unknown_not_zero(db_no_pool):
    """③：无池 ⇒ 报告写「取不到」，**不填 0**（页面/报告的既有降级纪律）。

    「不可判定」与「一只都买不起」是两个读数：前者是缺数据，后者是结论。
    填 0 会把前者伪装成后者。
    """
    c = connect(db_no_pool)
    try:
        rep = engine.build_report(c, START)
        entry = next(a for a in rep["accounts"] if a["account_id"] == ARM_AGENT)
        domain = entry["tradable_domain"]
        assert domain["available"] is False
        assert "候选池" in domain["reason"]
        text = engine.render_report(rep)
        assert "可下手域：**取不到**" in text
        assert "池内可下手" not in text, "取不到时不许打一行「可下手 0/0 只」"
    finally:
        c.close()


def test_t4_an_unreadable_account_degrades_instead_of_crashing_the_report(db):
    """③（续二）：账户口径读不出来（老账户 `params_json` 里没有 `initial_capital`）⇒
    报告**照常出**，这一节写「取不到」＋ 点名异常类型。

    这一条是**回归判据**：`build_report` 里多出来的这个块一旦把异常放出去，
    整份报告（以及 `/lab/paper`）就会因为一条读不出净入金的账户而 500 ——
    「报告里少一行是真的，500 是坏的」。
    """
    c = connect(db)
    try:
        paper_store.insert_account(
            c, account_id="arm-agent-v9", arm="agent", etf_target_pct=None,
            start_date=START, initial_cash=20000.0, initial_positions=[],
            initial_nav=20000.0,
            params={"executor": EXECUTOR_CHANNEL_A, "strategy_version": "v1"},
            now=NOW)
        paper_store.insert_nav(
            c, account_id="arm-agent-v9", date=START, cash=20000.0, positions=[],
            market_value=0.0, nav=20000.0, drawdown=0.0, cum_cost=0.0,
            cum_return=0.0, net_deposits=20000.0, index_300_level=None,
            index_300_asof=None, now=NOW)
        rep = engine.build_report(c, START)
        entry = next(a for a in rep["accounts"] if a["account_id"] == "arm-agent-v9")
        domain = entry["tradable_domain"]
        assert domain["available"] is False
        assert domain["type"] == "KeyError"
        assert "initial_capital" in domain["reason"]
        text = engine.render_report(rep)
        assert "可下手域：**取不到**" in text
    finally:
        c.close()


def test_t4_the_block_and_the_renderer_decline_to_guess(db):
    """③（续）：账户不存在 / 根本没有块 ⇒ 取不到，且不出现任何「0 只」的断言。"""
    c = connect(db)
    try:
        missing = engine.tradable_domain_block(c, "arm-agent-nope", START)
        assert missing["available"] is False and "不存在" in missing["reason"]
        assert any("取不到" in line
                   for line in engine.render_tradable_domain(missing))
        assert any("取不到" in line for line in engine.render_tradable_domain(None))
        for domain in (missing, None):
            for line in engine.render_tradable_domain(domain):
                assert "池内可下手" not in line
    finally:
        c.close()
