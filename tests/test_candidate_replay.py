"""Task 3：回放引擎 —— 调仓日、周期收益（等权/空池/成本/涨跌停）。"""

import pytest

from stocklab.candidate import replay
from stocklab.config.costs import CostModel
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-20T16:00:00+08:00"


def _dates(n, start="2026-01-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


# ---------- 调仓日 ----------

def test_rebalance_dates_take_every_kth_day():
    days = _dates(20)
    got = replay.rebalance_dates(days, period=5, start=days[0], end=days[-1])
    assert got == [days[0], days[5], days[10], days[15]]


def test_rebalance_dates_drop_partial_tail():
    """末尾不足一个周期的部分**丢弃**，不截断成短周期。"""
    days = _dates(13)
    got = replay.rebalance_dates(days, period=5, start=days[0], end=days[-1])
    assert got == [days[0], days[5], days[10]]
    assert days[12] not in got


def test_rebalance_dates_respect_window_bounds():
    days = _dates(30)
    got = replay.rebalance_dates(days, period=10, start=days[5], end=days[25])
    assert got == [days[5], days[15], days[25]]


def test_rebalance_dates_yields_only_start_when_window_shorter_than_period():
    days = _dates(3)
    assert replay.rebalance_dates(days, period=5, start=days[0],
                                  end=days[-1]) == [days[0]]


# ---------- 周期收益 ----------

def _db_with_bars(tmp_db, prices: dict[str, list[float]], dates: list[str]):
    """建库：标的 + 日历 + 逐日收盘价（价格序列与 dates 等长）。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main','stock',?)",
        [(code, code, NOW) for code in prices])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, p, p, p, p, NOW)
         for code, series in prices.items()
         for d, p in zip(dates, series)])
    c.commit()
    return c


def test_empty_pool_holds_cash_but_pays_liquidation_cost(tmp_db):
    """空池 → 不产生持仓收益，但仍要卖掉上一期持仓、付清仓成本。

    收益**不是 0** —— 是清仓成本的负值。这里手工算：第 0 期持有 100 股
    @10.00，第 1 期池空 → 必须卖出，费用 = `CostModel.fees('sell', 10.00, 100)`，
    以「占初始市值 1000 元」的比例计，即 `-fees/1000`。
    """
    from stocklab.config.costs import CostModel as CM

    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    costs = CM()
    expected_fee = costs.fees("sell", costs.fill_price("sell", 10.0), 100)
    expected = -expected_fee / 1000.0

    r = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short", costs=costs,
        _pools_for_test={dates[0]: ["000333"], dates[5]: []})
    assert len(r) == 1
    assert r[0] == pytest.approx(expected)
    assert r[0] < 0, "空池不是零收益 —— 清仓要付钱"


def test_flat_prices_give_zero_return_before_costs(tmp_db):
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    r = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short",
        costs=CostModel(commission_rate=0.0, min_commission=0.0,
                        transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                        slippage_bps=0.0),
        _pools_for_test={dates[0]: [], dates[5]: []})
    assert len(r) == 1 and r[0] == 0.0


# ---------- Task 4：Δ 与切分 ----------

def test_split_is_by_period_index_not_calendar():
    """按周期**序号**切 70/30。"""
    deltas = list(range(10))
    tr, va = replay.split_train_validate(deltas)
    assert tr == [0, 1, 2, 3, 4, 5, 6]
    assert va == [7, 8, 9]


def test_split_handles_short_series():
    tr, va = replay.split_train_validate([1.0, 2.0])
    assert tr == [1.0]
    assert va == [2.0]


def test_split_empty():
    assert replay.split_train_validate([]) == ([], [])


def test_split_len_one():
    """长度 1 时整个序列归训练段，验证段为空（无法切分，无推断价值）。"""
    tr, va = replay.split_train_validate([3.14])
    assert tr == [3.14]
    assert va == []


def test_split_keeps_order():
    tr, va = replay.split_train_validate([1, 2, 3, 4, 5])
    assert tr == [1, 2, 3]
    assert va == [4, 5]


def test_split_ratio_is_constant():
    assert replay.SPLIT_TRAIN_RATIO == 0.7


def test_deltas_are_candidate_minus_baseline(tmp_db):
    """Δ = 候选版本周期收益 − 基线版本周期收益。

    候选：持有 000333（nxt 里有它，从 dates[0]→dates[5] 涨 50%）；
    基线：两期都空仓（nxt 为空，收益=0）→ Δ > 0。

    注：`period_returns` 按 `nxt`（d1 的池成员）计算周期收益，因此要让
    候选真的「持有」000333，d1（dates[5]）的 pool 里必须包含它。
    """
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]},
                      dates)
    flat = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)
    # 候选：d1（dates[5]）的 nxt 包含 000333 → 计算 10→15 的收益（+50%）
    # 基线：两期都空仓（nxt 为空 → 收益 = 0）→ Δ = 0.5 > 0
    tr, va = replay.replay_period_deltas(
        c, candidate_script_id=1, baseline_script_id=1, pool="short",
        window_start=dates[0], window_end=dates[-1], costs=flat,
        _pools_for={"cand": {dates[0]: ["000333"], dates[5]: ["000333"]},
                    "base": {dates[0]: [], dates[5]: []}})
    allv = tr + va
    assert allv and all(x > 0 for x in allv)      # 涨了且基线空仓 → Δ > 0


def test_deltas_zero_when_versions_identical(tmp_db):
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    flat = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)
    same = {dates[0]: ["000333"], dates[5]: ["000333"]}
    tr, va = replay.replay_period_deltas(
        c, candidate_script_id=1, baseline_script_id=1, pool="short",
        window_start=dates[0], window_end=dates[-1], costs=flat,
        _pools_for={"cand": same, "base": same})
    # 短路路径（equal ids）：两次 period_returns 参数完全一致 → Δ 精确为 0。
    # 此断言钉住「等价性」而非依赖隐式假设。
    assert all(x == 0.0 for x in tr + va)


def test_cross_plugin_raises_value_error(tmp_db):
    """不同 plugin_id 的两个版本必须 raise ValueError（单变量原则守门）。"""
    from stocklab.plugin import store as plugin_store

    init_db(tmp_db)
    c = connect(tmp_db)
    sid_a = plugin_store.insert_script(
        c, plugin_id="plug_a", version="1", source_text="pass_a",
        note=None, now=NOW)
    sid_b = plugin_store.insert_script(
        c, plugin_id="plug_b", version="1", source_text="pass_b",
        note=None, now=NOW)

    with pytest.raises(ValueError, match="单变量原则"):
        replay.replay_period_deltas(
            c, candidate_script_id=sid_a, baseline_script_id=sid_b,
            pool="short", window_start="2026-01-01", window_end="2026-01-31")
