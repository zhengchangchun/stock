"""Task 7：`plugin` CLI 端到端。"""

import pytest

from stocklab.cli.main import main
from stocklab.plugin import lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
SCORE_SCRIPT = (
    "def run(ctx):\n"
    "    return {'score': 50.0, 'pass_flag': True, 'reason': 'r',"
    " 'risk_list': []}\n"
)
# Uses whitelisted builtins: float(), len(), list() — must not NameError in sandbox
BUILTIN_SCRIPT = (
    "def run(ctx):\n"
    "    items = list(range(3))\n"
    "    score = float(len(items))\n"
    "    return {'score': score, 'pass_flag': True, 'reason': 'ok', 'risk_list': []}\n"
)
BAD_SCRIPT = "import os\ndef run(ctx): return {}\n"
# Passes guard (no forbidden imports) but violates contract: score out of [0,100]
CONTRACT_VIOLATING_SCRIPT = (
    "def run(ctx):\n"
    "    return {'score': 999, 'pass_flag': True, 'reason': 'r', 'risk_list': []}\n"
)


@pytest.fixture
def db(tmp_db):
    init_db(tmp_db)
    return tmp_db


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db)])
    out = capsys.readouterr()
    return code, out.out + out.err


def write_script(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_submit_valid_script_reaches_pending_review(db, tmp_path, capsys):
    f = write_script(tmp_path, "p3.py", SCORE_SCRIPT)
    code, _ = run(db, "plugin", "submit", str(f), "--plugin-id", "3",
                  "--version", "1.0.0", "--actor", "tester",
                  "--now", NOW, capsys=capsys)
    assert code == 0
    c = connect(db)
    rows = store.list_scripts(c, plugin_id="3")
    assert len(rows) == 1
    assert lifecycle.script_state(c, rows[0]["script_id"]) == "pending_review"
    c.close()


def test_submit_dangerous_script_is_rejected_without_writing(db, tmp_path, capsys):
    f = write_script(tmp_path, "bad.py", BAD_SCRIPT)
    code, out = run(db, "plugin", "submit", str(f), "--plugin-id", "3",
                    "--version", "1.0.0", "--actor", "tester",
                    "--now", NOW, capsys=capsys)
    assert code != 0
    assert "禁止" in out
    c = connect(db)
    assert store.list_scripts(c, plugin_id="3") == []   # 一个字都没写库
    c.close()


def test_approve_then_list_shows_active(db, tmp_path, capsys):
    f = write_script(tmp_path, "p3.py", SCORE_SCRIPT)
    run(db, "plugin", "submit", str(f), "--plugin-id", "3", "--version",
        "1.0.0", "--actor", "tester", "--now", NOW, capsys=capsys)
    c = connect(db)
    sid = store.list_scripts(c, plugin_id="3")[0]["script_id"]
    c.close()

    code, _ = run(db, "plugin", "approve", str(sid), "--actor", "claude",
                  "--reason", "看过沙盒报告", "--now", NOW, capsys=capsys)
    assert code == 0

    code, out = run(db, "plugin", "list", "--plugin-id", "3", capsys=capsys)
    assert code == 0
    assert "active" in out
    assert "1.0.0" in out


def test_reject_marks_rejected(db, tmp_path, capsys):
    f = write_script(tmp_path, "p3.py", SCORE_SCRIPT)
    run(db, "plugin", "submit", str(f), "--plugin-id", "3", "--version",
        "1.0.0", "--actor", "tester", "--now", NOW, capsys=capsys)
    c = connect(db)
    sid = store.list_scripts(c, plugin_id="3")[0]["script_id"]
    c.close()
    code, _ = run(db, "plugin", "reject", str(sid), "--actor", "claude",
                  "--reason", "逻辑有问题", "--now", NOW, capsys=capsys)
    assert code == 0
    c = connect(db)
    assert lifecycle.script_state(c, sid) == "rejected"
    c.close()


def test_approve_without_reason_fails(db, tmp_path, capsys):
    f = write_script(tmp_path, "p3.py", SCORE_SCRIPT)
    run(db, "plugin", "submit", str(f), "--plugin-id", "3", "--version",
        "1.0.0", "--actor", "tester", "--now", NOW, capsys=capsys)
    c = connect(db)
    sid = store.list_scripts(c, plugin_id="3")[0]["script_id"]
    c.close()
    code, _ = run(db, "plugin", "approve", str(sid), "--actor", "claude",
                  "--reason", "", "--now", NOW, capsys=capsys)
    assert code != 0


def test_duplicate_version_fails_cleanly(db, tmp_path, capsys):
    f = write_script(tmp_path, "p3.py", SCORE_SCRIPT)
    for _ in range(2):
        code, _ = run(db, "plugin", "submit", str(f), "--plugin-id", "3",
                      "--version", "1.0.0", "--actor", "tester",
                      "--now", NOW, capsys=capsys)
    assert code != 0


def test_submit_script_using_whitelisted_builtins_reaches_pending_review(
        db, tmp_path, capsys):
    """回归：占位沙盒必须用 runtime.load_script 的白名单命名空间，
    而不是 {__builtins__: {}}。若错用空 __builtins__，float/len/list 等
    白名单内建函数会 NameError，合法脚本被误判为沙盒失败。"""
    f = write_script(tmp_path, "builtin.py", BUILTIN_SCRIPT)
    code, out = run(db, "plugin", "submit", str(f), "--plugin-id", "3",
                    "--version", "1.0.0", "--actor", "tester",
                    "--now", NOW, capsys=capsys)
    assert code == 0, f"submit failed (code={code}): {out}"
    c = connect(db)
    rows = store.list_scripts(c, plugin_id="3")
    assert len(rows) == 1
    assert lifecycle.script_state(c, rows[0]["script_id"]) == "pending_review"
    c.close()


# ---------- Task 17：真沙盒接线 ----------


def test_submit_writes_backtest_row(db, tmp_path, capsys):
    f = write_script(tmp_path, "p3.py", SCORE_SCRIPT)
    run(db, "plugin", "submit", str(f), "--plugin-id", "3", "--version",
        "1.0.0", "--actor", "tester", "--now", NOW, capsys=capsys)
    c = connect(db)
    rows = store.load_backtests(c)
    c.close()
    assert len(rows) == 1
    assert rows[0]["pool"] == "short"
    assert rows[0]["verdict"] == "INCONCLUSIVE"
    assert rows[0]["baseline_script_id"] is None


def test_inconclusive_still_allows_pending_review(db, tmp_path, capsys):
    """样本不足不是脚本的错 —— 不该阻止它进待审队列。"""
    f = write_script(tmp_path, "p3.py", SCORE_SCRIPT)
    code, _ = run(db, "plugin", "submit", str(f), "--plugin-id", "3",
                  "--version", "1.0.0", "--actor", "tester", "--now", NOW,
                  capsys=capsys)
    assert code == 0
    c = connect(db)
    sid = store.list_scripts(c, plugin_id="3")[0]["script_id"]
    assert lifecycle.script_state(c, sid) == "pending_review"
    c.close()


def test_second_version_records_baseline(db, tmp_path, capsys):
    """第二版必须带上 baseline_script_id = 当前 active。"""
    f1 = write_script(tmp_path, "v1.py", SCORE_SCRIPT)
    run(db, "plugin", "submit", str(f1), "--plugin-id", "3", "--version",
        "1.0.0", "--actor", "tester", "--now", NOW, capsys=capsys)
    c = connect(db)
    first = store.list_scripts(c, plugin_id="3")[0]["script_id"]
    c.close()
    run(db, "plugin", "approve", str(first), "--actor", "claude",
        "--reason", "ok", "--now", NOW, capsys=capsys)

    f2 = write_script(tmp_path, "v2.py", SCORE_SCRIPT.replace("50.0", "60.0"))
    run(db, "plugin", "submit", str(f2), "--plugin-id", "3", "--version",
        "1.1.0", "--actor", "tester", "--now", NOW, capsys=capsys)
    c = connect(db)
    rows = store.load_backtests(c)
    c.close()
    assert len(rows) == 2
    assert rows[1]["baseline_script_id"] == first


# ---------- Finding A regression test ----------


def test_contract_violating_script_is_rejected_by_submit(db, tmp_path, capsys):
    """回归（Finding A）：通过 guard（无危险 import）但违反契约（score > 100）的脚本
    必须在契约预检阶段被拒绝，且不能进入 pending_review。

    在修复前，`_run_sandbox` 跳过了 runtime.load_script，该脚本会被
    `sandbox.run_sandbox` 接受（沙盒是骨架，不执行脚本），错误地进入 pending_review。
    """
    f = write_script(tmp_path, "bad_contract.py", CONTRACT_VIOLATING_SCRIPT)
    code, out = run(db, "plugin", "submit", str(f), "--plugin-id", "3",
                    "--version", "1.0.0", "--actor", "tester",
                    "--now", NOW, capsys=capsys)
    assert code != 0, f"submit 应失败但返回 0；输出：{out}"
    c = connect(db)
    rows = store.list_scripts(c, plugin_id="3")
    # The script row is written before the probe runs, but state must be rejected
    if rows:
        state = lifecycle.script_state(c, rows[0]["script_id"])
        assert state != "pending_review", (
            f"契约违规脚本不应进入 pending_review，实际状态={state}")
    c.close()


# ---------- Fix round 2 regression: scripts that read ctx keys must not be rejected ----------


# A script that reads documented ctx keys unconditionally.
# Before the fix, fn({}) raised KeyError("bars"), which the probe's except
# caught and turned into passed=False → submit rejected.
CTX_READING_SCRIPT = (
    "def run(ctx):\n"
    "    bars = ctx['bars']\n"
    "    name = ctx['name']\n"
    "    score = 50.0 + len(bars) * 0.0\n"
    "    return {'score': score, 'pass_flag': True, 'reason': name, 'risk_list': []}\n"
)


def test_ctx_reading_script_reaches_pending_review(db, tmp_path, capsys):
    """回归（Fix round 2）：正常读取 ctx 文档化字段（bars/name 等）的脚本
    必须通过提交并进入 pending_review，不应因探针 ctx 为空 dict 而被误拒。

    修复前：`fn({})` → `KeyError: 'bars'` → `passed=False` → 脚本被驳回。
    修复后：探针使用 `PROBE_CTX`（契约声明的完整空形状），脚本正常运行。
    """
    f = write_script(tmp_path, "ctx_reader.py", CTX_READING_SCRIPT)
    code, out = run(db, "plugin", "submit", str(f), "--plugin-id", "3",
                    "--version", "1.0.0", "--actor", "tester",
                    "--now", NOW, capsys=capsys)
    assert code == 0, f"submit 应成功但返回 {code}；输出：{out}"
    c = connect(db)
    rows = store.list_scripts(c, plugin_id="3")
    assert len(rows) == 1
    state = lifecycle.script_state(c, rows[0]["script_id"])
    assert state == "pending_review", f"期望 pending_review，实际={state}；输出：{out}"
    c.close()



def test_submit_backtest_window_end_matches_now(db, tmp_path, capsys):
    """Finding C 修复：`window_end` 必须等于 `--now` 的日期部分，而不是系统时钟。"""
    f = write_script(tmp_path, "p3.py", SCORE_SCRIPT)
    run(db, "plugin", "submit", str(f), "--plugin-id", "3", "--version",
        "1.0.0", "--actor", "tester", "--now", NOW, capsys=capsys)
    c = connect(db)
    rows = store.load_backtests(c)
    c.close()
    assert len(rows) == 1
    assert rows[0]["window_end"] == "2026-09-18", (
        f"window_end 应为 2026-09-18，实际为 {rows[0]['window_end']!r}"
    )


# ---------- Final review fix wave regression tests ----------


# M2: A plugin-4-shaped script that reads ctx["raw_score"] by subscript.
# Before the fix, PROBE_CTX lacked "raw_score", so fn(PROBE_CTX) raised
# KeyError and the script was rejected even though it runs fine in production.
PLUGIN4_SUBSCRIPT_SCRIPT = (
    "def run(ctx):\n"
    "    score = ctx['raw_score'] * 0.9\n"
    "    return {'final_score': score, 'risk_out': []}\n"
)


def test_plugin4_subscript_raw_score_reaches_pending_review(db, tmp_path, capsys):
    """回归（M2）：插桩4 形脚本通过 subscript 读 ctx['raw_score'] 必须通过提交。

    修复前：PROBE_CTX 缺少 raw_score → fn(PROBE_CTX) KeyError → 误拒。
    修复后：PROBE_CTX 包含 raw_score=0.0 → 探针正常运行 → pending_review。
    """
    f = write_script(tmp_path, "p4_subscript.py", PLUGIN4_SUBSCRIPT_SCRIPT)
    code, out = run(db, "plugin", "submit", str(f), "--plugin-id", "4",
                    "--version", "1.0.0", "--actor", "tester",
                    "--now", NOW, capsys=capsys)
    assert code == 0, f"submit 应成功但返回 {code}；输出：{out}"
    c = connect(db)
    rows = store.list_scripts(c, plugin_id="4")
    assert len(rows) == 1
    state = lifecycle.script_state(c, rows[0]["script_id"])
    assert state == "pending_review", f"期望 pending_review，实际={state}；输出：{out}"
    c.close()


# M4: --actor "" must be rejected before any DB write.
def test_submit_empty_actor_is_rejected_before_db_write(db, tmp_path, capsys):
    """回归（M4）：--actor "" 必须在落库前被拒绝，且不留孤立 draft 行。

    修复前：insert_script 成功写入 draft 行，record_submit 随后 ValueError，
    遗留一个不可达的 draft 行污染 UNIQUE(plugin_id, version) 约束。
    修复后：在任何 DB 写之前校验 actor，返回非零退出码，库中无任何行。
    """
    f = write_script(tmp_path, "p3_empty_actor.py", SCORE_SCRIPT)
    code, out = run(db, "plugin", "submit", str(f), "--plugin-id", "3",
                    "--version", "1.0.0", "--actor", "",
                    "--now", NOW, capsys=capsys)
    assert code != 0, f"空 actor 应返回非零退出码，实际={code}；输出：{out}"
    c = connect(db)
    rows = store.list_scripts(c, plugin_id="3")
    assert rows == [], f"库中不应有任何行，实际={rows}"
    c.close()


# ---------- Task 8：plugin sandbox 子命令 ----------


def test_sandbox_cli_runs_and_reports(db, capsys):
    code, out = run(db, "plugin", "sandbox", "1", "--pool", "short",
                    "--window-start", "2015-01-01",
                    "--window-end", "2026-09-18", "--now", NOW, capsys=capsys)
    # 库里没有行情 → 回放得到空序列 → INCONCLUSIVE，但命令本身成功
    assert code == 0
    assert "INCONCLUSIVE" in out


def test_sandbox_cli_unknown_pool_fails(db, capsys):
    code, out = run(db, "plugin", "sandbox", "1", "--pool", "nope",
                    "--now", NOW, capsys=capsys)
    assert code != 0


def test_sandbox_cli_first_version_has_no_baseline(db, capsys):
    code, out = run(db, "plugin", "sandbox", "1", "--pool", "short",
                    "--now", NOW, capsys=capsys)
    assert code == 0
    assert "baseline" in out.lower() or "INCONCLUSIVE" in out

