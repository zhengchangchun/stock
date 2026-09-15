"""P11：CLI 层 —— `session tick` / `session backfill-close` / `review daily`。

给 cron 用的命令有两条硬要求，都在这里钉住：
**stdout 是一段可解析的 JSON**、**退出码能区分「做完了」与「出事了」**。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from stocklab.cli.main import main
from stocklab.data.models import Bar
from stocklab.predict.service import build_predictions
from stocklab.predict.store import insert_prediction
from stocklab.store.db import connect
from tests.test_predict_service import seed

CODE = "000333"
START = "2026-05-02"
TODAY = "2026-09-01"
NOW = f"{TODAY}T11:47:03+08:00"


def _bars(n=124):
    d0 = date.fromisoformat(START)
    return [Bar(code=CODE, date=(d0 + timedelta(days=i)).isoformat(),
                open=10.0 * (1 + 0.002 * i), high=10.2 * (1 + 0.002 * i),
                low=9.8 * (1 + 0.002 * i), close=10.0 * (1 + 0.002 * i),
                volume=1000, amount=None, turnover=None, source="test")
            for i in range(n)]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "a.db"
    conn = seed(path, {CODE: _bars()})
    rep = build_predictions(conn, "2026-08-30", [CODE])
    for p in rep["predictions"]:
        insert_prediction(conn, p, now=NOW)
    conn.close()
    return path


def _run(capsys, argv):
    rc = main(argv)
    return rc, capsys.readouterr()


def test_tick_prints_json_and_verifies(capsys, db):
    rc, out = _run(capsys, ["session", "tick", "--db", str(db), "--now", NOW,
                            "--no-capture"])
    assert rc == 0
    payload = json.loads(out.out)                     # stdout 必须是**纯 JSON**
    assert payload["job"] == "session_tick"
    assert payload["verify"]["cutoff"] == "2026-08-31"
    assert payload["verify"]["inserted"] == 1
    assert payload["collect"] == {"skipped": "capture_disabled"}
    assert payload["rolling"]["replay"] is not None
    assert payload["exit_code"] == 0


def test_tick_backfill_close_fills_the_day(capsys, db, tmp_path):
    """补一条当日 bar + 一条当日快照，再手动补跑回填 —— 端到端写进 bars_daily。"""
    conn = connect(db)
    conn.execute(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " amount, turnover, adj_mode, is_suspended, source, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,NULL,NULL,'none',0,'test',?)",
        (CODE, TODAY, 12.0, 12.0, 12.0, 12.0, 1000, NOW))
    conn.execute(
        "INSERT INTO quote_snapshots (code, trade_date, ts, price, pre_close,"
        " open, high, low, volume, amount, turnover, source, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (CODE, TODAY, "20260901150003", 12.0, 11.9, 11.9, 12.1, 11.8, 1000,
         1_234_567.0, 0.42, "tencent", NOW))
    conn.commit()
    conn.close()

    rc, out = _run(capsys, ["session", "backfill-close", "--date", TODAY,
                            "--db", str(db), "--now", f"{TODAY}T15:31:00+08:00"])
    assert rc == 0
    payload = json.loads(out.out)
    assert payload["updated"] == 1 and payload["filled"][0]["amount"] == 1_234_567.0

    conn = connect(db)
    row = conn.execute("SELECT amount, turnover FROM bars_daily WHERE code=? AND date=?",
                       (CODE, TODAY)).fetchone()
    other = conn.execute("SELECT COUNT(amount) FROM bars_daily WHERE date<>?",
                         (TODAY,)).fetchone()[0]
    conn.close()
    assert (row["amount"], row["turnover"]) == (1_234_567.0, 0.42)
    assert other == 0                                 # 别的日期一行没被填


def test_backfill_close_exits_nonzero_when_the_bar_is_missing(capsys, db):
    rc, out = _run(capsys, ["session", "backfill-close", "--date", TODAY,
                            "--db", str(db)])
    assert rc == 1                                    # 「有事要你看」而不是静默 OK
    assert json.loads(out.out)["skipped_missing_bar"] == [CODE]
    assert "ingest bars" in out.err


def test_review_daily_writes_report_and_labels_provenance(capsys, db, tmp_path):
    out_md = tmp_path / "r" / f"{TODAY}-review.md"
    rc, out = _run(capsys, ["review", "daily", "--date", TODAY, "--db", str(db),
                            "--out", str(out_md)])
    assert rc == 0
    payload = json.loads(out.out)
    assert payload["provenance"]["live_rows"] == 0     # 全是回放
    assert payload["live"] is None
    assert payload["disclosure"]["is_live_performance"] is False
    assert out_md.exists() and out_md.with_suffix(".json").exists()
    md = out_md.read_text(encoding="utf-8")
    assert "REPLAY（PIT 历史回放）" in md
    assert "LIVE（实盘累计）：窗口内 0 行" in md
    assert "不得" in md and "实盘表现" in md


def test_review_daily_is_byte_reproducible(capsys, db, tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    assert main(["review", "daily", "--date", TODAY, "--db", str(db),
                 "--out", str(a)]) == 0
    assert main(["review", "daily", "--date", TODAY, "--db", str(db),
                 "--out", str(b)]) == 0
    assert a.read_bytes() == b.read_bytes()
    assert a.with_suffix(".json").read_bytes() == b.with_suffix(".json").read_bytes()


def test_tick_refuses_a_database_without_the_new_table(capsys, tmp_path):
    """schema 未前滚 → 明确告诉用户跑 `db init`，不是抛栈。"""
    from stocklab.store.migrate import init_db

    path = tmp_path / "old.db"
    init_db(path)
    conn = connect(path)
    conn.execute("DROP TABLE quote_snapshots")
    conn.commit()
    conn.close()

    rc, out = _run(capsys, ["session", "tick", "--db", str(path), "--no-capture"])
    assert rc == 2
    assert "db init" in out.err


def test_tick_reports_a_missing_database(capsys, tmp_path):
    rc, out = _run(capsys, ["session", "tick", "--db", str(tmp_path / "nope.db")])
    assert rc == 2 and "db not found" in out.err
