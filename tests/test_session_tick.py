"""P11：`session tick` 的编排 —— 到期判据、幂等、失败留痕。

最重要的一条是**不误打未到期预测**：盘中拿半截 bar 给今天的预测打分，
会写下一行「预测错了」而它根本不成立。P7 的「asof/target 两根都必须在序列里」
拦不住这种情况 —— 今天那根 bar 真的在序列里，只是还没走完。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stocklab.data.models import Bar, Quote
from stocklab.predict.service import build_predictions
from stocklab.predict.store import insert_prediction
from stocklab.session.tick import (closed_through, is_trade_date_closed, run_tick)
from stocklab.store.db import connect
from tests.test_predict_service import seed

CODE = "000333"
START = "2026-05-02"
N = 124                     # 2026-05-02 .. 2026-09-02（建模需 ≥60 根历史）
TODAY = "2026-09-01"
NOW = f"{TODAY}T11:47:03+08:00"


def _bars(n=N, start=START):
    d0 = date.fromisoformat(start)
    return [Bar(code=CODE, date=(d0 + timedelta(days=i)).isoformat(),
                open=10.0 * (1 + 0.002 * i), high=10.0 * (1 + 0.002 * i) * 1.01,
                low=10.0 * (1 + 0.002 * i) * 0.99, close=10.0 * (1 + 0.002 * i),
                volume=1000, amount=None, turnover=None, source="test")
            for i in range(n)]


@pytest.fixture
def env(tmp_path):
    hist = _bars()
    conn = seed(tmp_path / "a.db", {CODE: hist})
    yield conn, tmp_path
    conn.close()


def _predict(conn, asof: str) -> str:
    """落一条 `asof` 的预测，返回它的 `target_date`。"""
    rep = build_predictions(conn, asof, [CODE])
    for p in rep["predictions"]:
        insert_prediction(conn, p, now=NOW)
    return rep["target_date"]


def _quote(ts: str, price=12.0) -> Quote:
    return Quote(code=CODE, name="美的集团", price=price, pre_close=11.9, open=11.9,
                 high=12.1, low=11.8, volume=1000, amount=1_000_000.0,
                 turnover=0.5, pe_ttm=10.0, float_mv=None, total_mv=None, pb=None,
                 ts=ts)


def _fetch(ts="20260901110000"):
    return lambda codes: [_quote(ts)]


def _verif_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]


# ---------- 纯函数：到期判据 ----------

def test_closed_through_skips_today_while_the_session_is_open(env):
    conn, _ = env
    from stocklab.session.tick import load_calendar

    cal, _err = load_calendar(conn)
    # 盘中（11:47）：今天不算已收盘 → 截到昨天
    assert closed_through(cal, f"{TODAY}T11:47:03+08:00") == "2026-08-31"
    # 收盘后（15:30）：今天算已收盘
    assert closed_through(cal, f"{TODAY}T15:30:00+08:00") == TODAY


def test_is_trade_date_closed():
    assert is_trade_date_closed("2026-08-31", NOW)
    assert not is_trade_date_closed(TODAY, NOW)
    assert is_trade_date_closed(TODAY, f"{TODAY}T15:00:00+08:00")


# ---------- 到期验证 ----------

def test_intraday_tick_does_not_score_todays_target(env):
    """**核心守卫**：`target_date == 今天` 的预测，盘中一根都不碰。"""
    conn, _ = env
    assert _predict(conn, "2026-08-30") == "2026-08-31"   # 已到期
    assert _predict(conn, "2026-08-31") == TODAY          # 今天到期，未收盘

    s = run_tick(conn, now=NOW, fetch=_fetch(), universe=())
    assert s["verify"]["cutoff"] == "2026-08-31"
    assert s["verify"]["verified_dates"] == ["2026-08-31"]
    assert s["verify"]["inserted"] == 1
    row = conn.execute("SELECT COUNT(*) FROM verifications WHERE target_date=?",
                       (TODAY,)).fetchone()[0]
    assert row == 0                                       # 今天那根 bar 还没走完


def test_after_close_tick_scores_today(env):
    conn, _ = env
    assert _predict(conn, "2026-08-31") == TODAY
    s = run_tick(conn, now=f"{TODAY}T15:30:00+08:00", fetch=_fetch("20260901150003"),
                 universe=())
    assert s["verify"]["cutoff"] == TODAY
    assert conn.execute("SELECT COUNT(*) FROM verifications WHERE target_date=?",
                        (TODAY,)).fetchone()[0] == 1


def test_verify_is_idempotent(env):
    conn, _ = env
    _predict(conn, "2026-08-30")
    run_tick(conn, now=NOW, fetch=_fetch(), universe=())
    n = _verif_count(conn)
    s2 = run_tick(conn, now=NOW, fetch=_fetch(), universe=())
    assert s2["verify"]["identical"] == 1 and s2["verify"]["inserted"] == 0
    assert _verif_count(conn) == n                        # 不重复打分


# ---------- 采集 ----------

def test_capture_and_idempotency(env):
    conn, _ = env
    from stocklab.config.universe import DEFAULT_UNIVERSE

    s1 = run_tick(conn, now=NOW, fetch=_fetch(), universe=DEFAULT_UNIVERSE)
    assert s1["collect"]["inserted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0] == 1

    # 同一时点（源站 ts 相同）再跑一次 → 一行不增
    s2 = run_tick(conn, now="2026-09-01T13:47:03+08:00", fetch=_fetch(),
                  universe=DEFAULT_UNIVERSE)
    assert s2["collect"]["identical"] == 1
    assert conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0] == 1


def test_intraday_capture_does_not_backfill(env):
    conn, _ = env
    from stocklab.config.universe import DEFAULT_UNIVERSE

    s = run_tick(conn, now=NOW, fetch=_fetch(), universe=DEFAULT_UNIVERSE)
    assert s["backfill"] == [{"trade_date": TODAY, "ran": False,
                              "reason": "session_open"}]


def test_fetch_failure_is_loud_and_writes_nothing(env):
    """断网/接口失败：显式报错 + `system_events` 留痕 + 退出码 1 + **零行写入**。"""
    conn, _ = env
    from stocklab.config.universe import DEFAULT_UNIVERSE

    def boom(codes):
        raise RuntimeError("connection refused")

    s = run_tick(conn, now=NOW, fetch=boom, universe=DEFAULT_UNIVERSE)
    assert s["ok"] is False and s["exit_code"] == 1
    assert "connection refused" in s["collect"]["error"]
    assert conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0] == 0
    ev = conn.execute("SELECT level, message FROM system_events WHERE module='session'"
                      " AND level='error'").fetchall()
    assert len(ev) == 1 and "采集失败" in ev[0]["message"]
    assert s["anomalies"][0]["kind"] == "collect_failed"


def test_partial_capture_is_reported_as_an_anomaly(env):
    """源站少回一个标的 ≠「今天只该有这一个」。"""
    conn, _ = env
    from stocklab.config.universe import DEFAULT_UNIVERSE

    s = run_tick(conn, now=NOW, fetch=_fetch(), universe=DEFAULT_UNIVERSE)
    assert s["collect"]["missing_codes"] == ["600690"]
    assert any(a["kind"] == "snapshot_missing_codes" for a in s["anomalies"])


def test_capture_can_be_skipped(env):
    conn, _ = env
    s = run_tick(conn, now=NOW, fetch=_fetch(), universe=(), capture=False)
    assert s["collect"] == {"skipped": "capture_disabled"}
    assert conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0] == 0


def test_tick_records_a_job_run(env):
    conn, _ = env
    run_tick(conn, now=NOW, fetch=_fetch(), universe=())
    row = conn.execute("SELECT job_name, status FROM job_runs"
                       " ORDER BY run_id DESC LIMIT 1").fetchone()
    assert row["job_name"] == "session_tick" and row["status"] == "ok"


def test_empty_calendar_is_not_a_hard_failure(env, tmp_path):
    """日历为空（未 `ingest index`）：采集照做（键幂等，猜错也不造假），
    验证显式跳过并说明原因。"""
    from stocklab.store.migrate import init_db

    db = tmp_path / "empty.db"
    init_db(db)
    conn = connect(db)
    try:
        s = run_tick(conn, now=NOW, fetch=_fetch(), universe=())
        assert s["calendar"]["covers_today"] is False
        assert s["calendar"]["error"]
        assert s["verify"]["skipped"] == "no_closed_session_in_calendar"
    finally:
        conn.close()
