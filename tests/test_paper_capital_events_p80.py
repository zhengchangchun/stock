"""P80 T1/T2：资本事件（`paper_capital_events`）＋ 口径接线。

本站要修的第一个故障是**口径**：AI 家族本金要 2 万 → 5 万，而
`paper_accounts` / `paper_nav_daily` 都是 append-only，就地改 `initial_cash`
会让 09-23/09-24 那 5×2 行净值**当场变成假账**（P62 的不变量
`nav == initial_cash + 重放成交 + mtm` 立刻不成立）。所以本金变更走**事件**：
`paper_capital_events` 一条入金，生效日 2026-09-28。

判据（逐条对应任务书 T1 / T2）：

| # | 判据 | 被摘掉后会红的实现 |
|---|---|---|
| ① | 同 `idem` 二次插入不新增、不报错 | 重跑 `paper init` 给同一笔入金记两行 |
| ② | `UPDATE` / `DELETE` / `INSERT OR REPLACE` 被触发器拒 | 改历史入金（= 伪造「当时有多少钱」） |
| ③ | 结构前滚幂等可重入；触发器漂移能补回 | 看着有表、其实能改历史行 |
| ④ | 入金日之前 `net_deposits == 20000`、当日 `== 50000`、现金 `+30000` | 把 5 万从起跑日生效（历史净值行变假） |
| ⑤ | `withdraw` 反向对称 | 符号只在 `kind` 一处解释这条纪律破掉 |
| ⑥ | 三条建账路径都自动挂上（幂等） | 只有 `paper init` 挂上了，`enroll` 漏 |
| ⑦ | 静态臂 / `arm-now` 零影响（净入金恒 20000） | 事件被挂到了所有臂上 |
| ⑧ | `cum_cost` 不因入金变化 | 把入金当成本（累计成本凭空 +3 万） |
| ⑨ | `seed_agent_capital_topup` 只准显式调用、只登记 marker | 挂进 `_apply_schema` ⇒ 某次写库顺手改本金 |
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from stocklab.paper import engine
from stocklab.paper import store as paper_store
from stocklab.paper.config import (AGENT_CAPITAL_TOPUP_DATE,
                                   AGENT_CAPITAL_TOPUP_IDEM,
                                   AGENT_EFFECTIVE_CAPITAL, ARM_AGENT,
                                   ARM_HOLD, INITIAL_CAPITAL, PAPER_START_DATE)
from stocklab.store.db import connect
from stocklab.store.migrate import (agent_capital_topup_missing,
                                    capital_events_need_p80, ensure_schema,
                                    init_db, migrate_p80_capital_events,
                                    schema_status, seed_agent_capital_topup)

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE
TOPUP = AGENT_CAPITAL_TOPUP_DATE          # 2026-09-28
BEFORE_TOPUP = "2026-09-25"
CAL = ("2026-09-11", "2026-09-14", START, "2026-09-24", BEFORE_TOPUP, TOPUP)
BARS = {
    "000333": {"2026-09-14": 86.80, START: 87.23},
    "510300": {START: 4.523},
    "510880": {START: 3.382},
}
POOL = tuple(BARS)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p80-capital.db"
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
    c.close()
    return path


def _acct(c, aid):
    return next(a for a in paper_store.load_accounts(c) if a["account_id"] == aid)


def _state(c, aid, asof):
    return engine._ledger_arm_state(c, _acct(c, aid), asof)


# ---------- ⑥ 三条建账路径都自动挂上 ----------

def test_t2_paper_init_attaches_the_topup_to_the_whole_agent_family(db):
    """⑥（路径①）：`paper init` 建的 AI 家族两条臂各有一条标准入金。

    静态臂（`arm-hold` / `arm-discipline-*`）**一条都没有** —— 本金分家（D1）。
    """
    c = connect(db)
    try:
        ai = sorted(a["account_id"] for a in paper_store.load_accounts(c)
                    if a["account_id"].startswith(ARM_AGENT))
        assert ai == [ARM_AGENT, f"{ARM_AGENT}-random"], ai
        for aid in ai:
            rows = paper_store.load_capital_events(c, aid)
            assert len(rows) == 1, (aid, rows)
            assert rows[0]["date"] == TOPUP and rows[0]["kind"] == "deposit"
            assert rows[0]["amount"] == 30000.0
            assert rows[0]["idem"] == engine.agent_capital_events_for(aid)[0][4]
            assert rows[0]["idem"].startswith(AGENT_CAPITAL_TOPUP_IDEM)
        assert paper_store.load_capital_events(c, ARM_HOLD) == []
        assert paper_store.load_capital_events(c, "arm-discipline-10") == []
    finally:
        c.close()


def test_t2_enroll_attaches_the_topup_to_the_version_account(db):
    """⑥（路径②）：`paper agent enroll` 建的新版本账户同样自动挂上。"""
    c = connect(db)
    try:
        engine.enroll_agent_arm(c, arm_name="arm-agent-p80t2", model_id="m",
                                prompt_sha256="a" * 64, now=NOW, start_date=START)
        rows = paper_store.load_capital_events(c, "arm-agent-p80t2")
        assert len(rows) == 1
        assert rows[0]["idem"] == engine.agent_capital_events_for("arm-agent-p80t2")[0][4]
    finally:
        c.close()


def test_t2_channel_a_account_attaches_the_topup(db):
    """⑥（路径③）：通路 A 的 `arm-agent-<策略版本>` 也挂（三条路径一个 helper）。"""
    from stocklab.m2 import channel_a

    c = connect(db)
    try:
        rep = channel_a.create_account(c, strategy_version="p80t3", now=NOW)
        aid = rep["account_id"]
        assert aid.startswith("arm-agent-")
        rows = paper_store.load_capital_events(c, aid)
        assert len(rows) == 1
        assert rows[0]["idem"] == engine.agent_capital_events_for(aid)[0][4]
    finally:
        c.close()


def test_t2_the_three_paths_agree_on_the_event(db):
    """⑥（对拍）：三条路径挂上的事件**逐字段相同**（`agent_capital_events_for` 一处定义）。"""
    from stocklab.m2 import channel_a

    c = connect(db)
    try:
        engine.enroll_agent_arm(c, arm_name="arm-agent-p80t4", model_id="m",
                                prompt_sha256="b" * 64, now=NOW, start_date=START)
        aid = channel_a.create_account(c, strategy_version="p80t4b",
                                       now=NOW)["account_id"]
        want = engine.agent_capital_events_for(ARM_AGENT)
        assert len(want) == 1
        for arm in (ARM_AGENT, "arm-agent-p80t4", aid):
            r = paper_store.load_capital_events(c, arm)[0]
            # 日 / 方向 / 金额 / 理由**逐字段相同**；幂等键带账户后缀（全表 UNIQUE、
            # 5 条账户各挂自己那条）—— 后缀由同一个 helper 推出，不手抄。
            assert (r["date"], r["kind"], r["amount"], r["note"]) == want[0][:4]
            assert r["idem"] == engine.agent_capital_events_for(arm)[0][4]
            assert r["idem"].startswith(AGENT_CAPITAL_TOPUP_IDEM)
        # 非 AI 家族 ⇒ 空（本金分家的判据本身）
        assert engine.agent_capital_events_for(ARM_HOLD) == []
    finally:
        c.close()


# ---------- ① 幂等 ----------

def test_t1_the_same_idem_lands_once(db):
    """①：同 `idem` 落两次 ⇒ 仍 1 行、不报错（`INSERT OR IGNORE`）。"""
    c = connect(db)
    try:
        first = paper_store.insert_capital_event(
            c, account_id=ARM_AGENT, date="2026-09-29", kind="deposit",
            amount=100.0, note="夹具", idem="p80t1-dup", now=NOW)
        again = paper_store.insert_capital_event(
            c, account_id=ARM_AGENT, date="2026-09-29", kind="deposit",
            amount=100.0, note="夹具", idem="p80t1-dup", now=NOW)
        assert first == 1 and again == 0
        rows = [r for r in paper_store.load_capital_events(c, ARM_AGENT)
                if r["idem"] == "p80t1-dup"]
        assert len(rows) == 1
    finally:
        c.close()


def test_t2_rerunning_init_writes_nothing(db):
    """①（重跑）：`paper init` 再跑一次 ⇒ 事件不新增、账户行一字不改。"""
    c = connect(db)
    try:
        before_accounts = paper_store.load_accounts(c)
        before_events = {a["account_id"]: paper_store.load_capital_events(c, a["account_id"])
                         for a in before_accounts}
        rep = engine.init_accounts(c, start_date=START, now=NOW)
        assert rep["created"] is False and rep["accounts"] == []
        assert paper_store.load_accounts(c) == before_accounts
        for aid, rows in before_events.items():
            assert paper_store.load_capital_events(c, aid) == rows, aid
        assert agent_capital_topup_missing(c) == []
    finally:
        c.close()


@pytest.mark.parametrize("amount,note", [(0.0, "零"), (-1.0, "负")])
def test_t1_amount_must_be_positive(db, amount, note):
    """①（形状）：`amount` 恒正 —— 方向由 `kind` 表达。

    存有符号数 + `kind` 两个真相，迟早有一处把符号加错，而且看不出（净入金少 3 万
    看起来像「算错了」而不是「符号反了」）。
    """
    c = connect(db)
    try:
        with pytest.raises(ValueError, match="必须为正"):
            paper_store.insert_capital_event(
                c, account_id=ARM_AGENT, date="2026-09-29", kind="deposit",
                amount=amount, note=note, idem=f"p80t1-bad-{amount}", now=NOW)
        with pytest.raises(ValueError, match="不是资本事件"):
            paper_store.insert_capital_event(
                c, account_id=ARM_AGENT, date="2026-09-29", kind="fee",
                amount=1.0, note="夹具", idem="p80t1-bad-kind", now=NOW)
    finally:
        c.close()


# ---------- ② append-only 触发器 ----------

@pytest.mark.parametrize("sql", [
    "UPDATE paper_capital_events SET amount = 90000",
    "DELETE FROM paper_capital_events",
])
def test_t1_append_only_triggers_reject_update_and_delete(db, sql):
    """②：`UPDATE` / `DELETE` 一律被触发器拒（入金是**事实**，改它 = 伪造账目）。

    `recursive_triggers` 是**第三条** SQL（`INSERT OR REPLACE` 的隐式 DELETE）
    能成立的原因（ERROR_DIARY #6）：不开它，隐式删行不触发 DELETE 触发器。
    """
    c = connect(db)
    try:
        assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
        with pytest.raises(sqlite3.Error, match="append-only"):
            c.execute(sql)
    finally:
        c.close()


def test_t1_insert_or_replace_cannot_bypass_the_trigger(db):
    """②（续）：`INSERT OR REPLACE` 靠**隐式 DELETE** 解唯一冲突 —— 也必须被拒。"""
    c = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute(
                "INSERT OR REPLACE INTO paper_capital_events (event_id, account_id,"
                " date, kind, amount, note, idem, created_at)"
                " VALUES (1, ?, ?, 'deposit', 1.0, '覆盖', 'x', ?)",
                (ARM_AGENT, TOPUP, NOW))
    finally:
        c.close()


# ---------- ③ 迁移幂等 / 可重入 ----------

def test_t1_the_migration_is_idempotent_and_repairs_a_drifted_db(db):
    """③：`migrate_p80_capital_events` 幂等可重入；触发器漂移时能补回来。"""
    c = connect(db)
    try:
        assert capital_events_need_p80(c) is False
        assert migrate_p80_capital_events(c) == []

        c.execute("DROP TRIGGER trg_paper_capital_events_no_update")
        c.execute("DROP TRIGGER trg_paper_capital_events_no_delete")
        c.commit()
        assert capital_events_need_p80(c) is True
        assert migrate_p80_capital_events(c) == ["paper_capital_events.triggers"]
        assert capital_events_need_p80(c) is False
        assert migrate_p80_capital_events(c) == []
        # `ensure_schema`（任一写库入口的守护）也认这张表 ⇒ 不把漂移的库前滚。
        assert ensure_schema(db) == []
    finally:
        c.close()


def test_t1_the_table_shape_is_append_only_with_a_unique_idem(db):
    """③（续）：`idem` UNIQUE 在、两个触发器名字与 `paper_agent_evals` 同款。"""
    c = connect(db)
    try:
        ddl = c.execute("SELECT sql FROM sqlite_master WHERE name ="
                        " 'paper_capital_events'").fetchone()[0]
        assert "idem        TEXT NOT NULL UNIQUE" in ddl
        assert "CHECK (kind IN ('deposit','withdraw'))" in ddl
        assert "CHECK (amount > 0)" in ddl
        trig = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE"
            " 'trg_paper_capital_events%'")}
        assert trig == {"trg_paper_capital_events_no_update",
                        "trg_paper_capital_events_no_delete"}
    finally:
        c.close()


# ---------- ⑨ 数据前滚：只登记 marker、只准显式调用 ----------

def test_t1_the_seed_migration_is_explicit_and_idempotent(tmp_path):
    """⑨：数据前滚的探测与写入都幂等，且能补上**建账时还没有这条口径**的老账户。

    夹具刻意**不跑** `engine.init_accounts`（那条路会自动挂事件），而是直接
    `insert_account` 造两条 AI 家族账户 —— 这正是真库里 5 条账户的样子：
    它们在 P80 之前就建好了，事件得由 `seed_agent_capital_topup` 显式补上。
    """
    path = tmp_path / "p80-seed.db"
    init_db(path)
    c = connect(path)
    try:
        c.execute("INSERT INTO paper_accounts (account_id, arm, etf_target_pct,"
                  " start_date, initial_cash, initial_positions_json, initial_nav,"
                  " params_json, created_at) VALUES ('arm-agent-old','agent',NULL,?,"
                  " 11320.0,'[]',20043.0,?,?)",
                  (START, json.dumps({"initial_capital": 20000.0}), NOW))
        c.execute("INSERT INTO paper_accounts (account_id, arm, etf_target_pct,"
                  " start_date, initial_cash, initial_positions_json, initial_nav,"
                  " params_json, created_at) VALUES ('arm-discipline-10',"
                  " 'discipline',10.0,?,11320.0,'[]',20043.0,?,?)",
                  (START, json.dumps({"initial_capital": 20000.0}), NOW))
        c.commit()
        assert agent_capital_topup_missing(c) == ["arm-agent-old"]
        assert seed_agent_capital_topup(c, NOW) == ["arm-agent-old"]
        assert agent_capital_topup_missing(c) == []
        assert seed_agent_capital_topup(c, NOW) == []
        # 老账户的**账户行**一字未改（本金靠事件加，不靠改行）。
        acct = _acct(c, "arm-agent-old")
        assert acct["initial_cash"] == 11320.0 and acct["initial_nav"] == 20043.0
        assert json.loads(acct["params_json"])["initial_capital"] == 20000.0
        assert paper_store.load_capital_events(c, "arm-discipline-10") == []
        # doctor 读得到的 marker：表在、触发器在、该挂的都挂上 ⇒ present。
        st = schema_status(c)["markers"]
        assert st["p80_capital_events"]["present"] is True
        assert st["p80_agent_capital_topup"]["present"] is True
    finally:
        c.close()


# ---------- ④ / ⑤ / ⑦ / ⑧ 口径接线 ----------

def test_t2_net_deposits_jumps_on_the_topup_date_not_before(db):
    """④（本站的核心读数）：入金日**之前** 2 万、**当日** 5 万、现金恰好 `+30000`。

    「之前」这一半是判据的关键：5 万若从起跑日生效，09-23/09-24 那几行净值的
    `net_deposits` 就对不上了（那是 append-only 的历史行，改不得）。
    """
    c = connect(db)
    try:
        assert _state(c, ARM_AGENT, START)["net_deposits"] == INITIAL_CAPITAL
        before = _state(c, ARM_AGENT, BEFORE_TOPUP)
        assert before["net_deposits"] == INITIAL_CAPITAL == 20000.0
        after = _state(c, ARM_AGENT, TOPUP)
        assert after["net_deposits"] == AGENT_EFFECTIVE_CAPITAL == 50000.0
        assert after["cash"] == round(before["cash"] + 30000.0, 4)
    finally:
        c.close()


def test_t2_withdraw_is_the_mirror_image(db):
    """⑤：`withdraw` 反向对称（现金与净入金一起减）——符号只在 `kind` 一处解释。"""
    c = connect(db)
    try:
        paper_store.insert_capital_event(
            c, account_id=ARM_AGENT, date="2026-09-29", kind="withdraw",
            amount=5000.0, note="夹具：取钱", idem="p80t2-wd", now=NOW)
        assert engine.capital_events_sum(c, ARM_AGENT, "2026-09-29") == 25000.0
        after = _state(c, ARM_AGENT, "2026-09-29")
        assert after["net_deposits"] == 45000.0
        assert after["cash"] == round(_state(c, ARM_AGENT, TOPUP)["cash"] - 5000.0, 4)
        # PIT：取钱那天之前一分钱都不受影响。
        assert _state(c, ARM_AGENT, BEFORE_TOPUP)["net_deposits"] == 20000.0
    finally:
        c.close()


def test_t2_static_arms_and_arm_now_are_untouched(db):
    """⑦：静态臂与 `arm-now`（实盘镜像）的净入金**恒 20000**。

    `arm-now` 走 `ledger_state`（读 `cash_flows`）—— 事件挂到它头上就等于给实盘
    凭空加 3 万，而实盘账本才是「我到底有多少钱」的唯一真相。
    """
    c = connect(db)
    try:
        for aid in (ARM_HOLD, "arm-discipline-05", "arm-discipline-10",
                    "arm-discipline-15"):
            assert _state(c, aid, TOPUP)["net_deposits"] == 20000.0, aid
        now_state = engine._arm_state(c, _acct(c, "arm-now"), TOPUP)
        assert now_state["net_deposits"] == 20000.0
        assert paper_store.load_capital_events(c, "arm-now") == []
    finally:
        c.close()


def test_t2_cum_cost_ignores_the_topup(db):
    """⑧：`cum_cost` 不因入金变化 —— 入金不是成本（与 P19 对入场费的处置同一条）。

    如果入金进了成本，页面上「累计成本」会凭空多 3 万，而「成本」这个词的意思
    会从「交易摩擦」变成「任何金额变动」。
    """
    c = connect(db)
    try:
        before = _state(c, ARM_AGENT, BEFORE_TOPUP)
        after = _state(c, ARM_AGENT, TOPUP)
        assert before["cum_cost"] == after["cum_cost"]
        assert after["cum_cost"] == 5.09    # 起跑那笔入场费（`seed_fee`）
    finally:
        c.close()


def test_t2_events_are_recorded_in_the_json_shape_of_the_task(db):
    """④（形状）：一行事件就是任务书 D2 的九个字段，`note` 非空。"""
    c = connect(db)
    try:
        row = paper_store.load_capital_events(c, ARM_AGENT)[0]
        assert set(row) == {"event_id", "account_id", "date", "kind", "amount",
                            "note", "idem", "created_at"}
        assert row["note"] and json.loads(json.dumps(row["note"])) == row["note"]
    finally:
        c.close()
