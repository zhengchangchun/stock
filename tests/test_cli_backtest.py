"""`adj rebuild` / `backtest run` 子命令：离线、只读库、复权价 + 成本 + 基准。"""

import json

import pytest

from stocklab.cli.main import (build_parser, cmd_adj_rebuild, cmd_backtest_run,
                               cmd_ingest_index)
from stocklab.config import paths
from stocklab.config.universe import DEFAULT_UNIVERSE
from stocklab.data.models import Bar, CorpAction
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T10:00:00+08:00"
DATES = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]
CLOSES = [10.0, 10.2, 10.1, 10.5, 10.8]


def _bars(code="000333", dates=None, closes=None):
    dates = dates or DATES
    closes = closes or CLOSES
    return [Bar(code=code, date=d, open=c, high=c + 0.1, low=c - 0.1, close=c,
                volume=10_000, amount=c * 10_000, turnover=1.0, source="test")
            for d, c in zip(dates, closes)]


def _seed(tmp_db, *, with_calendar=True):
    init_db(tmp_db)
    conn = connect(tmp_db)
    repo.upsert_instruments(conn, DEFAULT_UNIVERSE, now=NOW)
    repo.insert_bars(conn, _bars("000333"), now=NOW)
    if with_calendar:
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO trading_calendar (date, is_open, source,"
                " created_at) VALUES (?,1,'test',?)",
                [(d, NOW) for d in DATES])
    conn.close()


def _args(**kw):
    class A:
        pass

    a = A()
    for k, v in kw.items():
        setattr(a, k, v)
    return a


# ---------- 参数解析 ----------

def test_adj_rebuild_subcommand():
    args = build_parser().parse_args(["adj", "rebuild"])
    assert args.adj_action == "rebuild"
    assert args.func is cmd_adj_rebuild


def test_backtest_run_subcommand_defaults():
    args = build_parser().parse_args(["backtest", "run"])
    assert args.cash == 100_000.0
    assert args.benchmark == "index_300"


def test_ingest_index_subcommand_defaults():
    args = build_parser().parse_args(["ingest", "index"])
    assert args.symbol == "sh000300"
    assert args.func is cmd_ingest_index


# ---------- adj rebuild ----------

def test_adj_rebuild_writes_factors_offline(tmp_db, monkeypatch, capsys):
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed(tmp_db)
    assert cmd_adj_rebuild(_args(code=["000333"], source="test")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["codes"]["000333"]["factor_rows"] == len(DATES)
    conn = connect(tmp_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM adj_factors").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM adj_factor_blackout").fetchone()[0] == 0
    finally:
        conn.close()


def test_adj_rebuild_records_blackout_for_unpriceable_event(tmp_db, monkeypatch,
                                                            capsys):
    """无法定价的事件必须写进缺口表（下游据此拒绝跨越的窗口）。"""
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed(tmp_db)
    conn = connect(tmp_db)
    repo.insert_corp_actions(conn, [CorpAction("000333", DATES[2], DATES[1], "",
                                               None, "test")], now=NOW)
    conn.close()
    assert cmd_adj_rebuild(_args(code=["000333"], source="test")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["codes"]["000333"]["n_unusable"] == 1
    assert out["codes"]["000333"]["usable_from"] == DATES[2]
    conn = connect(tmp_db)
    try:
        assert conn.execute("SELECT cqr FROM adj_factor_blackout").fetchone()[0] == DATES[2]
    finally:
        conn.close()


def test_backtest_run_refuses_stock_with_unusable_chain(tmp_db, monkeypatch, capsys):
    """跨缺口的标的**不静默回退**：整条链路拒绝，命令在输出里写明 skipped。"""
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    conn = connect(tmp_db)
    repo.insert_corp_actions(conn, [CorpAction("000333", DATES[1], DATES[0], "",
                                               None, "test")], now=NOW)
    conn.close()
    args = _args(code=["000333"], start=None, end=DATES[-1], as_of=DATES[-1],
                 cash=100_000.0, benchmark="index_300")
    assert cmd_backtest_run(args) == 1        # 没有可用标的
    out = json.loads(capsys.readouterr().out)
    assert "000333" in out["skipped"]
    assert "无法定价" in out["skipped"]["000333"]


# ---------- backtest run ----------

def test_backtest_run_end_to_end_offline(tmp_db, monkeypatch, capsys):
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed(tmp_db)
    args = _args(code=["000333"], start=None, end=DATES[-1], as_of=DATES[-1],
                 cash=100_000.0, benchmark="index_300")
    assert cmd_backtest_run(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "000333"
    assert out["n_trades"] == 1
    assert out["costs_total"] > 0
    assert out["metrics"]["total_return"] == pytest.approx(
        out["final_nav"] / 100_000.0 - 1.0)
    # 复权价 + 扣成本 + in-sample：三项披露缺一不可
    assert out["disclosure"]["adjusted_prices"] is True
    assert out["disclosure"]["costs_included"] is True
    assert out["disclosure"]["insample"] is True
    # 未采集指数行情 → 基准显式 UNDETERMINED，而不是「不比」
    assert out["benchmark"]["status"] == "UNDETERMINED"
    assert "ingest index" in out["benchmark"]["note"]
    assert out["benchmark"]["excess_return"] is None


def test_backtest_run_uses_adjusted_prices_only(tmp_db, monkeypatch, capsys):
    """除权日：只有走复权层才不会有假跌幅（不复权价在库里跌 -5%）。"""
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    init_db(tmp_db)
    conn = connect(tmp_db)
    repo.upsert_instruments(conn, DEFAULT_UNIVERSE, now=NOW)
    # 2026-03-04 除权：前收 10.2 → 除权价 9.69（10派5元），不复权序列显示 -5%
    repo.insert_bars(conn, _bars("000333", closes=[10.0, 10.2, 9.69, 9.7, 9.8]),
                     now=NOW)
    repo.insert_corp_actions(conn, [CorpAction("000333", DATES[2], DATES[1],
                                               "10派5元", None, "test")], now=NOW)
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO trading_calendar (date, is_open, source,"
            " created_at) VALUES (?,1,'test',?)", [(d, NOW) for d in DATES])
    conn.close()
    assert cmd_adj_rebuild(_args(code=["000333"], source="test")) == 0
    capsys.readouterr()

    args = _args(code=["000333"], start=None, end=DATES[-1], as_of=DATES[-1],
                 cash=100_000.0, benchmark="index_300")
    assert cmd_backtest_run(args) == 0
    out = json.loads(capsys.readouterr().out)
    # 复权后 03-04 的跌幅被还原（≈ -0.98% = 9.69/(10.2*0.95) - 1），远小于 -5%
    navs = [out["initial_cash"], out["final_nav"]]
    assert navs[0] == 100_000.0
    assert out["metrics"]["max_drawdown"] > -0.03


def test_backtest_run_reports_benchmark_when_index_present(tmp_db, monkeypatch,
                                                           capsys):
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed(tmp_db)
    conn = connect(tmp_db)
    repo.insert_bars(conn, [Bar(code="sh000300", date=d, open=c, high=c, low=c,
                                close=c, volume=1000, amount=None, turnover=None,
                                source="test", adj_mode="none")
                            for d, c in zip(DATES, [4000.0, 4020.0, 4010.0,
                                                    4050.0, 4100.0])], now=NOW)
    conn.close()
    args = _args(code=["000333"], start=DATES[0], end=DATES[-1], as_of=DATES[-1],
                 cash=100_000.0, benchmark="index_300")
    assert cmd_backtest_run(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["benchmark"]["status"] == "OK"
    assert out["benchmark"]["benchmark_return"] == pytest.approx(0.025)
    assert out["benchmark"]["excess_return"] is not None
