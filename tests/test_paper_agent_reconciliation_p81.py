"""P81 / D3：意图 vs 落地**逐臂对账**（`engine.agent_block.reconciliation`）。

本站要修的是「不可见」：P79 已经把「AI 想动而没动成」的理由落进了
`paper_agent_evals`，但没有任何消费者。于是「AI 意图 67% 仓位、账户 0% 现金」
（P79 §0.1 的真实故障）在页面上依然看不见。

判据（逐条对应任务书 T3）：

| # | 判据 | 被摘掉后会红的实现 |
|---|---|---|
| ① | 有决策的**每一条臂**各一行（不写死 `arm-agent`），数字与台账对得上 | 只读 `arm-agent` ⇒ 页面长期展示一条空臂 |
| ② | 一条决策都没有 ⇒ `[]` ＋ `note`，**不报错** | 空列表冒充「全都落地了」 |
| ③ | 意图腿数 / 成交笔数 / 未成交腿**各自如实**（不 `min()` 抹平、不互相顶替） | 用成交笔数去顶意图腿数 |
| ④ | `agent_block` 的既有键**逐键仍在**（老读者不因本站而崩） | 顺手把既有键改名/挪位 |
| ⑤ | PIT：`asof` 之前的决策**不进**对账表 | 历史视图里显示未来那一天的决策 |

夹具里的数字一律**从被测模块取**（`store.load_agent_evals` / `store.trades_on`），
不手抄一份到测试里。
"""

from __future__ import annotations

import pytest

from stocklab.paper import agent_decide, engine
from stocklab.paper import store as paper_store
from stocklab.paper.config import PAPER_START_DATE
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE
EARLIER = "2026-09-14"
MODEL = "deepseek/deepseek-v4-pro"
PROMPT = "d" * 64

CAL = ("2026-09-11", EARLIER, START)
BARS = {
    "000333": {EARLIER: 86.80, START: 87.23},
    "510300": {START: 4.523},
    "510880": {START: 3.382},
    "600900": {START: 28.08},
    "603868": {START: 31.26},
    "sh000300": {START: 4450.04},
}
POOL = tuple(BARS)
#: 两条被点名的臂（kind 都是 `agent`，与真库的 `arm-agent-ds-v1/-v2` 同形）。
ARM_WITH_EVALS = "arm-agent-p81b"
ARM_FILLED = "arm-agent-p81a"

#: 目标市值**低于一手含费成本**的四只（照 P79 的真库形状：`< 1 手 ⇒ 不动`）。
UNAFFORDABLE = {"600900": 2710.0, "603868": 3010.0}
#: 目标市值**高于一手含费成本**的两只（ETF 便宜，500 元就够一手）。
AFFORDABLE = {"510300": 500.0, "510880": 500.0}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p81-recon.db"
    init_db(path)
    c = connect(path)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sh','main',?,?)",
                  [(code, code, "etf" if code.startswith("51") else "stock", NOW)
                   for code in POOL if code != "sh000300"])
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
    c.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
              " created_at) VALUES (?,'light','{}',?)", (START, NOW))
    sid = c.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
    for code in POOL:
        c.execute("INSERT INTO candidate_members (snapshot_id, code, pool, raw_score,"
                  " adj_score, reason, risk_json, status, entered_at)"
                  " VALUES (?,?, 'short', 1,1,'夹具','{}','观察中',?)",
                  (sid, code, NOW))
    c.commit()
    engine.init_accounts(c, start_date=START, now=NOW)
    c.close()
    return path


@pytest.fixture
def bare_db(tmp_path):
    """一条决策都没有的库（只有 `init_accounts` 建的臂）—— ② 用它。"""
    path = tmp_path / "p81-recon-bare.db"
    init_db(path)
    c = connect(path)
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES ('000333','000333','sz','main','stock',?)", (NOW,))
    c.execute("INSERT INTO trading_calendar (date, is_open, source, created_at)"
              " VALUES (?,1,'tencent',?)", (START, NOW))
    c.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
              " adj_mode, source, fetched_at)"
              " VALUES ('000333',?,'87.23','87.23','87.23','87.23',100,'none','x',?)",
              (START, NOW))
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES (?, 'deposit', 20000.0, '本金', ?)", (EARLIER, NOW))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES (?, '000333','buy',86.80,100,5.09, '首笔', ?)",
              (EARLIER, NOW))
    c.commit()
    engine.init_accounts(c, start_date=START, now=NOW)
    c.close()
    return path


def _enroll(c, arm):
    engine.enroll_agent_arm(c, arm_name=arm, model_id=MODEL, prompt_sha256=PROMPT,
                            now=NOW, start_date=START)


def _record(c, *, arm, targets):
    """按目标市值写一条操盘决策（权重由**账户自己的总资产**折出来）。"""
    state = engine.arm_state_for(c, arm, START)
    pool = agent_decide.pool_for(c, START)
    marks = {**engine.resolve_marks(c, set(pool["codes"]) | set(state["positions"]),
                                    START), **state["marks"]}
    total = state["total_assets"]
    legs = [{"code": code, "side": "buy",
             "target_weight_pct": round(money / total * 100.0, 2),
             "reason": f"{code}：夹具（目标市值 ¥{money:,.2f}）"}
            for code, money in targets.items()]
    weights = round(sum(d["target_weight_pct"] for d in legs), 6)
    payload = agent_decide.validate_payload(
        asof=START, payload={"asof": START, "decisions": legs,
                             "cash_pct": round(100.0 - weights, 6),
                             "rationale": "夹具：P81 T3 对账"},
        pool_codes=set(pool["codes"]), held_qty=state["positions"], marks=marks,
        total_assets=total)
    return agent_decide.record_portfolio_decision(
        c, arm=arm, asof=START, payload=payload, pool=pool, agent_kind="llm",
        model_id=MODEL, prompt_sha256=PROMPT, seed=0, context_sha256="e" * 64,
        now=NOW)


# ---------- ① 逐臂一行，数字对得上 ----------

def test_t3_every_arm_with_a_decision_gets_its_own_row(db):
    """①：两条有决策的臂各一行 —— **不写死 `arm-agent`**（那是页面长期展示空臂的成因）。

    一条「买得起两笔」的臂（成交 2 笔、无 evals）＋ 一条「全买不起」的臂（0 笔成交、
    evals 2 行）：两种形状都要在里面，且每个数都能在台账里对上。
    """
    c = connect(db)
    try:
        _enroll(c, ARM_FILLED)
        _enroll(c, ARM_WITH_EVALS)
        _record(c, arm=ARM_FILLED, targets=AFFORDABLE)
        _record(c, arm=ARM_WITH_EVALS, targets=UNAFFORDABLE)
        engine.agent_run(c, START, now=NOW, arms=[ARM_FILLED, ARM_WITH_EVALS])

        recon = engine.agent_block(c, START)["reconciliation"]
        by_arm = {r["arm"]: r for r in recon}
        assert set(by_arm) == {ARM_FILLED, ARM_WITH_EVALS}, \
            "只认 `arm-agent` 的实现会在这里红"
        for arm, want_planned in ((ARM_FILLED, len(AFFORDABLE)),
                                  (ARM_WITH_EVALS, len(UNAFFORDABLE))):
            row = by_arm[arm]
            trades = paper_store.trades_on(c, arm, START)
            evals = paper_store.load_agent_evals(c, arm=arm, asof=START)
            assert row["asof"] == START
            assert row["n_legs_planned"] == want_planned
            assert row["n_legs_filled"] == len(trades)
            assert row["n_legs_unfilled"] == len(evals)
            assert row["n_evals"] == len(evals)
            assert [u["code"] for u in row["unfilled"]] == \
                [e["code"] for e in evals]
        # 两种形状确实都在（否则上面那组恒等式可以靠「都是 0」蒙过）。
        assert by_arm[ARM_FILLED]["n_legs_filled"] == len(AFFORDABLE)
        assert by_arm[ARM_FILLED]["n_legs_unfilled"] == 0
        assert by_arm[ARM_WITH_EVALS]["n_legs_unfilled"] == len(UNAFFORDABLE)
        assert by_arm[ARM_WITH_EVALS]["n_legs_filled"] == 0
        # 理由原样带出来（不经对账表重算）。
        reasons = {u["code"]: u["reason"] for u in by_arm[ARM_WITH_EVALS]["unfilled"]}
        assert all("< 1 手" in r for r in reasons.values()), reasons
    finally:
        c.close()


# ---------- ② 一条决策都没有 ----------

def test_t3_no_decision_at_all_is_an_empty_list_with_a_note(bare_db):
    """②：没有决策 ⇒ `reconciliation == []` ＋ `note`，**不报错**。

    空列表**不是**「全都落地了」：没有决策就没有「意图」可比 —— 这句话必须写出来，
    否则页面上的空表会被读成「AI 说的都做到了」。
    """
    c = connect(bare_db)
    try:
        block = engine.agent_block(c, START)
        assert block["reconciliation"] == []
        assert "没有任何一条" in block["reconciliation_note"]
    finally:
        c.close()


# ---------- ③ 三个数各自如实 ----------

def test_t3_planned_filled_unfilled_are_three_independent_counts(db):
    """③：**同一个陷阱**——意图 4 条、成交 2 笔、未成交 2 条，三个数各自报。

    这条把 `min(planned, filled)` / 「用成交数顶意图数」那类抹平判红：
    2+2=4 不是巧合，而是「计划与落地之间的差额」本身就是要看的读数。
    """
    c = connect(db)
    try:
        _enroll(c, ARM_WITH_EVALS)
        _record(c, arm=ARM_WITH_EVALS, targets={**UNAFFORDABLE, **AFFORDABLE})
        engine.agent_run(c, START, now=NOW, arms=[ARM_WITH_EVALS])
        row = engine.agent_block(c, START)["reconciliation"][0]
        trades = paper_store.trades_on(c, ARM_WITH_EVALS, START)
        evals = paper_store.load_agent_evals(c, arm=ARM_WITH_EVALS, asof=START)
        assert row["n_legs_planned"] == 4, "载荷四条腿（这是夹具前提）"
        assert row["n_legs_filled"] == len(trades) == 2, \
            [t["code"] for t in trades]
        assert row["n_legs_unfilled"] == len(evals) == 2
        assert row["n_evals"] == row["n_legs_unfilled"] == 2
        assert row["n_legs_filled"] + row["n_legs_unfilled"] == row["n_legs_planned"]
        assert {u["code"] for u in row["unfilled"]} == set(UNAFFORDABLE)
    finally:
        c.close()


# ---------- ④ 既有键一个不少 ----------

#: P52/P79 起 `agent_block` 的既有键集（**冻结副本**）—— 老读者（页面 / `paper show`
#: 的下游 / `--json` 的脚本）不能因为本站加了对账段而崩。
EXISTING_KEYS: tuple[str, ...] = (
    "arm", "asof", "spec", "spec_sha256", "stop_loss_line", "change_space",
    "n_decisions", "n_spec_versions", "last_decision_asof", "last_decision",
    "n_reviews", "n_trials_total", "n_rejected", "max_trials_per_review",
    "rebalance_cadence", "first_asof", "last_asof", "history",
    "delta_vs_random", "delta_vs_random_available", "delta_vs_random_note",
    "counter_arm", "evidence_note",
)


def test_t3_the_existing_keys_are_all_still_there(db):
    """④：`agent_block` 的既有键**逐键仍在**（本站只增不减）。"""
    c = connect(db)
    try:
        block = engine.agent_block(c, START)
        missing = [k for k in EXISTING_KEYS if k not in block]
        assert missing == [], f"既有键被本站弄丢了：{missing}"
        assert block["arm"] == "arm-agent"
        assert "reconciliation" in block and "reconciliation_note" in block
    finally:
        c.close()


# ---------- ⑤ PIT：历史视图不显示未来的决策 ----------

def test_t3_a_historical_asof_does_not_show_a_later_decision(db):
    """⑤：`asof` 早于决策日 ⇒ 那一天的对账表是**空的**（`[]` ＋ note）。

    页面/`paper show` 会拿历史 asof 出图：把 `<= asof` 之外的决策读进来，
    等于在历史截图里显示未来 —— 与各臂净值同一条 PIT 纪律。
    """
    c = connect(db)
    try:
        _enroll(c, ARM_FILLED)
        _record(c, arm=ARM_FILLED, targets=AFFORDABLE)
        engine.agent_run(c, START, now=NOW, arms=[ARM_FILLED])
        assert engine.agent_block(c, EARLIER)["reconciliation"] == []
        assert len(engine.agent_block(c, START)["reconciliation"]) == 1
    finally:
        c.close()
