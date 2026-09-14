"""`adj rebuild` / `backtest run` 子命令：离线、只读库、复权价 + 成本 + 基准。"""

import json
import math

import pytest

from stocklab.cli.main import (build_parser, cmd_adj_rebuild, cmd_backtest_run,
                               cmd_backtest_strategy, cmd_backtest_walkforward,
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


# ---------- backtest walkforward（Task 24：样本量实证，不产生策略结论） ----------

def test_walkforward_subcommand_defaults():
    args = build_parser().parse_args(["backtest", "walkforward"])
    assert args.func is cmd_backtest_walkforward
    assert (args.train, args.test, args.step, args.embargo) == (250, 21, None, 5)
    assert args.threshold == 120          # R6 硬门槛
    assert args.out is None               # 默认落到 reports/<日期>-walkforward.md


def test_walkforward_writes_report_and_is_reproducible(tmp_db, monkeypatch,
                                                       capsys, tmp_path):
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed(tmp_db)

    def run(out):
        args = _args(code=["000333"], start=None, end=None, as_of=DATES[-1],
                     out=str(out), train=2, test=2, step=None, embargo=1,
                     threshold=120)
        assert cmd_backtest_walkforward(args) == 0
        capsys.readouterr()
        return json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))

    a = run(tmp_path / "a.md")
    b = run(tmp_path / "b.md")
    assert a == b, "同一输入必须得到同一折分（可复现）"
    assert a["sample_size"]["unit"] == "trading_day"
    assert a["disclosure"]["strategy_conclusion"] is None
    md = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert "样本外**交易日**" in md
    assert "标的-日行数" in md


def test_walkforward_insufficient_data_exits_2(tmp_db, monkeypatch, capsys,
                                               tmp_path):
    """数据太短必须**显式失败**（退出码 2），不得静默产出 0 折报告。"""
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed(tmp_db)
    args = _args(code=["000333"], start=None, end=None, as_of=DATES[-1],
                 out=str(tmp_path / "x.md"), train=10, test=5, step=None,
                 embargo=0, threshold=120)
    assert cmd_backtest_walkforward(args) == 2
    err = capsys.readouterr().err
    assert "会话数不足" in err
    assert not (tmp_path / "x.md").exists(), "失败时不允许留下半成品报告"


def test_walkforward_refuses_unusable_chain_without_fallback(tmp_db, monkeypatch,
                                                             capsys, tmp_path):
    """复权链不可用的标的进 skipped 且不回退到不复权（与 backtest run 同规则）。"""
    from stocklab.data.models import CorpAction

    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed(tmp_db)
    conn = connect(tmp_db)
    # content="" → 条款解析不出来 → 该事件不可定价 → 整条链对跨窗口拒绝
    repo.insert_corp_actions(conn, [CorpAction("000333", DATES[2], DATES[1], "",
                                               None, "test")], now=NOW)
    conn.close()
    args = _args(code=["000333"], start=None, end=None, as_of=DATES[-1],
                 out=str(tmp_path / "y.md"), train=2, test=2, step=None,
                 embargo=0, threshold=120)
    assert cmd_backtest_walkforward(args) == 1
    out = json.loads(capsys.readouterr().out)
    assert "000333" in out["skipped"]
    # 实测抛出的是 MissingFactor（读取层对「跨缺口窗口」的拒绝），
    # 且错误信息必须给出可执行的补救（usable_from 提示），而不是一句「失败了」。
    assert out["skipped"]["000333"].startswith("MissingFactor")
    assert "start >=" in out["skipped"]["000333"]


# ---------- backtest strategy（Task 28：注册表策略的样本外绩效 + 基准对照） ----------

def _long_dates(n=140):
    return [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]


def _seed_long(tmp_db, n=140):
    """足够切出多折的行情（正弦，均线会反复交叉 → 策略有成交）。"""
    init_db(tmp_db)
    conn = connect(tmp_db)
    repo.upsert_instruments(conn, DEFAULT_UNIVERSE, now=NOW)
    ds = _long_dates(n)
    closes = [round(10.0 + 2.0 * math.sin(i / 7.0), 4) for i in range(n)]
    repo.insert_bars(conn, [Bar(code="000333", date=d, open=c, high=c * 1.01,
                                low=c * 0.99, close=c, volume=10_000,
                                amount=c * 10_000, turnover=1.0, source="test")
                            for d, c in zip(ds, closes)], now=NOW)
    repo.insert_bars(conn, [Bar(code="sh000300", date=d, open=4000.0 + i * 2.0,
                                high=4000.0 + i * 2.0, low=4000.0 + i * 2.0,
                                close=4000.0 + i * 2.0, volume=1000, amount=None,
                                turnover=None, source="test", adj_mode="none")
                            for i, d in enumerate(ds)], now=NOW)
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO trading_calendar (date, is_open, source,"
            " created_at) VALUES (?,1,'test',?)", [(d, NOW) for d in ds])
    conn.close()
    return ds


def _bt_args(**kw):
    base = dict(strategy="trend_ma", code=["000333"], start=None,
                end=_long_dates()[-1], as_of=_long_dates()[-1], train=60,
                test=20, step=None, embargo=0, threshold=120, cash=100_000.0,
                benchmark="index_300", param=None, out=None)
    base.update(kw)
    return _args(**base)


def test_backtest_strategy_subcommand_defaults():
    args = build_parser().parse_args(["backtest", "strategy"])
    assert args.strategy == "trend_ma"
    assert args.train == 250 and args.test == 21
    assert args.func is cmd_backtest_strategy


def test_backtest_strategy_end_to_end_offline(tmp_db, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    monkeypatch.setattr(paths, "REPORT_DIR", tmp_path)
    _seed_long(tmp_db)
    out_md = tmp_path / "wf.md"
    assert cmd_backtest_strategy(_bt_args(out=str(out_md))) == 0
    assert out_md.exists() and out_md.with_suffix(".json").exists()
    report = json.loads(out_md.with_suffix(".json").read_text(encoding="utf-8"))
    perf = report["performance"]

    assert perf["strategy_id"] == "trend_ma"
    assert perf["params"] == {"fast": 20, "slow": 60, "atr_mult": 2.0}   # 未搜索
    assert perf["n_folds"] == 4
    assert perf["oos_start"] == _long_dates()[60]
    assert perf["oos_end"] == _long_dates()[139]
    # 样本量口径：交易日，不是标的-日行数
    assert perf["sample_size"]["unit"] == "trading_day"
    assert perf["sample_size"]["effective_n"] == 80
    assert perf["sample_size"]["oos_rows"] == 80          # 单标的 → 两者相等
    # 样本外 + 扣成本 + 复权价：三项披露缺一不可
    assert perf["disclosure"]["insample"] is False
    assert perf["disclosure"]["costs_included"] is True
    assert perf["disclosure"]["adjusted_prices"] is True
    # 基准同区间对照（指数已入库 → OK，且给出明确的胜负判断）
    assert report["benchmark"]["status"] == "OK"
    assert report["benchmark"]["excess_return"] is not None
    md = out_md.read_text(encoding="utf-8")
    assert "跑赢" in md or "跑不赢" in md
    assert "样本外" in md
    capsys.readouterr()


def test_backtest_strategy_rejects_unregistered(tmp_db, monkeypatch, capsys):
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed_long(tmp_db)
    assert cmd_backtest_strategy(_bt_args(strategy="no_such")) == 3
    assert "未注册" in capsys.readouterr().err


def test_backtest_strategy_rejects_out_of_range_param(tmp_db, monkeypatch, capsys):
    """参数越界必须在**读数据之前**报出来，且是干净退出码而不是抛栈。"""
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed_long(tmp_db)
    assert cmd_backtest_strategy(_bt_args(param=["fast=9999"])) == 3
    err = capsys.readouterr().err
    assert "越界" in err and "clamp" in err


def test_backtest_strategy_rejects_duplicate_param(tmp_db, monkeypatch, capsys):
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed_long(tmp_db)
    assert cmd_backtest_strategy(_bt_args(param=["fast=20", "fast=30"])) == 3
    assert "重复" in capsys.readouterr().err


def test_backtest_strategy_insufficient_data_exit_code_2(tmp_db, monkeypatch, capsys):
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    _seed_long(tmp_db)
    assert cmd_backtest_strategy(_bt_args(train=200, test=200)) == 2
    assert "会话数不足" in capsys.readouterr().err
