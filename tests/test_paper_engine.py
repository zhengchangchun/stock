"""P19：模拟盘引擎（`stocklab/paper/{store,engine}.py`）。

验收项在这里钉死：① 幂等（同日重跑零变化）、② 成本按标的口径（ETF 无印花税）、
⑤ 禁未来函数（喂未来价必须报错），以及 append-only 与「实盘账本镜像」语义。
"""

import json
import sqlite3

import pytest

from stocklab.paper import agent_spec, engine
from stocklab.paper import store
from stocklab.paper.rules import Decision
from stocklab.paper.config import (
    ARM_AGENT,
    ARM_AGENT_RANDOM,
    ARM_HOLD,
    ARM_KIND_AGENT,
    ARM_KIND_AGENT_RANDOM,
    ARM_NOW,
    DISCLAIMER,
    ETF_TRANCHES,
    ETF_WHITELIST,
    HOLD_CODE,
    INITIAL_CAPITAL,
    PAPER_START_DATE,
    PER_STEP_CASH_PCT,
)
from stocklab.portfolio.prices import Price
from stocklab.paper.rules import LookaheadError
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
CAL = ("2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17")

#: (code, 各日收盘)。09-15 = 87.23（与真实库一致），之后两天给两个方向不同的价，
#: 让「净值随价格走」可被观察到。
BARS = {
    "000333": {"2026-09-11": 86.26, "2026-09-14": 86.80, "2026-09-15": 87.23,
               "2026-09-16": 87.60, "2026-09-17": 86.10},
    "510300": {"2026-09-11": 4.510, "2026-09-14": 4.552, "2026-09-15": 4.523,
               "2026-09-16": 4.500, "2026-09-17": 4.480},
    "510880": {"2026-09-11": 3.400, "2026-09-14": 3.404, "2026-09-15": 3.382,
               "2026-09-16": 3.390, "2026-09-17": 3.400},
    "600690": {"2026-09-15": 21.17, "2026-09-16": 21.30, "2026-09-17": 21.00},
    "sh000300": {"2026-09-11": 4510.16, "2026-09-14": 4480.08,
                 "2026-09-15": 4450.04, "2026-09-16": 4460.00,
                 "2026-09-17": 4430.00},
}
INSTRUMENTS = (("000333", "美的集团", "sz", "stock"), ("600690", "海尔智家", "sz", "stock"),
               ("510300", "沪深300ETF", "sh", "etf"), ("510880", "红利ETF", "sh", "etf"))
SEED_FEE = 5.09

#: #49 的跨天回归专用：两个**全部收在止损线 82.14 之下**的交易日。
#: 单独一套而不是改 `CAL`/`BARS` —— 那两个被其他测试的逐日循环共用，
#: 往里面加一天会静默改变它们的覆盖面。
CAL_LOW = ("2026-09-18", "2026-09-21")
BARS_LOW = {
    "2026-09-18": {"000333": 80.00, "510300": 4.470, "510880": 3.360},
    "2026-09-21": {"000333": 79.00, "510300": 4.460, "510880": 3.350},
}

#: `init` 建出的全部账户：hold + now + 三档纪律臂 + 智能体臂与它的随机对照。
#: **从配置推出来**，不写死 7 —— 下一次多一条臂时，这里与下面几条计数一起自动跟上。
AGENT_ARMS = (ARM_AGENT, ARM_AGENT_RANDOM)
DISCIPLINE_ARMS = tuple(f"arm-discipline-{int(t):02d}" for t in ETF_TRANCHES)
ALL_ARMS = (ARM_HOLD, ARM_NOW, *DISCIPLINE_ARMS, *AGENT_ARMS)


@pytest.fixture
def db(tmp_db):
    """真实库的等价最小复现：日历 + 标的 + 行情 + 实盘账本（本金 20000 / 100 股）。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,?, 'main', ?, ?)",
                  [(code, name, mkt, typ, NOW) for code, name, mkt, typ in INSTRUMENTS])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    rows = [(code, d, close, "none", NOW)
            for code, series in BARS.items() for d, close in series.items()]
    c.executemany("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
                  " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
                  [(code, d, close, close, close, close, NOW)
                   for code, d, close, _adj, _n in rows])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES ('2026-09-14','deposit',?,'本金',?)", (INITIAL_CAPITAL, NOW))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note, created_at)"
              " VALUES ('2026-09-14',?, 'buy', 86.80, 100, ?, '实盘首笔', ?)",
              (HOLD_CODE, SEED_FEE, NOW))
    c.commit()
    c.close()
    return tmp_db


def _conn(db):
    return connect(db)


def _run_init(db, **kw):
    c = _conn(db)
    try:
        return engine.init_accounts(c, now=NOW, **kw)
    finally:
        c.close()


def _run_step(db, asof, **kw):
    c = _conn(db)
    try:
        return engine.step(c, asof, now=NOW, **kw)
    finally:
        c.close()


# ---------- init ----------

def test_init_creates_hold_now_discipline_and_agent_arms(db):
    rep = _run_init(db)
    assert rep["created"] is True
    c = _conn(db)
    rows = {r["account_id"]: dict(r) for r in c.execute("SELECT * FROM paper_accounts")}
    c.close()
    assert set(rows) == set(ALL_ARMS)
    assert rows[ARM_HOLD]["arm"] == "hold" and rows[ARM_HOLD]["etf_target_pct"] is None
    assert rows[ARM_NOW]["arm"] == "now"
    assert [rows[f"arm-discipline-{int(t):02d}"]["etf_target_pct"]
            for t in ETF_TRANCHES] == list(ETF_TRANCHES)
    # 起跑口径：本金 20000、100 股、现金 20000 − 8680 − 5.09
    assert rows[ARM_HOLD]["initial_cash"] == pytest.approx(20000 - 8680 - 5.09)
    # 起跑口径 = **09-15 收盘**的市值 + 现金（不是本金原值：入场费已花掉、
    # 持仓按当日收盘重估）。100×87.23 + 11314.91 = 20037.91
    assert rows[ARM_HOLD]["initial_nav"] == pytest.approx(100 * 87.23 + 11314.91)
    assert rows[ARM_HOLD]["initial_nav"] != pytest.approx(20000)


def test_init_wires_the_agent_arms_to_the_ledger_without_copying_the_spec(db):
    """智能体臂的 `etf_target_pct` 写 `None` —— ETF 目标来自台账，不抄第二份真相。

    把当时的 spec 抄进账户行，等于在库里存下第二个真相，而它**不随 spec 变**：
    `paper spec set` 改了台账之后，那一列会开始说谎。所以它必须是 `None`。
    """
    _run_init(db)
    c = _conn(db)
    rows = {r["account_id"]: dict(r) for r in c.execute("SELECT * FROM paper_accounts")}
    c.close()
    agent = rows[ARM_AGENT]
    random_arm = rows[ARM_AGENT_RANDOM]
    assert agent["arm"] == ARM_KIND_AGENT
    assert random_arm["arm"] == ARM_KIND_AGENT_RANDOM
    assert agent["etf_target_pct"] is None and random_arm["etf_target_pct"] is None
    # 两臂都指向同一张台账；random 还指名它对的是哪一条臂。
    assert json.loads(agent["params_json"])["spec_source"] == \
        agent_spec.TABLE_DECISIONS
    assert json.loads(random_arm["params_json"])["counter_arm"] == ARM_AGENT
    # 账户行里**没有** spec 的当前值（避免第二份真相）；`agent_default_spec` 只是
    # `init` 当时那一份默认值的快照，改配置不会回写已存账户，所以它必须与现推一致
    # —— 不一致就说明「默认 spec 从配置推出来」这句话在 init 那一刻就不成立了。
    params = json.loads(agent["params_json"])
    assert params["agent_default_spec"] == agent_spec.AGENT_DEFAULT_SPEC
    assert "spec" not in params and "spec_sha256" not in params


def test_init_rejects_ledger_disagreeing_with_declared_tape(db):
    """账本说的不是「100 股 @86.80」→ **报错**，不静默采用账本（口径先定死）。"""
    c = _conn(db)
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-14',?,'buy',86.80,100,5.09,'再来一笔',?)",
              (HOLD_CODE, NOW))
    c.commit()
    with pytest.raises(engine.LedgerMismatchError):
        engine.init_accounts(c, now=NOW)
    c.close()


def test_init_is_idempotent(db):
    _run_init(db)
    again = _run_init(db)
    assert again["created"] is False          # 已存在 → 不重复建、不改写
    c = _conn(db)
    n = c.execute("SELECT COUNT(*) n FROM paper_accounts").fetchone()["n"]
    c.close()
    assert n == len(ALL_ARMS)


# ---------- step：幂等（验收 ①） ----------

def test_step_is_byte_identical_on_rerun(db):
    """同日重跑：JSON 逐字节一致、不重复下单、不新增净值行。"""
    _run_init(db)
    first = _run_step(db, PAPER_START_DATE)
    c = _conn(db)
    n_trades = c.execute("SELECT COUNT(*) n FROM paper_trades").fetchone()["n"]
    n_nav = c.execute("SELECT COUNT(*) n FROM paper_nav_daily").fetchone()["n"]
    c.close()
    assert n_nav == len(ALL_ARMS)
    assert n_trades > 0, "纪律臂起跑日应当建仓"

    second = _run_step(db, PAPER_START_DATE)
    # 载荷只由「库里的行 + PIT 收盘价」决定 → 重跑**逐字节一致**（含净值/决定/规则评估）
    assert json.dumps(second, sort_keys=True, ensure_ascii=False) == \
           json.dumps(first, sort_keys=True, ensure_ascii=False)
    c = _conn(db)
    assert c.execute("SELECT COUNT(*) n FROM paper_trades").fetchone()["n"] == n_trades
    assert c.execute("SELECT COUNT(*) n FROM paper_nav_daily").fetchone()["n"] == n_nav
    c.close()


def test_step_before_start_date_is_rejected(db):
    _run_init(db)
    with pytest.raises(engine.PaperError) as e:
        _run_step(db, "2026-09-14")
    assert "起跑日" in str(e.value)


# ---------- 成本按标的口径（验收 ②） ----------

def test_etf_buy_has_zero_stamp_tax_in_db(db):
    _run_init(db)
    _run_step(db, PAPER_START_DATE)
    c = _conn(db)
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM paper_trades WHERE asset_class='etf'")]
    c.close()
    assert rows, "纪律臂起跑日应当建仓 ETF"
    for r in rows:
        assert r["stamp_tax"] == 0.0                     # ETF 免征（ADR-008）
        assert r["commission"] >= 5.0                    # 最低佣金
        assert r["code"] in ETF_WHITELIST
        assert r["rule_citation"]                          # 规则条文不许留空


def test_stock_sell_pays_stamp_tax(db):
    """跌破 82.14 → 整清 000333，卖出**要**收印花税（口径的另一侧）。"""
    c = _conn(db)
    c.execute("UPDATE bars_daily SET close=82.00 WHERE code='000333' AND date='2026-09-15'")
    c.commit()
    c.close()
    _run_init(db)
    _run_step(db, PAPER_START_DATE)
    c = _conn(db)
    row = c.execute("SELECT * FROM paper_trades WHERE account_id=? AND side='sell'",
                    (ARM_HOLD,)).fetchone()
    # arm-hold 不动：止损是纪律臂的动作，对照臂**不许**被殃及
    assert row is None
    row = c.execute("SELECT * FROM paper_trades WHERE account_id='arm-discipline-05'"
                    " AND code=? AND side='sell'", (HOLD_CODE,)).fetchone()
    assert row is not None and row["qty"] == 100
    assert row["stamp_tax"] > 0.0
    c.close()


# ---------- 纪律：不越界 ----------

def test_no_buy_on_non_whitelist_or_held_stock(db):
    """全臂扫一遍：除了白名单 ETF，不许有第二个买入标的（换家电股不算分散）。"""
    _run_init(db)
    for asof in ("2026-09-15", "2026-09-16", "2026-09-17"):
        _run_step(db, asof)
    c = _conn(db)
    buys = {r["code"] for r in c.execute("SELECT DISTINCT code FROM paper_trades"
                                        " WHERE side='buy'")}
    c.close()
    assert buys and buys <= set(ETF_WHITELIST)


def test_cash_floor_and_single_cap_hold_every_day(db):
    """逐日验证：现金 ≥45%、000333 权重 ≤40%（或如实标注未消除）。"""
    _run_init(db)
    for asof in CAL[2:]:
        rep = _run_step(db, asof)
        for acc in rep["accounts"]:
            for chk in acc["discipline"]:
                assert chk["status"] != "FAIL" or chk["check"] == "single_position_max_40pct"
    c = _conn(db)
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM paper_nav_daily WHERE account_id LIKE 'arm-discipline-%'")]
    c.close()
    assert rows
    for r in rows:
        pos = {p["code"]: p["qty"] for p in json.loads(r["positions_json"])}
        assert r["cash"] >= 0.45 * r["nav"] - 1e-6, "现金下限被击穿"
        assert pos.get(HOLD_CODE, 0) <= 100


def test_15pct_tranche_cannot_reach_target_while_hold_blocks_cash(db):
    """**本臂要暴露的结论**：43.53% 单票 + 45% 现金下限 → ETF 上限只有 11.47%。

    所以 15% 档在当前持仓下**建不满**，卡在现金下限上 —— 报告必须写明，
    而且必须记为 binding（不许静默停在 11%）。
    """
    _run_init(db)
    binding_seen = False
    for asof in CAL[2:]:
        rep = _run_step(db, asof)
        for acc in rep["accounts"]:
            if acc["account_id"] == "arm-discipline-15":
                for d in acc["decisions"]:
                    if "cash_band_floor_45pct" in d["binding_constraints"]:
                        binding_seen = True
    c = _conn(db)
    row = c.execute("SELECT * FROM paper_nav_daily WHERE account_id='arm-discipline-15'"
                    " ORDER BY date DESC LIMIT 1").fetchone()
    c.close()
    pos = {p["code"]: p["qty"] for p in json.loads(row["positions_json"])}
    etf_value = sum(
        q * BARS[code][row["date"]] for code, q in pos.items() if code in ETF_WHITELIST)
    assert etf_value / row["nav"] * 100.0 < 15.0
    assert binding_seen, "15% 档建不满时必须留下 cash_floor 的 binding 痕迹"


def test_per_step_cap_not_exceeded_on_any_day(db):
    """每个 step 的**合计**新动用现金 ≤5% 总资产（单次 = 每步累计）。"""
    _run_init(db)
    for asof in CAL[2:]:
        _run_step(db, asof)
        c = _conn(db)
        for acc in [r["account_id"] for r in c.execute(
                "SELECT DISTINCT account_id FROM paper_trades")]:
            spent = c.execute(
                "SELECT COALESCE(SUM(fill_price*qty+fee_total),0) s FROM paper_trades"
                " WHERE account_id=? AND date=? AND side='buy'", (acc, asof)).fetchone()["s"]
            nav = c.execute("SELECT nav FROM paper_nav_daily WHERE account_id=? AND date=?",
                            (acc, asof)).fetchone()
            if nav is not None:
                assert spent <= PER_STEP_CASH_PCT / 100.0 * nav["nav"] + 1e-6
        c.close()


# ---------- arm-now 是实盘账本的镜像 ----------

def test_arm_now_mirrors_ledger_while_hold_stays_frozen(db):
    _run_init(db)
    _run_step(db, PAPER_START_DATE)
    c = _conn(db)
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-16','510300','buy',4.500,200,5.00,"
              "'实盘买了 ETF',?)", (NOW,))
    c.commit()
    c.close()
    rep = _run_step(db, "2026-09-16")
    by_id = {a["account_id"]: a for a in rep["accounts"]}
    assert by_id[ARM_NOW]["positions"].get("510300") == 200       # 镜像跟上了
    assert "510300" not in by_id[ARM_HOLD]["positions"]           # 对照臂冻住
    assert by_id[ARM_HOLD]["positions"][HOLD_CODE] == 100
    # 出入金会改变 NAV 但不是收益：net_deposits 单独记，累计收益按净入金算
    assert by_id[ARM_NOW]["net_deposits"] == pytest.approx(INITIAL_CAPITAL)


def test_arm_hold_nav_moves_with_price(db):
    _run_init(db)
    _run_step(db, PAPER_START_DATE)
    rep = _run_step(db, "2026-09-16")
    hold = next(a for a in rep["accounts"] if a["account_id"] == ARM_HOLD)
    assert hold["nav"] == pytest.approx(100 * 87.60 + (20000 - 8680 - 5.09))
    assert hold["marks"]["000333"]["price"] == 87.60


# ---------- PIT（验收 ⑤） ----------

def test_future_price_raises_at_engine_level(db):
    """把未来价喂进估值/决策入口 → 必须报错（不是截断、不是忽略）。"""
    _run_init(db)
    c = _conn(db)
    with pytest.raises(LookaheadError):
        engine.step(c, PAPER_START_DATE, now=NOW, prices={
            "000333": Price(code="000333", price=99.0, source="bars",
                            price_asof="2026-09-16", detail="future"),
        })
    c.close()


def test_valuation_uses_close_not_intraday_snapshot(db):
    """收盘净值认 `bars_daily` 收盘价；盘中有个更高的快照也不许用它。"""
    c = _conn(db)
    c.execute("INSERT INTO quote_snapshots (code, trade_date, ts, price, volume,"
              " fetched_at, source) VALUES ('000333','2026-09-15','20260915135109',"
              "91.00,100, ?, 'tencent')", (NOW,))
    c.commit()
    c.close()
    _run_init(db)
    rep = _run_step(db, PAPER_START_DATE)
    hold = next(a for a in rep["accounts"] if a["account_id"] == ARM_HOLD)
    assert hold["nav"] == pytest.approx(100 * 87.23 + (20000 - 8680 - 5.09))
    assert hold["marks"][HOLD_CODE]["source"] == "bars"
    assert hold["marks"][HOLD_CODE]["price"] == 87.23


# ---------- append-only ----------

@pytest.mark.parametrize("table", ["paper_accounts", "paper_trades", "paper_nav_daily"])
def test_append_only_triggers_block_update_and_delete(db, table):
    _run_init(db)
    _run_step(db, PAPER_START_DATE)
    c = _conn(db)
    assert c.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"] > 0
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(f"UPDATE {table} SET created_at='x'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(f"DELETE FROM {table}")
    c.close()


def test_store_refuses_zero_share_or_hold_decisions(db):
    """写入层的两道校验都报**点名规则层**的 `ValueError`，不靠 `CHECK` 兜底。

    直接构造 `Decision`：即便将来有人把 `is_trade` 重新定义回去，
    写入层也不会把一个空操作写进去，更不会把整天的 step 事务拖垮（#49）。
    """
    _run_init(db)
    c = _conn(db)
    try:
        with pytest.raises(ValueError, match="hold 决定不能写成交"):
            store.insert_trade(
                c, account_id=ARM_HOLD, date="2026-09-16", now=NOW,
                decision=Decision(action="hold", code=HOLD_CODE, qty=100,
                                  rule_citation="", reason="不动"))
        with pytest.raises(ValueError, match="不是可执行的股数"):
            store.insert_trade(
                c, account_id=ARM_HOLD, date="2026-09-16", now=NOW,
                decision=Decision(action="sell", code=HOLD_CODE, qty=0,
                                  rule_citation="止损", reason="整清 0 股"))
    finally:
        c.close()


def test_ledger_cash_formula_matches_ledger_module_when_unfiltered(db):
    """arm-now 的现金公式必须与 `ledger.cash_summary` 同源（两处算迟早会漂移）。"""
    from stocklab.portfolio.ledger import cash_summary
    c = _conn(db)
    mine = engine.ledger_state(c, "2026-09-17")
    theirs = cash_summary(c)
    c.close()
    assert mine["cash"] == pytest.approx(theirs["cash"])
    assert mine["net_deposits"] == pytest.approx(theirs["net_deposits"])


# ---------- 报告 ----------

def test_report_has_three_arms_and_disclaimer(db, tmp_path):
    _run_init(db)
    _run_step(db, PAPER_START_DATE)
    c = _conn(db)
    md = engine.render_report(engine.build_report(c, PAPER_START_DATE))
    c.close()
    assert "arm-hold" in md and "arm-now" in md
    for t in ETF_TRANCHES:
        assert f"arm-discipline-{int(t):02d}" in md
    assert "最大回撤" in md and "累计成本" in md and "index_300" in md
    assert "样本 <120 交易日不算结论" in md
    assert "LIVE 仍为 0" in md
    assert "模拟盘 ≠ 实盘" in DISCLAIMER and "模拟盘 ≠ 实盘" in md


def test_step_after_stop_loss_cleared_the_position_does_not_crash(db):
    """跳天回归（ERROR_DIARY #49）：今天止损清仓 → 明天收盘仍在线下，step 仍要出净值。

    曾经的失败形态：止损规则产出「卖 0 股」→ `is_trade` 为真 → 撞上
    `paper_trades` 的 `CHECK (qty > 0)` → `step` 是一个事务
    ⇒ **当天五个账户一条净值都没落**，而且**每天**都会重现。
    """
    _run_init(db)
    c = _conn(db)
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL_LOW])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, px, px, px, px, NOW)
         for d, series in BARS_LOW.items() for code, px in series.items()])
    c.commit()
    c.close()

    _run_step(db, "2026-09-16")
    _run_step(db, CAL_LOW[0])            # 收盘 80.00 < 止损线 82.14 → 纪律臂整清
    rep = _run_step(db, CAL_LOW[1])      # 仍在线下、已无持仓 → 不许炸
    by_id = {a["account_id"]: a for a in rep["accounts"]}
    assert len(rep["accounts"]) == len(ALL_ARMS), "整天的净值必须都落库（事务不许半截）"
    for tranche in ETF_TRANCHES:
        acc = by_id[f"arm-discipline-{int(tranche):02d}"]
        assert HOLD_CODE not in acc["positions"]
        assert all(d["code"] != HOLD_CODE for d in acc["decisions"]), \
            "已清仓的止损不该再产出成交（#49）"
    c = _conn(db)
    try:
        zero = c.execute("SELECT COUNT(*) n FROM paper_trades WHERE qty <= 0"
                         ).fetchone()["n"]
    finally:
        c.close()
    assert zero == 0
