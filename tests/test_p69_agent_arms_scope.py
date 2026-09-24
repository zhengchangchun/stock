"""P69：认领范围（T1 `--arm` 选择器）与在飞/停飞位（T2 `params.live`）。

任务书 `docs/tasks/2026-09-24-p69-模拟操盘可见性.md` §T1 / §T2。

| 判据 | 被摘掉后会红的实现 |
|---|---|
| 传 `--arm` ⇒ 只认领点名的臂，其余账户**一行不写** | 驱动点名了，别的臂当天仍被写平盘行 |
| 不传 `--arm` 与「点名全部臂」**逐字段相同** | 过滤器顺手改了默认路径 |
| 点名一个不在认领范围的臂 ⇒ 退出码 2 ＋ 零写入 | 静默跳过 ⇒ 驱动以为落了日终 |
| 停飞臂（`live=false`）不被认领、不算缺决策 | 占位臂每天一条假警 ⇒ 日更退出码恒 1 |
| `live` 缺省 `true`；写错类型点名报错 | 写成 `"false"`（真值）⇒ 假警照旧 |
| 停飞只改 `params.live`，台账/净值/成交一行不动 | 「停飞」被实现成「清掉历史」 |

夹具与 `tests/test_p56_agent_arms.py` 同一套口径（离线、`paper init` 起）。
"""

from __future__ import annotations

import json

import pytest

from stocklab.cli.main import main
from stocklab.paper import engine, store as paper_store
from stocklab.paper.config import (
    ARM_AGENT,
    ARM_AGENT_RANDOM,
    EXECUTOR_AGENT_DECISION,
    HALTED_LABEL,
    LIVE_KEY,
)
from stocklab.store.db import connect
from stocklab.store import migrate
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
MODEL = "deepseek/deepseek-v4-pro"
PROMPT = "b" * 64
ARM_A = "arm-agent-ds-a"
ARM_B = "arm-agent-ds-b"


def make_db(tmp_path, name="p69.db"):
    """一份能跑 `paper init` + 决策循环的库（两处调用要**逐字节同源**时才建第二份）。"""
    path = tmp_path / name
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


@pytest.fixture
def db(tmp_path):
    return make_db(tmp_path)


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db), "--now", NOW])
    out, err = capsys.readouterr()
    return code, out, err


def _init(db, capsys):
    code, _, err = run(db, "paper", "init", capsys=capsys)
    assert code == 0, err


def _enroll(db, capsys, name):
    code, out, err = run(db, "paper", "agent", "enroll", "--arm-name", name,
                         "--model-id", MODEL, "--prompt-sha256", PROMPT,
                         capsys=capsys)
    assert code == 0, err
    return json.loads(out)


def _context_sha(db, capsys, arm, asof):
    code, out, err = run(db, "paper", "agent", "context", "--asof", asof,
                         "--arm", arm, capsys=capsys)
    assert code == 0, err
    return json.loads(out)["context_sha256"]


def _decide(db, capsys, arm, asof, tmp_path):
    """给 `arm` 写一条能落库的操盘决策（指纹从上下文现取，保证不是编的）。"""
    payload = {"asof": asof,
               "decisions": [{"code": "510300", "side": "buy",
                              "target_weight_pct": 10.0, "reason": "夹具"}],
               "cash_pct": 90.0, "rationale": "夹具",
               "context_sha256": _context_sha(db, capsys, arm, asof)}
    path = tmp_path / f"{arm}-{asof}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    code, _, err = run(db, "paper", "agent", "decide", "--asof", asof,
                       "--file", str(path), "--arm", arm, "--model-id", MODEL,
                       "--prompt-sha256", PROMPT, capsys=capsys)
    assert code == 0, err


def _random(db, capsys, asof):
    """随机对照臂的决策由**项目内**产出（`paper agent random`，种子固定）。"""
    code, _, err = run(db, "paper", "agent", "random", "--asof", asof,
                       "--arm", ARM_AGENT_RANDOM, capsys=capsys)
    assert code == 0, err


def _run(db, *extra, capsys):
    code, out, err = run(db, "paper", "agent", "run", "--asof", START, *extra,
                         capsys=capsys)
    return json.loads(out), code, err


def _snapshot(db):
    """全库行数 + 逐账户 `params_json` —— 「零写入」的判据。"""
    c = connect(db)
    try:
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("paper_accounts", "paper_nav_daily", "paper_trades",
                            "paper_agent_decisions")}
        params = {str(r["account_id"]): str(r["params_json"])
                  for r in c.execute("SELECT account_id, params_json"
                                     " FROM paper_accounts ORDER BY account_id")}
        return counts, params
    finally:
        c.close()


def _set_live(db, arm, value):
    """只在 **fixture / 库副本** 上用（真库那条路走 `store/migrate` 的迁移）。"""
    c = connect(db)
    try:
        c.execute("DROP TRIGGER IF EXISTS trg_paper_accounts_no_update")
        row = c.execute("SELECT params_json FROM paper_accounts WHERE account_id = ?",
                        (arm,)).fetchone()
        params = json.loads(row["params_json"] or "{}")
        params[LIVE_KEY] = value
        c.execute("UPDATE paper_accounts SET params_json = ? WHERE account_id = ?",
                  (json.dumps(params, ensure_ascii=False, sort_keys=True), arm))
        c.commit()
    finally:
        c.close()


def _prepare(db, capsys, tmp_path, *, decided=(ARM_A, ARM_B)):
    """`init` + 两条版本账户 + 决策（两条 LLM ＋ 随机臂）。"""
    _init(db, capsys)
    for name in (ARM_A, ARM_B):
        _enroll(db, capsys, name)
    for name in decided:
        _decide(db, capsys, name, START, tmp_path)
    _random(db, capsys, START)


# ══════════════════════════════════════════════════════════════════════
# T1：`paper agent run --arm`
# ══════════════════════════════════════════════════════════════════════

def test_t1_without_arm_every_claimed_account_is_still_claimed(db, capsys, tmp_path):
    """不传 `--arm` ⇒ 认领全部四条（默认路径一字不改）。"""
    _prepare(db, capsys, tmp_path)
    c = connect(db)
    try:
        claimed = [a["account_id"] for a in engine.agent_claim_accounts(c)]
    finally:
        c.close()
    assert claimed == sorted([ARM_AGENT, ARM_AGENT_RANDOM, ARM_A, ARM_B])
    rep, code, _ = _run(db, capsys=capsys)
    assert rep["n_claimed"] == 4
    assert [a["account_id"] for a in rep["accounts"]] == claimed
    assert code == 1, "`arm-agent` 无预注册 ⇒ 当天必然缺决策 ⇒ 退出码 1（T2 要修的就是它）"


def test_t1_only_the_named_arms_are_claimed(db, capsys, tmp_path):
    """点名两条 ⇒ `n_claimed=2`、回执只含这两条、其余账户**一行不写**、退出码 0。"""
    _prepare(db, capsys, tmp_path)
    rep, code, err = _run(db, "--arm", ARM_A, "--arm", ARM_AGENT_RANDOM,
                          capsys=capsys)
    assert code == 0, (rep, err)
    assert rep["n_claimed"] == 2
    assert [a["account_id"] for a in rep["accounts"]] == [ARM_A, ARM_AGENT_RANDOM]
    assert rep["anomaly"]["missing_decision"] == []
    c = connect(db)
    try:
        for aid in (ARM_AGENT, ARM_B):
            assert not paper_store.nav_exists(c, aid, START), \
                f"{aid} 没被点名，却写了当天的净值行"
    finally:
        c.close()


def test_t1_an_unnamed_arm_never_answers_already(db, capsys, tmp_path):
    """重跑一次：只回点名那两条 `already`，没点名的**不出现在回执里**。"""
    _prepare(db, capsys, tmp_path)
    _run(db, "--arm", ARM_A, "--arm", ARM_B, capsys=capsys)
    rep, code, err = _run(db, "--arm", ARM_A, "--arm", ARM_B, capsys=capsys)
    assert code == 0, (rep, err)
    assert [a["status"] for a in rep["accounts"]] == ["already", "already"]
    assert rep["n_claimed"] == 2, "没点名的臂不该因为「已存在」被列进来"


def test_t1_naming_an_arm_out_of_scope_is_named_and_writes_nothing(db, capsys):
    """点名的账户不在认领范围（别的执行者）⇒ 点名报错、退出码 2、**零写入**。"""
    _init(db, capsys)
    _enroll(db, capsys, ARM_A)
    before = _snapshot(db)
    code, out, err = run(db, "paper", "agent", "run", "--asof", START,
                         "--arm", "arm-agent-v1", capsys=capsys)
    assert code == 2, (out, err)
    assert "arm-agent-v1" in err
    assert f"{LIVE_KEY}=false" in err, "报错要点名「已停飞」这条可能的原因"
    assert _snapshot(db) == before


def test_t1_naming_an_arm_that_does_not_exist_is_named(db, capsys):
    """名字打错 ⇒ 同样点名报错（不静默跳过）。"""
    _init(db, capsys)
    code, _, err = run(db, "paper", "agent", "run", "--asof", START,
                       "--arm", "arm-agent-nope", capsys=capsys)
    assert code == 2
    assert "arm-agent-nope" in err


def test_t1_repeating_the_same_arm_does_not_double_claim(db, capsys, tmp_path):
    """`--arm A --arm A` ⇒ 仍只认领一条（去重，且不该被判成越界）。"""
    _prepare(db, capsys, tmp_path)
    rep, code, err = _run(db, "--arm", ARM_A, "--arm", ARM_A, capsys=capsys)
    assert code == 0, (rep, err)
    assert rep["n_claimed"] == 1


def test_t1_naming_all_arms_is_field_by_field_identical_to_no_filter(
        tmp_path, capsys):
    """**等价性判据**：`run`（不传）与 `run --arm <全部四条>` 逐字段相同。

    这是 T1「旧行为逐字段不变」在**今天**可复现的形式：两条命令走同一段实现，差别只在
    认领集合是不是被显式点名。两份夹具**逐字节同源** ⇒ 两条回执必须逐字段相等。

    任务书原判据（与 `ai-trader-2026-09-23/run-receipt.json` 逐字段相同）**不可复现**：
    ① `arm-agent-ds-v2` 是 09-23 23:27 才 enroll 的 —— 当时认领 3 条、现在 4 条；
    ② 那三条臂的 09-23 净值行已存在 ⇒ 重跑只会回 `already`（回执里是 `ran`）；
    ③ P62 已把 `arm-agent-ds-v1` 那行坏净值 11320.0 修成 19553.68。
    见任务书 §7 的 J-T1（本站对历史回执做的是**交集逐字段对照 ＋ 三条不可复现原因的归因**）。
    """
    plain_db = make_db(tmp_path, "plain.db")
    every_db = make_db(tmp_path, "every.db")
    for path in (plain_db, every_db):
        _prepare(path, capsys, tmp_path)
    plain, code_plain, _ = _run(plain_db, capsys=capsys)
    every, code_every, _ = _run(
        every_db, "--arm", ARM_AGENT, "--arm", ARM_AGENT_RANDOM,
        "--arm", ARM_A, "--arm", ARM_B, capsys=capsys)
    assert code_plain == code_every == 1
    assert plain == every, "点名全部臂与不点名必须逐字段相同（过滤器是纯选择器）"


# ══════════════════════════════════════════════════════════════════════
# T2：`params.live` 停飞位
# ══════════════════════════════════════════════════════════════════════

def test_t2_live_defaults_to_true():
    """缺省在飞 —— 老账户不加键也照跑（纯函数，不需要库）。"""
    assert engine.live_of({}) is True
    assert engine.live_of({LIVE_KEY: True}) is True


def test_t2_a_non_bool_live_is_named_not_coerced(db):
    """写错类型 ⇒ 点名报错（`"false"` 是真值 ⇒ 会静默继续报假警）。"""
    with pytest.raises(engine.PaperError) as e:
        engine.live_of({LIVE_KEY: "false"})
    assert LIVE_KEY in str(e.value) and "false" in str(e.value)
    with pytest.raises(engine.PaperError):
        engine.live_of({LIVE_KEY: 0})


def test_t2_a_halted_arm_is_not_claimed_and_not_missing(db, capsys, tmp_path):
    """停飞臂不进认领、不进 `missing_decision` ⇒ 那条假警消失、退出码回到 0。"""
    _prepare(db, capsys, tmp_path)
    _set_live(db, ARM_AGENT, False)
    rep, code, err = _run(db, capsys=capsys)
    assert ARM_AGENT not in [a["account_id"] for a in rep["accounts"]]
    assert ARM_AGENT not in [m["account_id"]
                             for m in rep["anomaly"]["missing_decision"]]
    assert rep["n_claimed"] == 3
    assert code == 0, (rep, err)      # 修 T2 之前这里是 1（arm-agent 的假警）


def test_t2_a_halted_arm_is_not_expected_to_decide(db, capsys):
    """`decision_expectation` 对停飞臂恒 `expected=False`（页面据此不写「缺决策」）。"""
    _init(db, capsys)

    def _expectation():
        c = connect(db)
        try:
            acct = next(a for a in engine.store.load_accounts(c)
                        if a["account_id"] == ARM_AGENT)
            return engine.decision_expectation(c, account=acct, asof=START)
        finally:
            c.close()

    live = _expectation()
    assert live["live"] is True and live["expected"] is True
    assert live["account_in_flight"] is True
    _set_live(db, ARM_AGENT, False)
    halted = _expectation()
    assert halted["live"] is False
    assert halted["account_in_flight"] is False
    assert halted["expected"] is False


def test_t2_a_halted_arm_keeps_its_history_row_for_row(db, capsys, tmp_path):
    """停飞**不改历史**：台账 / 净值 / 成交一行不动、逐字段相同。"""
    _prepare(db, capsys, tmp_path)
    _run(db, capsys=capsys)

    def _rows():
        c = connect(db)
        try:
            return {t: [tuple(r) for r in c.execute(*q)]
                    for t, q in {
                        "nav": ("SELECT * FROM paper_nav_daily"
                                " WHERE account_id = ? ORDER BY date", (ARM_A,)),
                        "decisions": ("SELECT * FROM paper_agent_decisions"
                                      " WHERE arm = ? ORDER BY asof", (ARM_A,)),
                        "trades": ("SELECT * FROM paper_trades"
                                   " WHERE account_id = ? ORDER BY trade_id", (ARM_A,)),
                    }.items()}
        finally:
            c.close()

    before = _rows()
    assert before["nav"] and before["decisions"], "夹具要先有历史可保"
    _set_live(db, ARM_A, False)
    assert _rows() == before


def test_t2_all_halted_is_a_named_error_not_a_silent_pass(db, capsys):
    """全部停飞 ⇒ 没有认领对象，点名报错（退出码 2），不是静默 exit 0。"""
    _init(db, capsys)
    for arm in (ARM_AGENT, ARM_AGENT_RANDOM):
        _set_live(db, arm, False)
    code, _, err = run(db, "paper", "agent", "run", "--asof", START, capsys=capsys)
    assert code == 2
    assert EXECUTOR_AGENT_DECISION in err


def test_t2_a_halted_arm_is_not_accepted_as_a_named_arm(db, capsys):
    """停飞臂被 `--arm` 点名 ⇒ 报错（它已经不是本命令的认领对象）。"""
    _init(db, capsys)
    _set_live(db, ARM_AGENT, False)
    code, _, err = run(db, "paper", "agent", "run", "--asof", START,
                       "--arm", ARM_AGENT, capsys=capsys)
    assert code == 2
    assert ARM_AGENT in err


# ══════════════════════════════════════════════════════════════════════
# T2 的落位：`migrate_p69_agent_arms_live`（真库那条路由 nanobot 显式调）
# ══════════════════════════════════════════════════════════════════════

def _table_counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("paper_accounts", "paper_nav_daily", "paper_trades",
                      "paper_agent_decisions", "real_trades", "cash_flows")}


def test_t2_migration_halts_exactly_the_two_named_arms(db, capsys, tmp_path):
    """迁移只动点名那两条：其余账户逐字节不变、行数不变、台账/净值/成交行数不变。"""
    _prepare(db, capsys, tmp_path)
    _enroll(db, capsys, "arm-agent-ds-v1")
    _decide(db, capsys, "arm-agent-ds-v1", START, tmp_path)
    _run(db, capsys=capsys)
    c = connect(db)
    try:
        before_params = {str(r["account_id"]): str(r["params_json"])
                         for r in c.execute("SELECT account_id, params_json"
                                            " FROM paper_accounts")}
        before_counts = _table_counts(c)
        changes = migrate.migrate_p69_agent_arms_live(c)
        after_params = {str(r["account_id"]): str(r["params_json"])
                        for r in c.execute("SELECT account_id, params_json"
                                           " FROM paper_accounts")}
        assert _table_counts(c) == before_counts, "迁移不许增删任何行"
    finally:
        c.close()
    assert changes == ["arm-agent: live -> False", "arm-agent-ds-v1: live -> False"]
    for aid in (ARM_AGENT, "arm-agent-ds-v1"):
        assert json.loads(after_params[aid])[LIVE_KEY] is False
        assert json.loads(before_params[aid]).get(LIVE_KEY) is None, "起点没有这个键"
        old = json.loads(before_params[aid]); new = json.loads(after_params[aid])
        new.pop(LIVE_KEY)
        assert new == old, f"{aid} 除 live 外的键值被改动了"
    others = set(before_params) - {ARM_AGENT, "arm-agent-ds-v1"}
    assert others and all(after_params[a] == before_params[a] for a in others)


def test_t2_migration_is_reentrant_and_leaves_the_trigger_in_place(db, capsys):
    _init(db, capsys)
    c = connect(db)
    try:
        assert migrate.migrate_p69_agent_arms_live(c) == ["arm-agent: live -> False"]
        assert migrate.migrate_p69_agent_arms_live(c) == [], "跑第二次必须是空操作"
        triggers = [str(r[0]) for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
            " AND name='trg_paper_accounts_no_update'")]
        assert triggers, "迁移摘过触发器，必须挂回去"
        with pytest.raises(Exception):
            c.execute("UPDATE paper_accounts SET params_json='{}'"
                      " WHERE account_id = 'arm-hold'")
    finally:
        c.close()


def test_t2_migration_is_a_noop_when_the_named_arms_do_not_exist(tmp_path):
    """库里没有点名的那些账户 ⇒ 空操作（不报错、不凭空造账户）。"""
    path = make_db(tmp_path, "empty.db")
    c = connect(path)
    try:
        assert migrate.migrate_p69_agent_arms_live(c) == []
        assert c.execute("SELECT COUNT(*) FROM paper_accounts").fetchone()[0] == 0
    finally:
        c.close()


def test_t2_the_probe_agrees_with_the_migration(db, capsys):
    """只读探针说「齐了」时迁移就该是空操作 —— 两者不许各说各话。"""
    _init(db, capsys)
    c = connect(db)
    try:
        assert migrate.agent_arms_need_p69_live(c) == [ARM_AGENT]
        migrate.migrate_p69_agent_arms_live(c)
        assert migrate.agent_arms_need_p69_live(c) == []
    finally:
        c.close()


def test_t2_show_calls_a_halted_arm_halted_not_missing(db, capsys):
    """`paper agent show`：停飞臂写「历史版本（已停飞）」且**不写「今日无决策」**。"""
    _init(db, capsys)
    code, out, err = run(db, "paper", "agent", "show", "--asof", START,
                         "--arm", ARM_AGENT, capsys=capsys)
    assert code == 0, err
    live = json.loads(out)
    assert live["live"] is True and live["missing_decision"]["expected"] is True
    assert "今日无决策" in live["missing_decision"]["note"]

    _set_live(db, ARM_AGENT, False)
    code, out, err = run(db, "paper", "agent", "show", "--asof", START,
                         "--arm", ARM_AGENT, capsys=capsys)
    assert code == 0, err
    halted = json.loads(out)
    assert halted["live"] is False
    assert halted["missing_decision"]["expected"] is False
    assert HALTED_LABEL in halted["missing_decision"]["note"]
    assert "今日无决策" not in halted["missing_decision"]["note"], \
        "停飞不是「今天没决定」—— 两种话术开头就不许一样"
