"""P37：CLI `paper spec set` / `paper spec show`（设计 §4.5）。

退出码是这里的主要验收项，因为它把两种「失败」分开了：

- **2** = 你给的东西不合法（越界 / 未知字段 / 不是 JSON / 账户不存在）——
  和 `paper` 其余子命令同一个 `_paper_fail` 约定；
- **1** = 你给的合法，但与库里已有的一版**撞了键** —— append-only，本命令不改写历史。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 同 `(arm, asof)` 同结果重跑 → exit 0「已存在且一致（未写入）」 | 每次调用都插一行（台账会被灌满） |
| 同 `(arm, asof)` 不同结果 → exit 1 且原行一字节不改 | 静默覆盖（append-only 成为空话） |
| `show` 的 JSON 与库内容同源 | 页面/CLI 各算一套 spec |
| 只在 `arm-agent` / `arm-agent-random` 上写 | 往静态臂上写出一张没人读的台账 |
| 越界 / 未知字段 → exit 2 且**一行都不写** | 先写后校验（半截状态） |
| `step` 之后成交的 `rule_citation` 落在 `RULE_CITATIONS_AGENT` | 参数化了却还在引用写死条文 |
"""

import json

import pytest

from stocklab.cli.main import main
from stocklab.paper import agent_spec
from stocklab.paper.config import ARM_AGENT, ARM_AGENT_RANDOM, RULE_CITATIONS_AGENT
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
CAL = ("2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16")
BARS = {
    "000333": {"2026-09-14": 86.80, "2026-09-15": 87.23, "2026-09-16": 87.60},
    "510300": {"2026-09-15": 4.523, "2026-09-16": 4.500},
    "510880": {"2026-09-15": 3.382, "2026-09-16": 3.390},
    "sh000300": {"2026-09-15": 4450.04, "2026-09-16": 4460.00},
}


@pytest.fixture
def db(tmp_db):
    """一份 `paper init` 过的库（口径与 `test_cli_paper` 同一套）。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sz','main',?,?)",
                  [(code, code, "stock" if code == "000333" else "etf", NOW)
                   for code in ("000333", "510300", "510880")])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, s in BARS.items() for d, v in s.items()])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (NOW,))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-14','000333','buy',86.80,100,5.09,"
              " '首笔',?)", (NOW,))
    c.commit()
    c.close()
    return tmp_db


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db), "--now", NOW])
    out, err = capsys.readouterr()
    return code, out, err


def _init(db, capsys):
    code, _, err = run(db, "paper", "init", capsys=capsys)
    assert code == 0, err


def _rows(db, arm=ARM_AGENT):
    c = connect(db)
    try:
        return agent_spec.load_decisions(c, arm)
    finally:
        c.close()


# ---------- show：只读、与库同源 ----------

def test_show_defaults_when_the_ledger_is_empty(db, capsys):
    _init(db, capsys)
    code, out, err = run(db, "paper", "spec", "show", "--arm", ARM_AGENT,
                         "--asof", "2026-09-21", capsys=capsys)
    assert code == 0, err
    p = json.loads(out)
    assert p["arm"] == ARM_AGENT and p["asof"] == "2026-09-21"
    assert p["account_exists"] is True
    assert p["spec"] == agent_spec.AGENT_DEFAULT_SPEC
    assert p["default_spec"] == agent_spec.AGENT_DEFAULT_SPEC
    assert p["spec_sha256"] == agent_spec.spec_sha256(agent_spec.AGENT_DEFAULT_SPEC)
    assert p["n_rows"] == 0 and p["history"] == []
    assert p["summary"]["n_reviews"] == 0
    assert p["reproducibility"]["reproducible"] is None
    assert set(p["change_space"]) == set(agent_spec.SPEC_SCHEMA)
    # stderr 上还有一行给终端看的摘要（stdout 是给机器读的完整 JSON）
    assert json.loads(err.strip().splitlines()[-1])["n_reviews"] == 0


def test_show_without_asof_uses_todays_date(db, capsys):
    _init(db, capsys)
    code, out, err = run(db, "paper", "spec", "show", "--arm", ARM_AGENT, capsys=capsys)
    assert code == 0, err
    assert json.loads(out)["asof"] == NOW[:10]


def test_show_on_an_uninitialised_store_says_the_account_is_missing(db, capsys):
    """没有账户也要答得出来（`account_exists: false`），而不是报错。"""
    code, out, err = run(db, "paper", "spec", "show", "--arm", ARM_AGENT,
                         "--asof", "2026-09-21", capsys=capsys)
    assert code == 0, err
    assert json.loads(out)["account_exists"] is False


def test_show_refuses_arms_whose_rules_are_not_spec_driven(db, capsys):
    _init(db, capsys)
    code, _, err = run(db, "paper", "spec", "show", "--arm", "arm-hold",
                       "--asof", "2026-09-21", capsys=capsys)
    assert code == 2
    assert "decision" not in json.loads(err)      # 不是冲突（1），是不合法（2）
    assert json.loads(err)["type"] == "SpecViolation"


# ---------- set：写一版 ----------

def test_set_writes_one_row_and_show_reads_it_back(db, capsys):
    _init(db, capsys)
    spec = '{"etf_target_pct": 12, "stop_loss_pct": -6.0}'
    code, out, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                         "--asof", "2026-09-16", "--spec", spec,
                         "--rationale", "手写一版", "--n-trials", "2", capsys=capsys)
    assert code == 0, err
    p = json.loads(out)
    assert p["status"] == "已写入" and p["n_trials"] == 2
    assert p["spec"]["etf_target_pct"] == 12.0
    assert p["spec"]["stop_loss_pct"] == -6.0
    assert p["spec"]["max_single_pct"] == \
        agent_spec.AGENT_DEFAULT_SPEC["max_single_pct"], "只写要改的字段（增量合并）"
    assert p["spec_before"] == agent_spec.AGENT_DEFAULT_SPEC
    assert p["context_sha256"]

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["decision_id"] == p["decision_id"]
    assert rows[0]["agent_kind"] == agent_spec.AGENT_KIND_MANUAL
    assert rows[0]["model_id"] == agent_spec.MANUAL_MODEL_ID
    assert rows[0]["prompt_sha256"] == agent_spec.MANUAL_PROMPT_SHA256
    assert rows[0]["seed"] == 0
    assert rows[0]["rationale"] == "手写一版"
    assert rows[0]["spec_after"] == p["spec"]

    code, out, err = run(db, "paper", "spec", "show", "--arm", ARM_AGENT,
                         "--asof", "2026-09-16", capsys=capsys)
    assert code == 0, err
    q = json.loads(out)
    assert q["spec"] == p["spec"]
    assert q["spec_sha256"] == agent_spec.spec_sha256(p["spec"])
    assert q["n_rows"] == 1
    assert q["summary"]["n_reviews"] == 1
    assert [h["decision_id"] for h in q["history"]] == [p["decision_id"]]


def test_set_is_idempotent_on_the_same_key(db, capsys):
    _init(db, capsys)
    argv = ("paper", "spec", "set", "--arm", ARM_AGENT, "--asof", "2026-09-16",
            "--spec", '{"etf_target_pct": 12}')
    code, first, err = run(db, *argv, capsys=capsys)
    assert code == 0, err
    code, second, err = run(db, *argv, capsys=capsys)
    assert code == 0, err
    p = json.loads(second)
    assert p["status"] == "已存在且一致（未写入）"
    assert p["decision_id"] == json.loads(first)["decision_id"]
    assert len(_rows(db)) == 1


def test_conflicting_second_version_exits_one_and_leaves_the_row_alone(db, capsys):
    _init(db, capsys)
    code, _, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                       "--asof", "2026-09-16", "--spec", '{"etf_target_pct": 12}',
                       capsys=capsys)
    assert code == 0, err
    before = _rows(db)[0]

    code, out, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                         "--asof", "2026-09-16", "--spec", '{"etf_target_pct": 20}',
                         capsys=capsys)
    assert code == 1, "同 (arm, asof) 撞键必须是「可预期冲突」而不是「输入非法」"
    assert out == ""
    conflict = json.loads(err)
    assert conflict["existing"]["decision_id"] == before["decision_id"]
    assert conflict["attempted"]["etf_target_pct"] == 20.0
    assert _rows(db) == [before], "原行必须一个字节都不改"


def test_conflict_that_races_past_the_precheck_also_exits_one(db, capsys, monkeypatch):
    """S3（P43）：预检漏掉、由 `record_decision` 撞上 `DecisionConflict` 的**竞态路径**，
    退出码也必须是 **1**（与预检分支同码）。

    预检（`decision_on`）就在正上方，所以这条路径几乎不可达 —— 但「同一件事两个退出码」
    正是本仓库反复要消掉的东西（ADR-019 的动机就是退出码只有一个真源）：**2** 在项目里
    是「你给的东西不合法」，而这个输入完全合法，只是撞了历史。

    复现方式：让第 1 次 `decision_on`（预检）返回 `None`（＝那时还没有这一行），
    第 2 次（`record_decision` 内部，ERROR_DIARY #25 的顺序）返回真实行 ——
    等价于「预检之后、落库之前，另一个写者把这一行写进去了」。
    """
    _init(db, capsys)
    code, _, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                       "--asof", "2026-09-16", "--spec", '{"etf_target_pct": 12}',
                       capsys=capsys)
    assert code == 0, err
    before = _rows(db)[0]

    real = agent_spec.decision_on
    seen = []

    def flaky(conn, arm, asof):
        seen.append((arm, asof))
        return None if len(seen) == 1 else real(conn, arm, asof)

    monkeypatch.setattr(agent_spec, "decision_on", flaky)
    code, out, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                         "--asof", "2026-09-16", "--spec", '{"etf_target_pct": 20}',
                         capsys=capsys)
    assert len(seen) == 2, "这条用例要的正是「预检过了、落库撞上」"
    assert code == 1, "竞态撞的也是「冲突」，不是「输入非法」"
    assert out == ""
    assert json.loads(err)["type"] == "DecisionConflict"
    assert _rows(db) == [before], "原行必须一个字节都不改"


def test_n_trials_and_rejected_are_part_of_the_idempotency_key(db, capsys):
    """同 spec 但试错次数不同 ⇒ 不是同一版（否则预算证据会被静默抹掉）。"""
    _init(db, capsys)
    argv = ("paper", "spec", "set", "--arm", ARM_AGENT, "--asof", "2026-09-16",
            "--spec", '{"etf_target_pct": 12}')
    assert run(db, *argv, capsys=capsys)[0] == 0
    code, _, err = run(db, *argv, "--n-trials", "2", capsys=capsys)
    assert code == 1
    assert json.loads(err)["attempted"]["etf_target_pct"] == 12.0
    assert len(_rows(db)) == 1


def test_set_accepts_the_random_counter_arm(db, capsys):
    _init(db, capsys)
    code, out, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT_RANDOM,
                         "--asof", "2026-09-16", "--spec", '{"etf_target_pct": 8}',
                         capsys=capsys)
    assert code == 0, err
    assert len(_rows(db, ARM_AGENT_RANDOM)) == 1
    assert _rows(db, ARM_AGENT) == [], "两臂的台账不许串台"


# ---------- set：可预期失败（exit 2，且一行都不写） ----------

@pytest.mark.parametrize("spec,why", [
    ('{"etf_target_pct": 30}', "高于上界"),
    ('{"etf_target_pct": 1}', "低于下界"),
    ('{"cash_floor_pct": 40}', "只许收紧"),
    ('{"kelly_fraction": 0.25}', "未知字段"),
    ('{"max_single_pct": "45"}', "类型不符"),
    ('{不是 json}', "不是合法 JSON"),
])
def test_bad_input_exits_two_without_writing_anything(db, capsys, spec, why):
    _init(db, capsys)
    code, out, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                         "--asof", "2026-09-16", "--spec", spec, capsys=capsys)
    assert code == 2, why
    assert out == ""
    assert json.loads(err)["type"] == "SpecViolation"
    assert _rows(db) == []


def test_arm_outside_the_spec_arms_is_rejected(db, capsys):
    _init(db, capsys)
    code, _, err = run(db, "paper", "spec", "set", "--arm", "arm-discipline-05",
                       "--asof", "2026-09-16", "--spec", '{"etf_target_pct": 12}',
                       capsys=capsys)
    assert code == 2
    assert "只" in json.loads(err)["error"]


def test_set_before_init_says_which_command_to_run(db, capsys):
    code, _, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                       "--asof", "2026-09-16", "--spec", '{"etf_target_pct": 12}',
                       capsys=capsys)
    assert code == 2
    assert "paper init" in json.loads(err)["error"]


def test_trial_budget_over_the_limit_is_rejected(db, capsys):
    _init(db, capsys)
    code, _, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                       "--asof", "2026-09-16", "--spec", '{"etf_target_pct": 12}',
                       "--n-trials", str(agent_spec.MAX_TRIALS_PER_REVIEW + 1),
                       capsys=capsys)
    assert code == 2
    assert json.loads(err)["type"] == "SpecViolation"
    assert _rows(db) == []


@pytest.mark.parametrize("bad", ["{不是数组}", '{"a": 1}'])
def test_rejected_must_be_a_json_array(db, capsys, bad):
    _init(db, capsys)
    code, _, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                       "--asof", "2026-09-16", "--spec", '{"etf_target_pct": 12}',
                       "--n-trials", "2", "--rejected", bad, capsys=capsys)
    assert code == 2
    assert _rows(db) == []


# ---------- spec 真的接进了执行内核 ----------

def test_step_after_a_spec_set_trades_on_the_agent_rule_book(db, capsys, tmp_path):
    """写一版 spec 之后 `paper step`：成交理由来自 `RULE_CITATIONS_AGENT`。"""
    _init(db, capsys)
    code, _, err = run(db, "paper", "spec", "set", "--arm", ARM_AGENT,
                       "--asof", "2026-09-15", "--spec", '{"etf_target_pct": 12}',
                       capsys=capsys)
    assert code == 0, err
    code, _, err = run(db, "paper", "step", "--asof", "2026-09-15",
                       "--out", str(tmp_path / "s.md"), capsys=capsys)
    assert code == 0, err

    c = connect(db)
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT account_id, qty, rule_citation, reason, binding_json"
            " FROM paper_trades WHERE account_id = ?", (ARM_AGENT,))]
        static = {dict(r)["rule_citation"] for r in c.execute(
            "SELECT rule_citation FROM paper_trades WHERE account_id = ?",
            ("arm-discipline-10",))}
    finally:
        c.close()
    assert rows, "arm-agent 起跑日应当建仓（默认 spec = arm-discipline-10 口径）"
    agent_book = set(RULE_CITATIONS_AGENT.values())
    for row in rows:
        assert row["rule_citation"] in agent_book
        # 溯源标签写进 reason：光有台账，读单笔成交的人还得自己 JOIN
        assert f"spec {agent_spec.spec_sha256({'etf_target_pct': 12})[:12]}" \
            in row["reason"]
    assert static and static.isdisjoint(agent_book), \
        "两条臂的条文表必须分得开，否则数不清几笔由 spec 触发"


def test_show_reports_the_ledger_counts(db, capsys):
    _init(db, capsys)
    run(db, "paper", "spec", "set", "--arm", ARM_AGENT, "--asof", "2026-09-16",
        "--spec", '{"etf_target_pct": 12}', "--n-trials", "2",
        "--rejected", '[{"field": "etf_target_pct", "value": 30, "reason": "越界"}]',
        capsys=capsys)
    code, out, err = run(db, "paper", "show", "--asof", "2026-09-16", capsys=capsys)
    assert code == 0, err
    # `show` 打两份 JSON：stdout = 完整载荷，stderr 末行 = 给终端看的摘要
    p = json.loads(err.strip().splitlines()[-1])
    assert p["agent_n_reviews"] == 1
    assert p["agent_n_trials_total"] == 2
    assert json.loads(out)["agent"]["n_reviews"] == 1


def test_report_lists_the_agent_arm_with_its_own_label(db, capsys, tmp_path):
    """报告「一、各臂净值」表里必须认得智能体臂（措辞与页面共用一份）。"""
    _init(db, capsys)
    out_path = tmp_path / "s.md"
    code, _, err = run(db, "paper", "step", "--asof", "2026-09-15",
                       "--out", str(out_path), capsys=capsys)
    assert code == 0, err
    md = out_path.read_text(encoding="utf-8")
    assert "arm-agent" in md and "arm-agent-random" in md
    assert "智能体动态编排（spec 台账）" in md
    assert "智能体随机改（阶段 3 才下单）" in md
    assert "口径未登记" not in md
