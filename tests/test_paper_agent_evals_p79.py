"""P79 / D3：AI 臂**未成交腿**留痕（`paper_agent_evals`）。

本站要修的第二个故障是**不可见**：`plan_orders` 早就算出了「差额 < 1 手 → 不动」
这类理由（`Decision(action="hold", reason=...)`），而 agent 那条执行路径
（`engine._step_all`）把它用下划线丢掉了 —— 页面上只看到「这条臂没动」，
看不到「为什么没动」（对比静态臂的 `evaluate`，它一直是留痕的）。

判据（逐条对应任务书 T2）：

| # | 判据 | 被摘掉后会红的实现 |
|---|---|---|
| ① | 同 `(arm, asof, code)` 二次落库不新增、不报错 | 每次调用都插一行 / 一天的事务被重复写炸 |
| ② | `UPDATE` / `DELETE` / `INSERT OR REPLACE` 被触发器拒 | 改历史理由（= 伪造「当时为什么没动」） |
| ③ | 09-23 ds-v1 那种「4 笔全不足一手」⇒ 落 4 行、reason 含「< 1 手」 | `_evals` 被下划线丢掉（本站之前的实现） |
| ④ | 静态臂 / m2 通路的 evals **不误落** | 把「纪律臂没动」也算成「AI 说了没做到」 |
| ⑤ | 迁移幂等可重入，触发器形状与 `paper_agent_decisions` 同款 | 漂移的库看着有表、其实能改历史行 |
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from stocklab.paper import agent_decide, engine
from stocklab.paper import store as paper_store
from stocklab.paper.config import (ARM_KIND_AGENT, EXECUTOR_CHANNEL_A,
                                   PAPER_START_DATE)
from stocklab.store.db import connect
from stocklab.store.migrate import (agent_evals_needs_p79, ensure_schema, init_db,
                                    migrate_p79_agent_evals)

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE
ARM = "arm-agent-p79t2"
MODEL = "deepseek/deepseek-v4-pro"
PROMPT = "c" * 64

CAL = ("2026-09-11", "2026-09-14", START)
#: 起跑那三只（静态臂要用到白名单 ETF 的价）＋ 09-23 那四只「一手买不起」的票
#: （真库 `arm-agent-ds-v1` 的 picks，价格取真库当日收盘）。
BARS = {
    "000333": {"2026-09-14": 86.80, START: 87.23},
    "510300": {START: 4.523},
    "510880": {START: 3.382},
    "600900": {START: 28.08},
    "603868": {START: 31.26},
    "002415": {START: 33.11},
    "000651": {START: 38.36},
}
POOL = tuple(BARS)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p79-evals.db"
    init_db(path)
    c = connect(path)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sh','main',?,?)",
                  [(code, code, "etf" if code.startswith("51") else "stock", NOW)
                   for code in POOL])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, series in BARS.items()
         for d, v in series.items()])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (NOW,))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-14','000333','buy',86.80,100,5.09,"
              " '首笔',?)", (NOW,))
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
    engine.enroll_agent_arm(c, arm_name=ARM, model_id=MODEL, prompt_sha256=PROMPT,
                            now=NOW, start_date=START)
    c.close()
    return path


def _inputs(c, arm=ARM):
    state = engine.arm_state_for(c, arm, START)
    pool = agent_decide.pool_for(c, START)
    marks = {**engine.resolve_marks(c, set(pool["codes"]) | set(state["positions"]),
                                    START), **state["marks"]}
    return state, pool, marks


def _record_decision(c, *, legs, cash_pct, arm=ARM, rationale="夹具：P79 T2"):
    """按**真库 09-23 的形状**写一条决策：四条买入腿、每只目标市值 < 一手含费。"""
    state, pool, marks = _inputs(c, arm)
    payload = agent_decide.validate_payload(
        asof=START, payload={"asof": START, "decisions": legs, "cash_pct": cash_pct,
                             "rationale": rationale},
        pool_codes=set(pool["codes"]), held_qty=state["positions"], marks=marks,
        total_assets=state["total_assets"])
    return agent_decide.record_portfolio_decision(
        c, arm=arm, asof=START, payload=payload, pool=pool,
        agent_kind="llm", model_id=MODEL, prompt_sha256=PROMPT, seed=0,
        context_sha256="a" * 64, now=NOW), state


#: 09-23 `arm-agent-ds-v1` 那四笔买入腿的**目标市值**（真库只读探针原文）。
#: 夹具账户总资产 ¥20,037.91 ⇒ 按比例折成权重（都落在「一手买不起」上：
#: 四只的一手含费成本都 ≥ ¥2,815）。
DRAWN_TARGETS = {"600900": 2710.0, "603868": 3010.0, "002415": 3180.0,
                 "000651": 3690.0}


def _expensive_legs(state):
    total = state["total_assets"]
    legs = []
    for code, target in DRAWN_TARGETS.items():
        legs.append({"code": code, "side": "buy",
                     "target_weight_pct": round(target / total * 100.0, 2),
                     "reason": f"{code}：池内标的，目标 ≈ 一手以下（夹具复刻 09-23）"})
    weights = round(sum(d["target_weight_pct"] for d in legs), 6)
    return legs, round(100.0 - weights, 6)


# ---------- ① 幂等 ----------

def test_t2_the_same_arm_asof_code_lands_once(db):
    """①：同 `(arm, asof, code)` 落两次 ⇒ 仍 1 行、不报错（`INSERT OR IGNORE`）。

    幂等的必要性来自「同一天重跑 `paper agent run`」：那不该炸，也不该多出一行。
    判据用**业务判据 + 结构性判据**两层（`UNIQUE(arm, asof, code)` 是兜底）。
    """
    d = paper_rules_decision()
    c = connect(db)
    try:
        store_id = paper_store.insert_agent_eval(c, arm=ARM, asof=START,
                                                 decision=d, now=NOW)
        again = paper_store.insert_agent_eval(c, arm=ARM, asof=START,
                                              decision=d, now=NOW)
        rows = paper_store.load_agent_evals(c, arm=ARM, asof=START)
        assert len(rows) == 1, rows
        assert rows[0]["eval_id"] == store_id
        assert again in (0, store_id), "重复落库不该报错（OR IGNORE 语义）"
    finally:
        c.close()


def paper_rules_decision():
    from stocklab.paper.rules import _hold
    return _hold("600900", "目标 ¥2,710.00 与现市值 ¥0.00 差 ¥2,710.00 < 1 手"
                          "（¥2,814.63）→ **不动**；目标**未达成**，如实上报",
                 constraints=("lot_100", "target_weight_from_ledger"))


# ---------- ② append-only 触发器 ----------

@pytest.mark.parametrize("sql", [
    "UPDATE paper_agent_evals SET reason = '改过'",
    "DELETE FROM paper_agent_evals",
])
def test_t2_append_only_triggers_reject_update_and_delete(db, sql):
    """②：`UPDATE` / `DELETE` 一律被触发器拒（理由改不得 —— 那是结论）。

    `recursive_triggers` 是**第三条** SQL（`INSERT OR REPLACE` 的隐式 DELETE）
    能成立的原因（ERROR_DIARY #6）：不开它，隐式删行不触发 DELETE 触发器，
    覆盖会静默成功 —— 于是「append-only」成了空话。
    """
    c = connect(db)
    try:
        paper_store.insert_agent_eval(c, arm=ARM, asof=START,
                                      decision=paper_rules_decision(), now=NOW)
        assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 1, \
            "连接必须打开 recursive_triggers（否则 INSERT OR REPLACE 拦不住）"
        with pytest.raises(sqlite3.Error, match="append-only"):
            c.execute(sql)
    finally:
        c.close()


def test_t2_insert_or_replace_cannot_bypass_the_trigger(db):
    """②（续）：`INSERT OR REPLACE` 靠**隐式 DELETE** 解唯一冲突 —— 也必须被拒。"""
    c = connect(db)
    try:
        paper_store.insert_agent_eval(c, arm=ARM, asof=START,
                                      decision=paper_rules_decision(), now=NOW)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute(
                "INSERT OR REPLACE INTO paper_agent_evals (eval_id, arm, asof, code,"
                " action, reason, constraints_json, raw, created_at)"
                " VALUES (1, ?, ?, '600900', 'hold', '覆盖', '[]', '{}', ?)",
                (ARM, START, NOW))
    finally:
        c.close()


# ---------- ③ 09-23 那种「4 笔全不足一手」 ----------

def test_t2_the_four_unaffordable_legs_land_four_rows(db):
    """③（本文件的核心读数）：四条买入腿全部「差 < 1 手 → 不动」⇒ 落 **4 行**。

    没有这条，`_evals` 被下划线丢掉这件事在页面上是不可见的（本站要修的故障 3）。
    断言同时钉住：`code` 是那四只、`reason` 里写着「< 1 手」、`action` 是 `hold`、
    `raw` 是整条 `Decision` 的 JSON。
    """
    c = connect(db)
    try:
        state = engine.arm_state_for(c, ARM, START)
        legs, cash_pct = _expensive_legs(state)
        agent_decide_id, _ = _record_decision(c, legs=legs, cash_pct=cash_pct)
        assert agent_decide_id > 0
        engine.agent_run(c, START, now=NOW, arms=[ARM])
        rows = paper_store.load_agent_evals(c, arm=ARM, asof=START)
        trades = paper_store.trades_on(c, ARM, START)
        assert trades == [], "夹具的前提是四笔全不足一手（不该有成交）"
        assert len(rows) == 4, [(r["code"], r["reason"][:40]) for r in rows]
        assert {r["code"] for r in rows} == set(DRAWN_TARGETS)
        for r in rows:
            assert r["action"] == "hold"
            assert "< 1 手" in r["reason"], r["reason"]
            assert json.loads(r["constraints_json"])  # 约束代号非空
            raw = json.loads(r["raw"])
            assert raw["reason"] == r["reason"] and raw["code"] == r["code"]
    finally:
        c.close()


def test_t2_the_successful_legs_do_not_land_evals(db):
    """反向自检：**成交了的腿不进 evals**（不是无脑记账）。

    用与 ③ 同一份夹具，但把目标权重抬到「买得起一手」⇒ 4 笔成交、`evals` 为空。
    这一条防的是「把所有腿都写进 evals」那种实现（它也能让 ③ 绿）。
    """
    c = connect(db)
    try:
        state = engine.arm_state_for(c, ARM, START)
        total = state["total_assets"]
        # 挑两只**最便宜**的（现金 ¥11,314.91 够）：目标给到「一手含费成本」之上
        # ⇒ `plan_target_weight` 会真的下单。
        codes = sorted(DRAWN_TARGETS, key=one_lot_money)[:2]
        legs = [{"code": code, "side": "buy",
                 "target_weight_pct": round(one_lot_money(code) * 1.05 / total * 100.0, 2),
                 "reason": f"{code}：目标 ≥ 一手含费（对照臂）"}
                for code in codes]
        cash_pct = round(100.0 - sum(d["target_weight_pct"] for d in legs), 6)
        _record_decision(c, legs=legs, cash_pct=cash_pct)
        engine.agent_run(c, START, now=NOW, arms=[ARM])
        assert paper_store.load_agent_evals(c, arm=ARM, asof=START) == []
        assert len(paper_store.trades_on(c, ARM, START)) == len(codes)
    finally:
        c.close()


def one_lot_money(code) -> float:
    """夹具里那几只的一手含费成本（从**被测模块**取，不手抄数字）。"""
    from stocklab.paper.rules import one_lot_cost
    return one_lot_cost(price=float(BARS[code][START]), asset_class="stock")


# ---------- ④ 不误落 ----------

def test_t2_static_arms_never_land_evals(db):
    """④：静态纪律臂走 `_plan_steps`，它的 `evaluate` 结果**不进**这张表。

    两种读数必须能分开：静态臂的「不动」是纪律条文的读数，
    AI 臂的「不动」是「它说了但没做到」—— 混进一张表就再也分不开了。
    """
    c = connect(db)
    try:
        engine.step(c, START, now=NOW)
        assert paper_store.load_agent_evals(c) == [], \
            "静态臂的 evals 不该落进 AI 臂的未成交腿台账"
        arms = {a["account_id"] for a in paper_store.load_accounts(c)}
        assert any(a.startswith("arm-discipline-") for a in arms)
    finally:
        c.close()


def test_t2_the_m2_channel_account_never_lands_evals(db):
    """④（续）：通路 A 的账户（`executor = m2_channel_a`）也不落这张表。

    它的日终**不走** `_step_all`（`m2/channel_a.py` 自己写 `insert_nav`），
    但它的账户形态会经过同一条 `has_rules` 分支 —— 那条分支的 `_evals` 仍是
    下划线。这里就让那条分支在**没有操盘决策**的账户上跑一遍，证明它不落。
    """
    c = connect(db)
    try:
        paper_store.insert_account(
            c, account_id="arm-agent-v1", arm=ARM_KIND_AGENT, etf_target_pct=None,
            start_date=START, initial_cash=20000.0, initial_positions=[],
            initial_nav=20000.0,
            params={"executor": EXECUTOR_CHANNEL_A, "strategy_version": "v1",
                    "initial_capital": 20000.0, "seed_fee": 0.0}, now=NOW)
        account = next(a for a in paper_store.load_accounts(c)
                       if a["account_id"] == "arm-agent-v1")
        engine._step_all(c, START, accounts=[account], now=NOW, prices=None,
                         claim_handover=False)
        assert paper_store.load_agent_evals(c, arm="arm-agent-v1") == []
        assert paper_store.nav_exists(c, "arm-agent-v1", START), "该日终确实跑过了"
    finally:
        c.close()


# ---------- ⑤ 迁移幂等 / 可重入 ----------

def test_t2_the_migration_is_idempotent_and_repairs_a_drifted_db(db):
    """⑤：`migrate_p79_agent_evals` 幂等、可重入；表/触发器漂移时能补回来。

    - 正常库 ⇒ 零动作（表与触发器都在）；
    - 触发器被 DROP 过 ⇒ 探测报「待迁移」、函数补回、再跑一遍零动作。
    """
    c = connect(db)
    try:
        assert agent_evals_needs_p79(c) is False
        assert migrate_p79_agent_evals(c) == []

        c.execute("DROP TRIGGER trg_paper_agent_evals_no_update")
        c.execute("DROP TRIGGER trg_paper_agent_evals_no_delete")
        c.commit()
        assert agent_evals_needs_p79(c) is True
        assert migrate_p79_agent_evals(c) == ["paper_agent_evals.triggers"]
        assert agent_evals_needs_p79(c) is False
        assert migrate_p79_agent_evals(c) == []
        # `ensure_schema`（任一写库入口的守护）也认这张表 ⇒ 不把漂移的库前滚。
        assert ensure_schema(db) == []
    finally:
        c.close()


def test_t2_the_table_shape_matches_the_decisions_ledger(db):
    """⑤（续）：`UNIQUE(arm, asof, code)` 在，触发器名字/形状与决策台账同款。"""
    c = connect(db)
    try:
        ddl = c.execute("SELECT sql FROM sqlite_master WHERE name ="
                        " 'paper_agent_evals'").fetchone()[0]
        assert "UNIQUE (arm, asof, code)" in ddl
        trig = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE"
            " 'trg_paper_agent_evals%'")}
        assert trig == {"trg_paper_agent_evals_no_update",
                        "trg_paper_agent_evals_no_delete"}
        # 空串而不是 NULL：`UNIQUE` 里 NULL 互不相等，用 NULL 会让重复落库不报错。
        assert "code             TEXT NOT NULL" in ddl
    finally:
        c.close()
