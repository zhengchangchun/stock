"""P84 / T4：`market` ＋ `own_history` 两块**进上下文与指纹**（只增键）。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| `DECISION_HASHED_KEYS` 末尾两键、前 12 键**逐位不变** | 顺手重排 / 改掉既有键 |
| `build_context`（spec 上下文）键集与指纹**逐位不变** | 给纪律臂那条也塞两块 |
| 注入什么就进什么 | 注入点被忽略，永远现算 |
| 塞 `asof` 之后的行 → 两块与 sha 逐字节不变（PIT） | 少了 `<= asof` 过滤 |
| 同输入两次调用 → 逐字节相同（确定性） | 块里混进 `now()` |
| 100 天 / 50 笔 / 20 条 ⇒ 20 / 10 / 5（K6） | 把全量塞进上下文 |
| 两块内没有前向字段（K5） | 往里塞 `signal` / `prob` |
| `NON_GOALS` 第 1 条**逐字**是 K5 文案 | 只改了实现没改「写给它看的禁令」 |
"""

from __future__ import annotations

import json

import pytest

from stocklab.paper import agent_context, engine, own_history
from stocklab.paper.config import ARM_AGENT, PAPER_START_DATE
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE
ARM = ARM_AGENT

#: P84 **之前**的 `DECISION_HASHED_KEYS`（冻结副本）—— 反向对照必须是改动前实现的
#: 一字不改副本，不能调用现役实现（那是同义反复）。
PRE_P84_HASHED_KEYS: tuple[str, ...] = (
    "arm", "asof", "account", "marks", "index_300", "pool", "guardrails",
    "tradability", "objective", "disclosure", "non_goals", "counter_arm",
)

#: K5 的**逐字**文案（照任务书 §0.5 抄，不是从实现里取 —— 同义反复的对照没有意义）。
K5_NON_GOAL_0 = (
    "不做涨跌预测：上下文里不许出现任何**前向**字段（模型概率、预测价/目标价、"
    "信号分、评级、买卖建议）；行情统计（指数已实现收益、当日涨跌家数、估值中位数、"
    "当日资金净流入）只作**状态描述**，不得当作方向信号 —— D-34 之后 AI 臂的决策空间"
    "是「方向＋仓位」，与纪律臂「禁方向择时」不是同一套规则，后者由 "
    "tests/test_paper_discipline_guard.py 按臂分作用域约束"
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p84-context.db"
    init_db(path)
    c = connect(path)
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES ('000333','美的集团','sh','main','stock',?)", (NOW,))
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in ("2026-09-11",
                                                                  "2026-09-14")])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW)
         for code, series in {"000333": {"2026-09-14": 86.80, START: 87.23},
                              "sh000300": {"2026-09-14": 4455.0, START: 4450.04},
                              "sh000905": {"2026-09-14": 7550.0, START: 7561.59}}
         .items() for d, v in series.items()])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (NOW,))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-14','000333','buy',86.80,100,5.09,"
              " '首笔',?)", (NOW,))
    c.commit()
    engine.init_accounts(c, start_date=START, now=NOW)
    # `init_accounts` 只给五条静态臂落净值 —— AI 臂的净值行由 `paper step` 出。
    # 这里补一行（@起跑日）好让 `own_history` 不是空壳。
    c.execute(
        "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " index_300_level, index_300_asof, created_at)"
        " VALUES (?,?,0.0,'[{\"code\":\"000333\",\"qty\":100}]',8723.0,8723.0,"
        " -0.05,5.09,0.0,20043.0,4450.04,?,?)", (ARM, START, START, NOW))
    c.commit()
    c.close()
    return path


def _context(db) -> dict:
    c = connect(db)
    try:
        return engine.decision_context_for(c, arm=ARM, asof=START)
    finally:
        c.close()


def _blob(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


# ---------- ① 键集与顺序 ----------

def test_t4_hashed_keys_append_two_and_keep_the_first_twelve_verbatim():
    keys = agent_context.DECISION_HASHED_KEYS
    assert keys[-2:] == ("market", "own_history")
    assert keys[:len(PRE_P84_HASHED_KEYS)] == PRE_P84_HASHED_KEYS
    assert len(keys) == len(PRE_P84_HASHED_KEYS) + 2


def test_t4_the_two_blocks_are_the_last_keys_of_the_context(db):
    ctx = _context(db)
    assert list(ctx)[-2:] == ["market", "own_history"]
    # 摘掉两块 ⇒ 剩下的键集**逐键**是 P84 之前那一份（一字不多、一字不少）
    assert set(ctx) == set(PRE_P84_HASHED_KEYS) | {"market", "own_history"}


def test_t4_the_spec_context_is_untouched(db):
    """`build_context`（纪律臂那条）**不加**这两块 —— 它的读者不是操盘手。"""
    c = connect(db)
    try:
        ctx = agent_context.build_context(c, arm=ARM, asof=START)
    finally:
        c.close()
    assert set(ctx) == set(agent_context.HASHED_KEYS)
    assert "market" not in ctx and "own_history" not in ctx
    # 指纹覆盖的键集也一字不变
    assert agent_context.HASHED_KEYS == (
        "arm", "asof", "spec", "spec_sha256", "ledger", "account", "marks",
        "index_300", "change_space", "disclosure", "non_goals", "citation_book",
        "counter_arm")


def test_t4_the_spec_context_fingerprint_is_byte_identical_to_the_p84_freeze(db):
    """spec 上下文的指纹按**冻结的键集与算法**重算 —— 逐位相同（不是「没报错」）。"""
    import hashlib
    c = connect(db)
    try:
        ctx = agent_context.build_context(c, arm=ARM, asof=START)
    finally:
        c.close()
    blob = json.dumps({k: ctx[k] for k in agent_context.HASHED_KEYS},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert agent_context.context_sha256(ctx) == hashlib.sha256(
        blob.encode("utf-8")).hexdigest()
    # 而且 P84 的两块**没有**混进 spec 上下文（混进去这条就会变）
    assert "market" not in ctx and "own_history" not in ctx


# ---------- ② 注入点 ----------

def test_t4_injection_lands_verbatim(db):
    fake_market = {"asof": START, "index": {}, "breadth": {"n_total": 7},
                   "valuation": {}, "money_flow": {}, "notes": ["注入的"]}
    fake_history = {"arm": ARM, "nav_series": [], "n_nav_days": 42,
                    "trades": [], "n_trades": 0, "n_buy": 0, "n_sell": 0,
                    "realized_fees_total": 0.0, "decisions": [], "n_decisions": 0,
                    "notes": []}
    c = connect(db)
    try:
        state = engine.arm_state_for(c, ARM, START)
        pool = {"asof": START, "codes": [], "pools": {}, "missing_pools": [],
                "available": False}
        ctx = agent_context.build_decision_context(
            c, arm=ARM, asof=START, pool=pool, cash=state["cash"],
            positions=state["positions"], marks={},
            total_assets=state["total_assets"], net_deposits=1.0,
            sellable_qty={}, market=fake_market, own_history=fake_history)
    finally:
        c.close()
    assert ctx["market"] is fake_market            # 注入什么就进什么（同一对象）
    assert ctx["own_history"] is fake_history


# ---------- ③ 指纹 ----------

def test_t4_the_blocks_enter_the_fingerprint(db):
    ctx = _context(db)
    sha = agent_context.decision_context_sha256(ctx)
    for key in ("market", "own_history"):
        without = {k: v for k, v in ctx.items() if k != key}
        with pytest.raises(KeyError):
            agent_context.decision_context_sha256(without)
    tampered = json.loads(json.dumps(ctx))
    tampered["market"]["breadth"]["n_total"] += 1
    assert agent_context.decision_context_sha256(tampered) != sha
    tampered2 = json.loads(json.dumps(ctx))
    tampered2["own_history"]["n_nav_days"] += 1
    assert agent_context.decision_context_sha256(tampered2) != sha


def test_t4_the_p84_key_append_changes_the_fingerprint_of_the_same_input(db):
    """同一输入按 P84 之前的键集算 ⇒ 另一个指纹（这正是「必须通报」的那件事）。"""
    import hashlib
    ctx = _context(db)
    blob = json.dumps({k: ctx[k] for k in PRE_P84_HASHED_KEYS},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert hashlib.sha256(blob.encode("utf-8")).hexdigest() \
        != agent_context.decision_context_sha256(ctx)


# ---------- ④ PIT ----------

def test_t4_rows_after_asof_leave_both_blocks_and_the_sha_untouched(db):
    before = _context(db)
    c = connect(db)
    try:
        c.executemany(
            "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
            " adj_mode, source, fetched_at)"
            " VALUES (?,?,?,?,?,?,100,'none','x',?)",
            [("000333", "2026-09-16", 99.0, 99.0, 99.0, 99.0, NOW),
             ("sh000300", "2026-09-16", 9999.0, 9999.0, 9999.0, 9999.0, NOW)])
        c.executemany(
            "INSERT INTO valuation_daily (code, date, pe_ttm, pb, source, fetched_at,"
            " created_at, resp_sha256) VALUES ('000333','2026-09-16',999.0,99.0,'x',?,?,"
            " 'h')", [(NOW, NOW)])
        c.executemany(
            "INSERT INTO money_flow_daily (code, date, main_net, source, fetched_at,"
            " created_at, resp_sha256)"
            " VALUES ('000333','2026-09-16',1e12,'x',?,?,'h')", [(NOW, NOW)])
        c.executemany(
            "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
            " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
            " index_300_level, index_300_asof, created_at)"
            " VALUES (?,?,0.0,'[]',0.0,20000.0,-0.01,9.0,0.0,20043.0,9999.0,?,?)",
            [(ARM, "2026-09-16", NOW, NOW)])
        c.execute(
            "INSERT INTO paper_trades (account_id, date, code, side, ref_price,"
            " fill_price, qty, commission, stamp_tax, transfer_fee, slippage_cost,"
            " fee_total, asset_class, rule_citation, reason, binding_json,"
            " price_source, price_asof, created_at)"
            " VALUES (?,'2026-09-16','000333','buy',99.0,99.0,100,0,0,0,0.0,5.0,"
            " 'stock','条文','后见','{}','bars','2026-09-16',?)", (ARM, NOW))
        c.execute(
            "INSERT INTO paper_agent_decisions (arm, asof, agent_kind, model_id,"
            " prompt_sha256, seed, context_sha256, spec_before_json, spec_after_json,"
            " n_trials, rejected_json, rationale, created_at, decision_kind,"
            " payload_json) VALUES (?,'2026-09-16','llm','m','p',0,?,'{}','{}',1,"
            " '[]','r',?,'portfolio','{}')", (ARM, "f" * 64, NOW))
        c.commit()
        after = engine.decision_context_for(c, arm=ARM, asof=START)
    finally:
        c.close()
    assert _blob(after["market"]) == _blob(before["market"])
    assert _blob(after["own_history"]) == _blob(before["own_history"])
    assert agent_context.decision_context_sha256(after) \
        == agent_context.decision_context_sha256(before)


# ---------- ⑤ 确定性 ----------

def test_t4_two_calls_are_byte_identical(db):
    first, second = _context(db), _context(db)
    assert _blob(first["market"]) == _blob(second["market"])
    assert _blob(first["own_history"]) == _blob(second["own_history"])
    assert agent_context.decision_context_sha256(first) \
        == agent_context.decision_context_sha256(second)


# ---------- ⑥ 尺寸有界 ----------

def test_t4_one_hundred_days_fifty_trades_twenty_decisions_are_all_capped(db):
    c = connect(db)
    try:
        c.executemany(
            "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
            " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
            " index_300_level, index_300_asof, created_at)"
            " VALUES (?,?,0.0,'[]',0.0,20000.0,-0.01,1.0,0.0,20043.0,4400.0,?,?)",
            [(ARM, f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}", NOW, NOW)
             for i in range(100)])
        for i in range(50):
            c.execute(
                "INSERT INTO paper_trades (account_id, date, code, side, ref_price,"
                " fill_price, qty, commission, stamp_tax, transfer_fee, slippage_cost,"
                " fee_total, asset_class, rule_citation, reason, binding_json,"
                " price_source, price_asof, created_at)"
                " VALUES (?,?, '000333','buy',1.0,1.0,100,0,0,0,0.0,1.0,'stock',"
                " '条文','理由','{}','bars',?,?)",
                (ARM, f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}",
                 f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}", NOW))
        for i in range(20):
            c.execute(
                "INSERT INTO paper_agent_decisions (arm, asof, agent_kind, model_id,"
                " prompt_sha256, seed, context_sha256, spec_before_json,"
                " spec_after_json, n_trials, rejected_json, rationale, created_at,"
                " decision_kind, payload_json) VALUES (?,?,'llm','m','p',0,?,"
                " '{}','{}',1,'[]','r',?,'portfolio','{}')",
                (ARM, f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}", "a" * 64, NOW))
        c.commit()
        ctx = engine.decision_context_for(c, arm=ARM, asof=START)
    finally:
        c.close()
    hist = ctx["own_history"]
    assert len(hist["nav_series"]) == own_history.NAV_LIMIT == 20
    assert len(hist["trades"]) == own_history.TRADE_LIMIT == 10
    assert len(hist["decisions"]) == own_history.DECISION_LIMIT == 5
    # 计数是全量，不是切片长度（101 = 夹具里 @起跑日 那一行 + 上面 100 行）
    assert hist["n_nav_days"] == 101 and hist["n_trades"] == 50
    assert hist["n_decisions"] == 20
    assert ctx["market"]["breadth"]["n_total"] == 1        # 该日只有 000333 一根 bar


# ---------- ⑦ 只描述不预测 ----------

_FORWARD_KEYS = ("predict", "forecast", "target", "prob", "signal", "score",
                 "rating", "advice", "recommend", "kelly", "momentum",
                 "direction", "expected", "confidence")


def _all_keys(obj) -> list[str]:
    if isinstance(obj, dict):
        return [k for k in obj] + [x for v in obj.values() for x in _all_keys(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _all_keys(v)]
    return []


def test_t4_neither_block_has_forward_looking_field_names(db):
    ctx = _context(db)
    for block in ("market", "own_history"):
        for key in _all_keys(ctx[block]):
            for banned in _FORWARD_KEYS:
                assert banned not in key.lower(), f"{block} 里出现了前向字段：{key}"


def test_t4_the_non_goal_is_the_verbatim_k5_text(db):
    assert agent_context.NON_GOALS[0] == K5_NON_GOAL_0
    ctx = _context(db)
    assert ctx["non_goals"][0] == K5_NON_GOAL_0
    # 后两条一字未动
    assert agent_context.NON_GOALS[1:] == (
        "不动成本口径 / PIT 判据 / 整手口径 / 白名单 / append-only 纪律",
        "不扩大变更空间本身（`SPEC_SCHEMA` 的区间不是可以改的字段）",
    )


def test_t4_the_market_and_history_blocks_are_described_in_the_context(db):
    """两块真的到了上下文里（不是只有键、内容是空壳）。"""
    ctx = _context(db)
    assert ctx["market"]["asof"] == START
    assert ctx["market"]["index"]["sh000300"]["level"] == 4450.04
    assert ctx["market"]["notes"]
    assert ctx["own_history"]["arm"] == ARM
    assert ctx["own_history"]["n_nav_days"] >= 1       # `init_accounts` 落了一行净值
