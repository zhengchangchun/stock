"""Task 15：主流程端到端（设计文档 §7 的 12 步）。"""

from datetime import date, timedelta

import pytest

from stocklab.candidate import run as candidate_run
from stocklab.cli.main import main
from stocklab.plugin import lifecycle, store
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-18T16:00:00+08:00"
ASOF = "2026-09-17"

PLUGINS = {
    "0": "def run(ctx):\n    return {'pass_flag': True, 'risk_note': []}\n",
    "1": "def run(ctx):\n    return {'score': 80.0, 'pass_flag': True,"
         " 'reason': '量价', 'risk_list': []}\n",
    "2": "def run(ctx):\n    return {'score': 60.0, 'pass_flag': True,"
         " 'reason': '景气', 'risk_list': []}\n",
    "3": "def run(ctx):\n    return {'score': 40.0, 'pass_flag': True,"
         " 'reason': '护城河', 'risk_list': []}\n",
    "4": "def run(ctx):\n    return {'final_score': ctx['raw_score'],"
         " 'risk_out': []}\n",
}


def _seed_db(tmp_db, *, n_days=300, n_instruments=3):
    """建一个够跑主流程的最小库：标的 + 日历 + 300 天 K 线 + 5 个 active 插桩。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    codes = ["000333", "600690", "600519"][:n_instruments]
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main','stock',?)",
        [(code, f"标的{code}", NOW) for code in codes])
    days = []
    cur = date(2025, 6, 1)
    while len(days) < n_days:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'tencent',?)",
                  [(d, NOW) for d in days])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, 10.0, 10.0, 10.0, 10.0, NOW)
         for code in codes for d in days])
    for pid, text in PLUGINS.items():
        sid = store.insert_script(c, plugin_id=pid, version="1.0.0",
                                  source_text=text, note=None, now=NOW)
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
    c.commit()
    return c


def test_run_produces_three_pools(tmp_db):
    c = _seed_db(tmp_db)
    result = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly",
                                         now=NOW)
    assert result.skipped is False
    pools = {m.pool for m in result.members}
    assert pools == {"short", "mid", "long"}
    c.close()


def test_run_is_idempotent(tmp_db):
    c = _seed_db(tmp_db)
    a = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    b = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    assert a.snapshot_id == b.snapshot_id
    assert b.skipped is True
    assert c.execute("SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0] == 1
    c.close()


def test_run_report_is_deterministic(tmp_db):
    c = _seed_db(tmp_db)
    a = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    b = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    assert a.report_md == b.report_md
    c.close()


def test_etf_only_in_short_pool(tmp_db):
    c = _seed_db(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('510300','沪深300ETF','sh','main','etf',?)",
              (NOW,))
    days = [r[0] for r in c.execute("SELECT date FROM trading_calendar"
                                    " ORDER BY date")]
    c.executemany("INSERT INTO bars_daily (code, date, open, high, low, close,"
                  " volume, adj_mode, source, fetched_at)"
                  " VALUES ('510300',?,4.5,4.5,4.5,4.5,1000,'none','x',?)",
                  [(d, NOW) for d in days])
    c.commit()
    result = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly",
                                         now=NOW)
    etf_pools = {m.pool for m in result.members if m.code == "510300"}
    assert etf_pools == {"short"}
    c.close()


def test_short_history_instrument_is_rejected(tmp_db):
    """只有 10 天历史的标的必须落进淘汰清单，且原因可读。"""
    c = _seed_db(tmp_db, n_days=300)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('000858','五粮液','sz','main','stock',?)",
              (NOW,))
    days = [r[0] for r in c.execute("SELECT date FROM trading_calendar"
                                    " ORDER BY date LIMIT 10")]
    c.executemany("INSERT INTO bars_daily (code, date, open, high, low, close,"
                  " volume, adj_mode, source, fetched_at)"
                  " VALUES ('000858',?,10,10,10,10,1000,'none','x',?)",
                  [(d, NOW) for d in days])
    c.commit()
    result = candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly",
                                         now=NOW)
    rejected = {r.code: r.reason for r in result.rejects}
    assert rejected.get("000858") == "insufficient_history"
    c.close()


def test_missing_plugin_aborts_run(tmp_db):
    """缺任一 active 插桩 → 明确报错，不产出半截快照。

    注意：不能用 DELETE 清掉插桩 —— 触发器会拦（append-only）。
    这里另建一个「有标的、有 K 线、但没有任何插桩」的库。
    """
    empty = tmp_db.parent / "no_plugin.db"
    init_db(empty)
    c = connect(empty)
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('000333','美的','sz','main','stock',?)",
              (NOW,))
    days = []
    cur = date(2025, 6, 1)
    while len(days) < 300:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    c.executemany("INSERT INTO bars_daily (code, date, open, high, low, close,"
                  " volume, adj_mode, source, fetched_at)"
                  " VALUES ('000333',?,10,10,10,10,1000,'none','x',?)",
                  [(d, NOW) for d in days])
    c.commit()

    with pytest.raises(lifecycle.NoActivePlugin):
        candidate_run.run_candidate(c, asof=ASOF, run_kind="weekly", now=NOW)
    assert c.execute(
        "SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0] == 0
    c.close()


def test_cli_candidate_run(tmp_db, capsys, tmp_path):
    c = _seed_db(tmp_db)
    c.close()
    out = tmp_path / "pool.md"
    code = main(["candidate", "run", "--asof", ASOF, "--run-kind", "weekly",
                 "--db", str(tmp_db), "--now", NOW, "--out", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "候选池报告" in text
    assert "## 短期池" in text
