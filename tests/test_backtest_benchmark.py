"""Task 23：基准对照（R5）—— 缺基准必须显式 UNDETERMINED，不许「不比」。"""

import sqlite3

import pytest

from stocklab.backtest.benchmark import (INDEX_SYMBOLS, BuyAndHoldBenchmark,
                                         IndexBenchmark, UndeterminedBenchmark,
                                         compare_to_benchmark, load_index_bars,
                                         resolve_benchmark)
from stocklab.backtest.metrics import summarize
from stocklab.backtest.portfolio import NavPoint
from stocklab.config.costs import CostModel
from stocklab.config.paths import SCHEMA_SQL
from stocklab.data.models import Bar
from stocklab.store import repo

DATES = ["2026-09-01", "2026-09-02", "2026-09-03"]
FREE = CostModel(slippage_bps=0.0, min_commission=0.0, commission_rate=0.0,
                 transfer_fee_rate=0.0, stamp_tax_rate=0.0)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    yield c
    c.close()


def bar(code, date, close, adj_mode="qfq"):
    return Bar(code=code, date=date, open=close, high=close, low=close, close=close,
               volume=100, amount=close * 100, turnover=1.0, source="test",
               adj_mode=adj_mode)


def navs(values):
    return [NavPoint(d, v) for d, v in zip(DATES, values)]


def test_buy_and_hold_benchmark_uses_same_cost_model():
    bars = [bar("000333", d, c) for d, c in zip(DATES, [10.0, 11.0, 12.0])]
    b = BuyAndHoldBenchmark(bars=bars)
    bench = b.nav_points(initial_cash=100_000.0, costs=CostModel())
    assert bench[-1].nav < 120_000.0
    assert b.name == "buy_and_hold"


def test_index_benchmark_is_point_return():
    bars = [bar("sh000300", d, c, adj_mode="none")
            for d, c in zip(DATES, [4000.0, 4100.0, 4400.0])]
    b = IndexBenchmark(symbol="sh000300", bars=bars)
    navs_out = b.nav_points(initial_cash=1_000_000.0, costs=FREE)
    assert navs_out[-1].nav == pytest.approx(1_100_000.0)      # +10%
    assert b.costs_included is False


def test_compare_reports_excess_over_index():
    bars = [bar("sh000300", d, c, adj_mode="none")
            for d, c in zip(DATES, [4000.0, 4100.0, 4400.0])]
    cmp = compare_to_benchmark(navs([100_000.0, 105_000.0, 121_000.0]), 100_000.0,
                               IndexBenchmark("sh000300", bars), FREE)
    assert cmp.status == "OK"
    assert cmp.benchmark_return == pytest.approx(0.10)
    assert cmp.excess_return == pytest.approx(0.21 - 0.10)
    assert "不含成本" in cmp.note


def test_negative_excess_is_reported_as_is():
    """跑不赢就明说：超额为负时不得改口径。"""
    bars = [bar("sh000300", d, c, adj_mode="none")
            for d, c in zip(DATES, [4000.0, 4100.0, 4400.0])]
    cmp = compare_to_benchmark(navs([100_000.0, 100_000.0, 102_000.0]), 100_000.0,
                               IndexBenchmark("sh000300", bars), FREE)
    assert cmp.excess_return == pytest.approx(-0.08)
    assert cmp.status == "OK"


def test_undetermined_benchmark_yields_explicit_status():
    cmp = compare_to_benchmark(navs([100_000.0, 100_000.0, 110_000.0]), 100_000.0,
                               UndeterminedBenchmark("index_300", "未采集指数行情"),
                               FREE)
    assert cmp.status == "UNDETERMINED"
    assert cmp.benchmark_return is None
    assert cmp.excess_return is None
    assert "未采集指数行情" in cmp.note
    assert cmp.strategy_return == pytest.approx(0.10)     # 策略数字仍然给出


def test_resolve_benchmark_without_index_data_is_undetermined(conn):
    b = resolve_benchmark(conn, "index_300", start=DATES[0], end=DATES[-1])
    assert isinstance(b, UndeterminedBenchmark)
    assert "ingest index" in b.reason
    assert INDEX_SYMBOLS["index_300"] == "sh000300"


def test_resolve_benchmark_with_index_data(conn):
    repo.insert_bars(conn, [bar("sh000300", d, c, adj_mode="none")
                            for d, c in zip(DATES, [4000.0, 4100.0, 4400.0])],
                     now="2026-09-15T00:00:00+08:00")
    b = resolve_benchmark(conn, "index_300", start=DATES[0], end=DATES[-1])
    assert isinstance(b, IndexBenchmark)
    assert b.symbol == "sh000300"
    assert len(b.bars) == 3


def test_load_index_bars_filters_window(conn):
    repo.insert_bars(conn, [bar("sh000300", d, c, adj_mode="none")
                            for d, c in zip(DATES, [4000.0, 4100.0, 4400.0])],
                     now="2026-09-15T00:00:00+08:00")
    assert len(load_index_bars(conn, "sh000300", start=DATES[1])) == 2
    assert load_index_bars(conn, "sh000300", end="2026-08-01") == []


def test_unknown_benchmark_key_is_undetermined_not_crash(conn):
    b = resolve_benchmark(conn, "csi500")
    assert isinstance(b, UndeterminedBenchmark)
    assert "未知基准" in b.reason


def test_comparison_does_not_compare_index_cost_included_flag_incorrectly(conn):
    """对照结果必须带上「哪边扣了成本」，否则读者会误以为两边同口径。"""
    repo.insert_bars(conn, [bar("sh000300", d, c, adj_mode="none")
                            for d, c in zip(DATES, [4000.0, 4000.0, 4000.0])],
                     now="2026-09-15T00:00:00+08:00")
    cmp = compare_to_benchmark(navs([100_000.0] * 3), 100_000.0,
                               resolve_benchmark(conn, "index_300"), FREE)
    d = cmp.as_dict()
    assert d["strategy_costs_included"] is True
    assert d["benchmark_costs_included"] is False
    assert d["excess_return"] == pytest.approx(0.0)


def test_compare_totals_match_summarize():
    bars = [bar("sh000300", d, c, adj_mode="none")
            for d, c in zip(DATES, [4000.0, 4050.0, 4200.0])]
    ns = navs([100_000.0, 101_000.0, 103_000.0])
    cmp = compare_to_benchmark(ns, 100_000.0, IndexBenchmark("sh000300", bars), FREE)
    assert cmp.strategy_return == pytest.approx(summarize(ns, 100_000.0)["total_return"])
