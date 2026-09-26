"""P80 T3/T4/T5：实时账户与购买力 / 输入侧 objective / 读数面净收益。

| # | 判据 | 被摘掉后会红的实现 |
|---|---|---|
| ① | 默认 asof = 库里有 PIT 收盘价的最近交易日 | 拿 `paper_nav_daily` 最新日期当默认（要等收盘链） |
| ② | `buying_power.max_lots_affordable` 与 `tradability.affordable_lots` **同源同值** | 两处各算一套（页面上两个「能买几手」） |
| ③ | 入金后 `max_lots_affordable` 按现金变多而变大 | 购买力仍按 2 万口径 |
| ④ | 缺账户 / 缺价 / 缺 `instruments.type` 点名，不猜数 | 静默少几行、退化成股票费率 |
| ⑤ | 退出码：0 = 打出载荷、2 = 输入不合法（零写入） | 报错却返回 0 |
| ⑥ | `objective` 进指纹（有/无 ⇒ sha 不同）＋ 文案逐字 | 写了 objective 但没进 `DECISION_HASHED_KEYS` |
| ⑦ | `profit_cny == nav − net_deposits`；入金后累计收益率不跳变 | 净收益按收益率反推 / 入金算成收益 |
"""

from __future__ import annotations

import json

import pytest

from stocklab.cli.main import main
from stocklab.paper import agent_context, engine
from stocklab.paper import store as paper_store
from stocklab.paper.config import (AGENT_CAPITAL_TOPUP_DATE,
                                   AGENT_EFFECTIVE_CAPITAL, ARM_AGENT,
                                   PAPER_START_DATE)
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = PAPER_START_DATE
TOPUP = AGENT_CAPITAL_TOPUP_DATE
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
    path = tmp_path / "p80-account.db"
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


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db), "--now", NOW])
    out, err = capsys.readouterr()
    return code, out, err


# ---------- ① 默认 asof ----------

def test_t3_default_asof_is_the_latest_pit_close_day(db):
    """①：不给 `--asof` ⇒ 取有 PIT 收盘价的最近交易日（**不等收盘链**）。"""
    c = connect(db)
    try:
        view = engine.account_view(c, ARM_AGENT)
        assert view["asof"] == START == "2026-09-15"
        assert view["asof_source"] == "latest_pit_close"
        # 库里一条收盘价都没有 ⇒ **点名报错**，不猜一天。
        c.execute("DELETE FROM bars_daily")
        c.commit()
        with pytest.raises(engine.PaperError, match="一条收盘价都没有"):
            engine.account_view(c, ARM_AGENT)
    finally:
        c.close()


# ---------- ② 购买力与 tradability 同源同值 ----------

def test_t3_buying_power_and_tradability_agree_to_the_cent(db):
    """②（对拍）：`paper account` 的购买力与 AI 上下文里的 `affordable_lots` 同值。"""
    c = connect(db)
    try:
        view = engine.account_view(c, ARM_AGENT, TOPUP)
        ctx = engine.decision_context_for(c, arm=ARM_AGENT, asof=TOPUP)
        trad = ctx["tradability"]
        assert trad["cash"] == view["buying_power"]["cash"]
        assert trad["net_deposits"] == view["account"]["net_deposits"]
        assert set(trad["by_code"]) == set(view["buying_power"]["by_code"])
        for code, row in view["buying_power"]["by_code"].items():
            assert trad["by_code"][code]["one_lot_cost"] == row["one_lot_cost"]
            assert trad["by_code"][code]["affordable_lots"] == row["max_lots_affordable"]
        # 持仓的可卖数（T+1）也进了上下文。
        assert trad["by_code"]["000333"]["sellable_qty"] == \
            view["positions_detail"][0]["sellable_qty"]
    finally:
        c.close()


def test_t3_affordable_lots_grows_with_the_deposit(db):
    """③：入金 3 万 ⇒ 每只 `max_lots_affordable` **变大或持平**，且至少一只变大。

    购买力必须按「现金」算，所以它一定跟着入金走 —— 若不跟着走，说明它读的是
    某个冻结的口径（如 `params_json.initial_capital`）。
    """
    c = connect(db)
    try:
        before = engine.account_view(c, ARM_AGENT, BEFORE_TOPUP)["buying_power"]["by_code"]
        after = engine.account_view(c, ARM_AGENT, TOPUP)["buying_power"]["by_code"]
        assert set(before) == set(after)
        grown = [code for code in before
                 if after[code]["max_lots_affordable"] > before[code]["max_lots_affordable"]]
        assert grown, (before, after)
        for code in before:
            assert after[code]["max_lots_affordable"] >= before[code]["max_lots_affordable"]
        # 510300 一手约 ¥460 ⇒ 3 万至少多买 60 手。
        assert after["510300"]["max_lots_affordable"] - \
            before["510300"]["max_lots_affordable"] > 50
    finally:
        c.close()


# ---------- ④ 点名，不猜数 ----------

def test_t3_unknown_arm_and_missing_price_are_named(db):
    """④：未知账户 ⇒ `PaperError`；持仓缺价 ⇒ `MissingPriceError`（不用成本价冒充）。"""
    c = connect(db)
    try:
        with pytest.raises(engine.PaperError, match="不存在"):
            engine.account_view(c, "arm-agent-nope")
        # 把 000333 的收盘价全部删掉 ⇒ 持仓不可判定（**点名**，不是静默算成 0 市值）。
        c.execute("DELETE FROM bars_daily WHERE code = '000333'")
        c.commit()
        with pytest.raises(engine.MissingPriceError, match="000333"):
            engine.account_view(c, ARM_AGENT, START)
    finally:
        c.close()


def test_t3_a_pool_code_without_a_type_is_named_not_guessed(db):
    """④（续）：池内标的有价但缺 `instruments.type` ⇒ 进 `errors`、**不进** `by_code`。"""
    c = connect(db)
    try:
        c.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
                  " adj_mode, source, fetched_at) VALUES ('601398',?,5,5,5,5,100,"
                  " 'none','x',?)", (START, NOW))
        c.commit()
        c.execute("INSERT INTO candidate_members (snapshot_id, code, pool, raw_score,"
                  " adj_score, reason, risk_json, status, entered_at) SELECT"
                  " MAX(snapshot_id),'601398','short',1,1,'夹具','{}','观察中',? FROM"
                  " candidate_snapshots", (NOW,))
        c.commit()
        view = engine.account_view(c, ARM_AGENT, START)
        assert "601398" not in view["buying_power"]["by_code"]
        assert any(e["code"] == "601398" for e in view["errors"]), view["errors"]
    finally:
        c.close()


# ---------- ⑤ 退出码 ----------

def test_t5_cli_account_and_capital_exit_codes(db, capsys):
    """⑤：0 = 打出载荷；2 = 输入不合法（**零写入**）。"""
    code, out, err = run(db, "paper", "account", "--arm", ARM_AGENT, capsys=capsys)
    assert code == 0, err
    assert "净收益(元)" in out and "最多" in out
    code, out, err = run(db, "paper", "account", "--arm", ARM_AGENT, "--json",
                         capsys=capsys)
    assert code == 0 and json.loads(out)["buying_power"]["cash"] > 0
    code, _, err = run(db, "paper", "account", "--arm", "arm-agent-nope", capsys=capsys)
    assert code == 2 and "不存在" in err

    c = connect(db)
    n_before = len(paper_store.load_capital_events(c, ARM_AGENT))
    c.close()
    # `--dry-run`：打出载荷、**零写入**。
    code, out, err = run(db, "paper", "capital", "add", "--arm", ARM_AGENT,
                         "--date", "2026-09-29", "--kind", "deposit",
                         "--amount", "1234", "--note", "夹具", "--dry-run",
                         capsys=capsys)
    assert code == 0 and json.loads(out)["dry_run"] is True, err
    code, _, err = run(db, "paper", "capital", "add", "--arm", "arm-agent-nope",
                       "--date", "2026-09-29", "--kind", "deposit",
                       "--amount", "1", "--note", "夹具", capsys=capsys)
    assert code == 2 and "不存在" in err
    c = connect(db)
    assert len(paper_store.load_capital_events(c, ARM_AGENT)) == n_before, \
        "dry-run / 不合法输入都不许写库"
    c.close()
    # 真写：幂等（同参数第二次 ⇒ written 0）。
    code, out, _ = run(db, "paper", "capital", "add", "--arm", ARM_AGENT,
                       "--date", "2026-09-29", "--kind", "deposit",
                       "--amount", "1234", "--note", "夹具", capsys=capsys)
    assert code == 0 and json.loads(out)["written"] == 1
    code, out, _ = run(db, "paper", "capital", "add", "--arm", ARM_AGENT,
                       "--date", "2026-09-29", "--kind", "deposit",
                       "--amount", "1234", "--note", "夹具", capsys=capsys)
    assert code == 0 and json.loads(out)["written"] == 0
    code, out, _ = run(db, "paper", "capital", "list", "--arm", ARM_AGENT,
                       capsys=capsys)
    assert code == 0
    listed = json.loads(out)
    assert listed["n_events"] == 2 and listed["signed_by_account"][ARM_AGENT] == 31234.0


# ---------- ⑥ objective 进指纹 ----------

def test_t4_objective_is_verbatim_and_enters_the_fingerprint(db):
    """⑥：文案逐字 == 任务书 D7；有/无 `objective` ⇒ 指纹不同（它真进了指纹）。"""
    assert agent_context.OBJECTIVE == {
        "goal": "扣除全部成本（佣金/印花税/过户费/滑点）后的净收益最大化",
        "note": "预测准确度只是手段，不是考核目标 —— 本期读数以 paper_nav_daily 的 "
                "cum_return 与净收益(元) 为准",
        "accounting": "net_deposits 含全部入金；cost 累计在 cum_cost",
    }
    assert "objective" in agent_context.DECISION_HASHED_KEYS
    c = connect(db)
    try:
        ctx = engine.decision_context_for(c, arm=ARM_AGENT, asof=TOPUP)
        assert ctx["objective"] == agent_context.OBJECTIVE
        with_obj = agent_context.decision_context_sha256(ctx)
        keys = tuple(k for k in agent_context.DECISION_HASHED_KEYS if k != "objective")
        without = {k: ctx[k] for k in keys}
        assert agent_context.decision_context_sha256(without | {"objective": ctx["objective"]}) \
            == with_obj
        # 同一份载荷、去掉 objective、按旧键集算 ⇒ **不同**（证明它真进了指纹）。
        import hashlib
        blob = json.dumps({k: ctx[k] for k in keys}, sort_keys=True,
                          ensure_ascii=False, separators=(",", ":"))
        assert hashlib.sha256(blob.encode()).hexdigest() != with_obj
    finally:
        c.close()


def test_t4_a_price_change_moves_the_fingerprint_through_affordable_lots(db):
    """⑥（续）：同一账户、只改现金（⇒ `affordable_lots` 变）⇒ 指纹变。"""
    c = connect(db)
    try:
        before = engine.decision_context_for(c, arm=ARM_AGENT, asof=BEFORE_TOPUP)
        after = engine.decision_context_for(c, arm=ARM_AGENT, asof=TOPUP)
        assert before["tradability"]["by_code"]["510300"]["affordable_lots"] \
            != after["tradability"]["by_code"]["510300"]["affordable_lots"]
        assert agent_context.decision_context_sha256(before) \
            != agent_context.decision_context_sha256(after)
    finally:
        c.close()


# ---------- ⑦ 读数面：净收益 ----------

def test_t5_profit_cny_is_nav_minus_net_deposits(db):
    """⑦：`profit_cny == round(nav − net_deposits, 4)`，且两天的口径各自成立。"""
    c = connect(db)
    try:
        for asof in (BEFORE_TOPUP, TOPUP):
            a = engine.account_view(c, ARM_AGENT, asof)["account"]
            assert a["profit_cny"] == round(a["nav"] - a["net_deposits"], 4), asof
            assert a["cum_return"] == round(a["nav"] / a["net_deposits"] - 1.0, 6)
        assert engine.account_view(c, ARM_AGENT, TOPUP)["account"]["net_deposits"] \
            == AGENT_EFFECTIVE_CAPITAL
    finally:
        c.close()


def test_t5_agent_show_carries_the_account_block(db, capsys):
    """⑦（续）：`paper agent show` 的 `account` 块有净收益(元) 与净入金。"""
    code, out, err = run(db, "paper", "agent", "show", "--arm", ARM_AGENT,
                         "--asof", TOPUP, capsys=capsys)
    assert code == 0, err
    blk = json.loads(out)["account"]
    assert blk["available"] is not False
    assert blk["net_deposits"] == AGENT_EFFECTIVE_CAPITAL
    assert blk["profit_cny"] == round(blk["nav"] - blk["net_deposits"], 4)
    for key in ("nav", "cum_return", "drawdown", "cum_cost"):
        assert key in blk


def test_t5_the_page_shows_profit_cny(db):
    """⑦（续）：`/lab/paper` 的 AI 操盘手表有「净收益(元)」列，且与引擎同值。

    同一页两个「收益」必须口径不同且**都写清楚**：累计收益率的分母会被入金改大，
    净收益(元) 不会 —— 只看前者会把「加钱了」读成「赚少了」。
    """
    from stocklab.labweb import paper_data, paper_render

    c = connect(db)
    try:
        data = paper_data.track(c, TOPUP)
        ops = paper_data.agent_ops(
            c, TOPUP,
            accounts=paper_store.load_accounts(c),
            arms=data["arms"],
            performance=paper_data.performance(c, TOPUP),
            paper_trades=paper_data._paper_trades(c, TOPUP))
        arm = next(a for a in ops["arms"] if a["account_id"] == ARM_AGENT)
        # 夹具还没跑 `paper step` ⇒ 没有净值行 ⇒ `build_report` 的账户条目为空，
        # 此时 `profit_cny` 如实为 `None`（**不是 0**）；有净值行的那一天由
        # `test_t5_profit_cny_is_nav_minus_net_deposits` 钉住。
        assert "profit_cny" in arm and arm["profit_cny"] is None
        assert data["arms"] == []
        # `performance` 的期初口径仍走引擎那一份（入金事件落地后不分叉）。
        assert paper_data.net_deposits_at(c, paper_store.load_accounts(c)[0],
                                          START) == 20000.0
        html = paper_render._ops_arms_table(ops)
        assert "净收益(元)" in html
        assert html.count("<th>") == 10
    finally:
        c.close()
