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
BAD_SCRIPT = "import os\ndef run(ctx): return {}\n"


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
