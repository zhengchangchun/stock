"""P56：AI 操盘手开跑 —— 决策循环的日更（D-48 / D-49 / D-50）。

任务书 `docs/tasks/2026-09-23-p56-AI操盘手开跑.md` §2 的判据逐条落在这里：

| 判据 | 被摘掉后会红的实现 |
|---|---|
| T1 `paper agent context` 只读 + 指纹稳定 | 上下文里混进 `> asof` 的行 / 两次跑不一样 |
| T2 `paper agent enroll` 形状 + 幂等 | 就地改写旧账户的预注册（「换到好看为止」） |
| T3 认领让出：`paper step` 不写 AI 臂的日终 | 收盘链先写净值 ⇒ 决策永不执行（D-50 的坑） |
| T4 `paper agent run` 的三种结果 | 缺决策被静默读成「决定不动手」 |
| T5 未知 `executor` fail-closed | 那条策略悄悄不下单，症状只在净值表上 |
| T6 预注册不匹配即拒 | 换了模型照样写进同一条臂 |
| T7 `context_sha256` 复核 | 载荷不是照着这一天的 PIT 上下文做的 |

夹具与 `tests/test_cli_paper_agent.py` 同一套口径（离线、`paper init` 起。）
"""

from __future__ import annotations

import json

import pytest

from stocklab.cli.main import main
from stocklab.paper import agent_context, agent_decide, engine
from stocklab.paper.config import (
    ARM_AGENT,
    ARM_AGENT_RANDOM,
    EXECUTOR_AGENT_DECISION,
    EXECUTOR_KEY,
)
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = "2026-09-15"
NEXT = "2026-09-16"
CAL = ("2026-09-11", "2026-09-14", START, NEXT)
BARS = {
    "000333": {"2026-09-11": 86.50, "2026-09-14": 86.80, START: 87.23,
               NEXT: 87.60},
    "510300": {START: 4.523, NEXT: 4.550},
    "510880": {START: 3.382, NEXT: 3.390},
    "sh000300": {START: 4450.04, NEXT: 4480.27},
}
POOL = ("000333", "510300", "510880")
MODEL = "deepseek/deepseek-v4-pro"      # **原样字面量**（经 commandcode 网关）
PROMPT = "b" * 64


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p56.db"
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
    c.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
              " created_at) VALUES (?,'light','{}',?)", (START, NOW))
    sid = c.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
    for code in POOL:
        c.execute("INSERT INTO candidate_members (snapshot_id, code, pool, raw_score,"
                  " adj_score, reason, risk_json, status, entered_at)"
                  " VALUES (?,?, 'short', 1,1,'夹具','{}','观察中',?)", (sid, code, NOW))
    c.commit()
    c.close()
    return path


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db), "--now", NOW])
    out, err = capsys.readouterr()
    return code, out, err


def _init(db, capsys):
    code, _, err = run(db, "paper", "init", capsys=capsys)
    assert code == 0, err


def _enroll(db, capsys, name="arm-agent-ds-v1", model=MODEL, prompt=PROMPT):
    code, out, err = run(db, "paper", "agent", "enroll", "--arm-name", name,
                         "--model-id", model, "--prompt-sha256", prompt,
                         capsys=capsys)
    assert code == 0, err
    return json.loads(out)


def _full(db, capsys):
    """init + enroll：后面每条用例都要的那份库。"""
    _init(db, capsys)
    return _enroll(db, capsys)


def _acct(db, arm):
    c = connect(db)
    try:
        return next(a for a in engine.store.load_accounts(c)
                    if a["account_id"] == arm)
    finally:
        c.close()


def _context_sha(db, capsys, arm, asof):
    code, out, err = run(db, "paper", "agent", "context", "--asof", asof,
                         "--arm", arm, capsys=capsys)
    assert code == 0, err
    return json.loads(out)["context_sha256"]


def _decisions(db, arm):
    c = connect(db)
    try:
        return agent_decide.load_decisions(c, arm)
    finally:
        c.close()


def _counts(db):
    c = connect(db)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("paper_accounts", "paper_nav_daily", "paper_trades",
                          "paper_agent_decisions")}
    finally:
        c.close()


def _write(tmp_path, payload, name="d.json"):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _payload(asof, decisions, *, cash_pct=None, rationale="夹具"):
    w = round(sum(d["target_weight_pct"] for d in decisions), 6)
    return {"asof": asof, "decisions": decisions,
            "cash_pct": (round(100.0 - w, 6) if cash_pct is None else cash_pct),
            "rationale": rationale}


BUY_10 = _payload(START, [{"code": "510300", "side": "buy",
                           "target_weight_pct": 10.0, "reason": "分散"}])


# ══════════════════════════════════════════════════════════════════════
# T1：`paper agent context` 只读 + 指纹稳定
# ══════════════════════════════════════════════════════════════════════

def test_t1_context_is_byte_identical_on_two_runs(db, capsys):
    """同 asof 连跑两次 stdout **逐字节一致** —— 上下文里没有时间戳/自增 id。"""
    arm = _full(db, capsys)["account_id"]
    assert _acct(db, arm)["params_json"]
    first = run(db, "paper", "agent", "context", "--asof", START, "--arm", arm,
                capsys=capsys)
    second = run(db, "paper", "agent", "context", "--asof", START, "--arm", arm,
                 capsys=capsys)
    assert first[0] == second[0] == 0
    assert first[1] == second[1], "同一库同一天两次跑出不同的上下文 ⇒ 不可复算"
    assert first[2] == second[2]


def test_t1_context_sha256_matches_the_pure_function(db, capsys):
    """输出里的 `context_sha256` == `decision_context_sha256(ctx)`（不许另算一套）。"""
    arm = _full(db, capsys)["account_id"]
    code, out, _ = run(db, "paper", "agent", "context", "--asof", START,
                       "--arm", arm, capsys=capsys)
    assert code == 0
    got = json.loads(out)
    assert got["context_sha256"] == agent_context.decision_context_sha256(got["context"])
    assert got["pool_codes"] == len(POOL)


def test_t1_a_future_bar_does_not_leak_into_the_context(db, capsys):
    """PIT 守卫：塞一条 `asof + 1` 的收盘价 ⇒ 指纹**逐字节不变**（不采用未来行）。"""
    arm = _full(db, capsys)["account_id"]
    before = run(db, "paper", "agent", "context", "--asof", START, "--arm", arm,
                 capsys=capsys)[1]
    c = connect(db)
    try:
        c.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
                  " adj_mode, source, fetched_at) VALUES ('510300','2026-09-30',"
                  " 99.0,99.0,99.0,99.0,100,'none','x',?)", (NOW,))
        c.commit()
    finally:
        c.close()
    after = run(db, "paper", "agent", "context", "--asof", START, "--arm", arm,
                capsys=capsys)[1]
    assert before == after, "上下文里出现了 asof 之后的行（PIT 失效）"
    assert "2026-09-30" not in after


def test_t1_a_future_price_in_the_injected_marks_is_rejected(db):
    """反向自检：把未来价**注入** `build_decision_context` ⇒ 报 LookaheadError。

    上一条钉的是「自己取价时不越界」，这一条钉的是「别人喂进来也不行」——
    两条路都要堵，否则守卫只拦一半。
    """
    from stocklab.paper.rules import LookaheadError
    from stocklab.portfolio.prices import Price
    c = connect(db)
    try:
        engine.init_accounts(c, start_date=START, now=NOW)
        with pytest.raises(LookaheadError):
            agent_context.build_decision_context(
                c, arm=ARM_AGENT, asof=START, pool={"codes": [], "available": False},
                cash=1.0, positions={},
                marks={"510300": Price(code="510300", price=9.9, source="x",
                                       price_asof="2026-09-20",
                                       detail="夹具：未来价")},
                total_assets=1.0)
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T2：`paper agent enroll` 幂等 + 形状
# ══════════════════════════════════════════════════════════════════════

def test_t2_enroll_creates_a_preregistered_version_account(db, capsys):
    """新账户：`arm='agent'`、`executor='agent_decision'`、**无** `m2_channel_a`。"""
    _init(db, capsys)
    rep = _enroll(db, capsys)
    assert rep["created"] is True and rep["account_id"] == "arm-agent-ds-v1"
    account = _acct(db, "arm-agent-ds-v1")
    assert account["arm"] == "agent"
    assert account["etf_target_pct"] is None
    params = json.loads(account["params_json"])
    assert params[EXECUTOR_KEY] == EXECUTOR_AGENT_DECISION
    assert "m2_channel_a" not in json.dumps(params), \
        "AI 操盘手的账户不许带通路 A 的声明（两者靠 executor 区分）"
    assert params["preregistered"] == {"model_id": MODEL, "prompt_sha256": PROMPT}
    assert "api_key" not in account["params_json"].lower(), "项目内零凭据"
    # 起跑口径与其余各臂同一份实现
    assert account["start_date"] == START
    assert float(account["initial_nav"]) > 0


def test_t2_enroll_is_idempotent_and_changes_nothing(db, capsys):
    """重跑：`created: false` 且**行一字不改**。"""
    _init(db, capsys)
    _enroll(db, capsys)
    first = _acct(db, "arm-agent-ds-v1")
    again = _enroll(db, capsys)
    assert again["created"] is False
    assert _acct(db, "arm-agent-ds-v1") == first


def test_t2_enroll_refuses_a_different_model_on_the_same_account(db, capsys):
    """换模型 = 开新版本账户：同名换模型**拒绝**，且旧行一个字不改。"""
    _init(db, capsys)
    _enroll(db, capsys)
    before = _acct(db, "arm-agent-ds-v1")
    code, _, err = run(db, "paper", "agent", "enroll", "--arm-name", "arm-agent-ds-v1",
                       "--model-id", "some/other-model", "--prompt-sha256", PROMPT,
                       capsys=capsys)
    assert code == 2, err
    assert "开新版本账户" in err
    assert _acct(db, "arm-agent-ds-v1") == before


BAD_NAMES = ("arm-agent", "arm-agent-random", "arm-agent-V1", "arm-agent-",
             "arm-agent- has space", "arm-agent-" + "x" * 33, "Arm-agent-v1",
             "arm-agent-v/1", "not-an-agent-arm")


@pytest.mark.parametrize("name", BAD_NAMES)
def test_t2_enroll_refuses_bad_arm_names(db, capsys, name):
    """拒 `arm-agent` / `arm-agent-random` / 大写 / 空格 / 超长 / 空版本。"""
    _init(db, capsys)
    code, _, err = run(db, "paper", "agent", "enroll", "--arm-name", name,
                       "--model-id", MODEL, "--prompt-sha256", PROMPT, capsys=capsys)
    assert code == 2, f"{name!r} 应当被拒，实际 exit={code}"
    assert name in err or "版本号" in err
    assert _counts(db)["paper_accounts"] == 7, "拒绝时一行都不许写"


def test_t2_enroll_refuses_a_prompt_sha_that_is_not_hex(db, capsys):
    """`--prompt-sha256` 的形状也钉住：手算的 64 位非十六进制串会被拒。"""
    _init(db, capsys)
    code, _, err = run(db, "paper", "agent", "enroll", "--arm-name", "arm-agent-v1",
                       "--model-id", MODEL, "--prompt-sha256", "p" * 64, capsys=capsys)
    assert code == 2 and "十六进制" in err


def test_t2_sha256_command_is_the_single_algorithm(tmp_path, capsys):
    """`paper agent sha256 --file` 对**文件字节**取 sha256，且两次一致（不碰库）。"""
    import hashlib
    f = tmp_path / "prompt.txt"
    f.write_bytes(b"you are a trader\n")
    code = main(["paper", "agent", "sha256", "--file", str(f)])
    out, err = capsys.readouterr()
    assert code == 0, err
    got = json.loads(out)
    assert got["sha256"] == hashlib.sha256(b"you are a trader\n").hexdigest()
    assert got["bytes"] == len(b"you are a trader\n")
    code2 = main(["paper", "agent", "sha256", "--file", str(f)])
    assert capsys.readouterr().out == out
    assert code2 == 0
    assert main(["paper", "agent", "sha256", "--file", str(tmp_path / "nope")]) == 2


# ══════════════════════════════════════════════════════════════════════
# T3：让出生效（`paper step` 不写 AI 臂的日终）
# ══════════════════════════════════════════════════════════════════════

def test_t3_paper_step_lets_go_of_the_claimed_accounts(db, tmp_path, capsys):
    """`executor='agent_decision'` 的账户 ⇒ `paper step` **不写**它的当日净值行；
    其余 5 条静态臂照旧写。"""
    arm = _full(db, capsys)["account_id"]
    code, out, err = run(db, "paper", "step", "--asof", START,
                         "--out", str(tmp_path / "s.md"), capsys=capsys)
    assert code == 0, err
    written = {a["account_id"] for a in json.loads(out)["accounts"]}
    assert written == {"arm-hold", "arm-now", "arm-discipline-05",
                       "arm-discipline-10", "arm-discipline-15"}
    assert ARM_AGENT not in written and ARM_AGENT_RANDOM not in written
    assert arm not in written
    c = connect(db)
    try:
        for a in (ARM_AGENT, ARM_AGENT_RANDOM, arm):
            assert not engine.store.nav_exists(c, a, START), \
                f"paper step 认领了 {a} 的日终 —— 之后的决策永远不会被执行（D-50）"
            # 但账户本身必须**看得见**（跳过是显式的，不是悄悄消失）
            assert a in {x["account_id"] for x in engine.store.load_accounts(c)}
    finally:
        c.close()


def test_t3_the_executor_field_is_what_decides_not_the_name(db, capsys):
    """判据是**字段**不是名字：把内置 `arm-agent` 的 `executor` 摘掉 ⇒ 它就被 step 认领。

    （用库副本上的直接改写来构造 —— 真实迁移只做加键，不会摘键。）
    """
    _full(db, capsys)
    c = connect(db)
    try:
        params = json.loads(_acct(db, ARM_AGENT)["params_json"])
        params.pop(EXECUTOR_KEY)
        c.execute("DROP TRIGGER IF EXISTS trg_paper_accounts_no_update")
        c.execute("UPDATE paper_accounts SET params_json=? WHERE account_id=?",
                  (json.dumps(params, sort_keys=True), ARM_AGENT))
        c.execute("CREATE TRIGGER IF NOT EXISTS trg_paper_accounts_no_update"
                  " BEFORE UPDATE ON paper_accounts"
                  " BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END")
        c.commit()
        # 直接调 `engine.step`：走 CLI 会先 `ensure_schema`，而 P56 的迁移会把这个
        # 键**自愈**补回来（它本来就该这么做）—— 那条路测不到「字段决定认领」。
        payload = engine.step(c, START, now=NOW)
        written = {a["account_id"] for a in payload["accounts"]}
        assert ARM_AGENT in written, \
            "摘掉 executor 之后 step 应当认领它 —— 判据是字段，不是账户名"
    finally:
        c.close()




# ══════════════════════════════════════════════════════════════════════
# T4：`paper agent run` 幂等与三种结果
# ══════════════════════════════════════════════════════════════════════

def test_t4_with_a_decision_it_trades_and_writes_nav(db, capsys, tmp_path):
    """有决策 ⇒ 成交 + 净值行，且与 `paper step` 的执行是**同一段实现**。"""
    arm = _full(db, capsys)["account_id"]
    payload = {**BUY_10, "context_sha256": _context_sha(db, capsys, arm, START)}
    assert _decide(db, capsys, arm, payload, tmp_path)[0] == 0
    code, out, err = run(db, "paper", "agent", "run", "--asof", START, capsys=capsys)
    assert code == 1, err      # 家族里另两条臂没有决策 ⇒ 缺决策异常
    mine = next(a for a in json.loads(out)["accounts"] if a["account_id"] == arm)
    assert mine["status"] == "ran" and mine["n_trades"] >= 1
    assert mine["decision_present"] is True
    assert mine["nav"] is not None
    c = connect(db)
    try:
        assert engine.store.nav_exists(c, arm, START)
        pos = {p["code"]: p["qty"] for p in json.loads(
            engine.store.latest_nav(c, arm, asof=START)["positions_json"])}
    finally:
        c.close()
    assert pos.get("510300"), "10% 的 510300 决策要真的落到持仓上"


def test_t4_without_a_decision_it_writes_a_flat_row_and_flags_it(db, capsys):
    """无决策 ⇒ **平盘净值行**（成交 0 笔）+ `anomaly.missing_decision`（交易日 ⇒ 1）。"""
    _full(db, capsys)
    code, out, err = run(db, "paper", "agent", "run", "--asof", START, capsys=capsys)
    assert code == 1, err
    got = json.loads(out)
    missing = {m["account_id"] for m in got["anomaly"]["missing_decision"]}
    assert missing == {ARM_AGENT, ARM_AGENT_RANDOM, "arm-agent-ds-v1"}
    for m in got["anomaly"]["missing_decision"]:
        assert m["is_trading_day"] is True and m["account_in_flight"] is True
        assert m["expected"] is True
        assert "不补造默认决策" in m["note"], "缺决策不许被读成「决定按默认纪律办」"
    c = connect(db)
    try:
        for a in (ARM_AGENT, ARM_AGENT_RANDOM, "arm-agent-ds-v1"):
            nav = engine.store.latest_nav(c, a, asof=START)
            assert nav is not None and nav["date"] == START, "缺决策也要有一条平盘行"
            assert c.execute("SELECT COUNT(*) FROM paper_trades WHERE account_id=?",
                             (a,)).fetchone()[0] == 0
            # 平盘 = 与「什么都不做」同值（同起点、没有任何成交）。
            # 成本**没有新增**：等于账户行里那个起跑入场费（5.09），不是 0
            # —— 「0 成本」会把起跑前置费用从读数里抹掉。
            hold = next(x for x in engine.store.load_accounts(c)
                        if x["account_id"] == "arm-hold")
            assert nav["nav"] == float(hold["initial_nav"])
            assert nav["cum_cost"] == json.loads(hold["params_json"])["seed_fee"] == 5.09
    finally:
        c.close()


def test_t4_on_a_non_trading_day_a_missing_decision_is_not_an_anomaly(db, capsys):
    """非交易日（周末）缺决策 ⇒ **不算异常**，退出码 0。"""
    _full(db, capsys)
    code, out, err = run(db, "paper", "agent", "run", "--asof", "2026-09-12",
                         capsys=capsys)
    assert code == 0, err
    got = json.loads(out)
    assert got["anomaly"]["missing_decision"] == []
    for entry in got["accounts"]:
        assert entry["missing_decision"]["is_trading_day"] is False
        assert entry["missing_decision"]["expected"] is False


def test_t4_rerunning_writes_nothing_and_makes_no_second_trade(db, capsys):
    """重跑 ⇒ `already`、行数与成交数不变。"""
    _full(db, capsys)
    run(db, "paper", "agent", "run", "--asof", START, capsys=capsys)
    before = _counts(db)
    code, out, err = run(db, "paper", "agent", "run", "--asof", START, capsys=capsys)
    assert code == 1, err
    assert all(a["status"] == "already" for a in json.loads(out)["accounts"])
    assert all(a["wrote_nav"] is False for a in json.loads(out)["accounts"])
    assert _counts(db) == before, "重跑写了东西（幂等失效）"


# ══════════════════════════════════════════════════════════════════════
# T5：未知 `executor` fail-closed
# ══════════════════════════════════════════════════════════════════════

def _set_executor(db, arm, value):
    c = connect(db)
    try:
        row = c.execute("SELECT params_json FROM paper_accounts WHERE account_id=?",
                        (arm,)).fetchone()
        params = json.loads(row["params_json"])
        params[EXECUTOR_KEY] = value
        c.execute("DROP TRIGGER IF EXISTS trg_paper_accounts_no_update")
        c.execute("UPDATE paper_accounts SET params_json=? WHERE account_id=?",
                  (json.dumps(params, sort_keys=True, ensure_ascii=False), arm))
        c.execute("CREATE TRIGGER IF NOT EXISTS trg_paper_accounts_no_update"
                  " BEFORE UPDATE ON paper_accounts"
                  " BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END")
        c.commit()
    finally:
        c.close()


def test_t5_an_unknown_executor_is_named_not_skipped(db, capsys):
    """`executor='nobody'` ⇒ `paper agent run` 与 `paper show` 都**点名报错**。"""
    _full(db, capsys)
    _set_executor(db, "arm-discipline-10", "nobody")
    c = connect(db)
    try:
        with pytest.raises(engine.UnknownExecutorError) as e:
            engine.agent_run(c, START, now=NOW)
    finally:
        c.close()
    assert "nobody" in str(e.value) and "arm-discipline-10" in str(e.value)

    code, _, err = run(db, "paper", "agent", "run", "--asof", START, capsys=capsys)
    assert code == 2 and "nobody" in err
    code, _, err = run(db, "paper", "show", "--asof", START, capsys=capsys)
    assert code == 2, "paper show 也必须点名报错（不许静默跳过那条账户）"
    assert "nobody" in err
    # 反向自检：未知值在**写决策**的那条路上也拦得住
    code, _, err = run(db, "paper", "agent", "context", "--asof", START,
                       "--arm", "arm-discipline-10", capsys=capsys)
    assert code == 2 and "nobody" in err


def test_t5_fail_closed_is_not_the_same_as_letting_go(db, capsys):
    """反向自检：未知值**不是**「让出」—— 让出会让那条臂悄悄不下单。"""
    _full(db, capsys)
    _set_executor(db, "arm-discipline-10", "nobody")
    c = connect(db)
    try:
        with pytest.raises(engine.UnknownExecutorError):
            next(engine.executor_kind(a) for a in engine.store.load_accounts(c)
                 if a["account_id"] == "arm-discipline-10")
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════
# T6 / T7：预注册比对与 `context_sha256` 复核（都在写库之前 ⇒ 零写入）
# ══════════════════════════════════════════════════════════════════════

def _decide(db, capsys, arm, payload, tmp_path, *, model=MODEL, prompt=PROMPT,
            name="d.json"):
    path = _write(tmp_path, payload, name)
    return run(db, "paper", "agent", "decide", "--asof", payload["asof"],
               "--file", str(path), "--arm", arm, "--model-id", model,
               "--prompt-sha256", prompt, capsys=capsys)


def test_t6_a_model_that_does_not_match_the_preregistration_is_refused(
        db, capsys, tmp_path):
    """预注册 `(m, s)`；`--model-id 别的` ⇒ exit 2、**零写入**。"""
    arm = _full(db, capsys)["account_id"]
    payload = {**BUY_10, "context_sha256": _context_sha(db, capsys, arm, START)}
    before = _counts(db)
    code, _, err = _decide(db, capsys, arm, payload, tmp_path,
                           model="deepseek/deepseek-v4-flash")
    assert code == 2, err
    assert "开新版本账户" in err and "预注册" in err
    assert _counts(db) == before, "预注册不匹配时写了东西"


def test_t6_a_prompt_that_does_not_match_the_preregistration_is_refused(
        db, capsys, tmp_path):
    arm = _full(db, capsys)["account_id"]
    payload = {**BUY_10, "context_sha256": _context_sha(db, capsys, arm, START)}
    before = _counts(db)
    code, _, err = _decide(db, capsys, arm, payload, tmp_path, prompt="c" * 64)
    assert code == 2 and _counts(db) == before


def test_t6_matching_the_preregistration_stores_the_row(db, capsys, tmp_path):
    arm = _full(db, capsys)["account_id"]
    payload = {**BUY_10, "context_sha256": _context_sha(db, capsys, arm, START)}
    code, out, err = _decide(db, capsys, arm, payload, tmp_path)
    assert code == 0, err
    assert json.loads(out)["status"] == "已写入"
    rows = _decisions(db, arm)
    assert len(rows) == 1 and rows[0]["model_id"] == MODEL
    assert rows[0]["prompt_sha256"] == PROMPT


def test_t6_an_account_without_preregistration_is_refused(db, capsys, tmp_path):
    """没有预注册的账户 ⇒ **也拒**（D-48 的闸门是机械的：不 warn、不静默采用）。

    内置 `arm-agent` 就是这样一条账户（迁移只补 `executor`、不补 model/prompt）：
    要接模型必须开新版本账户 —— 这条纪律挡的是「换到好看为止」。
    """
    _init(db, capsys)
    payload = {**BUY_10, "context_sha256": _context_sha(db, capsys, ARM_AGENT, START)}
    before = _counts(db)
    code, _, err = _decide(db, capsys, ARM_AGENT, payload, tmp_path)
    assert code == 2, err
    assert "没有预注册" in err and "paper agent enroll" in err
    assert _counts(db) == before, "没有预注册时写了东西"


def test_t7_a_wrong_context_sha256_is_refused_with_zero_writes(db, capsys, tmp_path):
    """载荷里的 `context_sha256` 塞错 ⇒ 拒绝且**零写入**（D-49）。"""
    arm = _full(db, capsys)["account_id"]
    payload = {**BUY_10, "context_sha256": "0" * 64}
    before = _counts(db)
    code, _, err = _decide(db, capsys, arm, payload, tmp_path)
    assert code == 2, err
    assert "上下文指纹不符" in err
    assert _counts(db) == before


def test_t7_a_missing_context_sha256_is_refused(db, capsys, tmp_path):
    """载荷**不带** `context_sha256` ⇒ 也拒（D-49 的「只能从 context 取输入」是硬的）。"""
    arm = _full(db, capsys)["account_id"]
    before = _counts(db)
    code, _, err = _decide(db, capsys, arm, BUY_10, tmp_path)
    assert code == 2, err
    assert "必须回传" in err
    assert _counts(db) == before


def test_t7_a_valid_context_sha256_is_stored_verbatim(db, capsys, tmp_path):
    """正确 ⇒ 落库，且台账里的 `context_sha256` 就是库里重算的那个值。"""
    arm = _full(db, capsys)["account_id"]
    want = _context_sha(db, capsys, arm, START)
    payload = {**BUY_10, "context_sha256": want}
    code, _, err = _decide(db, capsys, arm, payload, tmp_path)
    assert code == 0, err
    row = _decisions(db, arm)[0]
    assert row["context_sha256"] == want
    assert row["payload"]["context_sha256"] == want, \
        "回传的指纹要进载荷指纹（同一天两份不同上下文不是「重放」）"


def test_t7_a_later_pool_change_makes_the_old_payload_stale(db, capsys, tmp_path):
    """反向自检：上下文变了（池子变了）⇒ 旧载荷的指纹**对不上**，必须拒。

    这条钉的是「指纹真的在跟着输入走」—— 若指纹恒定，它会绿着而毫无保护。
    """
    arm = _full(db, capsys)["account_id"]
    stale = _context_sha(db, capsys, arm, START)
    c = connect(db)
    try:
        c.execute("INSERT INTO candidate_snapshots (asof, run_kind, params_json,"
                  " created_at) VALUES (?,'weekly','{}',?)", (START, NOW))
        sid = c.execute("SELECT MAX(snapshot_id) FROM candidate_snapshots").fetchone()[0]
        c.execute("INSERT INTO candidate_members (snapshot_id, code, pool, raw_score,"
                  " adj_score, reason, risk_json, status, entered_at)"
                  " VALUES (?,'000333','long',1,1,'夹具','{}','观察中',?)", (sid, NOW))
        c.commit()
    finally:
        c.close()
    fresh = _context_sha(db, capsys, arm, START)
    assert fresh != stale, "池子变了但指纹没变 —— 指纹漏掉了输入"
    payload = {**BUY_10, "context_sha256": stale}
    code, _, err = _decide(db, capsys, arm, payload, tmp_path)
    assert code == 2 and "上下文指纹不符" in err


# ══════════════════════════════════════════════════════════════════════
# T8：缺决策显式化（`show` 的三字段；页面渲染在 test_labweb_paper_agent.py）
# ══════════════════════════════════════════════════════════════════════

def test_t8_show_carries_the_three_field_missing_decision_block(db, capsys):
    """`missing_decision` 三字段齐全：`is_trading_day` / `account_in_flight` / `expected`。"""
    arm = _full(db, capsys)["account_id"]
    code, out, err = run(db, "paper", "agent", "show", "--asof", START,
                         "--arm", arm, capsys=capsys)
    assert code == 0, err
    got = json.loads(out)
    md = got["missing_decision"]
    assert md["is_trading_day"] is True
    assert md["account_in_flight"] is True
    assert md["expected"] is True
    assert md["present"] is False
    assert "今日无决策" in md["note"]
    assert got["preregistered"] == {"model_id": MODEL, "prompt_sha256": PROMPT}


def test_t8_show_marks_a_day_that_does_not_require_a_decision(db, capsys):
    """非交易日：`expected` 为假，措辞是「不要求」而不是「缺」。"""
    arm = _full(db, capsys)["account_id"]
    code, out, _ = run(db, "paper", "agent", "show", "--asof", "2026-09-12",
                       "--arm", arm, capsys=capsys)
    assert code == 0
    md = json.loads(out)["missing_decision"]
    assert md["is_trading_day"] is False and md["expected"] is False
    assert "不要求决策" in md["note"]


def test_t8_show_distinguishes_present_from_missing(db, capsys, tmp_path):
    """有决策时 `present` 为真且不再给「今日无决策」的措辞（两种形态不同）。"""
    arm = _full(db, capsys)["account_id"]
    payload = {**BUY_10, "context_sha256": _context_sha(db, capsys, arm, START)}
    assert _decide(db, capsys, arm, payload, tmp_path)[0] == 0
    code, out, _ = run(db, "paper", "agent", "show", "--asof", START,
                       "--arm", arm, capsys=capsys)
    assert code == 0
    md = json.loads(out)["missing_decision"]
    assert md["present"] is True and md["note"] is None


# ══════════════════════════════════════════════════════════════════════
# 迁移：`migrate_p56_agent_arms_executor` 可重入（任务书 §3.2）
# ══════════════════════════════════════════════════════════════════════

def test_migration_adds_executor_and_is_reentrant(db, capsys):
    """迁移：只加 `executor` 一个键，其余键值逐字不变；**跑两次结果相同**。"""
    from stocklab.store import migrate

    _init(db, capsys)
    c = connect(db)
    try:
        # 先手工还原成「P56 之前」的样子（摘掉 executor 键）
        rows = {r["account_id"]: json.loads(r["params_json"])
                for r in c.execute("SELECT account_id, params_json FROM paper_accounts")}
        c.execute("DROP TRIGGER IF EXISTS trg_paper_accounts_no_update")
        for aid, params in rows.items():
            params.pop(EXECUTOR_KEY, None)
            c.execute("UPDATE paper_accounts SET params_json=? WHERE account_id=?",
                      (json.dumps(params, ensure_ascii=False, sort_keys=True), aid))
        c.execute("CREATE TRIGGER IF NOT EXISTS trg_paper_accounts_no_update"
                  " BEFORE UPDATE ON paper_accounts"
                  " BEGIN SELECT RAISE(ABORT, 'paper_accounts is append-only'); END")
        c.commit()
        stale = [str(r["account_id"]) for r in c.execute(
            "SELECT account_id FROM paper_accounts ORDER BY account_id")]
        assert migrate.agent_arms_need_executor(c) == [ARM_AGENT, ARM_AGENT_RANDOM]

        changed = migrate.migrate_p56_agent_arms_executor(c)
        assert sorted(changed) == sorted([
            f"{ARM_AGENT}: executor -> 'agent_decision'",
            f"{ARM_AGENT_RANDOM}: executor -> 'agent_decision'"])
        after = {r["account_id"]: json.loads(r["params_json"])
                 for r in c.execute("SELECT account_id, params_json FROM paper_accounts")}
        # 静态 5 条**逐字段不变**；两条 AI 臂只多一个键
        for aid in stale:
            if aid in (ARM_AGENT, ARM_AGENT_RANDOM):
                assert after[aid][EXECUTOR_KEY] == EXECUTOR_AGENT_DECISION
                assert {k: v for k, v in after[aid].items() if k != EXECUTOR_KEY} \
                    == rows[aid], f"{aid} 除 executor 外的键值被改了"
            else:
                assert after[aid] == rows[aid], f"静态臂 {aid} 被迁移动了"
        # **可重入**：第二次什么都不做
        assert migrate.migrate_p56_agent_arms_executor(c) == []
        assert migrate.agent_arms_need_executor(c) == []
    finally:
        c.close()


def test_migration_is_a_noop_on_a_fresh_db(db, capsys):
    """新库：`paper init` 已经写好 executor ⇒ 迁移返回 `[]`（不是唯一真源）。"""
    from stocklab.store import migrate

    _init(db, capsys)
    c = connect(db)
    try:
        assert migrate.agent_arms_need_executor(c) == []
        assert migrate.migrate_p56_agent_arms_executor(c) == []
        assert migrate.schema_status(c)["markers"]["p56_agent_arms_executor"]["present"]
    finally:
        c.close()


def test_paper_init_is_idempotent_and_keeps_the_static_arms_byte_identical(db, capsys):
    """`paper init` 幂等：重跑只补缺口，静态臂的 `params_json` 逐字节不变。"""
    _init(db, capsys)
    c = connect(db)
    try:
        before = {r["account_id"]: r["params_json"] for r in c.execute(
            "SELECT account_id, params_json FROM paper_accounts")}
    finally:
        c.close()
    again = run(db, "paper", "init", capsys=capsys)
    assert again[0] == 0 and json.loads(again[1])["created"] is False
    c = connect(db)
    try:
        after = {r["account_id"]: r["params_json"] for r in c.execute(
            "SELECT account_id, params_json FROM paper_accounts")}
    finally:
        c.close()
    assert after == before
    assert len(after) == 7
    for aid in (ARM_AGENT, ARM_AGENT_RANDOM):
        assert json.loads(after[aid])[EXECUTOR_KEY] == EXECUTOR_AGENT_DECISION
