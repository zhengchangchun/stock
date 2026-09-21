"""`ingest bars` 的标的集合与 `--code` 语义（ERROR_DIARY #44 / 2026-09-21 的「默认全量只采 6 只」）。

背景（2026-09-21 实测）：库里有 21 只种子，而 `ingest bars` 原来是拿 `DEFAULT_UNIVERSE`
（只有 6 只：000333/600690 + 4 只 ETF）当标的集合 ⇒ 每日链**只会刷新这 6 只**，
其余 15 只的 K 线永远停在旧日期，且没有任何报错。修法：标的集合取自库里的
`instruments`（`_bars_universe`），`--code` 出现库外代码时退出 1 并打印，不静默跳过。

全部离线（`conftest` 禁真实联网），`fetch_daily_bars` 被替换成夹具。
"""

from __future__ import annotations

import json

import pytest

from stocklab.cli.main import (_bars_universe, _stock_universe, build_parser,
                              cmd_ingest_actions, cmd_ingest_bars,
                              cmd_ingest_valuation)
from stocklab.config import paths
from stocklab.config.universe import ASSET_ETF, ASSET_STOCK, DEFAULT_UNIVERSE, Instrument
from stocklab.data.models import Bar
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-21T16:00:00+08:00"

#: 两只**不在** DEFAULT_UNIVERSE 里的种子（这正是原来被丢掉的那 15 只之一）
OUTSIDE = (Instrument("000651", "格力电器", "sz", "main", ASSET_STOCK),
           Instrument("600519", "贵州茅台", "sh", "main", ASSET_STOCK))

DATES = ["2026-09-10", "2026-09-11", "2026-09-14"]


def _bars(code: str) -> list[Bar]:
    return [Bar(code=code, date=d, open=10.0, high=10.8, low=9.9, close=10.5,
                volume=1000, amount=10_500.0, turnover=1.0, source="test")
            for d in DATES]


@pytest.fixture
def db(tmp_db, tmp_path, monkeypatch):
    """把库与 raw_cache 都指到临时目录（不许碰真库/真缓存）。"""
    init_db(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    monkeypatch.setattr(paths, "RAW_CACHE_DIR", tmp_path / "raw_cache")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(paths, "REPORT_DIR", tmp_path / "reports")
    conn = connect(tmp_db)
    yield conn
    conn.close()


def test_bars_universe_reads_instruments_table(db):
    """库是真源：库里有什么（含 ETF），就采什么 —— 不再被 DEFAULT_UNIVERSE 砍成 6 只。"""
    repo.upsert_instruments(
        db, (*OUTSIDE, Instrument("510300", "沪深300ETF", "sh", "main", ASSET_ETF)),
        now=NOW)
    universe = _bars_universe(db)
    assert [i.code for i in universe] == ["000651", "510300", "600519"]
    assert {i.code: i.asset_type for i in universe}["510300"] == ASSET_ETF


def test_bars_universe_falls_back_to_default_universe(db):
    """库为空（还没 db init / ingest）→ 退回 DEFAULT_UNIVERSE，不空跑。"""
    assert _bars_universe(db) == tuple(DEFAULT_UNIVERSE)


def test_ingest_bars_cli_covers_codes_outside_default_universe(db, monkeypatch, capsys):
    """**回归**（本轮真实 bug）：`--code` 指向不在 DEFAULT_UNIVERSE 的种子，必须真的采到。

    修前：这两只被静默丢弃，命令 exit 0、库里 0 行。
    """
    repo.upsert_instruments(db, OUTSIDE, now=NOW)
    import stocklab.data.fetch as fetch_mod

    monkeypatch.setattr(fetch_mod, "fetch_daily_bars",
                        lambda client, *, code, start, end, **kw: _bars(code[2:]))

    args = build_parser().parse_args(
        ["ingest", "bars", "--days", "30",
         "--code", "000651", "--code", "600519"])
    assert cmd_ingest_bars(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] == 2, out
    rows = db.execute("SELECT code, COUNT(*) FROM bars_daily GROUP BY code"
                      " ORDER BY code").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("000651", 3), ("600519", 3)]


def test_ingest_bars_cli_rejects_unknown_code(db, monkeypatch, capsys):
    """`--code` 给库外代码 → 退出 1 并点名（ERROR_DIARY #44：不许静默跳过）。"""
    repo.upsert_instruments(db, OUTSIDE, now=NOW)
    import stocklab.data.fetch as fetch_mod

    def _boom(*a, **kw):
        raise AssertionError("未知代码不该走到抓取")

    monkeypatch.setattr(fetch_mod, "fetch_daily_bars", _boom)

    args = build_parser().parse_args(["ingest", "bars", "--code", "999999"])
    assert cmd_ingest_bars(args) == 1
    err = capsys.readouterr().err
    assert "999999" in err and "拒绝静默跳过" in err
    assert db.execute("SELECT COUNT(*) FROM bars_daily").fetchone()[0] == 0


def test_stock_universe_excludes_etf(db):
    """复权链用的集合只要股票（ADR-008：ETF 不进复权链），库为空时退回默认里的股票。"""
    repo.upsert_instruments(
        db, (*OUTSIDE, Instrument("510300", "沪深300ETF", "sh", "main", ASSET_ETF)),
        now=NOW)
    assert [i.code for i in _stock_universe(db)] == ["000651", "600519"]
    assert all(i.is_stock for i in _stock_universe(db))


def test_ingest_actions_rejects_etf_code(db, monkeypatch, capsys):
    """ETF 不在复权集合里 → 退出 1 点名（不能默默采一个不会建链的标的）。"""
    repo.upsert_instruments(
        db, (Instrument("510300", "沪深300ETF", "sh", "main", ASSET_ETF),),
        now=NOW)
    import stocklab.data.fetch as fetch_mod

    def _boom(*a, **kw):
        raise AssertionError("不该走到抓取")

    monkeypatch.setattr(fetch_mod, "fetch_corp_actions", _boom)

    args = build_parser().parse_args(["ingest", "actions", "--code", "510300"])
    assert cmd_ingest_actions(args) == 1
    err = capsys.readouterr().err
    assert "510300" in err and "拒绝静默跳过" in err


def test_ingest_valuation_rejects_unknown_code(db, capsys):
    """估值/资金流系列采集同样不接受库外代码（同一道闸）。"""
    repo.upsert_instruments(db, OUTSIDE, now=NOW)
    args = build_parser().parse_args(["ingest", "valuation", "--code", "999999"])
    assert cmd_ingest_valuation(args) == 1
    assert "999999" in capsys.readouterr().err
