"""P52 / D-34：AI 操盘手的决策载荷 —— 校验、落库、执行、成交语义。

本文件按任务书 §3 的判据组织，每条判据都在 docstring 里点名它**防的是哪种失效**：

| 判据 | 被摘掉后会红的实现 |
|---|---|
| T1 越界不写入：权重和≠100 / 负权重 / 池外 / 缺 reason / 想卖空 | 先写后校验、或静默夹紧 |
| T2 幂等 + append-only：同 `(arm, asof)` 重放逐行不变；UPDATE/DELETE/REPLACE 被拒 | 每次调用都插一行 / 静默覆盖 |
| T3 PIT：塞 `asof` 之后的行 → 上下文指纹不变 | 上下文里混进了未来行 |
| T4 成交语义复用：与既有引擎同输入逐字段相同，费用只有一处真源 | 抄一份费用公式，两边慢慢漂 |

数字一律**从被测模块取**（`CostModel` / `_fee_parts`），不手抄一份到测试里 ——
手抄的那份会与上游漂移，而漂移是静默的。
"""

from __future__ import annotations

import ast
import json
import random
import re
from pathlib import Path

import pytest

from stocklab.cli.main import main
from stocklab.config.costs import ASSET_ETF, ASSET_STOCK, CostModel
from stocklab.paper import agent_context, agent_decide, agent_pool, agent_spec
from stocklab.paper import engine as paper_engine
from stocklab.paper import rules as paper_rules
from stocklab.paper.config import (ARM_AGENT, ARM_AGENT_RANDOM, NO_SHORT_SIDE_MSG,
                                   PAPER_START_DATE, RANDOM_CASH_FLOOR,
                                   RANDOM_N_CODES, RULE_CITATIONS_AGENT_DECISION)
from stocklab.portfolio.prices import Price
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE                       # 2026-09-15
DAY2 = "2026-09-16"
DAY3 = "2026-09-17"
FUTURE = "2026-09-18"                          # 只在 T3 里当「未来」
BEYOND = "2026-09-19"                          # 更远的一天（用来塞「未来 bar」）
CAL = ("2026-09-11", "2026-09-14", START, DAY2, DAY3, FUTURE)
BARS = {
    "000333": {"2026-09-14": 86.80, "2026-09-15": 87.23, "2026-09-16": 87.60,
               "2026-09-17": 87.10, "2026-09-18": 86.40},
    "510300": {"2026-09-15": 4.523, "2026-09-16": 4.550, "2026-09-17": 4.532,
               "2026-09-18": 4.582},
    "510880": {"2026-09-15": 3.382, "2026-09-16": 3.390, "2026-09-17": 3.380,
               "2026-09-18": 3.375},
    "sh000300": {"2026-09-15": 4450.04, "2026-09-16": 4480.27,
                 "2026-09-17": 4460.16, "2026-09-18": 4507.39},
}
POOL = ("000333", "510300", "510880")


#: P56 / D-48：`decide` 只许写在**预注册过**的账户上（内置 `arm-agent` 没有预注册，
#: 要接模型必须开新版本账户）。本文件的绝大多数用例关心的是**载荷校验**，
#: 所以统一用这个版本账户来写。
TEST_ARM = "arm-agent-t1"
TEST_MODEL = "test-model"
TEST_PROMPT = "a" * 64          # 必须是 64 位**十六进制**（enroll 会逐字校验形状）


@pytest.fixture
def db(tmp_path):
    """一份 `paper init` 过、且**已有当日候选池**的库。"""
    path = tmp_path / "agent-decide.db"
    init_db(path)
    c = connect(path)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sz','main',?,?)",
                  [(code, code, "stock" if code == "000333" else "etf", NOW)
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
    _add_pool(c, START, POOL)
    paper_engine.init_accounts(c, start_date=START, now=NOW)
    # P56 / D-48：预注册的版本账户（与 `_decide` 的 model/prompt 逐字段一致）
    paper_engine.enroll_agent_arm(c, arm_name=TEST_ARM, model_id=TEST_MODEL,
                                  prompt_sha256=TEST_PROMPT, now=NOW,
                                  start_date=START)
    c.close()
    return path


def _add_pool(conn, asof: str, codes) -> None:
    conn.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
                 " created_at) VALUES (?,'light','{}',?)", (asof, NOW))
    sid = conn.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
    for code in codes:
        conn.execute("INSERT INTO candidate_members (snapshot_id, code, pool,"
                     " raw_score, adj_score, reason, risk_json, status, entered_at)"
                     " VALUES (?,?, 'short', 1.0, 1.0, '夹具', '{}', '观察中', ?)",
                     (sid, code, NOW))
    conn.commit()


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db), "--now", NOW])
    out, err = capsys.readouterr()
    return code, out, err


def _write(tmp_path, payload, name="d.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _payload(asof, decisions, *, cash_pct=None, rationale="夹具决策"):
    weights = round(sum(d["target_weight_pct"] for d in decisions), 6)
    return {"asof": asof, "decisions": decisions,
            "cash_pct": (round(100.0 - weights, 6) if cash_pct is None else cash_pct),
            "rationale": rationale}


def _context_sha(db, capsys, arm, asof):
    """按生成器的正规流程取 PIT 上下文指纹（D-49：`paper agent context` 是唯一输入源）。"""
    code, out, err = run(db, "paper", "agent", "context", "--asof", asof,
                         "--arm", arm, capsys=capsys)
    assert code == 0, err
    return json.loads(out)["context_sha256"]


def _decide(db, capsys, tmp_path, payload, *, arm=TEST_ARM, name="d.json",
            asof=None, context_sha256=None):
    asof = asof or payload["asof"]
    payload = {**payload, "context_sha256":
               context_sha256 or _context_sha(db, capsys, arm, asof)}
    path = _write(tmp_path, payload, name)
    return run(db, "paper", "agent", "decide", "--asof", asof,
               "--file", str(path), "--arm", arm,
               "--model-id", TEST_MODEL, "--prompt-sha256", TEST_PROMPT,
               capsys=capsys)


def _dump(path) -> dict:
    """**全量**快照：账户、净值、成交、台账四张表的每一行。

    T1 的判据是「拒绝时账户与净值一个字节不变」，所以要能逐表比对 ——
    只数行数会漏掉「行数没变但值变了」。
    """
    c = connect(path)
    try:
        return {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in ("paper_accounts", "paper_nav_daily", "paper_trades",
                          "paper_agent_decisions")}
    finally:
        c.close()


def _ledger_rows(path, arm=TEST_ARM):
    c = connect(path)
    try:
        return agent_decide.load_decisions(c, arm)
    finally:
        c.close()


# ---------- T1：越界即拒，且一个字节都不写 ----------

BAD_CASES = {
    "权重和大于100": _payload(START, [
        {"code": "510300", "side": "buy", "target_weight_pct": 60.0, "reason": "r"}],
        cash_pct=50.0),
    "权重和小于100": _payload(START, [
        {"code": "510300", "side": "buy", "target_weight_pct": 10.0, "reason": "r"}],
        cash_pct=80.0),
    "负权重": _payload(START, [
        {"code": "510300", "side": "buy", "target_weight_pct": -10.0, "reason": "r"}]),
    "单标的超过100": _payload(START, [
        {"code": "510300", "side": "buy", "target_weight_pct": 120.0, "reason": "r"}]),
    "池外标的": _payload(START, [
        {"code": "600690", "side": "buy", "target_weight_pct": 10.0, "reason": "r"}]),
    "缺reason": _payload(START, [
        {"code": "510300", "side": "buy", "target_weight_pct": 10.0, "reason": "  "}]),
    "未知字段(杠杆)": {**_payload(START, [
        {"code": "510300", "side": "buy", "target_weight_pct": 10.0, "reason": "r"}]),
        "leverage": 2.0},
    "载荷asof不一致": {**_payload(START, [
        {"code": "510300", "side": "buy", "target_weight_pct": 10.0, "reason": "r"}]),
        "asof": DAY3},
}


@pytest.mark.parametrize("name", sorted(BAD_CASES))
def test_t1_out_of_bounds_rejections_write_nothing(db, tmp_path, capsys, name):
    """T1：越界 → exit 2，且**账户与净值一个字节不变**（逐表逐行比对）。"""
    before = _dump(db)
    # 「载荷 asof 与 --asof 不一致」那一例要把命令行给的日子和载荷里的**分开**
    asof = START if name == "载荷asof不一致" else None
    code, _, err = _decide(db, capsys, tmp_path, BAD_CASES[name], name="bad.json",
                           asof=asof)
    assert code == 2, f"{name} 应当被拒：{err}"
    assert json.loads(err.strip().splitlines()[-1])["type"] == "DecisionPayloadError"
    assert _dump(db) == before, f"{name} 被拒之后库被改动了"


def test_t1_looking_bearish_on_an_unheld_code_is_a_no_short_rejection(
        db, tmp_path, capsys):
    """T1 的**做空**分支：`side="sell"` 一个没持有的标的 = 融券，报错文案要点名。"""
    before = _dump(db)
    payload = _payload(START, [{"code": "510300", "side": "sell",
                                "target_weight_pct": 0.0,
                                "reason": "看空，想卖"}])
    code, _, err = _decide(db, capsys, tmp_path, payload, name="short.json")
    assert code == 2, err
    err_json = json.loads(err.strip().splitlines()[-1])
    assert err_json["type"] == "DecisionPayloadError"
    # 「AI 想卖空」必须被读成一个明确的拒绝，而不是静默失败
    assert "无做空" in err_json["error"]
    assert "降低总仓位" in NO_SHORT_SIDE_MSG and "无做空" in NO_SHORT_SIDE_MSG
    assert _dump(db) == before


def test_t1_side_contradicting_the_target_weight_is_rejected(db, tmp_path, capsys):
    """`side` 是意图的声明：与「目标 vs 现市值」的方向矛盾就必须拒（不许自己猜）。"""
    before = _dump(db)
    # 现在持有 000333 100 股（≈ 43%），目标 10% ⇒ 隐含方向是「卖」，写 buy 是矛盾
    payload = _payload(START, [{"code": "000333", "side": "buy",
                                "target_weight_pct": 10.0, "reason": "r"}])
    code, _, err = _decide(db, capsys, tmp_path, payload, name="contra.json")
    assert code == 2, err
    assert "自相矛盾" in json.loads(err.strip().splitlines()[-1])["error"]
    assert _dump(db) == before


def test_t1_negative_cash_is_rejected(db, tmp_path, capsys):
    """负现金 = 借钱（杠杆）。`cash_pct` 与 `Σ权重` 两边都查，只查一边会漏。"""
    before = _dump(db)
    payload = _payload(START, [], cash_pct=-10.0)
    code, _, err = _decide(db, capsys, tmp_path, payload, name="negcash.json")
    assert code == 2, err
    assert _dump(db) == before


def test_t1_a_legal_all_cash_decision_is_accepted_and_trades_nothing(
        db, tmp_path, capsys):
    """反向自检：**全现金**是合法决策（空仓是一个决定），必须能写进去且不下单。"""
    payload = _payload(START, [], rationale="今天不投，空仓等机会")
    code, out, err = _decide(db, capsys, tmp_path, payload, name="cash.json")
    assert code == 0, err
    assert json.loads(out)["n_decisions"] == 0
    assert [d["decision_kind"] for d in _ledger_rows(db)] == ["portfolio"]
    c = connect(db)
    try:
        n = c.execute("SELECT COUNT(*) FROM paper_trades WHERE account_id = ?",
                      (ARM_AGENT,)).fetchone()[0]
    finally:
        c.close()
    assert n == 0, "全现金决策不该产生任何成交"


# ---------- T2：幂等 + append-only ----------

def test_t2_replaying_the_same_decision_writes_nothing(db, tmp_path, capsys):
    """同 `(arm, asof)` 同载荷重放 → exit 0「已存在且一致」，台账仍 1 行、逐行不变。"""
    payload = _payload(START, [{"code": "510300", "side": "buy",
                                "target_weight_pct": 10.0, "reason": "建仓"}])
    code, _, err = _decide(db, capsys, tmp_path, payload, name="a.json")
    assert code == 0, err
    after_first = _ledger_rows(db)
    assert len(after_first) == 1

    # 同内容但**键序不同**的载荷文件（canonical JSON 相同 ⇒ 幂等成立）
    shuffled = {k: payload[k] for k in ("rationale", "cash_pct", "decisions", "asof")}
    code, out, err = _decide(db, capsys, tmp_path, shuffled, name="b.json")
    assert code == 0, err
    assert json.loads(out)["status"] == "已存在且一致（未写入）"
    assert _ledger_rows(db) == after_first, "重放不许动台账一个字节"


def test_t2_a_different_payload_on_the_same_day_is_a_conflict(db, tmp_path, capsys):
    """同 `(arm, asof)` 不同载荷 → exit 1（冲突，不是「非法」），原行一字节不改。"""
    first = _payload(START, [{"code": "510300", "side": "buy",
                              "target_weight_pct": 10.0, "reason": "建仓"}])
    assert _decide(db, capsys, tmp_path, first, name="a.json")[0] == 0
    before = _dump(db)

    second = _payload(START, [{"code": "510880", "side": "buy",
                               "target_weight_pct": 20.0, "reason": "换一只"}])
    code, _, err = _decide(db, capsys, tmp_path, second, name="b.json")
    assert code == 1, f"撞键应当是 1（合法但与已有的一行撞了）：{err}"
    assert _dump(db) == before, "撞键时原行必须逐字节未变"
    assert "append-only" in json.loads(err.strip().splitlines()[-1])["error"]


def test_t2_ledger_triggers_reject_update_delete_and_replace(db, tmp_path, capsys):
    """三条 SQL：UPDATE / DELETE / `INSERT OR REPLACE` 全部被触发器拒，行数不变。

    `recursive_triggers=ON` 是第三条能成立的原因（P6 老坑）：不开它，
    `INSERT OR REPLACE` 的隐式 DELETE **不会**触发 DELETE 触发器，静默覆盖会成功。
    """
    payload = _payload(START, [{"code": "510300", "side": "buy",
                                "target_weight_pct": 10.0, "reason": "建仓"}])
    assert _decide(db, capsys, tmp_path, payload, name="a.json")[0] == 0
    c = connect(db)
    try:
        row = dict(c.execute("SELECT * FROM paper_agent_decisions").fetchone())
        assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 1, \
            "连接必须打开 recursive_triggers（否则 INSERT OR REPLACE 拦不住）"
        for sql, args in (
            ("UPDATE paper_agent_decisions SET rationale = 'x' WHERE decision_id = ?",
             (row["decision_id"],)),
            ("DELETE FROM paper_agent_decisions WHERE decision_id = ?",
             (row["decision_id"],)),
            ("INSERT OR REPLACE INTO paper_agent_decisions (decision_id, arm, asof,"
             " agent_kind, model_id, prompt_sha256, seed, context_sha256,"
             " spec_before_json, spec_after_json, created_at)"
             " VALUES (?,?,?,'llm','x','y',0,'z','{}','{}','t')",
             (row["decision_id"], row["arm"], row["asof"])),
        ):
            with pytest.raises(Exception, match="append-only|UNIQUE"):
                c.execute(sql, args)
        assert dict(c.execute(
            "SELECT * FROM paper_agent_decisions").fetchone()) == row, \
            "三条 SQL 之后原行必须逐字段不变"
    finally:
        c.close()


def test_t2_one_decision_per_day_but_two_arms_are_two_rows(db, tmp_path, capsys):
    """幂等键是 `(arm, asof)`：同一天两条臂各一条，互不挤占。"""
    other = "arm-agent-t2"
    c = connect(db)
    try:
        paper_engine.enroll_agent_arm(c, arm_name=other, model_id=TEST_MODEL,
                                      prompt_sha256=TEST_PROMPT, now=NOW,
                                      start_date=START)
    finally:
        c.close()
    for arm in (TEST_ARM, other):
        code, _, err = _decide(db, capsys, tmp_path, _payload(START, []),
                               arm=arm, name=f"{arm}.json")
        assert code == 0, err
    left = [r["arm"] for r in _ledger_rows(db, TEST_ARM)]
    right = [r["arm"] for r in _ledger_rows(db, other)]
    assert left == [TEST_ARM] and right == [other]


# ---------- T3：PIT ----------

def _context_at(db, asof):
    c = connect(db)
    try:
        state = paper_engine.arm_state_for(c, ARM_AGENT, asof)
        pool = agent_pool.pool_snapshot(c, asof)
        marks = {**paper_engine.resolve_marks(c, set(pool["codes"]), asof),
                 **state["marks"]}
        ctx = agent_context.build_decision_context(
            c, arm=ARM_AGENT, asof=asof, pool=pool, cash=state["cash"],
            positions=state["positions"], marks=marks,
            total_assets=state["total_assets"])
        return agent_context.decision_context_sha256(ctx), ctx
    finally:
        c.close()


def test_t3_future_rows_do_not_change_the_context_fingerprint(db):
    """T3：把 `asof` 之后的 `bars_daily` / `paper_nav_daily` / 候选池行塞进库 →
    上下文指纹**一个字符都不变**。

    这条防的是「AI 看过未来」：一旦指纹会随未来数据变，台账里的
    `context_sha256` 就再也证明不了「它当时只看到了这些」。
    """
    asof = DAY2
    before, _ = _context_at(db, asof)

    c = connect(db)
    try:
        c.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
                  " adj_mode, source, fetched_at) VALUES ('510300',?,9,9,9,9,100,"
                  " 'none','x',?)", (BEYOND, NOW))
        c.execute("INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
                  " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
                  " created_at) VALUES ('arm-agent',?,1,'[]',1,1,0,0,0,1,?)",
                  (FUTURE, NOW))
        _add_pool(c, FUTURE, ("510880",))
    finally:
        c.close()
    after, _ = _context_at(db, asof)
    assert after == before, "未来行混进了上下文指纹（PIT 破了）"


def test_t3_the_context_stops_at_asof(db):
    """正面判据：上下文里的价格与池子**只含 `<= asof`** 的日期。"""
    _, ctx = _context_at(db, DAY2)
    for code, mark in ctx["marks"].items():
        assert mark["price_asof"] <= DAY2, f"{code} 的价来自未来"
    for name, pool in ctx["pool"]["pools"].items():
        assert pool["snapshot_asof"] <= DAY2, f"{name} 池取自未来快照"
    assert ctx["index_300"]["price_asof"] <= DAY2


def test_t3_a_future_price_in_the_injected_marks_is_a_lookahead_error(db):
    """注入（而不是查库）的价格同样受守卫：喂未来价**报错**，不是静默采用。"""
    from stocklab.paper.rules import LookaheadError
    from stocklab.portfolio.prices import Price

    c = connect(db)
    try:
        with pytest.raises(LookaheadError):
            agent_context.build_decision_context(
                c, arm=ARM_AGENT, asof=DAY2, pool={"codes": []}, cash=1.0,
                positions={}, marks={"510300": Price(
                    code="510300", price=1.0, source="bars", price_asof=FUTURE,
                    detail=FUTURE)}, total_assets=1.0)
    finally:
        c.close()


# ---------- T4：成交语义复用 ----------

def test_t4_agent_orders_use_the_same_cost_semantics_as_the_static_arms():
    """T4：同 `(side, price, qty, 口径)` 下，操盘手的订单与静态臂的 `plan_*`
    产出**逐字段相同**的 `fill_price` / 各项费用 / `slippage_cost` / `asset_class`。

    两条路各自算一遍费用就会漂，而漂是静默的（净值只是「有点不一样」）。
    """
    price, target = 4.523, 200
    # ① ETF：与静态臂那条路（`plan_etf_buy`）逐字段对拍。
    #    两边都拿「差额 = 200 股 × 价」当输入 ⇒ 应当得出同一个股数与同一套费用。
    etf = CostModel(asset_class=ASSET_ETF)
    static = paper_rules.plan_etf_buy(
        code="510300", price=price, cash=10_000.0, total_assets=20_000.0,
        leg_gap_value=price * target, costs=etf)
    agent = paper_rules.plan_target_weight(
        code="510300", side="buy", target_value=price * target, price=price,
        qty_held=0, cash=10_000.0, total_assets=20_000.0,
        asset_class=ASSET_ETF, costs=etf)
    assert static.qty == agent.qty, "同样的输入应当买出同样的股数"
    assert static.fill_price == agent.fill_price
    assert static.fees == agent.fees
    assert static.asset_class == agent.asset_class

    # ② 股票口径：没有对应的 `plan_*` 买入路径，就对拍**规范算法本身**
    #    （`CostModel.fill_price` + `_fee_parts`，即费用拆分的唯一真源）
    stock = CostModel(asset_class=ASSET_STOCK)
    sell = paper_rules.plan_target_weight(
        code="000333", side="sell", target_value=0.0, price=86.40, qty_held=100,
        cash=1_000.0, total_assets=20_000.0, asset_class=ASSET_STOCK, costs=stock)
    fill = stock.fill_price("sell", 86.40)
    assert sell.fill_price == round(fill, 4)
    assert sell.fees == paper_rules._fee_parts(stock, "sell", fill, sell.qty, ref=86.40)
    assert sell.fees["stamp_tax"] > 0, "股票卖出要收印花税（ETF 免征）"
    assert etf.fees("sell", fill, 100) == pytest.approx(5.0 + 0.0, abs=1e-9), \
        "ETF 卖出：佣金取下限 5 元、印花税 0"


def test_t4_the_stored_trade_matches_the_cost_model_field_by_field(db, tmp_path, capsys):
    """T4 的**落库版**：成交行里的每个费用字段都等于 `CostModel` 现算的值。

    比对的是库里的行，不是内存里的对象 —— 写入层掉一个字段也会被抓住。
    """
    payload = _payload(START, [{"code": "510300", "side": "buy",
                                "target_weight_pct": 10.0, "reason": "建仓"}])
    assert _decide(db, capsys, tmp_path, payload, name="a.json")[0] == 0
    # P56 / D-50：AI 臂的日终由决策循环落（`paper step` 已让出它）——
    # 拿 `paper step` 跑这笔决策，只会得到「它永不下单」（正是 D-50 要挡的那条坑）。
    assert main(["paper", "agent", "run", "--asof", START,
                 "--db", str(db), "--now", NOW]) == 1, \
        "家族里另两条臂（arm-agent / arm-agent-random）没有决策 ⇒ 退出码 1；" \
        "本用例只关心 arm-agent-t1 的成交"
    capsys.readouterr()

    c = connect(db)
    try:
        t = dict(c.execute("SELECT * FROM paper_trades WHERE account_id = ?",
                           (TEST_ARM,)).fetchone())
        assert t["price_asof"] == START and t["price_source"] == "bars", \
            "价格出处与所属日期必须落在成交行里（可审计的第一问）"
        costs = CostModel(asset_class=t["asset_class"])
        ref = float(t["ref_price"])
        # ⚠️ 重算必须用**未取整**的成交价：`_fee_parts` 的滑点按 `|fill − ref| × 股数`
        # 算，拿库里那个取整到 4 位的 `fill_price` 去反推会差一分钱（实测 0.90 vs 0.92）。
        # 这一分钱就是「重算与原始计算用了不同输入」的典型形态。
        fill = costs.fill_price("buy", ref)
        assert t["fill_price"] == round(fill, 4)
        expected = paper_rules._fee_parts(costs, "buy", fill, int(t["qty"]), ref=ref)
        for key, value in expected.items():
            assert t[key if key != "total" else "fee_total"] == value, key
        # 溯源标签写进 reason：成交行是 append-only，必须自己说得清照哪条决策下的
        led = agent_decide.load_decisions(c, TEST_ARM)[0]
        assert agent_decide.payload_tag(led["payload"]) in t["reason"]
        assert t["rule_citation"] in set(RULE_CITATIONS_AGENT_DECISION.values())
    finally:
        c.close()


def test_t4_fee_rates_are_defined_in_exactly_one_place():
    """T4 第二半：**全仓只有一处费用常量真源** —— 别的 `paper/*.py` 不许碰费率。

    `CostModel` 是费率的唯一定义处；`paper/rules.py` 的 `_fee_parts` 是唯一的
    拆分明细处。操盘手那条路（`agent_decide.py`）只能调它们，不能自己写公式。
    """
    paper_dir = Path(agent_decide.__file__).parent
    banned = {"commission_rate", "min_commission", "stamp_tax_rate",
              "transfer_fee_rate", "slippage_bps"}
    offenders: dict[str, list[str]] = {}
    for path in sorted(paper_dir.glob("*.py")):
        if path.name == "rules.py":
            continue        # `_fee_parts` 是那唯一一处
        ids = {n.id for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
               if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.Attribute)}
        hit = sorted(banned & ids)
        if hit:
            offenders[path.name] = hit
    assert offenders == {}, (
        f"费用常量在 {offenders} 里被二次定义 —— 费用只许有一处真源"
        f"（`config/costs.CostModel` + `paper/rules._fee_parts`）")


# ---------- 候选池 ----------

def test_pool_is_the_union_of_the_three_pools_and_fails_closed_when_empty(db):
    """池 = 短/中/长并集；**空池 ⇒ 任何标的都在池外**（fail-closed，不是放行）。"""
    c = connect(db)
    try:
        c.execute("INSERT INTO candidate_members (snapshot_id, code, pool, raw_score,"
                  " adj_score, reason, risk_json, status, entered_at)"
                  " VALUES (1,'510880','mid',1,1,'x','{}','观察中',?)", (NOW,))
        c.commit()
        snap = agent_pool.pool_snapshot(c, START)
        assert set(snap["codes"]) == set(POOL) | {"510880"}
        assert snap["available"] is True
        assert set(snap["missing_pools"]) == {"long"}, "没快照的池要如实列出来"
        empty = agent_pool.pool_snapshot(c, "2026-09-11")   # 池快照在 START 才有
        assert empty["available"] is False and empty["codes"] == []
        assert "fail-closed" in empty["reason"]
    finally:
        c.close()


def test_the_pool_reader_does_not_import_the_stock_picking_pipeline():
    """候选池读取走**裸 SQL**：`paper/` 不接选股链路的护栏不为它开洞。

    与 `tests/test_paper_discipline_guard.py` 是同一件事的两面 ——
    这里点名 `agent_pool.py`，免得将来有人顺手 `from stocklab.candidate import ...`。
    """
    src = Path(agent_pool.__file__).read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    assert not [m for m in mods if m.startswith("stocklab.candidate")], sorted(mods)


# ---------- 随机对照臂 ----------

def test_random_arm_is_deterministic_and_obeys_every_guardrail(db, tmp_path, capsys):
    """随机臂：同种子逐字节可复现，且**同护栏**（在池内、权重和 100、有理由）。"""
    from stocklab.paper.config import RANDOM_MODEL_ID

    code, out, err = run(db, "paper", "agent", "random", "--asof", START,
                         "--arm", ARM_AGENT_RANDOM, "--seed", "7", capsys=capsys)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["n_decisions"] >= 1, "随机臂至少要投一只（否则它没在对照）"
    rows = _ledger_rows(db, ARM_AGENT_RANDOM)
    assert rows[0]["model_id"] == RANDOM_MODEL_ID
    assert rows[0]["agent_kind"] == "random"
    assert rows[0]["decision_kind"] == "portfolio"
    body = rows[0]["payload"]
    assert {d["code"] for d in body["decisions"]} <= set(POOL), "随机也不许出池"
    assert abs(sum(d["target_weight_pct"] for d in body["decisions"])
               + body["cash_pct"] - 100.0) < 1e-6
    assert all(d["reason"].strip() for d in body["decisions"])

    # 同种子重放 → 「已存在且一致」，台账仍 1 行
    before = _dump(db)
    code, out2, err = run(db, "paper", "agent", "random", "--asof", START,
                          "--arm", ARM_AGENT_RANDOM, "--seed", "7", capsys=capsys)
    assert code == 0, err
    assert json.loads(out2)["status"] == "已存在且一致（未写入）"
    assert _dump(db) == before


def test_random_arm_and_agent_arm_share_the_same_guardrails(db, tmp_path, capsys):
    """两臂共用同一个校验器：随机臂的载荷也要能过 `validate_payload` 的全部护栏。

    没有这条，「同护栏」就只是文档里的一句话，而随机臂可以悄悄越过池边界。
    """
    c = connect(db)
    try:
        state = paper_engine.arm_state_for(c, ARM_AGENT_RANDOM, START)
        pool = agent_pool.pool_snapshot(c, START)
        marks = {**paper_engine.resolve_marks(c, set(pool["codes"]), START),
                 **state["marks"]}
        for seed in range(5):
            payload = agent_decide.random_payload(
                arm=ARM_AGENT_RANDOM, asof=START, pool_codes=set(pool["codes"]),
                held_qty=state["positions"], marks=marks,
                total_assets=state["total_assets"], seed=seed)
            validated = agent_decide.validate_payload(
                asof=START, payload=payload, pool_codes=set(pool["codes"]),
                held_qty=state["positions"], marks=marks,
                total_assets=state["total_assets"])
            assert validated["decisions"] or validated["cash_pct"] == 100.0
            for d in validated["decisions"]:
                assert d["side"] in ("buy", "sell")
                assert 0.0 <= d["target_weight_pct"] <= 100.0
    finally:
        c.close()


def test_spec_rows_cannot_be_executed_as_portfolio_decisions(db, tmp_path, capsys):
    """反向自检：台账里只有 **spec** 行时，`paper step` 必须当「没有决策」处理。

    两类行共用一张表，若按 `(arm, asof)` 硬读而不过滤 `decision_kind`，
    spec 行会给出一个空载荷 ⇒ 看起来像「这天决定空仓」。**静默**比报错难查。
    """
    c = connect(db)
    try:
        agent_spec.record_decision(
            c, arm=ARM_AGENT, asof=START,
            spec_before=agent_spec.spec_before(c, ARM_AGENT, START),
            spec_after=agent_spec.apply_spec({}, {"etf_target_pct": 12.0}),
            agent_kind=agent_spec.AGENT_KIND_MANUAL, model_id="manual",
            prompt_sha256=agent_spec.MANUAL_PROMPT_SHA256, seed=0,
            context_sha256="c" * 64, now=NOW)
        assert agent_decide.portfolio_decision_on(c, ARM_AGENT, START) is None
        paper_engine.step(c, START, now=NOW)
        n = c.execute("SELECT COUNT(*) FROM paper_trades WHERE account_id = ?",
                      (ARM_AGENT,)).fetchone()[0]
    finally:
        c.close()
    assert n == 0, "spec 行不是操盘决策，不许被执行"


def test_p65_random_payloads_can_overdraw_and_the_gate_rejects_them(db):
    """**P65/P66 的读数已由 P68 取代** —— 本用例现在钉住「口径 v3 之后不再有透支」。

    历史：P65 时本用例断言「随机臂会透支、闸门会拒」（旧口径 `uniform(0, 100)`，
    不看存量持仓）。P66 把敞口上界改成 `(100 − RANDOM_CASH_FLOOR) − reserved`
    并把 `reserved` 定义成「**不在**本轮 picks 里的存量」之后，真库 200 种子
    从 31/200 → **1/200**（夹具 11/40 → 4/40），但**没有归零**。
    仅存的那一种（seed 97 逐笔复现）：抽中的存量票 `000333` 目标市值落在
    「0 与一手之间」⇒ 执行层按整手向下取整 ⇒ **一股都卖不出去**
    （1 手 = ¥8,247 ≈ 总资产的 42%）⇒ 那份「卖出会释放现金」的假设落空。

    P68 的结论是：**那条假设本身就不该写进生成器**。`reserved` 改成全部存量
    占比之后，生成器不再依赖任何一笔减仓 ⇒ 本条断言**「零被拒」**
    （夹具 200 个种子），把「不再有可构造的透支载荷」从一句推变化成读数。

    ⚠️ 闸门本身**没有**被放松：对**旧口径**手工构造的载荷它照样拒
    （见 `test_p66_the_gate_still_rejects_an_old_cadence_payload`）——
    “上游不再产出”与“闸门不再拦”是两件事。
    """
    c = connect(db)
    try:
        state = paper_engine.arm_state_for(c, ARM_AGENT_RANDOM, START)
        pool = agent_pool.pool_snapshot(c, START)
        marks = {**paper_engine.resolve_marks(c, set(pool["codes"]), START),
                 **state["marks"]}
        locked = sum(float(marks[code].price) * int(qty)
                     for code, qty in state["positions"].items() if code in marks)
        assert locked > 0, "夹具账户必须真的握着存量持仓，否则本判据空转"

        rejected: list[int] = []
        for seed in range(200):
            payload = agent_decide.random_payload(
                arm=ARM_AGENT_RANDOM, asof=START, pool_codes=set(pool["codes"]),
                held_qty=state["positions"], marks=marks,
                total_assets=state["total_assets"], seed=seed)
            validated = agent_decide.validate_payload(
                asof=START, payload=payload, pool_codes=set(pool["codes"]),
                held_qty=state["positions"], marks=marks,
                total_assets=state["total_assets"])
            try:
                agent_decide.execute_decision(
                    c, arm=ARM_AGENT_RANDOM, asof=START, decision=validated,
                    cash=float(state["cash"]), positions=dict(state["positions"]),
                    marks=marks, total_assets=float(state["total_assets"]))
            except agent_decide.CashShortfall:
                rejected.append(seed)

        assert rejected == [], (
            f"口径 v3（P68）之后随机臂在夹具账户上不该再被拒一次：{rejected}")
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# P68：随机对照臂的敞口上界（口径 v3，`reserved` = **全部**存量持仓占比）
#
# 口径 v2（P66）扣掉的是「**不在**本轮 picks 里」的存量，漏掉了「在 picks 里
# 但**减不动**」的：执行层的卖出是整手的，差额 < 1 手 ⇒ `hold` ⇒ 那笔市值
# 一分钱都不会变成现金。v3 删掉那个减免 ⇒ 生成器不依赖任何一笔减仓也能成交。
# ══════════════════════════════════════════════════════════════════════

#: 真库 `arm-agent-random` 在 2026-09-23 的读数（只读探针）：总资产 ¥19,567，
#: 其中 `000333 ×100 @ ¥82.47 = ¥8,247`（**42.1475%**）是存量持仓。
#: 判据直接钉在这组真数字上，而不是钉在一个凑出来的整数上。
P66_TOTAL = 19567.0
P66_HELD = {"000333": 100}
P66_CLOSES = {"000333": 82.47, "510300": 4.031, "510880": 3.155,
              "600900": 28.08, "601398": 7.16}
P66_POOL = tuple(P66_CLOSES)
P66_ASOF = "2026-09-23"       # 只进 `_seed_material`（种子材料），不查库
P66_RESERVED = 42.1475        # = round(8247 / 19567 × 100, 4)
P66_CAP = round((100.0 - 10.0) - P66_RESERVED, 2)     # 47.85

#: 真库 2026-09-23 的**完整候选池与收盘价**（11 只，只读探针取出的原文）＋
#: 账户状态。用途：判据 2 要让 seed 97 在**同一份池**上逐位复现真库读数 ——
#: 池子不同 ⇒ `rng.sample` 的 `usable` 列表不同 ⇒ 抽到的 picks 不同，
#: 「seed 97 被拒 ¥657.06」这件事就复现不出来。
P68_POOL_CLOSES = {
    "000333": 82.47, "000651": 38.36, "002032": 39.10, "002415": 33.11,
    "002508": 16.48, "600036": 40.60, "600519": 1251.24, "600900": 28.08,
    "601318": 53.87, "601398": 8.09, "603868": 31.26,
}
P68_CASH = 11320.0
P68_TOTAL = 19567.0
P68_HELD = {"000333": 100}

#: 夹具 `db`（`arm-agent-random` 起跑账户：现金 ¥11,314.91 ＋ `000333 ×100`）
#: 上，口径 v3 之后 seed 0..199 的**被拒集合**：空集（v2 时是这 4 个）。
#: 钉死它是因为「残余归零」这件事本身有读数，而不是一句「好多了」。
P66_FIXTURE_RESIDUAL_SEEDS: list[int] = []


def _p66_marks() -> dict:
    return {code: Price(code=code, price=price, source="bars_daily",
                        price_asof=P66_ASOF, detail="P66 夹具")
            for code, price in P66_CLOSES.items()}


def _p66_payload(seed: int, *, held_qty=None, total_assets=P66_TOTAL) -> dict:
    return agent_decide.random_payload(
        arm=ARM_AGENT_RANDOM, asof=P66_ASOF, pool_codes=set(P66_POOL),
        held_qty=P66_HELD if held_qty is None else held_qty,
        marks=_p66_marks(), total_assets=total_assets, seed=seed)


def _p66_number(payload: dict, key: str) -> float:
    """从 `rationale` 里读回 `reserved` / `cap` 的**实际取值**。

    为什么从文本里读而不是从返回值里读：口径切换必须**在台账上可见**
    （`paper_agent_decisions.rationale` 是事后唯一能读的东西，而载荷本身
    只许有 `DECISION_PAYLOAD_KEYS` 那几个键 —— 加一个 `_debug_*` 会被
    写入口的未知字段闸门拒掉）。所以本用例顺带钉住「这两个数真的写下来了」。
    """
    m = re.search(rf"{key}=(-?[0-9.]+)%", str(payload["rationale"]))
    assert m, f"rationale 里没有 {key} 的实际取值：{payload['rationale']}"
    return float(m.group(1))


def _p68_marks() -> dict:
    """真库 2026-09-23 的 11 只池内标的收盘价（`P68_POOL_CLOSES` 的 Price 形态）。"""
    return {code: Price(code=code, price=price, source="bars_daily",
                        price_asof=P66_ASOF, detail="P68 夹具")
            for code, price in P68_POOL_CLOSES.items()}


def _exec_random_payload(c, payload: dict, *, marks: dict) -> float:
    """把一份随机臂载荷走完整条执行链（rebase → execute），返回成交后现金。

    每次传**全新**的 `positions` dict —— `_settle` 原地改它（探针第一版复用
    同一个 dict，上一个 seed 的副作用会污染下一个）。
    """
    rebased = agent_decide.rebase_payload(
        payload, total_assets=P68_TOTAL, marks=marks, positions=dict(P68_HELD))
    cash_after, _pos, _orders, _evals = agent_decide.execute_decision(
        c, arm=ARM_AGENT_RANDOM, asof=P66_ASOF, decision=rebased, cash=P68_CASH,
        positions=dict(P68_HELD), marks=marks, total_assets=P68_TOTAL)
    return cash_after


def _p68_v2_reserved_pct(*, picks, held_qty, marks, total_assets) -> float:
    """**口径 v2（P66）** 的 `_reserved_pct` —— `git show HEAD` 里那段的一字不改副本。

    本站删掉的正是「排除 picks」这一句，所以这里**不能**调用
    `agent_decide._reserved_pct`：反向对照要证明的是「v2 的读数是这样」，
    而不是「新实现的读数是这样」（后者是同义反复）。
    """
    if total_assets is None or float(total_assets) <= 0:
        return 0.0
    picked = set(picks)
    total = 0.0
    for code, qty in held_qty.items():
        if code in picked or code not in marks or int(qty or 0) <= 0:
            continue
        value = float(marks[code].price) * int(qty)
        total += round(value / float(total_assets) * 100.0, 4)
    return total


def _p68_v2_payload(*, arm, asof, pool_codes, held_qty, marks, total_assets,
                    seed) -> dict:
    """**口径 v2（P66）** 的 `random_payload` —— 改动前实现的一字不改副本。

    与现役实现的差别只有一处：`reserved` 用 `_p68_v2_reserved_pct`（排除 picks）。
    `rng` 的调用顺序逐字相同 ⇒ 同一 seed 的 `k` / `picks` 两版**逐位相同**，
    只有 `exposure` 那一档数不同（判据 4 的黄金表就是这么来的）。
    """
    rng = random.Random(agent_decide._seed_material(arm, asof, seed))
    usable = sorted(c2 for c2 in pool_codes if c2 in marks)
    if not usable:
        return {"asof": asof, "decisions": [], "cash_pct": 100.0,
                "rationale": f"随机对照臂：{asof} 没有可定价的池内标的 → 全现金"}
    lo, hi = RANDOM_N_CODES
    k = max(1, min(len(usable), rng.randint(lo, hi)))
    picks = sorted(rng.sample(usable, k))
    reserved = _p68_v2_reserved_pct(picks=picks, held_qty=held_qty, marks=marks,
                                    total_assets=total_assets)
    cap = round((100.0 - RANDOM_CASH_FLOOR) - reserved, 2)
    exposure = round(rng.uniform(0.0, max(0.0, cap)), 2)
    raw = [rng.random() for _ in picks]
    total = sum(raw)
    weights = [round(exposure * w / total, 2) for w in raw] if total else \
        [0.0 for _ in picks]
    drift = round(exposure - sum(weights), 2)
    weights[-1] = round(weights[-1] + drift, 2)
    items: list[dict] = []
    for code, weight in zip(picks, weights):
        if weight <= 0:
            continue
        qty = int(held_qty.get(code, 0) or 0)
        price = float(marks[code].price)
        target_value = round(total_assets * weight / 100.0, 4)
        items.append({
            "code": code, "target_weight_pct": weight,
            "side": agent_decide.side_for(
                target_value=target_value,
                current_value=round(price * qty, 4)) or agent_decide.SIDE_BUY,
            "reason": f"夹具：口径 v2 抽中 {code}"})
    return {"asof": asof, "decisions": items,
            "cash_pct": round(100.0 - sum(d["target_weight_pct"] for d in items), 2),
            "rationale": (f"夹具：**口径 v2（P66）** reserved={reserved:g}%、"
                          f"cap={cap:g}%，seed={seed}")}


def test_p68_every_holding_is_reserved_regardless_of_picks():
    """判据 2 的**纯函数面**：`reserved` = 全部存量，**与 picks 无关**。

    v2 在这里会给出 `[510300] ⇒ 42.1475`、`[000333, 510300] ⇒ 0.0`（两个不同的数）；
    v3 必须**同一个数**——这就是「不因 picks 而减免」的可断言形式。
    签名里也**没有** `picks` 参数了（恒不被使用的参数不许留）。
    """
    marks, held = _p66_marks(), P66_HELD
    assert agent_decide._reserved_pct(held_qty=held, marks=marks,
                                      total_assets=P66_TOTAL) == P66_RESERVED
    # 反向：v2 的副本在同一输入上给出**两个不同的数**，证明上面那条不是空转。
    assert _p68_v2_reserved_pct(picks=["510300"], held_qty=held, marks=marks,
                                total_assets=P66_TOTAL) == P66_RESERVED
    assert _p68_v2_reserved_pct(picks=["000333", "510300"], held_qty=held,
                                marks=marks, total_assets=P66_TOTAL) == 0.0


def test_p66_the_cap_mirrors_a1_and_deducts_all_the_locked_holdings():
    """口径 v3：`cap = (100 − RANDOM_CASH_FLOOR) − reserved`，与 A1 v1.0.4 同一条。

    「同护栏」这句形容词由**同一个数**兑现：`RANDOM_CASH_FLOOR` 必须等于
    `m2_a1` 的 `CASH_FLOOR`（对拍，不 import m2 —— 方向是 m2 → paper）。
    """
    from stocklab.m2.builtin import a1_pick

    assert RANDOM_CASH_FLOOR == a1_pick.CASH_FLOOR == 10.0

    drawn: set[float] = set()
    for seed in range(50):
        payload = _p66_payload(seed)
        reserved = _p66_number(payload, "reserved")
        cap = _p66_number(payload, "cap")
        # 口径 v3：存量票 `000333`（42.1475%）**无论在不在 picks 里**都占额度
        # —— v2 的「在 picks 里 ⇒ reserved=0、cap=90」那一支已被删掉。
        assert reserved == P66_RESERVED, seed
        assert cap == P66_CAP == 47.85, seed
        assert "口径 v3（P68）" in payload["rationale"], "口径标识必须写在台账上"
        assert "不因 picks 而减免" in payload["rationale"], \
            "v3 与 v2 的差别必须写在台账上（读台账的人不必靠日期猜口径）"
        exposure = round(100.0 - float(payload["cash_pct"]), 2)
        assert exposure <= cap + 1e-9, (seed, exposure, cap)
        assert 0.0 <= exposure <= 100.0, "敞口不可能为负，也不可能超过 100"
        drawn.add(exposure)
    assert len(drawn) > 1, "50 个种子的敞口全一样 ⇒ 抽取区间没在被使用"


def test_p68_seed_97_counterexample_is_pinned_in_both_versions(db):
    """**判据 2（最硬的一条）**：v2 与 v3 并排钉在**同一个 seed 97** 上。

    夹具就是真库 2026-09-23 的形态本身：11 只池（`P68_POOL_CLOSES`）、
    现金 ¥11,320、`000333 ×100`、总资产 ¥19,567。

    ## v2（P66 口径，`_reserved_pct` 排除 picks 里的存量）—— 真库只读探针原文

    ```
    seed=97  exposure=83.79  reserved=0.0  cap=90.0  verdict=cash_reject
    picks: [('000333', 10.6), ('000651', 23.72), ('002508', 12.38), ('601398', 37.09)]
    arm-agent-random 在 2026-09-23：账上可用现金 ¥11,320.00，本轮需要 ¥11,977.06，
    成交后现金 ¥-657.06（越界 ¥657.06）—— 这笔钱不在账上，买它就是借钱（杠杆）。
    **整轮拒绝、零写入**：不夹紧、不缩单、不部分成交。code=cash
    ```

    为什么 v2 会算出 `reserved=0`：`000333` 在 picks 里 ⇒ 被排除。而它的目标
    市值 ¥2,074.10 与现市值 ¥8,247.00 差 ¥6,172.90 **不足一手 ¥8,242.88**
    ⇒ 执行层「不动」⇒ 那 42.15% **一分钱都不会变成现金**。

    ## v3（P68）—— 同一天的只读探针原文

    ```
    seed=97  reserved=42.1475  cap=47.85  exposure=44.55  verdict=ok
    cash_after=8077.37
    ```

    `picks` 与 v2 **逐位相同**（只有敞口那一档数变了）—— 判据 4 的黄金表。
    """
    c = connect(db)
    try:
        # `execute_decision` 要对每个 code 查 `instruments.type`；夹具库只登记了
        # 起跑那三只 ⇒ 把这份真库形态用到的 4 只补上（本地、不影响别的用例）。
        c.executemany(
            "INSERT OR IGNORE INTO instruments (code, name, market, board, type,"
            " added_at) VALUES (?,?,'sz','main','stock',?)",
            [(code, code, NOW) for code in
             ("000333", "000651", "002508", "601398")])
        c.commit()
        marks = _p68_marks()
        pool_codes = set(P68_POOL_CLOSES)

        # ---------- v3（现役实现） ----------
        v3 = agent_decide.random_payload(
            arm=ARM_AGENT_RANDOM, asof=P66_ASOF, pool_codes=pool_codes,
            held_qty=P68_HELD, marks=marks, total_assets=P68_TOTAL, seed=97)
        assert [d["code"] for d in v3["decisions"]] == \
            ["000333", "000651", "002508", "601398"], "picks 必须与 v2 逐位相同"
        assert [d["target_weight_pct"] for d in v3["decisions"]] == \
            [5.64, 12.61, 6.58, 19.72]
        assert abs(_p66_number(v3, "reserved") - 42.15) <= 0.01
        assert abs(_p66_number(v3, "cap") - 47.85) <= 0.01
        assert round(100.0 - float(v3["cash_pct"]), 2) == 44.55
        cash_after = _exec_random_payload(c, v3, marks=marks)
        assert cash_after >= 0.0, cash_after
        assert cash_after == pytest.approx(8077.37, abs=0.01)

        # ---------- v2（冻结副本）—— 同一个 seed、同一份池 ----------
        v2 = _p68_v2_payload(
            arm=ARM_AGENT_RANDOM, asof=P66_ASOF, pool_codes=pool_codes,
            held_qty=P68_HELD, marks=marks, total_assets=P68_TOTAL, seed=97)
        assert _p68_v2_reserved_pct(picks=[d["code"] for d in v2["decisions"]],
                                    held_qty=P68_HELD, marks=marks,
                                    total_assets=P68_TOTAL) == 0.0
        assert round((100.0 - RANDOM_CASH_FLOOR) - 0.0, 2) == 90.0
        assert round(100.0 - float(v2["cash_pct"]), 2) == 83.79
        with pytest.raises(agent_decide.CashShortfall) as exc:
            _exec_random_payload(c, v2, marks=marks)
        assert exc.value.code == "cash"
        assert "越界 ¥657.06" in exc.value.reason, exc.value.reason
        assert "¥11,977.06" in exc.value.reason, exc.value.reason
    finally:
        c.close()


def test_p66_the_draw_sequence_is_unchanged_from_the_old_cadence():
    """只有**抽取区间**变了：`k` 只数、`picks` 名单与旧口径逐位相同。

    黄金表取自改动前的实现（`git show HEAD:stocklab/paper/agent_decide.py`
    在同一个 ctx 上跑出来的 `k` / `codes`）。「其余一个字不改」这句话只能靠
    这张表兑现 —— 它一红就说明 `rng` 的**调用顺序**被动过（那会让台账里
    同一个种子对不上净值）。
    """
    golden = {0: (4, ["000333", "510880", "600900", "601398"]),
              1: (1, ["510880"]),
              2: (4, ["000333", "510300", "510880", "601398"]),
              3: (2, ["510300", "600900"]),
              4: (1, ["600900"]),
              5: (2, ["510880", "601398"]),
              6: (4, ["000333", "510300", "510880", "601398"]),
              7: (3, ["000333", "510300", "600900"]),
              8: (4, ["510300", "510880", "600900", "601398"]),
              9: (2, ["510880", "600900"])}
    #: 旧口径（`uniform(0, 100)`）在**无持仓**（reserved=0 ⇒ cap=90）时的敞口，
    #: 与**本站**在同一个 ctx 上的敞口。新值 ≈ 旧值的 0.9 倍（区间只缩了 10 个点），
    #: 但**不是**逐位相等：`round(0.9 × round(100u, 2), 2)` 与 `round(90u, 2)`
    #: 会差一分（seed 7 就是 8.23 vs 8.22）—— 两次取整的地方不同，别把
    #: 「≈0.9 倍」当成可以逐位对上的恒等式。
    old_exposure = {0: 66.18, 1: 83.19, 2: 78.99, 3: 51.74, 4: 25.78,
                    5: 97.0, 6: 1.11, 7: 9.14, 8: 90.23, 9: 62.87}
    new_exposure = {0: 59.56, 1: 74.87, 2: 71.09, 3: 46.57, 4: 23.2,
                    5: 87.3, 6: 1.0, 7: 8.22, 8: 81.21, 9: 56.58}
    for seed, (k, codes) in golden.items():
        payload = _p66_payload(seed, held_qty={})
        assert len(payload["decisions"]) == k, seed
        assert [d["code"] for d in payload["decisions"]] == codes, seed
        exposure = round(100.0 - float(payload["cash_pct"]), 2)
        assert exposure == new_exposure[seed], seed
        assert abs(exposure - 0.9 * old_exposure[seed]) <= 0.01, seed


def test_p66_same_seed_twice_is_byte_identical():
    """逐字节可复现是硬要求：同 `(arm, asof, seed)` 两遍必须同 `canonical_payload`。"""
    for seed in (0, 7, 97):
        first = agent_decide.canonical_payload(_p66_payload(seed))
        second = agent_decide.canonical_payload(_p66_payload(seed))
        assert first == second, seed


def test_p66_the_degenerate_paths_are_pinned_to_the_old_bytes():
    """退化路径：**能逐位相同的那一条必须逐位相同**，其余如实说明。

    - 池内没有可定价标的 ⇒ 仍走**全现金早退**（在抽 rng 之前就返回）⇒
      与旧口径**逐字节相同**（下面钉的是改动前跑出来的原文）；
    - 无持仓 / `total_assets` 缺失或 ≤ 0 ⇒ `reserved = 0`（`_reserved_pct`
      的退化口径，与 A1 同款）⇒ `cap = 90`。
      ⚠️ **这一条与旧口径不是逐位相同**：旧口径的区间是 `uniform(0, 100)`，
      没有 10% 现金下限，所以同一个 seed 的敞口是 `1/0.9` 倍。
      任务书 §2 的括注「必须与旧版逐位相同」在这里**不成立**（它是从
      P65 §3.2 步骤 4 抄过来的，那句在 A1 上为真：1.0.2 的上限也是 90）。
      本条如实钉住差异，不把括注当判据（ERROR_DIARY #74 的同型）。
    """
    empty = {"asof": P66_ASOF, "decisions": [], "cash_pct": 100.0,
             "rationale": f"随机对照臂：{P66_ASOF} 没有可定价的池内标的 → 全现金"}
    got = agent_decide.random_payload(
        arm=ARM_AGENT_RANDOM, asof=P66_ASOF, pool_codes=set(P66_POOL), marks={},
        held_qty=P66_HELD, total_assets=P66_TOTAL, seed=0)
    assert agent_decide.canonical_payload(got) == agent_decide.canonical_payload(empty)

    for total in (None, 0.0, -1.0):
        assert agent_decide._reserved_pct(held_qty=P66_HELD,
                                          marks=_p66_marks(),
                                          total_assets=total) == 0.0
    assert agent_decide._reserved_pct(held_qty={}, marks=_p66_marks(),
                                      total_assets=P66_TOTAL) == 0.0
    assert _p66_number(_p66_payload(0, held_qty={}), "cap") == 90.0
    # `total_assets=None` 在旧口径下就是**崩**的（TypeError，`None × weight`），
    # 本站一个字没动那条路径 ⇒ 它今天照样崩。不许把「本来就没有的路径」修成
    # 一条新路径（那会悄悄多出「没有总资产也能下单」这个状态）。
    with pytest.raises(TypeError):
        _p66_payload(0, total_assets=None)


def test_p66_a_cap_at_or_below_zero_lands_on_all_cash_without_negative_weights():
    """`reserved` 吃到 ≥ 90% ⇒ `cap ≤ 0` ⇒ 敞口 0、全现金，**不许**出现负权重。

    seed 1 抽中的是 `[510880]`（见上一条的黄金表）⇒ 两笔存量票都不在 picks 里：
    `42.1475 + 400 × 28.08 / 19567 × 100 = 99.5487` ⇒ `cap = −9.55`。
    """
    payload = _p66_payload(1, held_qty={"000333": 100, "600900": 400})
    assert _p66_number(payload, "cap") <= 0.0
    assert payload["decisions"] == []
    assert payload["cash_pct"] == 100.0


def test_p68_the_residual_overdraw_is_gone_and_only_that_changed(db):
    """**判据 1 的夹具镜像**：口径 v3 之后，夹具账户 200 个种子**零被拒**。

    v2（P66）的实测是 4/200（这 4 个全是「存量票被抽中」的种子）；真库形态是
    1/200（seed 97）。v3 把 `reserved` 改成全部存量之后**归零** ——
    真实原因就是任务书 §2 那句话：生成器不再依赖任何一笔减仓也能成交。

    本用例还钉住「**只动了敞口那一档**」：同一 seed 的 `k` / `picks` 与 v2
    逐位相同（`_p68_v2_payload` 是改动前的副本，`rng` 调用顺序逐字相同）。
    """
    c = connect(db)
    try:
        state = paper_engine.arm_state_for(c, ARM_AGENT_RANDOM, START)
        pool = agent_pool.pool_snapshot(c, START)
        marks = {**paper_engine.resolve_marks(c, set(pool["codes"]), START),
                 **state["marks"]}
        cash0 = float(state["cash"])
        total0 = float(state["total_assets"])
        pos0 = dict(state["positions"])

        def run(payload):
            try:
                agent_decide.execute_decision(
                    c, arm=ARM_AGENT_RANDOM, asof=START, decision=payload,
                    cash=cash0, positions=dict(pos0), marks=marks,
                    total_assets=total0)
                return False
            except agent_decide.CashShortfall:
                return True

        rejected: list[int] = []
        same_draw = 0
        for seed in range(200):
            new = agent_decide.random_payload(
                arm=ARM_AGENT_RANDOM, asof=START, pool_codes=set(pool["codes"]),
                held_qty=pos0, marks=marks, total_assets=total0, seed=seed)
            new = agent_decide.validate_payload(
                asof=START, payload=new, pool_codes=set(pool["codes"]),
                held_qty=pos0, marks=marks, total_assets=total0)
            new = agent_decide.rebase_payload(
                new, total_assets=total0, marks=marks, positions=pos0)
            old = _p68_v2_payload(
                arm=ARM_AGENT_RANDOM, asof=START, pool_codes=set(pool["codes"]),
                held_qty=pos0, marks=marks, total_assets=total0, seed=seed)
            # 判据 4 的黄金表：`k` 与 `picks` 逐位不变（**只有敞口那一档数变**，
            # 所以权重**不**逐位相同 —— 权重 = 敞口 × rand/Σrand）。
            assert [d["code"] for d in new["decisions"]] \
                == [d["code"] for d in old["decisions"]], seed
            same_draw += 1
            if run(new):
                rejected.append(seed)
        assert same_draw == 200, "对照组必须真的跑了 200 个种子（不是空转）"
        assert rejected == [], f"口径 v3 之后不该再有透支的种子：{rejected}"
    finally:
        c.close()


def _p66_old_cadence_payload(seed: int, *, pool_codes, held_qty, marks,
                             total_assets) -> dict:
    """**旧口径（v1）** 的载荷：`exposure = rng.uniform(0.0, 100.0)`。

    这是 `git show HEAD:stocklab/paper/agent_decide.py` 里那段抽样的**冻结副本**
    —— 一字不改地抄过来。本站删掉的正是这个区间，所以这里**不能**调用
    `random_payload`：反向自检要证明的是「**闸门**没被顺手改弱」，
    而不是「新口径的载荷会被拒」（后者是同义反复）。
    """
    rng = random.Random(agent_decide._seed_material(ARM_AGENT_RANDOM, START, seed))
    usable = sorted(c for c in pool_codes if c in marks)
    lo, hi = RANDOM_N_CODES
    k = max(1, min(len(usable), rng.randint(lo, hi)))
    picks = sorted(rng.sample(usable, k))
    exposure = round(rng.uniform(0.0, 100.0), 2)              # ← v1 的区间
    raw = [rng.random() for _ in picks]
    total = sum(raw)
    weights = [round(exposure * w / total, 2) for w in raw] if total else \
        [0.0 for _ in picks]
    drift = round(exposure - sum(weights), 2)
    weights[-1] = round(weights[-1] + drift, 2)
    items: list[dict] = []
    for code, weight in zip(picks, weights):
        if weight <= 0:
            continue
        qty = int(held_qty.get(code, 0) or 0)
        price = float(marks[code].price)
        target_value = round(total_assets * weight / 100.0, 4)
        items.append({
            "code": code, "target_weight_pct": weight,
            "side": agent_decide.side_for(
                target_value=target_value,
                current_value=round(price * qty, 4)) or agent_decide.SIDE_BUY,
            "reason": f"夹具：旧口径（v1）抽中 {code}"})
    return {"asof": START, "decisions": items,
            "cash_pct": round(100.0 - sum(d["target_weight_pct"] for d in items), 2),
            "rationale": f"夹具：**旧口径（v1）** uniform(0, 100)，seed={seed}"}


#: 旧口径（v1）在夹具账户上 seed 0..39 的被拒种子（**改动前实测 11/40**，
#: 与 P65 任务书 §7.7-1 的「40 个种子粒度上实测 11/40」逐位相同）。
P66_OLD_CADENCE_REJECTED_IN_40 = [3, 8, 12, 14, 17, 26, 27, 29, 30, 32, 38]


def test_p66_the_gate_still_rejects_an_old_cadence_payload(db):
    """**反向自检**：手工构造**旧口径**（`uniform(0, 100)`）的载荷 ⇒ 必须仍被拒。

    本站修的是**口径**（`random_payload` 的抽取区间），不是闸门。把闸门顺手删掉
    或改弱（「只记日志不拒」/「按可用现金缩单」）的那一刻，这条用例必须判红。

    seed 8 是刻意挑的：那份旧载荷的 picks 是 `['510300']`（**不**含存量票），
    敞口 98.51% ⇒ 拒绝的理由是「目标敞口 > 可用现金」，与「整手卖不出存量票」
    那条残余缺陷**无关** ⇒ 它单独证明闸门对**本站已经不可能再产出的那种载荷**
    依然是活的（口径 v3 的 `cap ≤ 47.85` 让它不可构造）。
    """
    c = connect(db)
    try:
        state = paper_engine.arm_state_for(c, ARM_AGENT_RANDOM, START)
        pool = agent_pool.pool_snapshot(c, START)
        marks = {**paper_engine.resolve_marks(c, set(pool["codes"]), START),
                 **state["marks"]}
        cash0 = float(state["cash"])
        total0 = float(state["total_assets"])
        pos0 = dict(state["positions"])

        def run(payload):
            validated = agent_decide.validate_payload(
                asof=START, payload=payload, pool_codes=set(pool["codes"]),
                held_qty=pos0, marks=marks, total_assets=total0)
            rebased = agent_decide.rebase_payload(
                validated, total_assets=total0, marks=marks, positions=pos0)
            return agent_decide.execute_decision(
                c, arm=ARM_AGENT_RANDOM, asof=START, decision=rebased, cash=cash0,
                positions=dict(pos0), marks=marks, total_assets=total0)

        old_payload = _p66_old_cadence_payload(
            8, pool_codes=set(pool["codes"]), held_qty=pos0, marks=marks,
            total_assets=total0)
        assert round(sum(d["target_weight_pct"] for d in old_payload["decisions"])
                     + old_payload["cash_pct"], 6) == 100.0, \
            "载荷本身合规 —— 下面那次拒绝不可能来自载荷层那道闸门"
        assert "000333" not in [d["code"] for d in old_payload["decisions"]], \
            "seed 8 的旧载荷刻意不含存量票（见 docstring）"
        assert round(100.0 - old_payload["cash_pct"], 2) > _p66_number(
            _p66_payload(8, held_qty=pos0), "cap"), \
            "口径 v3 的 cap 必须让这种载荷不可构造，否则本用例没在测「已删掉的那条路」"
        with pytest.raises(agent_decide.CashShortfall) as exc:
            run(old_payload)
        assert exc.value.code == "cash", "读数上必须能与载荷错分开"

        # 同一条读数逐 seed 钉住：旧口径 11/40、口径 v3 **0/40**（都是实测）。
        old_rejected = [s for s in range(40)
                        if _p66_is_rejected(run, _p66_old_cadence_payload(
                            s, pool_codes=set(pool["codes"]), held_qty=pos0,
                            marks=marks, total_assets=total0))]
        new_rejected = [s for s in range(40)
                        if _p66_is_rejected(run, agent_decide.random_payload(
                            arm=ARM_AGENT_RANDOM, asof=START,
                            pool_codes=set(pool["codes"]), held_qty=pos0,
                            marks=marks, total_assets=total0, seed=s))]
        assert old_rejected == P66_OLD_CADENCE_REJECTED_IN_40, old_rejected
        assert new_rejected == P66_FIXTURE_RESIDUAL_SEEDS == [], new_rejected
        assert set(new_rejected) <= set(old_rejected), \
            "新口径的被拒集合必须是旧口径的**子集**（口径只收紧了上界）"

        # 直接喂闸门：结算后的现金为负 ⇒ 拒（不经 `plan_orders` 的独立一读）。
        with pytest.raises(agent_decide.CashShortfall):
            agent_decide._gate_cash(
                arm=ARM_AGENT_RANDOM, asof=START, cash_before=cash0,
                cash_after=-1.0, positions=pos0, marks=marks,
                total_assets=total0)
    finally:
        c.close()


def _p66_is_rejected(run, payload) -> bool:
    try:
        run(payload)
    except agent_decide.CashShortfall:
        return True
    return False
