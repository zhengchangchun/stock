"""P62：AI 臂的净值行必须等于「重放 + 收盘 mark-to-market」。

**这条判据取代 P52 的那一条**（任务书 §3.4）。P52 写的是「与 `paper step`
同口径算出的值一致」—— 而 AI 臂**根本不走** `paper step`（它在 `paper step` 里
被 `external_executor` 让出，日终由 `paper agent run` 落），所以那条判据是**空转**的：
它绿，是因为期望值是从实现反推出来的。真库里于是躺着一条
`arm-agent-ds-v1 / 2026-09-23`：现金 ¥11,320（执行前）、持仓 `000333×0`（执行后）、
nav ¥8,233.68 凭空消失。

| 判据 | 被摘掉后会红的实现 |
|---|---|
| T1 卖出当天现金记入所得 | `execute_decision` 原样返回入参现金（P62 的 bug） |
| T2 买入当天现金已扣、市值已含 | 同上（买入侧） |
| T3 不变量：`nav == 重放 + MTM`（逐日、多日） | 任何「写入」与「重放」不同源的实现 |
| T4 零股条目不许出现 | `positions[code] = 0` 而不 pop（`000333×0` 那一行） |
| T5 五条静态臂不受影响 | 把结算逻辑改坏的「修法」 |

数字一律**从被测模块取**（`engine._ledger_arm_state` / `mark_to_market`），
不手抄一份到测试里：手抄的那份会与上游漂移，而漂移是静默的。
"""

from __future__ import annotations

import json

import pytest

from stocklab.cli.main import main
from stocklab.paper import engine as paper_engine
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = "2026-09-15"
DAY2 = "2026-09-16"
DAY3 = "2026-09-17"
DAY4 = "2026-09-18"
CAL = ("2026-09-14", START, DAY2, DAY3, DAY4)
BARS = {
    "000333": {"2026-09-14": 86.80, "2026-09-15": 87.23, "2026-09-16": 87.60,
               "2026-09-17": 87.10, "2026-09-18": 86.40},
    "510300": {START: 4.523, DAY2: 4.550, DAY3: 4.532, DAY4: 4.582},
    "510880": {START: 3.382, DAY2: 3.390, DAY3: 3.380, DAY4: 3.375},
    "sh000300": {START: 4450.04, DAY2: 4480.27, DAY3: 4460.16, DAY4: 4507.39},
}
POOL = ("000333", "510300", "510880")
DAYS = (START, DAY2, DAY3, DAY4)

TEST_ARM = "arm-agent-t1"
TEST_MODEL = "claude-code"
TEST_PROMPT = "a" * 64


@pytest.fixture
def db(tmp_path):
    """`paper init` 前的一份库：交易日历 + 行情 + 本金 + 实盘种子 + 每日候选池。"""
    path = tmp_path / "p62-nav.db"
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
    # 每个交易日一份候选池快照：`paper agent context` 按 `<= asof` 取最近一份，
    # 逐日各建一份是为了让「这一天看得见什么」在夹具里也是逐日明确的。
    for asof in DAYS:
        c.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
                  " created_at) VALUES (?,'light','{}',?)", (asof, NOW))
        sid = c.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
        for code in POOL:
            c.execute("INSERT INTO candidate_members (snapshot_id, code, pool,"
                      " raw_score, adj_score, reason, risk_json, status, entered_at)"
                      " VALUES (?,?, 'short', 1,1,'夹具','{}','观察中',?)",
                      (sid, code, NOW))
    c.commit()
    c.close()
    return path


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db), "--now", NOW])
    out, err = capsys.readouterr()
    return code, out, err


def _json(out):
    return json.loads(out)


def _init(db, capsys):
    code, _, err = run(db, "paper", "init", capsys=capsys)
    assert code == 0, err


def _enroll(db, capsys, name=TEST_ARM):
    code, _, err = run(db, "paper", "agent", "enroll", "--arm-name", name,
                       "--model-id", TEST_MODEL, "--prompt-sha256", TEST_PROMPT,
                       capsys=capsys)
    assert code == 0, err
    return name


def _decide(db, tmp_path, capsys, *, asof, payload, arm=TEST_ARM, name="d.json"):
    """走**正规写入口**：先取 PIT 上下文指纹，再 `decide`（D-49 的闸门开着）。"""
    code, out, err = run(db, "paper", "agent", "context", "--asof", asof,
                         "--arm", arm, capsys=capsys)
    assert code == 0, err
    body = {**payload, "context_sha256": _json(out)["context_sha256"]}
    path = tmp_path / f"{asof}-{name}"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    code, out, err = run(db, "paper", "agent", "decide", "--asof", asof,
                         "--file", str(path), "--arm", arm,
                         "--model-id", TEST_MODEL, "--prompt-sha256", TEST_PROMPT,
                         capsys=capsys)
    assert code == 0, err
    return path


def _item(code, side, weight, reason="夹具"):
    return {"code": code, "side": side, "target_weight_pct": weight, "reason": reason}


def _run_agent(db, capsys, asof):
    """`paper agent run`：退出码 1 = 家族里有人缺决策（内置臂没决策），不是失败。"""
    code, out, err = run(db, "paper", "agent", "run", "--asof", asof, capsys=capsys)
    assert code in (0, 1), err
    return _json(out)


def _step(db, capsys, asof):
    code, _, err = run(db, "paper", "step", "--asof", asof, capsys=capsys)
    assert code == 0, err


def _nav_row(conn, account_id, date):
    row = conn.execute(
        "SELECT * FROM paper_nav_daily WHERE account_id = ? AND date = ?",
        (account_id, date)).fetchone()
    assert row is not None, f"{account_id}/{date} 没有净值行"
    return dict(row)


def _positions(cash_row):
    return {str(p["code"]): int(p["qty"])
            for p in json.loads(cash_row["positions_json"] or "[]")}


def _replay(conn, account_id, date):
    """判据的**重放半边**：`_ledger_arm_state`（自身成交重放）+ `mark_to_market`。

    这是任务书 §3.2 点名的两个既有实现 —— 测试里不另算一份净值。
    """
    account = next(a for a in
                   (dict(r) for r in conn.execute("SELECT * FROM paper_accounts"))
                   if a["account_id"] == account_id)
    state = paper_engine._ledger_arm_state(conn, account, date)
    marks = paper_engine.resolve_marks(conn, set(state["positions"]), date)
    mv, _ = paper_engine.mark_to_market(state["positions"], marks)
    return {"cash": round(state["cash"], 4), "positions": dict(state["positions"]),
            "nav": round(state["cash"] + mv, 4)}


def _assert_row_equals_replay(conn, account_id, date, *, tol=1e-6):
    row = _nav_row(conn, account_id, date)
    want = _replay(conn, account_id, date)
    assert abs(row["cash"] - want["cash"]) <= tol, (
        f"{account_id}/{date} 的现金 {row['cash']} ≠ 重放 {want['cash']}"
        f"（差 {row['cash'] - want['cash']:,.4f}）—— 净值行记的是执行前的状态")
    assert _positions(row) == want["positions"], (
        f"{account_id}/{date} 的持仓 {_positions(row)} ≠ 重放 {want['positions']}")
    assert abs(row["nav"] - want["nav"]) <= tol, (
        f"{account_id}/{date} 的 nav {row['nav']} ≠ 重放 {want['nav']}"
        f"（差 {row['nav'] - want['nav']:,.4f}）")
    return row


# ---------- 夹具自身的自检：逐日跑一遍「正规链」 ----------

def _build_four_days(db, tmp_path, capsys):
    """D1 卖光 + 买两只 / D2 买回（整手取整未达目标）/ D3 全不动 / D4 换仓。

    跑的是**正规链**：`paper agent decide` → `paper agent run`，静态五条臂照旧
    `paper step`（它们在 `agent run` 里被让出，两条命令各管各的）。
    """
    _init(db, capsys)
    arm = _enroll(db, capsys)
    _decide(db, tmp_path, capsys, asof=START, payload={
        "asof": START, "cash_pct": 80.0, "rationale": "清仓 000333，配置两只 ETF",
        "decisions": [_item("000333", "sell", 0.0, "清仓"),
                      _item("510300", "buy", 10.0), _item("510880", "buy", 10.0)]})
    _run_agent(db, capsys, START)
    _step(db, capsys, START)

    _decide(db, tmp_path, capsys, asof=DAY2, payload={
        "asof": DAY2, "cash_pct": 20.0, "rationale": "加仓 000333 到 50%、510300 到 30%",
        "decisions": [_item("000333", "buy", 50.0), _item("510300", "buy", 30.0)]})
    _run_agent(db, capsys, DAY2)
    _step(db, capsys, DAY2)

    conn = connect(db)
    try:
        state = paper_engine.arm_state_for(conn, arm, DAY3)
        total = float(state["total_assets"])
        items, weight = [], 0.0
        for code, qty in sorted(state["positions"].items()):
            w = round(float(state["marks"][code].price) * qty / total * 100.0, 6)
            items.append(_item(code, "buy", w, "按现值原样持有"))
            weight += w
    finally:
        conn.close()
    _decide(db, tmp_path, capsys, asof=DAY3, payload={
        "asof": DAY3, "cash_pct": round(100.0 - weight, 6),
        "rationale": "今天不动：目标 = 现值", "decisions": items})
    _run_agent(db, capsys, DAY3)
    _step(db, capsys, DAY3)

    _decide(db, tmp_path, capsys, asof=DAY4, payload={
        "asof": DAY4, "cash_pct": 50.0, "rationale": "清 510880，510300 加到 50%",
        "decisions": [_item("510880", "sell", 0.0, "清仓"),
                      _item("510300", "buy", 50.0)]})
    _run_agent(db, capsys, DAY4)
    _step(db, capsys, DAY4)
    return arm


# ---------- T1 / T2：结算 ----------

def test_t1_sell_proceeds_are_in_the_nav_row(db, tmp_path, capsys):
    """卖出当天：现金必须**记入所得**（执行前现金的那种写法在这里红）。

    P62 的真库症状：`arm-agent-ds-v1` 卖光 100 股 000333 后，净值行里的现金
    仍是执行前的 ¥11,320 —— 少 ¥8,233.68。
    """
    arm = _build_four_days(db, tmp_path, capsys)
    conn = connect(db)
    try:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades WHERE account_id = ? AND date = ?"
            " ORDER BY trade_id", (arm, START))]
        sells = [t for t in trades if t["side"] == "sell" and t["code"] == "000333"]
        assert sells and sells[0]["qty"] == 100, trades      # 夹具非空：真卖了
        account = next(dict(r) for r in conn.execute(
            "SELECT * FROM paper_accounts WHERE account_id = ?", (arm,)))
        row = _nav_row(conn, arm, START)
        proceeds = round(sells[0]["fill_price"] * sells[0]["qty"]
                         - sells[0]["fee_total"], 2)
        out = sum(round(t["fill_price"] * t["qty"] + t["fee_total"], 2)
                  for t in trades if t["side"] == "buy")
        # 净值行的现金必须与**成交行**自洽：初始 + 卖出所得 − 买入支出。
        # 旧实现里它是执行前的 ¥11,314.91（一个字都没动）—— 那一条在这里红。
        assert abs(float(row["cash"])
                   - (float(account["initial_cash"]) + proceeds - out)) <= 1e-6, (
            f"现金 ¥{row['cash']} 与成交行对不上（应为 "
            f"{float(account['initial_cash']) + proceeds - out:,.2f}；"
            f"卖出所得 ¥{proceeds:,.2f}）—— 这正是 P62 的 bug")
        _assert_row_equals_replay(conn, arm, START)
    finally:
        conn.close()


def test_t2_buy_cash_is_deducted_and_market_value_included(db, tmp_path, capsys):
    """买入当天：现金已扣、市值已含 —— 且两者与重放逐字段一致。"""
    arm = _build_four_days(db, tmp_path, capsys)
    conn = connect(db)
    try:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades WHERE account_id = ? AND date = ?",
            (arm, START))]
        assert any(t["side"] == "buy" for t in trades), trades
        row = _assert_row_equals_replay(conn, arm, START)
        assert row["market_value"] > 0, "买入当天市值不该是 0"
        assert float(row["nav"]) == pytest.approx(row["cash"] + row["market_value"],
                                                  abs=1e-6)
    finally:
        conn.close()


# ---------- T3 / T4：不变量 ----------

def test_t3_nav_equals_replay_on_every_day(db, tmp_path, capsys):
    """**核心判据**：逐日 `nav == 重放 + MTM`（含买 / 卖 / 整手未达目标 / 不动）。

    「整手向下取整未达目标」那天也在这里：目标是 50%、实际只买得起 1 手，
    执行层如实上报 —— 净值行照样必须等于重放（**不是**等于目标）。
    """
    arm = _build_four_days(db, tmp_path, capsys)
    conn = connect(db)
    try:
        n_trades_by_day = {}
        for day in DAYS:
            n_trades_by_day[day] = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE account_id = ? AND date = ?",
                (arm, day)).fetchone()[0]
            _assert_row_equals_replay(conn, arm, day)
        # 夹具覆盖度：买卖混合 + 一天零成交（否则这条用例是空转的）
        assert n_trades_by_day[START] >= 3 and n_trades_by_day[DAY2] >= 1
        assert n_trades_by_day[DAY3] == 0, "D3 应当是「不动」的一天"
        assert n_trades_by_day[DAY4] >= 1
    finally:
        conn.close()


def test_t4_no_zero_qty_positions_in_any_nav_row(db, tmp_path, capsys):
    """清仓日不许留 `qty == 0` 的条目（真库那行 `[{"000333", 0}]` 在这里红）。"""
    arm = _build_four_days(db, tmp_path, capsys)
    conn = connect(db)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_nav_daily WHERE account_id = ? ORDER BY date", (arm,))]
        assert rows
        for row in rows:
            bad = {c: q for c, q in _positions(row).items() if q <= 0}
            assert not bad, f"{row['date']} 的净值行里有零股条目 {bad}"
        assert "000333" not in _positions(_nav_row(conn, arm, START)), \
            "清仓日 000333 应当整个键消失（不是留一个 0）"
    finally:
        conn.close()


# ---------- T5：静态臂不受影响 ----------

def test_t5_all_arms_obey_the_same_invariant(db, tmp_path, capsys):
    """**每一条臂的每一行**都对得上重放 —— 包括五条静态臂（防回归）。

    静态臂走 `_plan_steps`、AI 臂走 `execute_decision`（P62 才修好），判据同一条：
    **持仓集合必须精确相等**（零股条目、漏结算的持仓都落这里），现金/净值则分两档：

    - **决策驱动臂**（`arm-agent*`，P62 修的正是这条路径）：精确到 1e-6；
    - **静态臂**：允许 `¥0.01 × 当日笔数` 的**分币级**差 —— 那是 `_plan_steps` 用
      `Decision.amount`（先舍到分）而重放不不舍造成的**既有**口径差，真库
      `arm-discipline-*` 上实测存在（见任务书 §9.5，不属本站，未改）。
      本用例把它**限死在分币级**：任何「结算少了一笔」的实现都会远远越界。
    """
    _build_four_days(db, tmp_path, capsys)
    conn = connect(db)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_nav_daily ORDER BY account_id, date")]
        assert len(rows) >= 20, f"净值行太少（{len(rows)}）—— 夹具没跑起来"
        arms = {str(r["account_id"]) for r in rows}
        assert {"arm-discipline-05", "arm-discipline-10", "arm-discipline-15",
                "arm-hold", "arm-now"} <= arms
        checked = 0
        for row in rows:
            aid, date = str(row["account_id"]), str(row["date"])
            state = paper_engine.arm_state_for(conn, aid, date)
            assert _positions(row) == {c: int(q) for c, q
                                       in state["positions"].items()}, \
                f"{aid}/{date} 的持仓与重放不符"
            want = round(state["cash"] + state["market_value"], 4)
            n = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE account_id = ? AND date = ?",
                (aid, date)).fetchone()[0]
            bound = 1e-6 if aid.startswith("arm-agent") else 0.01 * n + 1e-6
            assert abs(row["cash"] - state["cash"]) <= bound, f"{aid}/{date} cash"
            assert abs(row["nav"] - want) <= bound, f"{aid}/{date} nav"
            checked += 1
        assert checked == len(rows)
    finally:
        conn.close()
