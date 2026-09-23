"""P52 的 CLI 面：`paper agent decide|random|show` 与 `fund ingest|show`。

退出码的语义与 `paper` 其余子命令一致（`_paper_fail` 的约定）：

- **2** = 你给的东西不合法（载荷越界 / 臂不存在 / 文件读不到 / 源文本格式变了）；
- **1** = 你给的合法，但与库里已有的那一行**撞了键** —— append-only，不改写历史；
- **0** = 写入或「已存在且一致（未写入）」。

`fund ingest --file` 走的是本地文本，**不联网**：解析与落库路径与 `--fetch` 完全
一致（同一个 `parse_pingzhongdata`），所以离线验证过的就是线上要跑的那条。
"""

from __future__ import annotations

import json

import pytest

from stocklab.cli.main import main
from stocklab.fund import nav as fund_nav
from stocklab.paper import agent_decide
from stocklab.paper.config import ARM_AGENT, ARM_AGENT_RANDOM
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
START = "2026-09-15"
CAL = ("2026-09-14", START, "2026-09-16")
BARS = {
    "000333": {"2026-09-14": 86.80, "2026-09-15": 87.23},
    "510300": {"2026-09-15": 4.523},
    "510880": {"2026-09-15": 3.382},
    "sh000300": {"2026-09-15": 4450.04},
}
FAKE_JS = ('var Data_netWorthTrend = [{"x":1757865600000,"y":1.0},'
           '{"x":1757952000000,"y":1.02}];')


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cli-agent.db"
    init_db(path)
    c = connect(path)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sz','main',?,?)",
                  [(code, code, "stock" if code == "000333" else "etf", NOW)
                   for code in ("000333", "510300", "510880")])
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
    for code in ("000333", "510300", "510880"):
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


#: 预注册账户（P56 / D-48）：`decide` 只许写在**声明了自己是哪一版**的账户上，
#: 所以夹具统一 enroll 一个版本账户来写决策。内置 `arm-agent` 不在这条路上
#: （enroll 明令拒收它）—— 见 `test_decide_refuses_a_ledger_without_preregistration`。
TEST_ARM = "arm-agent-t1"
TEST_MODEL = "claude-code"
TEST_PROMPT = "a" * 64


def _enroll(db, capsys, name=TEST_ARM, model_id=TEST_MODEL, prompt=TEST_PROMPT):
    code, out, err = run(db, "paper", "agent", "enroll", "--arm-name", name,
                         "--model-id", model_id, "--prompt-sha256", prompt,
                         capsys=capsys)
    assert code == 0, err
    return name


def _context_sha(db, capsys, arm, asof):
    """按**生成器的正规流程**取 PIT 上下文指纹（D-49：唯一输入源）。"""
    code, out, err = run(db, "paper", "agent", "context", "--asof", asof,
                         "--arm", arm, capsys=capsys)
    assert code == 0, err
    return json.loads(out)["context_sha256"]


def _with_context(db, capsys, payload, *, arm=TEST_ARM, sha=None):
    return {**payload, "context_sha256": sha or _context_sha(
        db, capsys, arm, payload["asof"])}


def _decide_args(path, arm=TEST_ARM):
    return ("paper", "agent", "decide", "--asof", START, "--file", str(path),
            "--arm", arm, "--model-id", TEST_MODEL, "--prompt-sha256", TEST_PROMPT)


def _write(tmp_path, payload, name="d.json"):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ---------- 写入口 ----------

def test_decide_writes_one_row_and_prints_its_fingerprints(db, tmp_path, capsys):
    """写入成功 → exit 0，stdout 带 payload/context 两个指纹（可复现的凭据）。"""
    _init(db, capsys)
    arm = _enroll(db, capsys)
    payload = {"asof": START, "cash_pct": 90.0, "rationale": "建 10% 的 510300",
               "decisions": [{"code": "510300", "side": "buy",
                              "target_weight_pct": 10.0, "reason": "分散"}]}
    path = _write(tmp_path, _with_context(db, capsys, payload, arm=arm))
    code, out, err = run(db, *_decide_args(path, arm=arm), capsys=capsys)
    assert code == 0, err
    got = json.loads(out)
    assert got["status"] == "已写入" and got["n_decisions"] == 1
    assert len(got["payload_sha256"]) == 64 and len(got["context_sha256"]) == 64
    assert got["pool_codes"] == 3
    assert got["note"].startswith("写入口只校验"), "载荷要自己说清「写入口不做执行」"


def test_decide_refuses_an_arm_that_is_not_an_agent_arm(db, tmp_path, capsys):
    """别的臂上没有人在读决策文件 —— 写下去只会产出一张假台账。"""
    _init(db, capsys)
    payload = {"asof": START, "cash_pct": 100.0, "rationale": "x", "decisions": []}
    code, _, err = run(db, *_decide_args(_write(tmp_path, payload), arm="arm-hold"),
                       capsys=capsys)
    assert code == 2
    assert "paper agent" in json.loads(err.strip().splitlines()[-1])["error"]


def test_decide_reports_an_unreadable_file_instead_of_crashing(db, capsys):
    """文件读不到 → exit 2 + 说清是哪一步（不是 traceback）。"""
    _init(db, capsys)
    code, _, err = run(db, "paper", "agent", "decide", "--asof", START,
                       "--file", "/nonexistent/d.json", "--arm", ARM_AGENT,
                       "--model-id", "m", "--prompt-sha256", "p", capsys=capsys)
    assert code == 2
    assert "读不到决策文件" in json.loads(err.strip().splitlines()[-1])["error"]


def test_decide_requires_model_and_prompt_fingerprints(db, tmp_path, capsys):
    """`--model-id` / `--prompt-sha256` 必填：换模型/换提示词 = 换口径，必须留痕。

    缺了它们这一版决策就再也说不清「谁产出的」，而台账是 append-only 的 ——
    事后补不上。所以宁可让 argparse 直接把命令拦住。
    """
    _init(db, capsys)
    payload = {"asof": START, "cash_pct": 100.0, "rationale": "x", "decisions": []}
    path = _write(tmp_path, payload)
    # 只缺一个也要拦：两者是同一版决策的两半，缺任何一半都说不清「谁产出的」。
    # `main` 把 argparse 的 `SystemExit` 收成了退出码（全仓统一），所以这里断言退出码。
    for missing, flag in ((("--model-id", "m"), "--prompt-sha256"),
                          (("--prompt-sha256", "p"), "--model-id")):
        argv = ["paper", "agent", "decide", "--asof", START, "--file", str(path),
                *missing, "--db", str(db), "--now", NOW]
        assert main(argv) == 2
        _, err = capsys.readouterr()
        assert flag in err, err


def test_random_then_step_then_show_is_a_full_round_trip(db, tmp_path, capsys):
    """`random` → `agent run` → `show`：串起来能跑通，且 `show` 与库同源。

    P56 / D-50：中间那一步从 `paper step` 换成 `paper agent run` —— 随机臂也在
    `agent_decision` 家族里，`paper step` 已经让出它（否则决策永不执行）。
    """
    _init(db, capsys)
    code, out, err = run(db, "paper", "agent", "random", "--asof", START,
                         "--arm", ARM_AGENT_RANDOM, "--seed", "3", capsys=capsys)
    assert code == 0, err
    assert json.loads(out)["n_decisions"] >= 1
    code, out, err = run(db, "paper", "agent", "run", "--asof", START, capsys=capsys)
    # 退出码是**整条家族**的结论：随机臂有决策、内置 `arm-agent` 没有
    # ⇒ 那天仍有一条「该有的决策」缺席 ⇒ 1（异常），回执里逐账户点名。
    assert code == 1, err
    by_id = {a["account_id"]: a for a in json.loads(out)["accounts"]}
    assert by_id[ARM_AGENT_RANDOM]["decision_present"] is True
    assert by_id[ARM_AGENT_RANDOM]["status"] == "ran"
    assert [m["account_id"] for m in json.loads(out)["anomaly"]["missing_decision"]] \
        == [ARM_AGENT]
    code, out, err = run(db, "paper", "agent", "show", "--asof", START,
                         "--arm", ARM_AGENT_RANDOM, capsys=capsys)
    assert code == 0, err
    got = json.loads(out)
    assert got["n_decisions"] == 1 and got["decision_on_asof"] is not None
    assert got["account_exists"] is True
    codes = got["pool_on_asof"]["codes"]
    assert codes == sorted(codes), "池子要稳定排序（可重放）"
    assert set(codes) == {"000333", "510300", "510880"}
    assert got["guardrails"], "护栏清单必须随 `show` 一起给出来"


def test_show_reports_the_audit_of_the_ledger(db, tmp_path, capsys):
    """`paper agent show` 之后，审计判据 `audit_decisions` 对得上成交。"""
    _init(db, capsys)
    run(db, "paper", "agent", "random", "--asof", START, "--arm", ARM_AGENT_RANDOM,
        "--seed", "1", capsys=capsys)
    code, _, err = run(db, "paper", "agent", "run", "--asof", START, capsys=capsys)
    assert code == 1, ("随机臂有决策、内置 arm-agent 没有 ⇒ 家族整体报缺决策：" + err)
    c = connect(db)
    try:
        audit = agent_decide.audit_decisions(
            c, arms=(ARM_AGENT, ARM_AGENT_RANDOM))
    finally:
        c.close()
    assert audit["ok"] is True, audit["violations"]
    assert audit["checked"] >= 1, "夹具里随机臂应当至少成交过一次"


# ---------- 基金净值 ----------

def test_fund_ingest_from_a_local_file_then_show(db, tmp_path, capsys):
    """`fund ingest --file`（离线）→ 落库 → `fund show` 报区间与缺失清单。"""
    js = tmp_path / "110011.js"
    js.write_text(FAKE_JS, encoding="utf-8")
    code, out, err = run(db, "fund", "ingest", "--code", "110011",
                         "--file", str(js), capsys=capsys)
    assert code == 0, err
    got = json.loads(out)
    assert got["parsed"] == 2 and got["written"] == 2
    assert got["source"] == fund_nav.SOURCE_EASTMONEY_JS

    code, out, err = run(db, "fund", "show", "--asof", START, capsys=capsys)
    assert code == 0, err
    shown = json.loads(out)
    assert shown["table_present"] is True
    assert "110011" in shown["codes_in_db"]
    assert "110011" not in shown["codes_missing"]
    assert len(shown["pool"]) == len(fund_nav.EQUAL_WEIGHT_POOL)
    assert "近似" in shown["approx_note"] and "非官方" in shown["approx_note"]


def test_fund_ingest_rejects_a_bad_source_file(db, tmp_path, capsys):
    """源文本格式变了 → exit 2 + 报错，**不写任何行**（不是静默写 0 行）。"""
    bad = tmp_path / "bad.js"
    bad.write_text("var nothing = 1;", encoding="utf-8")
    code, _, err = run(db, "fund", "ingest", "--code", "110011", "--file", str(bad),
                       capsys=capsys)
    assert code == 2
    assert "Data_netWorthTrend" in json.loads(err.strip().splitlines()[-1])["error"]
    c = connect(db)
    try:
        assert c.execute("SELECT COUNT(*) FROM fund_nav_daily").fetchone()[0] == 0
    finally:
        c.close()


def test_fund_ingest_is_idempotent_on_the_second_run(db, tmp_path, capsys):
    """同一份文件再跑一次 → 0 写入（只增，不重复）。"""
    js = tmp_path / "110011.js"
    js.write_text(FAKE_JS, encoding="utf-8")
    run(db, "fund", "ingest", "--code", "110011", "--file", str(js), capsys=capsys)
    code, out, err = run(db, "fund", "ingest", "--code", "110011",
                         "--file", str(js), capsys=capsys)
    assert code == 0, err
    assert json.loads(out)["written"] == 0
